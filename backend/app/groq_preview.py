"""显式联网的Groq演练；默认合成资料，真实案例须显式选择，不由pytest自动运行。"""

import argparse
import asyncio
from datetime import date
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import httpx

from app.providers.groq import GroqModel, GroqSettings
from app.providers.llm import ModelClient, ModelProviderError
from app.schemas.ask import AskResult, ModelTurn, ToolExchange
from app.schemas.character import CharacterRecord
from app.schemas.query import Game
from app.schemas.source import SourceRecord
from app.schemas.tools import ToolError, ToolResponse
from app.services.ask import AskService
from app.services.data_processor import DataProcessor, EvidenceBlock, ManualExtractor, SourceDocument
from app.services.import_characters import import_character_files
from app.services.knowledge_tools import KnowledgeTools
from app.services.text_library import load_text_library

WORKSPACE = Path(__file__).resolve().parents[2]
INSTRUCTIONS = Path(__file__).parent / "providers/gamewise_instructions.md"


class _ComparisonTools(KnowledgeTools):
    """本次已授权案例的范围限制；不改变通用知识工具的查询规则。"""

    def invoke(self, name: str, arguments: str) -> ToolResponse:
        try:
            args = json.loads(arguments)
        except ValueError:
            return super().invoke(name, arguments)
        if isinstance(args, dict) and name in {"discover_catalog", "search_records"} and args.get("record_type", "skill") == "skill":
            if args.get("character") not in ("刘备", "曹操"):
                return ToolResponse(preview=True, error=ToolError(code="scope_denied",
                    message="本次仅授权OL刘备与曹操，请明确character。"))
            if name == "search_records":
                if args.get("mode") not in (None, "经典", "经典版本"):
                    return ToolResponse(preview=True, error=ToolError(code="scope_denied",
                        message="本次仅允许经典模式，准确值为经典版本。"))
                args["mode"] = "经典版本"
                arguments = json.dumps(args, ensure_ascii=False)
        return super().invoke(name, arguments)


class _ReplayThenModel:
    """恢复真实案例的已保存选择；本地工具重验成功后才继续请求模型。"""

    def __init__(self, previous: tuple[ToolExchange, ...], model: ModelClient, *, finalize_only: bool = False) -> None:
        if any(e.call.name not in {"discover_catalog", "search_records"} for e in previous):
            raise ValueError("当前只支持没有补读步骤的查询断点，不能恢复任意工具")
        self.previous, self.model = previous, model
        self.finalize_only = finalize_only

    async def respond(self, *, question: str, tools: list[dict[str, object]],
                      history: tuple[ToolExchange, ...]) -> ModelTurn:
        excluded = {"results": {"__all__": {"records": {"__all__": {"citation_id"}}}}}
        for old, current in zip(self.previous, history):
            if old.response.model_dump(exclude=excluded) != current.response.model_dump(exclude=excluded):
                raise ModelProviderError("恢复时本地资料或范围结果已变化，停止发送旧上下文")
        if len(history) < len(self.previous):
            return self.previous[len(history)].call.model_copy(deep=True)
        return await self.model.respond(question=question, tools=[] if self.finalize_only else tools, history=history)


def build_synthetic_database(root: Path) -> Path:
    """复用正式校验/导入入口，在临时库放入一份明确虚构的技能。"""
    text = "准备阶段，你可以弃置1张手牌，然后摸3张牌；每回合限一次。"
    meta = "测试武将甲 测试经典 武将包 测试包 吴"
    raw = root / "synthetic.txt"
    raw.write_text(meta + "\n试学：" + text, encoding="utf-8")
    source = SourceRecord(source_id="synthetic-source", title="人工构造工具调用样本", game_id="synthetic-a",
        topic="synthetic", source_type="local", source_locator="synthetic:example-1", content_path=str(raw),
        collected_at=date.today(), content_hash=hashlib.sha256(raw.read_bytes()).hexdigest(),
        usage_note="项目人工构造内容，仅用于API流程实验，不代表游戏事实")
    document = SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="meta", text=meta, locator="第1行"),
        EvidenceBlock(block_id="skill", text=text, verbatim_text=text, locator="第2行")))
    data = CharacterRecord(character="测试武将甲", mode="测试经典", package_kind="武将包",
        package_name="测试包", faction="吴", skills=[{"skill_name": "试学", "skill_text": text}])
    batch = DataProcessor(CharacterRecord).process(document, ManualExtractor([
        {"data": data.model_dump(), "block_ids": ["meta", "skill"]}]))
    processed = root / "synthetic.json"
    processed.write_text(batch.model_dump_json(), encoding="utf-8")
    database = root / "synthetic.sqlite3"
    import_character_files(database, [processed], [Game(game_id="synthetic-a", name="人工构造测试服")])
    return database


