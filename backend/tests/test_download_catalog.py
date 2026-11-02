"""下载与解析分离；验证缓存、页面身份和网络暂停，不访问真实网站。"""

import csv
from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from experiments.download_catalog import retry_delay, run_downloads, worker_lock


def catalog(tmp_path):
    directory = tmp_path / "catalog"
    directory.mkdir()
    for branch in ("sgsol", "msgs", "sgs"):
        with (directory / f"{branch}-candidates.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["branch", "page_id", "title", "source_groups"])
            writer.writeheader()
            for number in range(1, (3 if branch == "sgsol" else 2)):
                writer.writerow({"branch": branch, "page_id": number, "title": f"武将{number}", "source_groups": ""})
    return directory


def response(target):
    # 故意不是解析器能处理的 HTML；原始采集不依赖网页排版。
    return json.dumps({"parse": {"pageid": target.page_id, "title": target.title, "revid": 1,
                                  "text": {"*": "<div><h2>未知布局"}}}, ensure_ascii=False).encode("utf-8")


def test_raw_collection_does_not_require_skill_parser_or_create_database(tmp_path):
    database = tmp_path / "db.sqlite3"
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), database, fetcher=response)
    assert result["downloaded_pages"] == 4 and result["raw_download_complete"]
    assert not result["answerable"] and not database.exists()


