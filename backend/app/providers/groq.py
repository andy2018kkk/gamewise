"""Groq消息转换与单次请求；查询、额度和引用检查仍由AskService负责。"""

import asyncio
import json
import math
import re
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.providers.llm import ModelProviderError
from app.schemas.ask import FinalAnswerTurn, ModelTurn, ToolCallTurn, ToolExchange


class GroqSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr
    model: str = Field(min_length=1)

    @classmethod
    def from_file(cls, path: Path) -> "GroqSettings":
        """只读两项简单KEY=VALUE配置，不执行变量、命令或打印文件。"""
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"GROQ_API_KEY", "GROQ_MODEL"}:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
        if not values.get("GROQ_API_KEY") or not values.get("GROQ_MODEL"):
            raise ValueError("本地配置缺少GROQ_API_KEY或GROQ_MODEL")
        return cls(api_key=values["GROQ_API_KEY"], model=values["GROQ_MODEL"])


class RequestUsage(BaseModel):
    http_status: int
    error_code: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    rate_limit_wait_seconds: float = 0
    failure_reason: str | None = None
    failed_tool_name: str | None = None
    elapsed_seconds: float = 0
    token_limit: int | None = None
    requested_tokens: int | None = None
    retry_after_seconds: float | None = None
    rate_limit_kind: str | None = None
    server_wait_seconds: float | None = None


def _tool_failure_details(error: object) -> dict[str, str]:
    """只提取固定分类及已知工具名；不记录原始错误、生成正文或参数值。"""
    if not isinstance(error, dict):
        return {}
    message = error.get("message")
    message = message.lower() if isinstance(message, str) else ""
    generation = error.get("failed_generation")
    generation = generation if isinstance(generation, str) else ""
    reason = "unclassified"
    if "not in request.tools" in message or "not present" in message or "was not offered" in message:
        reason = "unknown_tool"
    elif "did not match" in message or "parameters" in message or "schema" in message:
        reason = "schema_validation"
    elif "parallel" in message or "multiple" in message:
        reason = "multiple_calls"
    match = re.search(r"(?:tool ['\"]|<function=|\"name\"\s*:\s*\")([\w.]+)", message + "\n" + generation)
    details = {"failure_reason": reason}
    if match:
        name = match.group(1)
        details["failed_tool_name"] = name if name in {
            "discover_catalog", "search_records", "read_evidence", "final", "respond", "json", "multi_tool_use.parallel"
        } else "unregistered_or_unidentified"
    return details


class _Function(BaseModel):
    name: str = Field(min_length=1)
    arguments: str = Field(min_length=1)


class _Call(BaseModel):
    id: str = Field(min_length=1)
    type: str
    function: _Function


class _Message(BaseModel):
    content: str | None = None
    tool_calls: list[_Call] | None = None


class _Choice(BaseModel):
    finish_reason: str
    message: _Message


class _Completion(BaseModel):
    choices: list[_Choice] = Field(min_length=1, max_length=1)


