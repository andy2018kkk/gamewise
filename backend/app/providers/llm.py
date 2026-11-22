"""回答模型的可替换接口及脚本替身；没有外部API、密钥或网络调用。"""

from collections import deque
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from app.schemas.ask import ModelTurn, ToolExchange


class ModelProviderError(Exception):
    """服务适配器已识别的模型故障；不能当作证据不足。"""

    def __init__(self, message: str, *, kind: Literal["unknown", "rate_limit", "authentication",
                 "permission", "network", "tool_rejected", "request_rejected", "unavailable",
                 "invalid_response", "incomplete_response"] = "unknown", http_status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status

    @property
    def public_message(self) -> str:
        """固定文案与状态码，绝不转发原始异常、响应正文或请求配置。"""
        messages = {
            "unknown": "模型服务调用失败，具体原因尚未识别",
            "rate_limit": "模型平台限流，本次问答未完成，请稍后手动再试",
            "authentication": "模型平台鉴权失败，请检查后端密钥是否有效",
            "permission": "模型平台拒绝访问，请检查账号或模型使用权限",
            "network": "无法连接模型平台，请检查后端网络连接",
            "tool_rejected": "工具调用被模型平台拒绝，本次未完成；不是本地资料查询无结果",
            "request_rejected": "模型平台拒绝了请求，请检查模型及请求配置",
            "unavailable": "模型平台暂时不可用，请稍后手动再试",
            "invalid_response": "模型返回的内容或工具动作格式异常，无法继续处理",
            "incomplete_response": "模型回复未完整结束，本次未采用截断内容",
        }
        status = f"（HTTP {self.http_status}）" if self.http_status is not None else ""
        return messages[self.kind] + status


class ModelClient(Protocol):
    async def respond(self, *, question: str, tools: list[dict[str, object]],
                      history: tuple[ToolExchange, ...]) -> ModelTurn: ...


class ScriptedModel:
    """按预设步骤模拟模型选择；用于验证调用流程，不证明真实模型智能。"""

    def __init__(self, steps: Sequence[ModelTurn | Callable[[tuple[ToolExchange, ...]], ModelTurn]]) -> None:
        self._steps = deque(steps)

    async def respond(self, *, question: str, tools: list[dict[str, object]],
                      history: tuple[ToolExchange, ...]) -> ModelTurn:
        if not self._steps:
            raise ModelProviderError("模型替身的预设步骤已耗尽")
        step = self._steps.popleft()
        return step(history) if callable(step) else step.model_copy(deep=True)
