"""用预设模型脚本演练真实本地只读工具；不理解任意问题、不调用LLM API。"""

import argparse
import asyncio
from collections.abc import Callable
import json
from pathlib import Path

from app.providers.llm import ScriptedModel
from app.schemas.ask import FinalAnswerTurn, ModelTurn, ToolCallTurn, ToolExchange
from app.services.ask import AskService
from app.services.knowledge_tools import KnowledgeTools
from app.services.text_library import load_text_library

WORKSPACE = Path(__file__).resolve().parents[2]


def _call(number: int, name: str, **arguments: object) -> ToolCallTurn:
    return ToolCallTurn(call_id=str(number), name=name, arguments=json.dumps(arguments, ensure_ascii=False))


def preview_steps(game: str, left: str, right: str) -> list[ModelTurn | Callable[[tuple[ToolExchange, ...]], ModelTurn]]:
    """查询顺序预设，范围和补读编号从实际工具结果取得。"""
    def skill(history: tuple[ToolExchange, ...], number: int, character: str) -> ModelTurn:
        results = history[0].response.results
        modes = results[0].catalog.modes if results and results[0].catalog else []
        mode = next((m for m in modes if m in {"经典", "经典版本"}), None)
        if mode is None:
            return FinalAnswerTurn(status="needs_clarification", answer="演示对象没有明确的经典模式记录，请检查目录。")
        return _call(number, "search_records", game_ids=[game], character=character, mode=mode)

    def text_search(history: tuple[ToolExchange, ...], number: int, record_type: str, query: str) -> ModelTurn:
        scopes = [result.scope for result in history[-1].response.results if result.scope]
        if not scopes:
            return FinalAnswerTurn(status="insufficient_evidence", answer="未配置本次演示需要的文本范围。")
        return _call(number, "search_records", record_type=record_type,
                     scope_id=scopes[0].scope_id, query=query, top_k=1)

    def read_last(history: tuple[ToolExchange, ...]) -> ModelTurn:
        records = [record for result in history[-1].response.results for record in result.records]
        if not records:
            return FinalAnswerTurn(status="insufficient_evidence", answer="策略参考未命中，本次流程停止补读。")
        return _call(8, "read_evidence", citation_id=records[0].citation_id)

    def finish(history: tuple[ToolExchange, ...]) -> FinalAnswerTurn:
        records = [record for exchange in history for result in exchange.response.results for record in result.records]
        ids = list(dict.fromkeys(record.citation_id for record in records))
        gaps = [result.status for exchange in history for result in exchange.response.results
                if result.status not in {"catalog_available", "preview_available"}]
        answer = (f"预设流程已尝试获取{left}、{right}技能及规则/策略参考，并补读命中文章章节。"
                  "这只是取证流程预览，没有真实LLM分析或强度推荐；历史规则尚未映射到现行玩法，社区观点不等于官方规则。")
        if gaps:
            answer += "实际缺口：" + "、".join(gaps)
        return FinalAnswerTurn(status="partial" if ids else "insufficient_evidence", answer=answer, citation_ids=ids)

    return [_call(1, "discover_catalog", game_ids=[game], character=left),
            lambda h: skill(h, 2, left), lambda h: skill(h, 3, right),
            _call(4, "discover_catalog", record_type="rule"),
            lambda h: text_search(h, 5, "rule", "主公 体力上限"),
            _call(6, "discover_catalog", record_type="analysis"),
            lambda h: text_search(h, 7, "analysis", "主公 生存 配合"), read_last, finish]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=WORKSPACE / "data/local/knowledge.local/gamewise.sqlite3")
    parser.add_argument("--text-config", type=Path, default=WORKSPACE / "backend/text-collections.example.json")
    parser.add_argument("--game", default="sanguosha-ol")
    parser.add_argument("--left", default="刘备")
    parser.add_argument("--right", default="曹操")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE / "data/local/tool-preview.local/unified")
    args = parser.parse_args()
    library = load_text_library(args.text_config)
    question = f"{args.game}的{args.left}和{args.right}谁适合做主公？"
    service = AskService(ScriptedModel(preview_steps(args.game, args.left, args.right)),
        lambda: KnowledgeTools(args.database, [args.game], preview=True, text_library=library))
    result = asyncio.run(service.ask(question))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    lines = ["# 本地虚拟模型调用预览", "", question, "",
             "调用顺序预设，查询和补读使用真实本地资料；没有真实LLM、联网、评分或正式回答。",
             "本页每条正文仅展示前240字符；完整工具返回见result.json。检索命中不等于内容足以支持结论。", ""]
    for index, exchange in enumerate(result.trace, 1):
        lines.extend([f"## 第{index}步：{exchange.call.name}", "", "请求：", "```json", exchange.call.arguments, "```"])
        if exchange.response.error:
            lines.append(f"错误：{exchange.response.error.code} / {exchange.response.error.message}")
        for scope in exchange.response.results:
            lines.append(f"范围：{scope.scope.scope_id if scope.scope else scope.game_id}；状态：{scope.status}")
            for record in scope.records:
                data = record.model_dump()
                lines.extend([f"证据：{record.citation_id}", data.get("skill_text", data.get("text", ""))[:240],
                              "来源：" + data.get("source_locator", data.get("source_url", ""))])
                if data.get("truncated"):
                    lines.append("补读正文超过额度，结果明确标记截断。")
                lines.extend(data.get("limitations", []))
        lines.append("")
    lines.extend(["## 最终状态", "", result.status, result.answer or "", result.error.message if result.error else ""])
    (args.output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(dict(status=result.status, calls=len(result.trace), citations=len(result.citations),
                         output=str(args.output_dir.resolve()), answerable=False), ensure_ascii=False))
    if result.error:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
