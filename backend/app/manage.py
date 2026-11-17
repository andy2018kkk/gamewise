"""本地 SQLite 导入与待审资料查询入口。"""

import argparse
import json
from pathlib import Path
import sqlite3

from pydantic import TypeAdapter

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import Game
from app.services.import_characters import import_character_files
from app.services.import_sources import import_source_files
from app.services.rule_documents import process_rule_pages
from app.services.knowledge_tools import KnowledgeTools
from app.services.text_library import load_text_library
from app.services.character_families import CharacterFamilies

WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = WORKSPACE / "data/local/knowledge.local/gamewise.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sources = commands.add_parser("import-sources", help="登记规则或参考文件的来源和快照，不写武将表")
    sources.add_argument("manifests", type=Path, nargs="+")
    sources.add_argument("--games-config", type=Path, required=True)
    sources.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    tool = commands.add_parser("tool", help="模拟模型发出只读工具调用；不接真实LLM")
    tool.add_argument("name", choices=("discover_catalog", "search_records", "read_evidence"))
    tool.add_argument("--arguments-file", type=Path, required=True, help="工具参数JSON文件")
    tool.add_argument("--game", action="append", dest="allowed_games", required=True, help="由程序限定的允许范围")
    tool.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    tool.add_argument("--preview", action="store_true", help="显式读取待审正文，仅本地开发预览")
    tool.add_argument("--text-config", type=Path, help="本地文本范围配置；模型不能指定此路径")
    tool.add_argument("--family-config", type=Path, help="程序提供的人物关联目录配置，模型不能指定路径")
    rules = commands.add_parser("process-rules", help="离线整理基础规则正文，不写数据库")
    rules.add_argument("--manifest", type=Path, default=WORKSPACE / "data/local/sgs-rules.local/manifest.json")
    rules.add_argument("--output-dir", type=Path, default=WORKSPACE / "data/local/rules-processed.local")
    importer = commands.add_parser("import-samples", help="导入新格式待审 JSON，验证原文快照后整批保存")
    importer.add_argument("input_dir", type=Path, nargs="?", default=WORKSPACE / "data/local/character-preview.local")
    importer.add_argument("--games-config", type=Path, default=Path(__file__).resolve().parents[1] / "catalog.json")
    importer.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    stats = commands.add_parser("stats", help="查看数据库各表数量")
    stats.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    query = commands.add_parser("query", help="只读查询待审资料，不接 LLM")
    query.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    query.add_argument("--game", action="append", dest="game_ids")
    query.add_argument("--character")
    query.add_argument("--skill", dest="skill_name")
    query.add_argument("--mode")
    query.add_argument("--package", dest="package_name")
    query.add_argument("--faction")
    query.add_argument("--catalog", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "import-sources":
            games = TypeAdapter(list[Game]).validate_json(args.games_config.read_text(encoding="utf-8-sig"))
            report = import_source_files(args.database, args.manifests, games)
            output = {"database": str(args.database.resolve()), **report.model_dump()}
        elif args.command == "tool":
            tools = KnowledgeTools(args.database, args.allowed_games, preview=args.preview,
                text_library=load_text_library(args.text_config) if args.text_config else None,
                character_families=CharacterFamilies.from_config(args.family_config) if args.family_config else None)
            result = tools.invoke(args.name, args.arguments_file.read_text(encoding="utf-8-sig"))
            print(result.model_dump_json(indent=2))
            if result.error is not None:
                raise SystemExit(2)
            return
        elif args.command == "process-rules":
            output = process_rule_pages(args.manifest, args.output_dir)
        elif args.command == "import-samples":
            if not args.input_dir.is_dir():
                raise FileNotFoundError(f"输入目录不存在：{args.input_dir}")
            games = TypeAdapter(list[Game]).validate_json(args.games_config.read_text(encoding="utf-8"))
            paths = sorted(args.input_dir.glob("*-characters.json"))
            report = import_character_files(args.database, paths, games)
            output = {"database": str(args.database.resolve()), **report.model_dump()}
        else:
            with KnowledgeRepository(args.database, readonly=True) as repository:
                if args.command == "stats":
                    output = {"database": str(args.database.resolve()), "totals": repository.counts(), "answerable": False}
                else:
                    if args.catalog:
                        if args.skill_name is not None or args.mode is not None:
                            parser.error("目录查询不接受 --skill 或 --mode")
                        result = repository.discover_catalog(game_ids=args.game_ids, character=args.character,
                                                             package_name=args.package_name, faction=args.faction)
                    else:
                        result = repository.search_records(game_ids=args.game_ids, character=args.character,
                                                           skill_name=args.skill_name, mode=args.mode,
                                                           package_name=args.package_name, faction=args.faction)
                    output = {"answerable": False, "note": "本地待审资料查询，不是正式问答。",
                              "results": [item.model_dump() for item in result]}
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, f"操作失败：{error}\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
