"""终端问答入口：只接收问题和允许的服，模型自主选择已有只读工具。"""
import argparse
import asyncio
import json
from pathlib import Path

from app.ask_runtime import run_question
from app.providers.groq import GroqSettings
from app.services.character_families import CharacterFamilies
from app.services.text_library import load_text_library

WORKSPACE = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-remote", action="store_true", help="确认本次调用外部模型")
    parser.add_argument("--question", required=True, help="本次用户问题，不套用固定案例")
    parser.add_argument("--game", action="append", dest="games", required=True, help="允许查询的发行分支ID，可重复")
    parser.add_argument("--env-file", type=Path, default=WORKSPACE / ".env")
    parser.add_argument("--model", choices=["openai/gpt-oss-120b", "qwen/qwen3.8-27b"], help="本次选择已确认模型")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE / "data/local/tool-preview.local/groq-question")
    args = parser.parse_args()
    if not args.question.strip() or not all(game.strip() for game in args.games):
        parser.error("请提供非空问题和允许查询的发行分支")
    if not args.run_remote:
        parser.error("本入口调用外部API，须显式提供--run-remote")
    try:
        settings = GroqSettings.from_file(args.env_file, model=args.model)
    except OSError:
        parser.error("无法读取本地Groq配置文件；未发送请求，未显示配置内容")
    except ValueError:
        parser.error("模型或密钥配置缺失/无效：请通过--model或GROQ_MODEL指定模型，并设置GROQ_API_KEY；未发送请求")
    if settings.model not in {"openai/gpt-oss-120b", "qwen/qwen3.8-27b"}:
        parser.error("请选择已确认的模型；未发送请求")
    run = asyncio.run(run_question(args.question, settings,
        database=WORKSPACE / "data/local/knowledge.local/gamewise.sqlite3", game_ids=args.games,
        text_library=load_text_library(WORKSPACE / "backend/text-collections.example.json"),
        character_families=CharacterFamilies.from_config(WORKSPACE / "backend/character-families.example.json")))
    result = run.result.model_dump(mode="json")
    report = {"question": args.question, "model": settings.model, "allowed_game_ids": args.games,
              "elapsed_seconds": run.elapsed_seconds, "usage": [u.model_dump() for u in run.usage], "result": result}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 模型问答记录", "", "问题：" + args.question, "模型：" + settings.model, ""]
    for index, exchange in enumerate(result["trace"], 1):
        lines.extend([f"## 第{index}步：{exchange['call']['name']}", "参数：" + exchange["call"]["arguments"],
                      "程序实际返回：", "```json", json.dumps(exchange["response"], ensure_ascii=False, indent=2), "```", ""])
    lines.extend(["## 最终回答", result["answer"] or "未生成回答", "状态：" + result["status"],
                  "错误：" + json.dumps(result["error"], ensure_ascii=False), "", "## 来源"])
    for index, citation in enumerate(result["citations"], 1):
        title = citation.get("heading") or (citation.get("character", "") + " / " + citation.get("skill_name", ""))
        lines.append(f"{index}. {title}；{citation.get('source_url', citation.get('source_locator', ''))}")
    (args.output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": result["status"], "tool_calls": len(result["trace"]),
                      "output": str(args.output_dir.resolve())}, ensure_ascii=False))
    if result["error"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
