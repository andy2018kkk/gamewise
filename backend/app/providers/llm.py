"""回答模型的可替换接口及脚本替身；没有外部API、密钥或网络调用。"""

from collections import deque
from collections.abc import Callable, Sequence
from typing import Protocol

from app.schemas.ask import ModelTurn, ToolExchange


class ModelProviderError(Exception):
    """服务适配器已识别的模型故障；不能当作证据不足。"""


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
