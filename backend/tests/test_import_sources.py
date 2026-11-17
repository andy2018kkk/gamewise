"""来源登记不产生武将、技能；验证文件完整性和原子性。"""

from datetime import date
import hashlib

import pytest

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import Game
from app.schemas.source import SourceBatch, SourceRecord
from app.services.import_sources import import_source_files


def manifest(root, name, *, game="reference", source_id=None, title="合成分析摘要"):
    body = root / f"{name}.md"
    body.write_text("合成社区观点，仅用于测试，不是游戏规则。", encoding="utf-8")
    source = SourceRecord(source_id=source_id or name, title=title, game_id=game,
        topic="analysis_reference", content_path=body.name, source_type="local",
        source_locator=str(body), collected_at=date(2026, 11, 15),
        content_hash=hashlib.sha256(body.read_bytes()).hexdigest())
    path = root / f"{name}.json"
    path.write_text(SourceBatch(sources=[source]).model_dump_json(), encoding="utf-8")
    return path


def test_sources_persist_and_repeat_without_creating_warriors(tmp_path):
    path = manifest(tmp_path, "one")
    database = tmp_path / "test.sqlite3"
    games = [Game(game_id="reference", name="参考资料")]
    first = import_source_files(database, [path], games)
    assert first.added == dict(games=1, sources=1, source_snapshots=1,
                              sanguosha_characters=0, sanguosha_skills=0)
    assert not first.answerable
    again = import_source_files(database, [path], games)
    assert not any(again.added.values())
    with KnowledgeRepository(database, readonly=True) as repository:
        row = repository.connection.execute("SELECT * FROM source_snapshots").fetchone()
        assert row["review_status"] == "pending" and row["version_ids_json"] == "[]"
        assert row["content_path"] == str((tmp_path / "one.md").resolve())


@pytest.mark.parametrize("failure", ["tamper", "missing", "unknown_game"])
def test_invalid_files_never_create_database(tmp_path, failure):
    path = manifest(tmp_path, "one", game="unknown" if failure == "unknown_game" else "reference")
    body = tmp_path / "one.md"
    if failure == "tamper":
        body.write_text("被改写", encoding="utf-8")
    elif failure == "missing":
        body.unlink()
    database = tmp_path / "test.sqlite3"
    with pytest.raises((ValueError, OSError)):
        import_source_files(database, [path], [Game(game_id="reference", name="参考")])
    assert not database.exists()


def test_conflict_rolls_back_whole_batch(tmp_path):
    database = tmp_path / "test.sqlite3"
    games = [Game(game_id="reference", name="参考")]
    original = manifest(tmp_path, "original")
    import_source_files(database, [original], games)
    valid = manifest(tmp_path, "new")
    conflict = manifest(tmp_path, "conflict", source_id="original", title="冲突标题")
    with pytest.raises(ValueError, match="冲突"):
        import_source_files(database, [valid, conflict], games)
    with KnowledgeRepository(database, readonly=True) as repository:
        assert repository.counts()["sources"] == 1
