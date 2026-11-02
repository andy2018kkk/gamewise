"""受控取证循环：模型动作 → 只读工具 → 结果 → 后续动作或结束。"""

import asyncio
from collections.abc import Callable
import json
import re
from time import monotonic

from pydantic import TypeAdapter, ValidationError

from app.providers.llm import ModelClient, ModelProviderError
from app.schemas.ask import AskError, AskResult, FinalAnswerTurn, ModelTurn, ToolExchange
from app.schemas.source import NonBlankText
from app.services.knowledge_tools import KnowledgeTools


class AskService:
    """每个问题创建新工具会话，限定次数、时间与证据量，不修改知识库。"""

    def __init__(self, client: ModelClient, tools_factory: Callable[[], KnowledgeTools], *,
                 max_tool_calls: int = 8, max_rounds: int = 10,
                 max_evidence_characters: int = 20_000, timeout_seconds: float = 30) -> None:
        if (max_tool_calls < 1 or max_rounds < 1 or max_evidence_characters < 1
                or not 0 < timeout_seconds <= 300):
            raise ValueError("调用循环额度不合法")
        self.client, self.tools_factory = client, tools_factory
        self.max_tool_calls, self.max_rounds = max_tool_calls, max_rounds
        self.max_evidence_characters = max_evidence_characters
        self.timeout_seconds = timeout_seconds

    async def ask(self, question: str) -> AskResult:
        history: list[ToolExchange] = []
        tools = self.tools_factory()
        allowed_citations: set[str] = set()
        seen_requests: set[str] = set()
        seen_ids: set[str] = set()
        total_characters = 0
        deadline = monotonic() + self.timeout_seconds

        def failure(code: str, message: str) -> AskResult:
            return AskResult(status="error", trace=history, preview=tools.preview,
                             error=AskError(code=code, message=message))

        try:
            question = TypeAdapter(NonBlankText).validate_python(question, strict=True)
        except ValidationError:
            return failure("invalid_question", "问题必须是非空文字")
        for _ in range(self.max_rounds):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return failure("model_timeout", "本次问答已达到时间上限")
            try:
                raw_turn = await asyncio.wait_for(self.client.respond(question=question,
                    tools=tools.definitions(), history=tuple(item.model_copy(deep=True) for item in history)), remaining)
                turn = TypeAdapter(ModelTurn).validate_python(raw_turn, strict=True)
            except TimeoutError:
                return failure("model_timeout", "等待模型响应超时")
            except ModelProviderError:
                return failure("model_error", "模型服务调用失败，不是证据不足")
            except ValidationError:
                return failure("invalid_model_output", "模型返回的动作不符合约定格式")
            if isinstance(turn, FinalAnswerTurn):
                if re.search(r"https?://", turn.answer, re.IGNORECASE):
                    return failure("invalid_model_output", "模型应返回证据编号，不能自行生成来源链接")
                if len(set(turn.citation_ids)) != len(turn.citation_ids):
                    return failure("invalid_citation", "模型返回了重复引用")
                if any(citation not in allowed_citations for citation in turn.citation_ids):
                    return failure("invalid_citation", "模型引用了本问题未取得的证据")
                if turn.status in {"answered", "partial"} and not turn.citation_ids:
                    return failure("missing_citation", "事实回答缺少本次检索证据")
                return AskResult(status="preview_answer" if turn.status == "answered" else turn.status,
                    answer=turn.answer, citations=[tools.resolve_citation(c) for c in turn.citation_ids],
                    trace=history, preview=tools.preview,
                    notes=["本地流程验证；待审证据仍不可作为正式回答，引用合法不代表结论一定正确。"])
            if turn.call_id in seen_ids:
                return failure("duplicate_call_id", "工具请求编号重复")
            if len(history) >= self.max_tool_calls:
                return failure("tool_limit", "工具调用已达到上限，未继续查询")
            try:
                canonical = json.dumps(json.loads(turn.arguments), ensure_ascii=False, sort_keys=True)
            except ValueError:
                canonical = turn.arguments
            key = f"{turn.name}\0{canonical}"
            if key in seen_requests:
                return failure("repeated_call", "停止重复的相同工具请求；请缩小条件或说明缺口")
            seen_ids.add(turn.call_id)
            seen_requests.add(key)
            remaining = deadline - monotonic()
            if remaining <= 0:
                return failure("tool_timeout", "执行工具前已达到时间上限")
            try:
                started = monotonic()
                response = await asyncio.wait_for(asyncio.to_thread(tools.invoke, turn.name, turn.arguments), remaining)
            except TimeoutError:
                return failure("tool_timeout", "等待只读工具超时")
            history.append(ToolExchange(call=turn, response=response, elapsed_ms=round((monotonic() - started) * 1000, 3)))
            total_characters += len(response.model_dump_json())
            if total_characters > self.max_evidence_characters:
                return failure("evidence_budget", "本次累计工具结果超出额度，未继续发送给模型")
            if response.error is not None and response.error.code == "storage_error":
                return failure("storage_error", "资料读取失败，不是无资料")
            allowed_citations.update(record.citation_id for result in response.results for record in result.records)
        return failure("round_limit", "模型交互轮数已达到上限")
