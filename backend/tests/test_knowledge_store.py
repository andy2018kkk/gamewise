"""SQLite 存取行为测试；使用临时库及合成资料，不下载真实资料。"""

from datetime import date
import hashlib
import json
import sqlite3

import pytest

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.character import CharacterRecord
from app.schemas.query import Game
from app.schemas.source import SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, ManualExtractor, SourceDocument
from app.services.import_characters import import_character_files
from experiments.query_samples import load_samples, discover_catalog, search_records


def save_batch(root, source_id, game_id="a", package="包甲", text="准备阶段，摸2张牌。", mode="经典", character="合成武将"):
    raw_path = root / f"{source_id}-raw.txt"
    raw_path.write_text(f"{character}\n{mode}\n武将包：{package}\n吴\n合成技能：{text}", encoding="utf-8")
    source = SourceRecord(
        source_id=source_id, title=f"合成页面-{source_id}", game_id=game_id,
        topic="合成入库测试", content_path=str(raw_path), source_type="local", source_locator=str(raw_path),
        collected_at=date(2026, 10, 28), content_hash=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
    )
    document = SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="skill", text=f"合成技能：{text}", verbatim_text=text, locator="第6行"),
        EvidenceBlock(block_id="meta", text=f"{character} {mode} 武将包：{package} 吴", locator="第1至4行"),
    ))
    data = CharacterRecord(character=character, mode=mode, package_kind="武将包", package_name=package,
                           faction="吴", skills=[{"skill_name": "合成技能", "skill_text": text}])
    result = DataProcessor(CharacterRecord).process(document, ManualExtractor([
        {"data": data.model_dump(), "block_ids": ["skill", "meta"]},
    ]))
    path = root / f"{source_id}-processed.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


@pytest.fixture
def setup_data(tmp_path):
    games = [Game(game_id=key, name=f"合成服{key}", category="synthetic") for key in "abc"]
    paths = [save_batch(tmp_path, "one"), save_batch(tmp_path, "two", "b", package="包乙", mode="1V1")]
    return tmp_path / "knowledge.sqlite3", games, paths


def test_import_persists_and_repeated_import_adds_nothing(setup_data):
    database, games, paths = setup_data
    first = import_character_files(database, paths, games)
    assert first.totals == {"games": 3, "sources": 2, "source_snapshots": 2,
                            "sanguosha_characters": 2, "sanguosha_skills": 2}
    second = import_character_files(database, paths, games)
    assert all(count == 0 for count in second.added.values())
    assert second.totals == first.totals
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts() == first.totals


