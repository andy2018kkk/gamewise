"""查询本地待审技能样本；不联网、不写库、不接入正式问答。"""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Literal

from app.schemas.character import CharacterRecord
from app.schemas.query import CatalogItem, Game, QueryFilters as _Filters, SampleRecord, SearchResult, mode_labels
from app.services.data_processor import ProcessingResult
from experiments.process_bwiki import SkillRecord


@dataclass(frozen=True)
class SampleStore:
    games: tuple[Game, ...]
    batches: tuple[ProcessingResult[SkillRecord] | ProcessingResult[CharacterRecord], ...]


def load_samples(
    paths: list[Path], games: list[Game], *, record_format: Literal["skill", "character"] = "skill",
) -> SampleStore:
    """一次加载并验证所有输入；文件错误不能伪装成未收录。"""
    game_ids = {game.game_id for game in games}
    if not games or len(game_ids) != len(games):
        raise ValueError("游戏目录不能为空或有重复编号")
    if record_format not in {"skill", "character"}:
        raise ValueError("样本格式必须明确为 skill 或 character")
    batch_type = ProcessingResult[CharacterRecord] if record_format == "character" else ProcessingResult[SkillRecord]
    batches = []
    source_ids, record_ids = set(), set()
    for path in paths:
        batch = batch_type.model_validate_json(path.read_text(encoding="utf-8"))
        if batch.source.game_id not in game_ids:
            raise ValueError("样本所属游戏未登记")
        if batch.source.source_id in source_ids:
            raise ValueError("本实验不允许重复来源；多个快照的选择需另行设计")
        if batch.source.review_status != "pending":
            raise ValueError("本入口只处理待审实验样本")
        source_ids.add(batch.source.source_id)
        for record in batch.records:
            if not record.record_id.strip() or record.record_id in record_ids:
                raise ValueError("样本记录编号为空或重复")
            record_ids.add(record.record_id)
            skills = record.data.skills if isinstance(record.data, CharacterRecord) else [record.data]
            for skill in skills:
                body = " ".join(skill.skill_text.split())
                if not record.evidence or not any(
                    body == " ".join((block.verbatim_text or block.text).split()) for block in record.evidence
                ):
                    raise ValueError("技能原文缺少完整证据对应")
        batches.append(batch)
    return SampleStore(tuple(games), tuple(batches))


def _selected_games(store: SampleStore, game_ids: list[str] | None) -> tuple[Game, ...]:
    if game_ids is None:
        return store.games
    known = {game.game_id for game in store.games}
    if not game_ids or len(set(game_ids)) != len(game_ids) or any(value not in known for value in game_ids):
        raise ValueError("游戏范围为空、重复或未登记")
    return tuple(game for game in store.games if game.game_id in game_ids)


def _matching_records(store: SampleStore, game: Game, filters: _Filters) -> list[SampleRecord]:
    matches = []
    for batch in store.batches:
        if batch.source.game_id != game.game_id:
            continue
        for record in batch.records:
            data = record.data
            character_data = data if isinstance(data, CharacterRecord) else None
            skills = character_data.skills if character_data is not None else [data]
            for skill in skills:
                # 武将记录可含多个技能；返回独立技能编号，同时保留父记录以便回溯。
                record_id = (hashlib.sha256(f"{record.record_id}\0{skill.skill_name}".encode("utf-8")).hexdigest()
                             if character_data is not None else record.record_id)
                item = SampleRecord(
                    character=data.character, mode=data.mode, skill_name=skill.skill_name, skill_text=skill.skill_text,
                    record_id=record_id, parent_record_id=record.record_id if character_data is not None else None,
                    game_id=game.game_id, version_ids=list(batch.source.version_ids), source_id=batch.source.source_id,
                    source_locator=batch.source.source_locator, content_path=batch.source.content_path,
                    content_hash=batch.source.content_hash, locators=[block.locator for block in record.evidence],
                    package_kind=character_data.package_kind if character_data is not None else None,
                    package_name=character_data.package_name if character_data is not None else None,
                    faction=character_data.faction if character_data is not None else None,
                    hp_raw=character_data.hp_raw if character_data is not None else None,
                    tags=skill.tags if character_data is not None else None,
                )
                if all(value is None or (getattr(item, name) in mode_labels(value, game.category) if name == "mode"
                                        else getattr(item, name) == value) for name, value in filters.model_dump().items()):
                    matches.append(item)
    return matches


