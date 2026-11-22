"""通用问题的运行装配；不包含固定题目、人物名单或网页路由。"""

from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import httpx

from app.providers.groq import GroqModel, GroqSettings, RequestUsage
from app.schemas.ask import AskResult, ConversationTurn
from app.services.ask import AskService
from app.services.character_families import CharacterFamilies
from app.services.knowledge_tools import KnowledgeTools
from app.services.text_library import TextLibrary


@dataclass(frozen=True)
class QuestionRun:
    result: AskResult
    usage: tuple[RequestUsage, ...]
    elapsed_seconds: float


async def run_question(question: str, settings: GroqSettings, *, database: Path,
                       game_ids: list[str], text_library: TextLibrary | None = None,
                       character_families: CharacterFamilies | None = None,
                       conversation: tuple[ConversationTurn, ...] = ()) -> QuestionRun:
    """按输入问题运行现有模型/工具循环；资料范围由调用程序提供。"""
    if not question.strip() or not game_ids:
        raise ValueError("请提供非空问题及允许查询的游戏范围")
    instructions = (Path(__file__).parent / "providers/gamewise_instructions.md").read_text(encoding="utf-8")
    def tools() -> KnowledgeTools:
        return KnowledgeTools(database, game_ids, preview=True, max_records=3,
            max_characters=None, text_library=text_library, character_families=character_families)
    async with httpx.AsyncClient() as client:
        qwen = settings.model == "qwen/qwen3.8-27b"
        model = GroqModel(settings, client, instructions=instructions, allowed_game_ids=game_ids,
            reasoning_effort="none" if qwen else "medium",
            max_rate_limit_retries=1, max_rate_limit_wait_seconds=60,
            max_estimated_total_tokens=8000 if qwen else None, conversation=conversation)
        service = AskService(model, tools, max_tool_calls=8, max_rounds=9,
            max_evidence_characters=None, timeout_seconds=300)
        started = monotonic()
        result = await service.ask(question)
        return QuestionRun(result, tuple(model.usage), round(monotonic() - started, 3))