def test_same_snapshot_identity_with_new_record_id_cannot_duplicate(setup_data):
    database, games, paths = setup_data
    first = import_character_files(database, [paths[0]], games)
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    payload["records"][0]["record_id"] = "different-processing-result"
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="同一快照.*冲突"):
        import_character_files(database, [paths[0]], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts() == first.totals


@pytest.mark.parametrize("filters", [
    {"character": "合成武将"}, {"skill_name": "合成技能"}, {"package_name": "包甲"},
    {"faction": "吴"}, {"game_ids": ["b"], "character": "合成武将", "mode": "1V1"},
    {"game_ids": ["a"], "package_name": "包乙"}, {"game_ids": ["c"], "skill_name": "合成技能"},
])
def test_sqlite_query_equals_json_query(setup_data, filters):
    database, games, paths = setup_data
    import_character_files(database, paths, games)
    store = load_samples(paths, games, record_format="character")
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.search_records(**filters) == search_records(store, **filters)
        catalog_filters = {key: value for key, value in filters.items() if key in {"game_ids", "character", "package_name", "faction"}}
        assert repository.discover_catalog(**catalog_filters) == discover_catalog(store, **catalog_filters)


def test_same_skill_in_different_packages_keeps_both_texts(setup_data, tmp_path):
    database, games, paths = setup_data
    other = save_batch(tmp_path, "other-package", package="包丙", text="准备阶段，弃1张牌。")
    import_character_files(database, [paths[0], other], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        result = repository.search_records(game_ids=["a"], skill_name="合成技能")[0]
        assert len(result.records) == 2
        assert {record.skill_text for record in result.records} == {"准备阶段，摸2张牌。", "准备阶段，弃1张牌。"}
        assert len({record.record_id for record in result.records}) == 2
        assert repository.search_records(package_name="包丙")[0].records[0].skill_text == "准备阶段，弃1张牌。"


def test_same_character_and_identical_skill_in_two_modes_stay_separate(setup_data):
    from pathlib import Path
    database, games, paths = setup_data
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    raw_path = Path(payload["source"]["content_path"])
    raw_path.write_text(raw_path.read_text(encoding="utf-8") + "\n国战", encoding="utf-8")
    payload["source"]["content_hash"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    source = SourceRecord.model_validate(payload["source"])
    classic = CharacterRecord.model_validate(payload["records"][0]["data"])
    national = classic.model_copy(update={"mode": "国战"})
    document = SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="skill", text=f"合成技能：{classic.skills[0].skill_text}",
                      verbatim_text=classic.skills[0].skill_text, locator="共同原文"),
        EvidenceBlock(block_id="meta", text="合成武将 经典 国战 武将包 包甲 吴", locator="模式及属性"),
    ))
    batch = DataProcessor(CharacterRecord).process(document, ManualExtractor([
        {"data": record.model_dump(), "block_ids": ["skill", "meta"]} for record in (classic, national)
    ]))
    paths[0].write_text(batch.model_dump_json(), encoding="utf-8")
    import_character_files(database, [paths[0]], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        records = repository.search_records(game_ids=["a"], character="合成武将")[0].records
        assert len(records) == 2 and {record.mode for record in records} == {"经典", "国战"}
        assert len({record.record_id for record in records}) == 2
        assert records[0].skill_text == records[1].skill_text
        assert len(repository.search_records(game_ids=["a"], character="合成武将", mode="国战")[0].records) == 1


@pytest.mark.parametrize("requested", ["经典", "经典版本"])
def test_mode_labels_match_only_in_registered_category_without_mixing_modes(tmp_path, requested):
    games = [Game(game_id="a", name="甲", category="sanguosha"), Game(game_id="b", name="乙", category="sanguosha"),
             Game(game_id="c", name="其他游戏", category="other")]
    paths = [save_batch(tmp_path, "a-classic", mode="经典", text="甲服经典原文"),
             save_batch(tmp_path, "a-national", mode="国战", text="甲服国战原文"),
             save_batch(tmp_path, "b-classic", "b", mode="经典版本", text="乙服经典原文"),
             save_batch(tmp_path, "c-classic", "c", mode="经典版本", text="其他游戏原文")]
    database = tmp_path / "modes.sqlite3"
    import_character_files(database, paths, games)
    store = load_samples(paths, games, record_format="character")
    with KnowledgeRepository(database, readonly=True) as repo:
        actual = repo.search_records(game_ids=["a", "b"], character="合成武将", mode=requested)
        expected = search_records(store, game_ids=["a", "b"], character="合成武将", mode=requested)
        assert [r.model_dump() for r in actual] == [r.model_dump() for r in expected]
        assert [(r.game_id, [(s.mode, s.skill_text) for s in r.records]) for r in actual] == [
            ("a", [("经典", "甲服经典原文")]), ("b", [("经典版本", "乙服经典原文")])]
        unrelated = repo.search_records(game_ids=["c"], character="合成武将", mode="经典")[0]
        assert not unrelated.records
        assert repo.search_records(game_ids=["a"], character="合成武将", mode="经典限定")[0].records == []


def test_conflict_rolls_back_earlier_insertions_in_same_batch(setup_data, tmp_path):
    database, games, paths = setup_data
    initial = import_character_files(database, [paths[0]], games)
    new = save_batch(tmp_path, "new-first")
    changed = json.loads(paths[0].read_text(encoding="utf-8"))
    changed["source"]["title"] = "同编号不同标题"
    paths[0].write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        import_character_files(database, [new, paths[0]], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts() == initial.totals


def test_reused_record_id_with_different_package_is_conflict(setup_data, tmp_path):
    database, games, paths = setup_data
    initial = import_character_files(database, [paths[0]], games)
    other = save_batch(tmp_path, "other", package="包丙")
    payload = json.loads(other.read_text(encoding="utf-8"))
    payload["records"][0]["record_id"] = json.loads(paths[0].read_text(encoding="utf-8"))["records"][0]["record_id"]
    other.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        import_character_files(database, [other], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts() == initial.totals


def test_actual_sql_error_midway_rolls_back_whole_batch(setup_data):
    database, games, paths = setup_data
    with KnowledgeRepository(database, initialize=True):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER fail_skill BEFORE INSERT ON sanguosha_skills BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        import_character_files(database, paths, games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert all(value == 0 for value in repository.counts().values())


def test_existing_parent_cannot_silently_gain_a_skill(setup_data):
    from pathlib import Path
    database, games, paths = setup_data
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    extra_text = "结束阶段，摸1张牌。"
    payload["records"][0]["evidence"].append({"block_id": "extra", "text": extra_text, "locator": "新增原文位置"})
    raw_path = Path(payload["source"]["content_path"])
    raw_path.write_text(raw_path.read_text(encoding="utf-8") + "\n" + extra_text, encoding="utf-8")
    payload["source"]["content_hash"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    initial = import_character_files(database, [paths[0]], games)
    payload["records"][0]["data"]["skills"].append({"skill_name": "新增技能", "skill_text": extra_text})
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="技能集合.*冲突"):
        import_character_files(database, [paths[0]], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts() == initial.totals


@pytest.mark.parametrize("mutation", ["changed_raw", "missing_raw", "no_hash", "wrong_skill", "unregistered_game"])
def test_invalid_import_cannot_create_database(setup_data, mutation):
    database, games, paths = setup_data
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    if mutation == "changed_raw":
        from pathlib import Path
        Path(payload["source"]["content_path"]).write_text("已改动", encoding="utf-8")
    elif mutation == "missing_raw":
        payload["source"]["content_path"] += ".missing"
    elif mutation == "no_hash":
        payload["source"]["content_hash"] = None
    elif mutation == "wrong_skill":
        payload["records"][0]["data"]["skills"][0]["skill_text"] = "原文没有的技能"
    else:
        payload["source"]["game_id"] = "unknown"
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        import_character_files(database, paths, games)
    assert not database.exists()


@pytest.mark.parametrize("filters", [{}, {"skill_name": " "}, {"character": "合成武将", "game_ids": []},
                                     {"character": "合成武将", "game_ids": ["unknown"]},
                                     {"skill_name": "' OR 1=1 --"}])
def test_invalid_filters_or_sql_injection_cannot_bypass_filtering(setup_data, filters):
    database, games, paths = setup_data
    import_character_files(database, paths, games)
    with KnowledgeRepository(database, readonly=True) as repository:
        if "OR 1=1" in filters.get("skill_name", ""):
            assert all(not side.records for side in repository.search_records(**filters))
        else:
            with pytest.raises(ValueError):
                repository.search_records(**filters)


def test_foreign_keys_and_unique_skill_constraints(setup_data):
    database, games, paths = setup_data
    import_character_files(database, paths, games)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO sanguosha_skills VALUES ('missing', '技能', '原文', NULL)")
        row = connection.execute("SELECT * FROM sanguosha_skills LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO sanguosha_skills VALUES (?, ?, ?, ?)", row)


def test_query_missing_or_incompatible_database_is_not_empty_result(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        KnowledgeRepository(missing, readonly=True)
    assert not missing.exists()
    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="结构"):
        KnowledgeRepository(unknown, initialize=True)
    with sqlite3.connect(unknown) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]
