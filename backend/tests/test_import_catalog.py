"""全量队列的分批、断点和站点异常暂停，全部使用合成响应。"""

import csv
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app.schemas.query import Game
from experiments.import_catalog import ORDER, prepare_queue, run_queue
from experiments.import_catalog import _save_queue


def setup_catalog(tmp_path, ol_count=2):
    root = tmp_path / "catalog"
    root.mkdir()
    for branch in ORDER:
        count = ol_count if branch == "sgsol" else 1
        with (root / f"{branch}-candidates.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["branch", "page_id", "title", "source_groups"])
            writer.writeheader()
            for i in range(1, count + 1):
                writer.writerow({"branch": branch, "page_id": i, "title": f"武将{i}", "source_groups": ""})
    games = [Game(game_id="sanguosha-ol", name="OL"), Game(game_id="sanguosha-mobile", name="移动版"),
             Game(game_id="sanguosha-shizhounian", name="十周年")]
    return root, games


def response(target):
    label = "basic-info-row-label" if target.branch == "msgs" else "技能标签"
    section = "经典" if target.branch == "msgs" else "经典版本"
    html = (f'<h2>{section}</h2><div class="character-lines-and-skills-section"><div>'
            '<div class="middle-grey-side-transparen-bg">技能</div><div><div>'
            f'<div class="{label}"><a href="/{target.branch}/skill">技能甲</a></div>'
            '<div>出牌阶段，摸1张牌。</div></div></div></div></div>')
    return json.dumps({"parse": {"pageid": target.page_id, "title": target.title,
                                 "revid": 1, "text": {"*": html}}}, ensure_ascii=False).encode("utf-8")


def test_all_candidates_are_planned_in_order_with_bounded_batches(tmp_path):
    catalog, _ = setup_catalog(tmp_path, 17)
    queue = prepare_queue(tmp_path / "queue", catalog, tmp_path / "db.sqlite3")
    assert queue["total_pages"] == 19
    assert [(b["branch"], b["count"]) for b in queue["batches"]] == [
        ("sgsol", 15), ("sgsol", 2), ("msgs", 1), ("sgs", 1)]
    assert prepare_queue(tmp_path / "queue", catalog, tmp_path / "db.sqlite3") == queue


def test_full_queue_imports_and_second_run_does_not_fetch(tmp_path):
    catalog, games = setup_catalog(tmp_path)
    root, database = tmp_path / "queue", tmp_path / "db.sqlite3"
    first = run_queue(root, catalog, database, games, fetcher=response, max_batches=0)
    assert first["full_import_complete"] and first["counts"]["imported"] == 4
    second = run_queue(root, catalog, database, games,
                       fetcher=lambda t: pytest.fail("已完成队列不应重新联网"), max_batches=0)
    assert second["counts"] == first["counts"] and not second["answerable"]


def test_rejection_pauses_entire_queue_and_requires_explicit_resume(tmp_path):
    catalog, games = setup_catalog(tmp_path, 17)
    root, database, calls = tmp_path / "queue", tmp_path / "db.sqlite3", []
    def blocked(target):
        calls.append(target.page_id)
        if target.page_id == 2:
            raise HTTPError(target.api_url, 567, "synthetic rejection", None, None)
        return response(target)
    first = run_queue(root, catalog, database, games, fetcher=blocked, max_batches=0)
    assert first["paused"] and calls == [1, 2]
    assert first["counts"]["queued"] == 4
    second = run_queue(root, catalog, database, games,
                       fetcher=lambda t: pytest.fail("暂停后不能自动重试"), max_batches=0)
    assert second["paused"]
    third = run_queue(root, catalog, database, games, fetcher=response, max_batches=0, resume=True)
    assert third["full_import_complete"] and third["counts"]["imported"] == 19


def test_changed_catalog_does_not_overwrite_frozen_queue(tmp_path):
    catalog, _ = setup_catalog(tmp_path)
    root = tmp_path / "queue"
    prepare_queue(root, catalog, tmp_path / "db.sqlite3")
    original = (root / "queue.json").read_bytes()
    with (catalog / "msgs-candidates.csv").open("a", encoding="utf-8") as handle:
        handle.write("msgs,9,新增武将,\n")
    with pytest.raises(ValueError, match="目录"):
        prepare_queue(root, catalog, tmp_path / "db.sqlite3")
    assert (root / "queue.json").read_bytes() == original


def test_queue_save_retries_transient_windows_file_replacement(tmp_path, monkeypatch):
    original, calls = Path.replace, []
    def locked_once(path, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError("synthetic temporary file lock")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", locked_once)
    _save_queue(tmp_path, {"paused": True})
    assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8")) == {"paused": True}
    assert len(calls) == 2
    _save_queue(tmp_path, {"paused": True})
    assert len(calls) == 2  # 相同进度不重复替换文件。