def _status(store: SampleStore, game_id: str, has_matches: bool) -> Literal["sample_available", "no_match_in_sample", "not_collected"]:
    if has_matches:
        return "sample_available"
    if any(batch.source.game_id == game_id for batch in store.batches):
        return "no_match_in_sample"
    return "not_collected"


def discover_catalog(
    store: SampleStore, *, game_ids: list[str] | None = None, character: str | None = None,
    package_name: str | None = None, faction: str | None = None,
) -> list[CatalogItem]:
    """列出本次加载样本的覆盖；不是全站目录，不推断系列或上线状态。"""
    filters = _Filters(character=character, package_name=package_name, faction=faction)
    result = []
    for game in _selected_games(store, game_ids):
        records = _matching_records(store, game, filters)
        result.append(CatalogItem(
            game_id=game.game_id, name=game.name, status=_status(store, game.game_id, bool(records)),
            characters=sorted({record.character for record in records}),
            modes=sorted({record.mode for record in records}), record_count=len(records),
            packages=sorted({record.package_name for record in records if record.package_name is not None}),
            factions=sorted({record.faction for record in records if record.faction is not None}),
        ))
    return result


def search_records(
    store: SampleStore, *, game_ids: list[str] | None = None, character: str | None = None,
    skill_name: str | None = None, mode: str | None = None,
    package_name: str | None = None, faction: str | None = None,
) -> list[SearchResult]:
    """精确筛选待审样本；未给模式则分别保留各模式，条件取交集。"""
    filters = _Filters(character=character, skill_name=skill_name, mode=mode, package_name=package_name, faction=faction)
    if all(value is None for value in (filters.character, filters.skill_name, filters.package_name, filters.faction)):
        raise ValueError("至少提供武将名、技能名、包名或势力")
    result = []
    for game in _selected_games(store, game_ids):
        records = _matching_records(store, game, filters)
        result.append(SearchResult(
            game_id=game.game_id, status=_status(store, game.game_id, bool(records)), records=records,
        ))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path, help="已有 processed.json 所在目录")
    parser.add_argument("--game", action="append", dest="game_ids", help="游戏编号，可重复传入")
    parser.add_argument("--character")
    parser.add_argument("--skill", dest="skill_name")
    parser.add_argument("--mode")
    parser.add_argument("--package", dest="package_name")
    parser.add_argument("--faction")
    parser.add_argument("--format", choices=("skill", "character"), default="skill", dest="record_format")
    parser.add_argument("--catalog", action="store_true")
    args = parser.parse_args()
    # 此处仅是实验入口的三服配置，不进入通用查询函数。
    games = [Game(game_id="sanguosha-shizhounian", name="十周年"),
             Game(game_id="sanguosha-ol", name="OL"),
             Game(game_id="sanguosha-mobile", name="移动版")]
    suffix = "characters" if args.record_format == "character" else "processed"
    store = load_samples([args.input_dir / f"{branch}-{suffix}.json" for branch in ("sgs", "sgsol")],
                         games, record_format=args.record_format)
    if args.catalog:
        if args.skill_name is not None or args.mode is not None:
            parser.error("目录查询不接受 --skill 或 --mode")
        result = discover_catalog(store, game_ids=args.game_ids, character=args.character,
                                  package_name=args.package_name, faction=args.faction)
    else:
        result = search_records(store, game_ids=args.game_ids, character=args.character,
                                skill_name=args.skill_name, mode=args.mode,
                                package_name=args.package_name, faction=args.faction)
    print(json.dumps({"experiment_only": True, "answerable": False,
                      "note": "待审样本预览，未验证规则适用范围，不是正式问答。",
                      "results": [item.model_dump() for item in result]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
