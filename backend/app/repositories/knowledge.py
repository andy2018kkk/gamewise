"""SQLite 待审资料存取；不做规则准入，也不选择最新快照。"""

import hashlib
from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from typing import Self

from pydantic import BaseModel

from app.schemas.character import CharacterRecord
from app.schemas.query import CatalogItem, Game, PreviewStatus, QueryFilters, SampleRecord, SearchResult
from app.schemas.source import SourceRecord
from app.services.data_processor import ProcessingResult

SCHEMA_VERSION = 1
APPLICATION_ID = 0x47574149
TABLE_KEYS = {
    "games": ("game_id",), "sources": ("source_id",), "source_snapshots": ("snapshot_id",),
    "sanguosha_characters": ("record_id",), "sanguosha_skills": ("character_record_id", "skill_name"),
}
SCHEMA = """
CREATE TABLE games (
    game_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL
);
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY NOT NULL, game_id TEXT NOT NULL REFERENCES games(game_id),
    title TEXT NOT NULL, source_type TEXT NOT NULL CHECK(source_type IN ('web','local')),
    source_locator TEXT NOT NULL
);
CREATE TABLE source_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL, source_id TEXT NOT NULL REFERENCES sources(source_id),
    content_hash TEXT NOT NULL, content_path TEXT NOT NULL, collected_at TEXT NOT NULL,
    usage_note TEXT, review_status TEXT NOT NULL CHECK(review_status='pending'), review_note TEXT,
    version_ids_json TEXT NOT NULL CHECK(json_valid(version_ids_json)),
    UNIQUE(source_id, content_hash)
);
CREATE TABLE sanguosha_characters (
    record_id TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
    character TEXT NOT NULL, mode TEXT NOT NULL, package_kind TEXT, package_name TEXT, faction TEXT, hp_raw TEXT,
    review_status TEXT NOT NULL CHECK(review_status='pending'),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json))
);
CREATE TABLE sanguosha_skills (
    character_record_id TEXT NOT NULL REFERENCES sanguosha_characters(record_id), skill_name TEXT NOT NULL,
    skill_text TEXT NOT NULL, tags_json TEXT CHECK(tags_json IS NULL OR json_valid(tags_json)),
    PRIMARY KEY(character_record_id, skill_name)
);
CREATE INDEX source_game ON sources(game_id);
CREATE INDEX snapshot_source ON source_snapshots(source_id);
CREATE INDEX character_filters ON sanguosha_characters(character, mode, package_name, faction);
CREATE INDEX skill_name ON sanguosha_skills(skill_name);
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ImportReport(BaseModel):
    added: dict[str, int]
    totals: dict[str, int]
    answerable: bool = False


class KnowledgeRepository:
    def __init__(self, path: Path, *, initialize: bool = False, readonly: bool = False) -> None:
        if initialize and readonly:
            raise ValueError("只读连接不能初始化")
        path = path.resolve()
        if initialize:
            path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ro" if readonly else ("rwc" if initialize else "rw")
        self.connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            application = self.connection.execute("PRAGMA application_id").fetchone()[0]
            if initialize and not tables and version == 0 and application == 0:
                self.connection.executescript(
                    f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA application_id={APPLICATION_ID};\n"
                    f"PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
                )
            elif version != SCHEMA_VERSION or application != APPLICATION_ID or tables != set(TABLE_KEYS):
                raise ValueError("数据库结构或版本不兼容，未执行迁移")
            if readonly:
                self.connection.execute("PRAGMA query_only=ON")
        except Exception:
            self.connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.connection.close()

    def counts(self) -> dict[str, int]:
        return {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLE_KEYS}

    def _insert_or_match(self, table: str, values: dict[str, object]) -> bool:
        keys = TABLE_KEYS[table]  # 表和列均来自模块内构造，查询内容一律绑定参数。
        where = " AND ".join(f"{key}=?" for key in keys)
        row = self.connection.execute(f"SELECT * FROM {table} WHERE {where}", tuple(values[key] for key in keys)).fetchone()
        if row is not None:
            if dict(row) != values:
                raise ValueError(f"{table} 已有编号与导入内容冲突")
            return False
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self.connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))
        return True

    def _save_source(self, source: SourceRecord, insert: Callable[[str, dict[str, object]], bool]) -> None:
        if source.review_status != "pending" or not source.content_hash:
            raise ValueError("当前存储入口只接受有内容指纹的待审资料")
        insert("sources", {key: getattr(source, key) for key in
                           ("source_id", "game_id", "title", "source_type", "source_locator")})
        snapshot_id = hashlib.sha256(_json([source.source_id, source.content_hash]).encode("utf-8")).hexdigest()
        insert("source_snapshots", {
            "snapshot_id": snapshot_id, "source_id": source.source_id, "content_hash": source.content_hash,
            "content_path": source.content_path, "collected_at": source.collected_at.isoformat(),
            "usage_note": source.usage_note, "review_status": source.review_status,
            "review_note": source.review_note, "version_ids_json": _json(source.version_ids),
        })

    def import_sources(self, games: list[Game], sources: list[SourceRecord]) -> ImportReport:
        """登记文件资料的来源和快照；不写武将或技能，不自动批准。"""
        added = dict.fromkeys(TABLE_KEYS, 0)

        def insert(table: str, values: dict[str, object]) -> bool:
            created = self._insert_or_match(table, values)
            added[table] += int(created)
            return created

        with self.connection:
            for game in games:
                insert("games", game.model_dump())
            for source in sources:
                self._save_source(source, insert)
        return ImportReport(added=added, totals=self.counts())

    def import_batches(self, games: list[Game], batches: list[ProcessingResult[CharacterRecord]]) -> ImportReport:
        """调用方先完成来源、指纹和证据校验；一批写入只有一次提交。"""
        added = dict.fromkeys(TABLE_KEYS, 0)

        def insert(table: str, values: dict[str, object]) -> bool:
            created = self._insert_or_match(table, values)
            added[table] += int(created)
            return created

        with self.connection:
            for game in games:
                insert("games", game.model_dump())
            for batch in batches:
                source = batch.source
                if source.review_status != "pending" or batch.review_status != "pending":
                    raise ValueError("当前存储入口仅接受待审资料")
                self._save_source(source, insert)
                snapshot_id = hashlib.sha256(_json([source.source_id, source.content_hash]).encode("utf-8")).hexdigest()
                for record in batch.records:
                    data = record.data
                    duplicate = self.connection.execute(
                        "SELECT record_id FROM sanguosha_characters WHERE snapshot_id=? AND character=? AND mode=? AND record_id<>?",
                        (snapshot_id, data.character, data.mode, record.record_id),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError("同一快照的武将模式已有不同记录，处理结果冲突，未自动覆盖或追加")
                    created = insert("sanguosha_characters", {
                        "record_id": record.record_id, "snapshot_id": snapshot_id,
                        **data.model_dump(exclude={"skills"}), "review_status": batch.review_status,
                        "evidence_json": _json([block.model_dump() for block in record.evidence]),
                    })
                    if not created:
                        stored_skills = [tuple(row) for row in self.connection.execute(
                            "SELECT skill_name,skill_text,tags_json FROM sanguosha_skills WHERE character_record_id=? ORDER BY skill_name",
                            (record.record_id,),
                        )]
                        expected_skills = sorted((skill.skill_name, skill.skill_text,
                                                  None if skill.tags is None else _json(skill.tags)) for skill in data.skills)
                        if stored_skills != expected_skills:
                            raise ValueError("武将记录编号已有技能集合与导入内容冲突")
                    for skill in data.skills:
                        insert("sanguosha_skills", {
                            "character_record_id": record.record_id, "skill_name": skill.skill_name,
                            "skill_text": skill.skill_text, "tags_json": None if skill.tags is None else _json(skill.tags),
                        })
        return ImportReport(added=added, totals=self.counts())

    def _games(self, game_ids: list[str] | None) -> list[Game]:
        games = [Game(**dict(row)) for row in self.connection.execute("SELECT * FROM games ORDER BY rowid")]
        if game_ids is None:
            return games
        known = {game.game_id for game in games}
        if not game_ids or len(set(game_ids)) != len(game_ids) or any(game_id not in known for game_id in game_ids):
            raise ValueError("游戏范围为空、重复或未登记")
        return [game for game in games if game.game_id in game_ids]

    def _query(self, game_id: str, filters: QueryFilters) -> list[SampleRecord]:
        columns = {"character": "c.character", "mode": "c.mode", "skill_name": "k.skill_name",
                   "package_name": "c.package_name", "faction": "c.faction"}
        conditions, values = ["s.game_id=?", "c.review_status='pending'", "p.review_status='pending'"], [game_id]
        for name, value in filters.model_dump().items():
            if value is not None:
                conditions.append(f"{columns[name]}=?")
                values.append(value)
        rows = self.connection.execute("""
            SELECT c.record_id AS parent_record_id,c.character,c.mode,c.package_kind,c.package_name,c.faction,c.hp_raw,
                   c.evidence_json,k.skill_name,k.skill_text,k.tags_json,s.game_id,s.source_id,s.source_locator,
                   p.version_ids_json,p.content_path,p.content_hash
            FROM sanguosha_characters c JOIN sanguosha_skills k ON k.character_record_id=c.record_id
            JOIN source_snapshots p ON p.snapshot_id=c.snapshot_id JOIN sources s ON s.source_id=p.source_id
            WHERE """ + " AND ".join(conditions) + " ORDER BY c.rowid,k.rowid", values)
        result = []
        for row in rows:
            item = dict(row)
            evidence = json.loads(item.pop("evidence_json"))
            item["locators"] = [block["locator"] for block in evidence]
            item["version_ids"] = json.loads(item.pop("version_ids_json"))
            tags = item.pop("tags_json")
            item["tags"] = None if tags is None else json.loads(tags)
            item["record_id"] = hashlib.sha256(f"{item['parent_record_id']}\0{item['skill_name']}".encode("utf-8")).hexdigest()
            result.append(SampleRecord(**item))
        return result

    def _status(self, game_id: str, has_records: bool) -> PreviewStatus:
        if has_records:
            return "sample_available"
        exists = self.connection.execute("SELECT 1 FROM sources WHERE game_id=? LIMIT 1", (game_id,)).fetchone()
        return "no_match_in_sample" if exists else "not_collected"

    def search_records(
        self, *, game_ids: list[str] | None = None, character: str | None = None, skill_name: str | None = None,
        mode: str | None = None, package_name: str | None = None, faction: str | None = None,
    ) -> list[SearchResult]:
        filters = QueryFilters(character=character, skill_name=skill_name, mode=mode, package_name=package_name, faction=faction)
        if all(value is None for value in (filters.character, filters.skill_name, filters.package_name, filters.faction)):
            raise ValueError("至少提供武将名、技能名、包名或势力")
        result = []
        for game in self._games(game_ids):
            records = self._query(game.game_id, filters)
            result.append(SearchResult(game_id=game.game_id, status=self._status(game.game_id, bool(records)), records=records))
        return result

    def discover_catalog(
        self, *, game_ids: list[str] | None = None, character: str | None = None,
        package_name: str | None = None, faction: str | None = None,
    ) -> list[CatalogItem]:
        filters = QueryFilters(character=character, package_name=package_name, faction=faction)
        result = []
        for game in self._games(game_ids):
            records = self._query(game.game_id, filters)
            result.append(CatalogItem(
                game_id=game.game_id, name=game.name, status=self._status(game.game_id, bool(records)),
                characters=sorted({record.character for record in records}), modes=sorted({record.mode for record in records}),
                record_count=len(records), packages=sorted({record.package_name for record in records if record.package_name is not None}),
                factions=sorted({record.faction for record in records if record.faction is not None}),
            ))
        return result
