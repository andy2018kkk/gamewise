"""下载后入库衔接：使用合成页面，不访问网站。"""

import json
from urllib.error import HTTPError

from experiments.download_catalog import run_downloads
from experiments.full_import import CachedImporter
from test_import_catalog import response, setup_catalog


def test_downloads_import_incrementally_and_resume_without_duplicates(tmp_path):
    catalog, games = setup_catalog(tmp_path)
    root, database = tmp_path / "queue", tmp_path / "db.sqlite3"
    importer = CachedImporter(root, catalog, database, games)
    first = run_downloads(root, catalog, database, fetcher=response,
                          max_requests=1, on_batch=importer)
    assert first["downloaded_pages"] == 1
    status = importer.save()
    assert status["imported_pages"] == 1 and not status["answerable"]
    # 同一批次剩余页面随后下载，已完成的单页不重复插入。
    second = run_downloads(root, catalog, database, fetcher=response, on_batch=importer)
    assert second["raw_download_complete"]
    status = importer.save()
    assert status["complete_pages"] == 4
    assert status["database_totals"]["sources"] == 4
    totals = status["database_totals"]
    reloaded = CachedImporter(root, catalog, database, games)
    run_downloads(root, catalog, database, on_batch=reloaded)
    assert reloaded.save()["database_totals"] == totals


def test_bad_layout_retains_raw_and_does_not_block_other_pages(tmp_path):
    catalog, games = setup_catalog(tmp_path)
    root, database = tmp_path / "queue", tmp_path / "db.sqlite3"
    def mixed(target):
        payload = json.loads(response(target))
        if target.branch == "sgsol" and target.page_id == 1:
            payload["parse"]["text"]["*"] = "<h2>未知排版</h2>"
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    importer = CachedImporter(root, catalog, database, games)
    raw = run_downloads(root, catalog, database, fetcher=mixed, on_batch=importer)
    status = importer.save()
    assert raw["downloaded_pages"] == 4 and status["imported_pages"] == 3
    assert status["pages"]["sgsol-1"]["error"]
    assert not status["pages"]["sgsol-1"]["complete"]
    assert (root / "sgsol-001/raw/sgsol-1.json").exists()


def test_network_pause_still_imports_cached_pages(tmp_path):
    catalog, games = setup_catalog(tmp_path)
    root, database = tmp_path / "queue", tmp_path / "db.sqlite3"
    def blocked(target):
        if target.page_id == 2:
            raise HTTPError(target.api_url, 567, "temporary", {}, None)
        return response(target)
    importer = CachedImporter(root, catalog, database, games)
    result = run_downloads(root, catalog, database, fetcher=blocked, on_batch=importer)
    assert result["paused"] and importer.save()["imported_pages"] == 1


def test_downloaded_pages_import_before_long_network_wait(tmp_path):
    catalog, games = setup_catalog(tmp_path)
    root, database = tmp_path / "queue", tmp_path / "db.sqlite3"
    importer = CachedImporter(root, catalog, database, games)
    def blocked(target):
        if target.page_id == 2:
            raise HTTPError(target.api_url, 567, "temporary", {}, None)
        return response(target)
    def verify_during_wait(seconds):
        assert seconds == 60
        status = importer.save()
        assert status["imported_pages"] == 1
        assert status["database_totals"]["sanguosha_characters"] == 1
    result = run_downloads(root, catalog, database, fetcher=blocked, on_batch=importer,
                           wait_on_567=True, max_requests=3, sleep=verify_during_wait)
    assert result["paused"] and importer.save()["imported_pages"] == 1
