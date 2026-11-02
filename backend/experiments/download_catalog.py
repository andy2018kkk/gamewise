"""只保存全量原始 API 快照；不解析技能、不入库、不调用模型。"""

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError

from experiments.batch_bwiki import BatchReport, BatchTarget, DownloadEvent, PageResult, fetch_page, _write_report
from experiments.import_catalog import prepare_queue, _save_queue, _write_json


@contextmanager
def worker_lock(root: Path) -> Iterator[None]:
    """Windows 本地采集只允许一个 CLI 进程，退出后由系统释放文件锁。"""
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    with (root / "download.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("该目录已有采集进程，未重复启动") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _validate_raw(raw: bytes, target: BatchTarget) -> None:
    payload = json.loads(raw)
    parsed = payload.get("parse", {}) if isinstance(payload, dict) else {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("text", {}), dict):
        raise ValueError("API 页面结构无效")
    if (not isinstance(payload, dict) or "error" in payload or parsed.get("title") != target.title
            or type(parsed.get("pageid")) is not int or parsed.get("pageid") != target.page_id or type(parsed.get("revid")) is not int
            or parsed["revid"] <= 0 or not isinstance(parsed.get("text", {}).get("*"), str)):
        raise ValueError("响应不是指定页面的有效展开快照，未计为下载成功")


def download_page(page: PageResult, batch_root: Path,
                  fetcher: Callable[[BatchTarget], bytes] | None) -> bool:
    """返回是否新发出请求；未知或被改动的缓存不覆盖。"""
    path = Path(page.snapshot) if page.snapshot else batch_root / "raw" / f"{page.target.key}.json"
    page.snapshot = str(path.resolve())
    if path.exists():
        raw = path.read_bytes()
        if page.sha256 is None or hashlib.sha256(raw).hexdigest() != page.sha256:
            raise ValueError("缓存缺少已登记指纹或内容已变，未覆盖")
        requested = False
    else:
        if page.sha256 is not None:
            raise FileNotFoundError("已登记快照缺失，不自动替换")
        if fetcher is None:
            raise FileNotFoundError("尚无缓存，未授权本次联网")
        raw = fetcher(page.target)
        _validate_raw(raw, page.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(raw)
        temporary.replace(path)
        page.sha256 = hashlib.sha256(raw).hexdigest()
        page.collected_at = datetime.now(timezone.utc).isoformat()
        requested = True
    _validate_raw(raw, page.target)
    if page.status != "imported":
        page.status = "downloaded"
    page.error, page.download_error = None, None
    return requested


def _save_http_diagnostic(batch_root: Path, page: PageResult, error: HTTPError, *, attempt: int | None = None) -> None:
    directory = batch_root / "errors"
    directory.mkdir(exist_ok=True)
    body = error.read(16384) if getattr(error, "fp", None) is not None else b""
    suffix = f"-attempt-{attempt}" if attempt is not None else ""
    body_path = directory / f"{page.target.key}{suffix}-response.txt"
    body_path.write_bytes(body)
    headers = error.headers or {}
    detail = {"status": error.code, "url": page.target.api_url,
              "content_type": headers.get("Content-Type"), "server": headers.get("Server"),
              "retry_after": headers.get("Retry-After"), "body_path": str(body_path.resolve()),
              "saved_body_bytes": len(body), "may_be_truncated": len(body) == 16384}
    text = json.dumps(detail, ensure_ascii=False, indent=2) + "\n"
    (directory / f"{page.target.key}{suffix}-diagnostic.json").write_text(text, encoding="utf-8")
    if attempt is not None:
        (directory / f"{page.target.key}-response.txt").write_bytes(body)
        (directory / f"{page.target.key}-diagnostic.json").write_text(text, encoding="utf-8")


def retry_delay(error: HTTPError, default: float, *, now: datetime | None = None) -> float:
    """有效 Retry-After 不缩短；格式错误时采用本地退避值。"""
    value = (error.headers or {}).get("Retry-After")
    if value is None:
        return default
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            seconds = (instant - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return default
    return max(default, seconds) if math.isfinite(seconds) else default


def download_status(root: Path, queue: dict) -> dict:
    branches = {branch: {"target": 0, "downloaded": 0, "failed": 0, "queued": 0} for branch in queue["order"]}
    issues = []
    for batch in queue["batches"]:
        report = BatchReport.model_validate_json((root / batch["name"] / "report.json").read_text(encoding="utf-8"))
        for page in report.pages:
            row = branches[page.target.branch]
            row["target"] += 1
            if page.status in {"downloaded", "prepared", "imported"} and page.sha256:
                row["downloaded"] += 1
            elif page.status == "failed":
                row["failed"] += 1
                issues.append({"branch": page.target.branch, "title": page.target.title, "error": page.error})
            else:
                row["queued"] += 1
    total = sum(row["downloaded"] for row in branches.values())
    result = {"total_candidate_pages": queue["total_pages"], "downloaded_pages": total,
              "raw_download_complete": total == queue["total_pages"], "branches": branches,
              "paused": queue["paused"], "pause_reason": queue["pause_reason"],
              "answerable": False, "scope": "仅原始页面下载，不表示结构化或审核完成", "issues": issues}
    _write_json(root / "download-status.json", result)
    return result


def run_downloads(root: Path, catalog_dir: Path, database: Path, *,
                  fetcher: Callable[[BatchTarget], bytes] | None = None,
                  resume: bool = False, max_requests: int = 0,
                  retry_delays: tuple[float, ...] = (),
                  wait_on_567: bool = False,
                  sleep: Callable[[float], None] = time.sleep,
                  on_batch: Callable[[Path], None] | None = None) -> dict:
    if max_requests < 0:
        raise ValueError("请求上限不能为负，0 表示持续推进直到结束或异常暂停")
    if len(retry_delays) > 2 or any(not math.isfinite(d) or d < 0 or d > 300 for d in retry_delays):
        raise ValueError("最多两次等待重试，每次等待配置须在0至300秒内")
    queue = prepare_queue(root, catalog_dir, database)
    # 暂停状态仍可离线审计已有缓存；不会重新发出网络请求。
    if not queue["paused"] or resume:
        queue["paused"], queue["pause_reason"] = False, None
    requests = 0
    for batch in queue["batches"]:
        batch_root = root / batch["name"]
        path = batch_root / "report.json"
        report = BatchReport.model_validate_json(path.read_text(encoding="utf-8"))
        for page in report.pages:
            if page.error and "暂停剩余联网请求" in page.error and page.sha256 is None and not page.download_error:
                page.status, page.error = "queued", None
                _write_report(path, report)
            cached = page.sha256 is not None and Path(page.snapshot or "").is_file()
            if not cached and (queue["paused"] or (max_requests and requests >= max_requests)):
                continue
            if not cached and fetcher is None:
                continue
            def bounded_fetch(target: BatchTarget) -> bytes:
                nonlocal requests
                index = 0
                while True:
                    requests += 1
                    page.download_attempts += 1
                    try:
                        raw = fetcher(target)
                    except HTTPError as error:
                        page.status, page.error = "failed", f"HTTPError: {error}"
                        page.download_error = page.error
                        page.download_events.append(DownloadEvent(attempt=page.download_attempts,
                            outcome="http_error", http_status=error.code))
                        _save_http_diagnostic(batch_root, page, error, attempt=page.download_attempts)
                        persistent = error.code == 567 and wait_on_567
                        within_cap = not max_requests or requests < max_requests
                        can_retry = within_cap and (persistent or
                            (error.code in {429, 567} and index < len(retry_delays)))
                        default_delay = min(60 * 2 ** min(index, 4), 900) if persistent else (
                            retry_delays[index] if can_retry else 0)
                        delay = retry_delay(error, default_delay) if can_retry else 0
                        if not can_retry or (delay > 300 and not persistent):
                            raise
                        page.download_events.append(DownloadEvent(attempt=page.download_attempts,
                            outcome="retry_wait", http_status=error.code, wait_seconds=delay))
                        policy = "持续等待" if persistent else "有限重试"
                        queue["paused"], queue["pause_reason"] = True, f"{page.error}；等待 {delay:g} 秒后{policy}"
                        queue["retry_batch"] = batch["name"]
                        _write_report(path, report)
                        _save_queue(root, queue)
                        download_status(root, queue)
                        # 长时间网络等待不应阻塞本批已落盘页面的本地入库。
                        if on_batch is not None:
                            on_batch(batch_root)
                        print(f"{target.branch}/{target.title}: 等待 {delay:g} 秒后重试（{policy}）", flush=True)
                        sleep(delay)
                        queue["paused"], queue["pause_reason"] = False, None
                        index += 1
                        continue
                    page.download_events.append(DownloadEvent(attempt=page.download_attempts, outcome="success"))
                    return raw
                raise RuntimeError("有限重试未返回结果")
            try:
                download_page(page, batch_root, bounded_fetch if fetcher is not None else None)
            except HTTPError as error:
                page.status, page.error = "failed", f"HTTPError: {error}"
                page.download_error = page.error
                if error.code in {403, 429, 567}:
                    queue["paused"], queue["pause_reason"] = True, page.error
                    queue["retry_batch"] = batch["name"]
            except (OSError, ValueError) as error:
                page.status, page.error = "failed", f"{type(error).__name__}: {error}"
            _write_report(path, report)
            if not cached:
                _save_queue(root, queue)
                download_status(root, queue)
                print(f"{page.target.branch}/{page.target.title}: {page.status}", flush=True)
        _save_queue(root, queue)
        if on_batch is not None:
            on_batch(batch_root)
    return download_status(root, queue)


def main() -> None:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=workspace / "data/local/full-import.local")
    parser.add_argument("--catalog-dir", type=Path, default=workspace / "data/local/sgs-catalog.local")
    parser.add_argument("--database", type=Path, default=workspace / "data/local/knowledge.local/gamewise.sqlite3")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--wait-on-567", action="store_true", help="HTTP 567 持续退避等待，不因次数耗尽退出")
    args = parser.parse_args()
    def paced_fetch(target: BatchTarget) -> bytes:
        time.sleep(10)
        return fetch_page(target, attempts=1)
    with worker_lock(args.root):
        worker = {"pid": os.getpid(), "state": "running",
                  "started_at_utc": datetime.now(timezone.utc).isoformat()}
        _write_json(args.root / "worker.json", worker)
        try:
            result = run_downloads(args.root, args.catalog_dir, args.database,
                                   fetcher=paced_fetch if args.fetch else None,
                                   resume=args.resume, max_requests=args.max_requests, retry_delays=(30, 60),
                                   wait_on_567=args.wait_on_567)
        except BaseException as error:
            worker.update(state="failed", error_type=type(error).__name__)
            _write_json(args.root / "worker.json", worker)
            raise
        worker["state"] = "paused" if result["paused"] else ("completed" if result["raw_download_complete"] else "stopped")
        _write_json(args.root / "worker.json", worker)
    print(json.dumps({k: v for k, v in result.items() if k != "issues"}, ensure_ascii=False, indent=2))
    if result["paused"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