def test_cached_pages_are_not_requested_again(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    run_downloads(root, directory, database, fetcher=response)
    second = run_downloads(root, directory, database, fetcher=lambda t: pytest.fail("不能重复请求缓存"))
    assert second["raw_download_complete"]


def test_blocked_download_preserves_diagnostics_and_pauses_all_branches(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    calls = []
    def blocked(target):
        calls.append((target.branch, target.page_id))
        if target.page_id == 2:
            raise HTTPError(target.api_url, 567, "synthetic rejection", None, None)
        return response(target)
    first = run_downloads(root, directory, database, fetcher=blocked)
    assert calls == [("sgsol", 1), ("sgsol", 2)] and first["paused"]
    diagnostic = json.loads((root / "sgsol-001/errors/sgsol-2-diagnostic.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == 567
    second = run_downloads(root, directory, database, fetcher=lambda t: pytest.fail("暂停不能自动继续"))
    assert second["paused"] and second["downloaded_pages"] == 1
    third = run_downloads(root, directory, database, fetcher=response, resume=True)
    assert third["raw_download_complete"] and not third["paused"]


@pytest.mark.parametrize("mutation", ["wrong_title", "wrong_page_id", "api_error", "bad_parse"])
def test_invalid_response_never_counts_as_collected(tmp_path, mutation):
    def wrong(target):
        payload = json.loads(response(target))
        if mutation == "wrong_title": payload["parse"]["title"] = "另一个武将"
        elif mutation == "wrong_page_id": payload["parse"]["pageid"] = 999
        elif mutation == "bad_parse": payload["parse"] = []
        else: payload = {"error": {"code": "missingtitle"}}
        return json.dumps(payload).encode("utf-8")
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3", fetcher=wrong)
    assert not result["raw_download_complete"] and result["downloaded_pages"] == 0
    assert all(row["failed"] == row["target"] for row in result["branches"].values())


def test_changed_cache_is_reported_and_not_overwritten(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    run_downloads(root, directory, database, fetcher=response)
    path = root / "sgsol-001/raw/sgsol-1.json"
    path.write_bytes(b"changed")
    result = run_downloads(root, directory, database, fetcher=lambda t: pytest.fail("不应覆盖缓存"))
    assert not result["raw_download_complete"] and path.read_bytes() == b"changed"
    assert "缓存" in result["issues"][0]["error"]


def test_transient_567_waits_then_succeeds_preserving_first_response(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    calls, waits = [], []
    def temporary(target):
        calls.append(target)
        if len(calls) == 1:
            raise HTTPError(target.api_url, 567, "temporary", {}, BytesIO(b"first-response"))
        return response(target)
    result = run_downloads(root, directory, database, fetcher=temporary,
                           retry_delays=(30, 60), sleep=waits.append)
    assert result["raw_download_complete"] and waits == [30]
    assert len(calls) == 5
    assert (root / "sgsol-001/errors/sgsol-1-attempt-1-response.txt").read_bytes() == b"first-response"


def test_repeated_567_stops_after_three_attempts_and_does_not_touch_next_page(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    calls, waits = [], []
    def blocked(target):
        calls.append(target)
        raise HTTPError(target.api_url, 567, "temporary", {}, BytesIO(b"response"))
    result = run_downloads(root, directory, database, fetcher=blocked,
                           retry_delays=(30, 60), sleep=waits.append)
    assert result["paused"] and result["downloaded_pages"] == 0
    assert len(calls) == 3 and all(t.page_id == 1 and t.branch == "sgsol" for t in calls)
    assert waits == [30, 60]


def test_403_does_not_retry_and_retry_after_long_delay_pauses(tmp_path):
    for code, headers in [(403, {}), (429, {"Retry-After": "600"})]:
        parent = tmp_path / str(code)
        parent.mkdir()
        directory = catalog(parent)
        calls, waits = [], []
        def blocked(target):
            calls.append(target)
            raise HTTPError(target.api_url, code, "blocked", headers, None)
        result = run_downloads(tmp_path / f"raw-{code}", directory, tmp_path / "db.sqlite3",
                               fetcher=blocked, retry_delays=(30, 60), sleep=waits.append)
        assert result["paused"] and len(calls) == 1 and waits == []


def test_retry_after_seconds_date_and_invalid_value():
    instant = datetime(2026, 10, 29, 0, 0, 0, tzinfo=timezone.utc)
    for value, expected in [("90", 90), ("Thu, 29 Oct 2026 00:02:00 GMT", 120),
                            ("invalid", 30), ("-1", 30), ("nan", 30)]:
        error = HTTPError("https://example.test", 429, "temporary", {"Retry-After": value}, None)
        assert retry_delay(error, 30, now=instant) == expected


def test_continuous_567_wait_exceeds_old_retry_limit_then_recovers(tmp_path):
    calls, waits = [], []
    def temporary(target):
        calls.append(target)
        if len(calls) <= 6:
            raise HTTPError(target.api_url, 567, "temporary", {}, None)
        return response(target)
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3",
                           fetcher=temporary, wait_on_567=True, sleep=waits.append)
    assert result["raw_download_complete"] and not result["paused"]
    assert waits == [60, 120, 240, 480, 900, 900]
    assert all(t == calls[0] for t in calls[:7])


def test_continuous_567_honors_server_wait_and_explicit_request_cap(tmp_path):
    calls, waits = [], []
    def blocked(target):
        calls.append(target)
        raise HTTPError(target.api_url, 567, "temporary", {"Retry-After": "1200"}, None)
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3",
                           fetcher=blocked, wait_on_567=True, max_requests=3, sleep=waits.append)
    assert result["paused"] and len(calls) == 3 and waits == [1200, 1200]


@pytest.mark.parametrize("code", [403, 429])
def test_continuous_567_does_not_enable_unlimited_retry_for_other_codes(tmp_path, code):
    calls, waits = [], []
    def blocked(target):
        calls.append(target)
        raise HTTPError(target.api_url, code, "blocked", {}, None)
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3",
                           fetcher=blocked, wait_on_567=True, retry_delays=(30, 60), sleep=waits.append)
    assert result["paused"]
    assert len(calls) == (1 if code == 403 else 3)
    assert waits == ([] if code == 403 else [30, 60])


def test_request_cap_counts_retries(tmp_path):
    calls, waits = [], []
    def blocked(target):
        calls.append(target)
        raise HTTPError(target.api_url, 567, "temporary", {}, None)
    result = run_downloads(tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3",
                           fetcher=blocked, max_requests=2, retry_delays=(30, 60), sleep=waits.append)
    assert len(calls) == 2 and waits == [30] and result["paused"]


def test_interrupted_wait_keeps_pause_checkpoint(tmp_path):
    root, directory, database = tmp_path / "raw", catalog(tmp_path), tmp_path / "db.sqlite3"
    def blocked(target):
        raise HTTPError(target.api_url, 567, "temporary", {}, None)
    def interrupt(delay):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_downloads(root, directory, database, fetcher=blocked,
                      retry_delays=(30, 60), sleep=interrupt)
    assert json.loads((root / "queue.json").read_text(encoding="utf-8"))["paused"]
    result = run_downloads(root, directory, database, fetcher=lambda t: pytest.fail("等待中断后不自动联网"))
    assert result["paused"]


def test_live_progress_is_saved_before_next_request(tmp_path):
    root, directory = tmp_path / "raw", catalog(tmp_path)
    calls = []
    def fetch(target):
        calls.append(target)
        if len(calls) == 2:
            progress = json.loads((root / "download-status.json").read_text(encoding="utf-8"))
            assert progress["downloaded_pages"] == 1
        return response(target)
    run_downloads(root, directory, tmp_path / "db.sqlite3", fetcher=fetch, max_requests=2)
    assert len(calls) == 2


def test_second_cli_worker_cannot_acquire_same_directory(tmp_path):
    pytest.importorskip("msvcrt")
    with worker_lock(tmp_path):
        with pytest.raises(RuntimeError, match="已有采集进程"):
            with worker_lock(tmp_path):
                pytest.fail("不能同时启动两个采集进程")
    with worker_lock(tmp_path):
        pass