async def run_preview(database: Path, settings: GroqSettings, *, case: str = "synthetic",
                      resume_report: Path | None = None, finalize_only: bool = False) -> dict:
    real = case == "ol-comparison"
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    games = ["sanguosha-ol"] if real else ["synthetic-a"]
    if real:
        instructions += ("\n本次授权仅OL刘备与曹操经典身份局，模式准确值为经典版本，"
                         "省略mode时程序限定此模式。本题明确要求综合技能、规则、策略："
                         "实际取得三类资料后再综合分析；未取得任何一类要如实说明缺口，不能伪称已查阅。"
                         "本次每次文本最多返回2条。先覆盖各必要资料类型，再决定是否扩查同一类；"
                         "控制片段数量，只有缺关键前提时才补读；不要无关扩查。"
                         "这次回答控制在350个汉字左右：简短条件推荐、引用依据、适用限制。"
                         "工具已单独显示完整技能原文，不在answer重写技能的触发、数量或次数；"
                         "只分析已查两人的适配，不举其他武将例子。使用实际支持结论的技能、规则、策略引用，"
                         "把自己的分析标为推断。历史规则未确认适用、待审预览及无胜率数据写进answer。")
    if finalize_only:
        if resume_report is None:
            raise ValueError("仅在有真实查询断点时进入收尾阶段")
        instructions += "\n本次恢复只做收尾，禁止新查询；已有证据不足的部分在答案中说明，不得补造。"
    question = ("三国杀OL经典身份局里，刘备和曹操谁更适合当主公？请结合双方技能、主公相关规则和策略参考分析，"
                "说明各自适合的条件，不要只给强度排名。当前资料仅测试预览；历史规则适用性不明确时明确说明。" if real else
                "人工构造测试服有哪些武将和模式？请选择目录中一名武将，查出其完整技能并解释代价、效果和次数限制。标明这是虚构测试。")
    def tools_factory() -> KnowledgeTools:
        if real:
            return _ComparisonTools(database, games, preview=True, max_records=2,
                text_library=load_text_library(WORKSPACE / "backend/text-collections.example.json"))
        return KnowledgeTools(database, games, preview=True)
    async with httpx.AsyncClient() as client:
        qwen_trial = settings.model == "qwen/qwen3.8-27b"
        model = GroqModel(settings, client, instructions=instructions, allowed_game_ids=games,
                          max_completion_tokens=2048 if real and not qwen_trial else 1024,
                          max_rate_limit_retries=1 if real else 0, max_rate_limit_wait_seconds=60,
                          reasoning_effort="none" if qwen_trial else ("medium" if real else "low"))
        runner: ModelClient = model
        replayed = 0
        if resume_report is not None:
            saved = json.loads(resume_report.read_text(encoding="utf-8"))
            if not real or saved.get("question") != question or saved.get("synthetic_only") is not False:
                raise ValueError("断点必须是同一已授权真实案例，不能替换问题或合成资料")
            previous = AskResult.model_validate(saved["result"])
            if previous.answer or len(previous.trace) > 8:
                raise ValueError("只恢复未完成且未超预算的记录")
            replayed = len(previous.trace)
            runner = _ReplayThenModel(tuple(previous.trace), model, finalize_only=finalize_only)
        service = AskService(runner, tools_factory, max_tool_calls=8 if real else 3,
                             max_rounds=9 if real else 4, timeout_seconds=300 if real else 90)
        started = monotonic()
        result = await service.ask(question)
        records = [r for exchange in result.trace for scope in exchange.response.results for r in scope.records]
        return {"model": settings.model, "synthetic_only": not real, "question": question,
                "max_completion_tokens": model.max_completion_tokens,
                "reasoning_effort": model.reasoning_effort,
                "resumed_from": str(resume_report) if resume_report else None,
                "replayed_tool_choices": replayed,
                "finalize_only": finalize_only,
                "checks": {"skill_characters": sorted({r.character for r in records if hasattr(r, "character")}),
                           "text_types": sorted({r.record_type for r in records if hasattr(r, "record_type")})},
                "elapsed_seconds": round(monotonic() - started, 3),
                "usage": [u.model_dump() for u in model.usage], "result": result.model_dump(mode="json")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-remote", action="store_true", help="确认向Groq发送显式选择的测试样本范围")
    parser.add_argument("--case", choices=["synthetic", "ol-comparison"], default="synthetic",
                        help="默认合成；ol-comparison显式使用已授权的OL刘备/曹操经典及规则、策略片段")
    parser.add_argument("--env-file", type=Path, default=WORKSPACE / ".env")
    parser.add_argument("--model", choices=["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
                        help="本次覆盖已确认模型，不修改.env或密钥")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume-report", type=Path, help="仅恢复同一真实案例未完成的目录/查询步骤；先本地重验，不重发已完成的模型请求")
    parser.add_argument("--finalize-only", action="store_true", help="仅在恢复断点后依据已有证据提交答案，不再开放查询；可返回不足")
    args = parser.parse_args()
    if not args.run_remote:
        parser.error("本入口会调用外部API，需要显式--run-remote；真实案例还须明确--case ol-comparison")
    if (args.resume_report or args.finalize_only) and args.case != "ol-comparison":
        parser.error("恢复或收尾参数只适用于已授权的ol-comparison案例")
    if args.finalize_only and args.resume_report is None:
        parser.error("--finalize-only必须配合--resume-report，不能跳过资料获取")
    args.output_dir = args.output_dir or WORKSPACE / "data/local/tool-preview.local" / ("groq-real-case-3" if args.case == "ol-comparison" else "groq-synthetic")
    try:
        settings = GroqSettings.from_file(args.env_file)
    except (OSError, ValueError):
        parser.error("无法读取本地Groq配置；未发送请求，未显示配置内容")
    if args.model:
        settings = settings.model_copy(update={"model": args.model})
    if args.case == "ol-comparison":
        if settings.model not in {"openai/gpt-oss-120b", "qwen/qwen3.8-27b"}:
            parser.error("真实案例只允许已确认的GPT-OSS或Qwen模型；未发送请求")
        report = asyncio.run(run_preview(WORKSPACE / "data/local/knowledge.local/gamewise.sqlite3", settings,
                                       case=args.case, resume_report=args.resume_report, finalize_only=args.finalize_only))
    else:
        with TemporaryDirectory(prefix="gamewise-synthetic-") as directory:
            report = asyncio.run(run_preview(build_synthetic_database(Path(directory)), settings))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    result = report["result"]
    lines = ["# Groq模型调用演练", "", "合成资料测试。" if report["synthetic_only"] else "OL刘备/曹操经典多来源测试预览；范围未扩展。",
             "模型：" + report["model"], "问题：" + report["question"], ""]
    if report["resumed_from"]:
        lines.extend(["断点来源：" + report["resumed_from"],
                      "已保存的真实模型选择本地重验重放：" + str(report["replayed_tool_choices"]) + "步；其后才是本次新的模型调用。",
                      "本次耗时和用量只计算恢复运行，不包含前次尝试。", ""])
    if report["finalize_only"]:
        lines.extend(["本次为显式收尾阶段，只开放提交答案；不足部分不得补造。", ""])
    for index, exchange in enumerate(result["trace"], 1):
        lines.extend([f"## 第{index}步：{exchange['call']['name']}", "", "参数：" + exchange["call"]["arguments"], "",
                      "程序实际返回：", "```json", json.dumps(exchange["response"], ensure_ascii=False, indent=2), "```", ""])
    lines.extend(["## 模型最终回答", "", (result["answer"] or "未生成回答").replace("\\n", "\n"), "",
                  "状态：" + result["status"], "错误：" + json.dumps(result["error"], ensure_ascii=False), "",
                  "各次请求用量：" + json.dumps(report["usage"], ensure_ascii=False), "",
                  "没有保存模型内部推理；合成临时库已清理，真实库仅只读查询，调用记录保留用于审查。"])
    lines.extend(["", "## 答案引用列表（程序从本次证据填写）", ""])
    for index, citation in enumerate(result["citations"], 1):
        title = citation.get("heading") or (citation.get("character", "") + " / " + citation.get("skill_name", ""))
        locator = citation.get("source_url", citation.get("source_locator", ""))
        lines.append(f"{index}. {title}；{locator}")
    (args.output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": result["status"], "tool_calls": len(result["trace"]),
                      "usage": report["usage"], "output": str(args.output_dir.resolve())}, ensure_ascii=False))
    if result["error"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
