"""把网页问题交给通用问答入口，再提取可展示结果。"""

from pathlib import Path
from urllib.parse import urlsplit

from app.ask_runtime import run_question
from app.providers.groq import GroqSettings
from app.schemas.tools import ToolRecord
from app.schemas.ask import ConversationTurn
from app.schemas.web_ask import WebAnswer, WebSource, WebStep
from app.services.character_families import CharacterFamilies
from app.services.text_library import load_text_library

ROOT = Path(__file__).resolve().parents[3]
GAME_IDS = ["sanguosha-ol", "sanguosha-shizhounian", "sanguosha-mobile"]


async def answer_web_question(question: str, *, history: tuple[ConversationTurn, ...] = ()) -> WebAnswer:
    """网页使用已批准的Qwen与本地资料配置，不接受客户端配置覆盖。"""
    try:
        settings = GroqSettings.from_file(ROOT / ".env", model="qwen/qwen3.8-27b")
        database = ROOT / "data/local/knowledge.local/gamewise.sqlite3"
        if not database.is_file():
            return WebAnswer(status="configuration_error", error="本地知识库数据库不存在，请检查资料配置。")
        library = load_text_library(ROOT / "backend/text-collections.example.json")
        families = CharacterFamilies.from_config(ROOT / "backend/character-families.example.json")
    except (OSError, ValueError):
        return WebAnswer(status="configuration_error", error="本地模型或资料配置缺失/无效，请检查密钥、资料及索引文件。")
    run = await run_question(question, settings, database=database, game_ids=GAME_IDS,
                             text_library=library, character_families=families, conversation=history)
    result = run.result
    sources = []
    for number, source in enumerate(result.citations, 1):
        if isinstance(source, ToolRecord):
            title = f"{source.character} · {source.mode} · {source.skill_name}"
            text, locator = source.skill_text, source.source_locator
        else:
            title = f"{source.title} · {source.heading}"
            text, locator = source.text, source.source_url
        url = locator if urlsplit(locator).scheme in {"http", "https"} else None
        sources.append(WebSource(number=number, title=title, game_id=source.game_id, text=text, url=url))
    labels = {"discover_catalog": "查看资料目录", "get_character_skills": "查询武将技能",
              "search_reference": "搜索规则或策略", "read_evidence": "补读资料原文"}
    steps = [WebStep(action=labels.get(item.call.name, "查询资料"),
                     records=sum(len(scope.records) for scope in item.response.results),
                     error=item.response.error.message if item.response.error else None)
             for item in result.trace]
    return WebAnswer(status=result.status, answer=result.answer, sources=sources, steps=steps,
                     error=result.error.message if result.error else None,
                     elapsed_seconds=run.elapsed_seconds)