class GroqModel:
    """每轮重建问题与已执行工具历史；不保存模型内部推理或自行执行工具。"""

    def __init__(self, settings: GroqSettings, client: httpx.AsyncClient, *, instructions: str,
                 allowed_game_ids: list[str], max_completion_tokens: int = 1024,
                 max_rate_limit_retries: int = 0, max_rate_limit_wait_seconds: float = 45,
                 reasoning_effort: Literal["none", "default", "low", "medium", "high"] = "low") -> None:
        if not instructions.strip() or not allowed_game_ids or not 128 <= max_completion_tokens <= 4096:
            raise ValueError("模型说明、允许范围或输出额度无效")
        if max_rate_limit_retries not in (0, 1) or not 0 <= max_rate_limit_wait_seconds <= 60:
            raise ValueError("限流最多重试一次，单次等待最多60秒")
        self.settings, self.client = settings, client
        self.instructions = instructions + "\n允许的game_id：" + json.dumps(allowed_game_ids)
        self.instructions += "\n本接口结束动作名为json：任务完成或证据不足时调用json提交最终结果。它不是资料查询或代码执行，参数必须符合给出的答案Schema。"
        self.max_completion_tokens = max_completion_tokens
        if reasoning_effort not in {"none", "default", "low", "medium", "high"}:
            raise ValueError("不支持的推理预算")
        self.reasoning_effort = reasoning_effort
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds
        self.usage: list[RequestUsage] = []

    async def respond(self, *, question: str, tools: list[dict[str, object]],
                      history: tuple[ToolExchange, ...]) -> ModelTurn:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self.instructions}, {"role": "user", "content": question}]
        for exchange in history:
            messages.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": exchange.call.call_id, "type": "function", "function": {
                    "name": exchange.call.name, "arguments": exchange.call.arguments}}]})
            messages.append({"role": "tool", "tool_call_id": exchange.call.call_id,
                             "content": exchange.response.model_dump_json(exclude_none=True, exclude={
                                 "results": {"__all__": {"records": {"__all__": {
                                     "content_path", "content_hash", "context_hash", "parent_record_id",
                                     "record_id", "locators", "start", "end"}}}}})})
        payload = {"model": self.settings.model, "messages": messages,
                      "tools": [{"type": "function", "function": tool} for tool in tools] + [{
                          "type": "function", "function": {"name": "json",
                              "description": "结束本次问答并提交答案、状态和本次引用编号；不读取资料。事实/观点区分、适用限制保留，正文用[1]对应引用顺序，不生成URL。",
                              "parameters": FinalAnswerTurn.model_json_schema()}}],
                      "tool_choice": "auto" if tools else {"type": "function", "function": {"name": "json"}},
                      "parallel_tool_calls": False,
                      "reasoning_effort": self.reasoning_effort, "max_completion_tokens": self.max_completion_tokens,
                      "stream": False}
        for attempt in range(self.max_rate_limit_retries + 1):
            started = monotonic()
            try:
                response = await self.client.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.api_key.get_secret_value()},
                    json=payload, timeout=30, follow_redirects=False)
            except httpx.TimeoutException:
                raise TimeoutError("Groq请求超时") from None
            except httpx.HTTPError:
                raise ModelProviderError("Groq网络请求失败") from None
            self.usage.append(RequestUsage(http_status=response.status_code,
                                           elapsed_seconds=round(monotonic() - started, 4)))
            limit = response.headers.get("x-ratelimit-limit-tokens", "")
            if limit.isdigit() and len(limit) <= 9:
                self.usage[-1].token_limit = int(limit)
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("retry-after", ""))
                    if math.isfinite(retry_after) and retry_after >= 0:
                        self.usage[-1].retry_after_seconds = retry_after
                except ValueError:
                    pass
                try:
                    error_payload = response.json().get("error", {})
                    message = error_payload.get("message", "") if isinstance(error_payload, dict) else ""
                except (ValueError, AttributeError):
                    message = ""
                if isinstance(message, str):
                    for marker, kind in (("TPD", "tokens_per_day"), ("TPM", "tokens_per_minute"),
                                         ("RPD", "requests_per_day"), ("RPM", "requests_per_minute")):
                        if marker in message:
                            self.usage[-1].rate_limit_kind = kind
                            break
                    delay = re.search(r"try again in ([0-9.hms]+)", message, re.IGNORECASE)
                    if delay:
                        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", delay.group(1))
                        if parts and "".join(a + b for a, b in parts) == delay.group(1):
                            self.usage[-1].server_wait_seconds = sum(float(a) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[b] for a, b in parts)
                    requested = re.search(r"Requested\s+(\d{1,9})\b", message, re.IGNORECASE)
                    if requested:
                        self.usage[-1].requested_tokens = int(requested.group(1))
                usage = self.usage[-1]
                if usage.token_limit and usage.requested_tokens and usage.requested_tokens > usage.token_limit:
                    usage.failure_reason = "request_exceeds_token_limit"
                    break  # 等待不能让超出单分钟总容量的单个请求变小。
            if response.status_code != 429 or attempt >= self.max_rate_limit_retries:
                break
            try:
                wait = float(response.headers.get("retry-after", ""))
            except ValueError:
                break
            # 遵守服务端等待值；缺失、异常或超出预算即停止，不缩短后强行重试。
            if not math.isfinite(wait) or not 0 <= wait <= self.max_rate_limit_wait_seconds:
                break
            self.usage[-1].error_code = "rate_limit_exceeded"
            self.usage[-1].rate_limit_wait_seconds = wait
            await asyncio.sleep(wait)  # 仍受AskService整次问答期限约束，可取消。
        # 付费、鉴权、服务器或格式错误不重试，不记录原始错误正文。
        if response.status_code != 200:
            try:
                error = response.json().get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, AttributeError):
                code = None
            # 只保留已知固定错误码，不带回服务商可能回显的提示词和密钥。
            self.usage[-1].error_code = code if isinstance(code, str) and code in {
                "tool_use_failed", "rate_limit_exceeded", "invalid_api_key", "invalid_request_error"
            } else "unclassified"
            if code == "tool_use_failed":
                for key, value in _tool_failure_details(error).items():
                    setattr(self.usage[-1], key, value)
            raise ModelProviderError(f"Groq HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ModelProviderError("Groq返回了非JSON响应") from None
        if not isinstance(payload, dict):
            raise ModelProviderError("Groq返回了异常响应结构")
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage[-1] = self.usage[-1].model_copy(update={
                key: value for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int})
        choice = _Completion.model_validate(payload).choices[0]
        if choice.finish_reason == "tool_calls":
            calls = choice.message.tool_calls or []
            if len(calls) != 1 or calls[0].type != "function":
                raise ModelProviderError("当前只接受单个顺序工具动作")
            call = calls[0]
            if call.function.name == "json":
                return FinalAnswerTurn.model_validate_json(call.function.arguments)
            return ToolCallTurn(call_id=call.id, name=call.function.name, arguments=call.function.arguments)
        if choice.finish_reason != "stop" or choice.message.tool_calls:
            raise ModelProviderError("模型响应未完整结束，未采用截断内容")
        return FinalAnswerTurn.model_validate_json(choice.message.content or "")
