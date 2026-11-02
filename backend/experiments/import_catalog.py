"""全量候选目录的分批待审导入；复用 batch_bwiki，网络异常时暂停。"""

import argparse
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import re
import time

from pydantic import TypeAdapter

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import Game
from experiments.batch_bwiki import BatchReport, BatchTarget, PageResult, fetch_page, run_batch, _write_report
from experiments.bwiki_relations import load_candidates
from experiments.preview_bwiki import BRANCHES

ORDER = ("sgsol", "msgs", "sgs")


def prepare_queue(root: Path, catalog_dir: Path, database: Path) -> dict:
    """冻结本轮候选清单；入库记录仅说明有资料，不视为页面已完整处理。"""
    candidates = load_candidates(catalog_dir)
    hashes = {branch: hashlib.sha256((catalog_dir / f"{branch}-candidates.csv").read_bytes()).hexdigest()
              for branch in ORDER}
    path = root / "queue.json"
    if path.exists():
        queue = json.loads(path.read_text(encoding="utf-8"))
        if queue["catalog_hashes"] != hashes:
            raise ValueError("目录与冻结清单不一致，请先审查变更，不覆盖既有队列")
        batches = queue["batches"]
        if (queue["total_pages"] != len(candidates) or sum(batch["count"] for batch in batches) != len(candidates)
                or len({batch["name"] for batch in batches}) != len(batches)
                or any(batch["branch"] not in ORDER or not re.fullmatch(r"(?:sgsol|msgs|sgs)-\d{3}", batch["name"])
                       or not batch["name"].startswith(batch["branch"] + "-")
                       or not 1 <= batch["count"] <= 15 for batch in batches)):
            raise ValueError("队列目录、数量或批次路径不合法，未执行")
        return queue
    receipts = {}
    if database.exists():
        with KnowledgeRepository(database, readonly=True) as repository:
            for candidate in candidates:
                source_id = f"bwiki-{candidate.branch}-{candidate.title}"
                rows = repository.connection.execute(
                    "SELECT p.content_path,p.content_hash,p.collected_at FROM sources s "
                    "JOIN source_snapshots p ON s.source_id=p.source_id WHERE s.source_id=? AND s.game_id=?",
                    (source_id, BRANCHES[candidate.branch][1]),
                ).fetchall()
                if len(rows) == 1:
                    receipts[(candidate.branch, candidate.page_id)] = dict(rows[0])
    batches = []
    root.mkdir(parents=True, exist_ok=True)
    for branch in ORDER:
        # 先扩大正文覆盖，已有资料的页面仍排入末尾做章节审计，不自动算完成。
        selected = sorted((c for c in candidates if c.branch == branch),
                          key=lambda c: ((c.branch, c.page_id) in receipts, c.page_id))
        for offset in range(0, len(selected), 15):
            name = f"{branch}-{offset // 15 + 1:03d}"
            pages = []
            for candidate in selected[offset:offset + 15]:
                target = BatchTarget(branch=branch, page_id=candidate.page_id, title=candidate.title)
                # 已经用户核对过的单页补充说明排除，不推广同名章节。
                if (branch, candidate.page_id, candidate.title) == ("msgs", 12525, "谋刘备"):
                    target.section_exclusions["巅峰"] = "用户已核对为经典技能的重复补充说明，当前不单独收录"
                receipt = receipts.get((branch, candidate.page_id))
                pages.append(PageResult(target=target, snapshot=receipt["content_path"],
                    sha256=receipt["content_hash"], collected_at=receipt["collected_at"])
                    if receipt else PageResult(target=target))
            batch_root = root / name
            batch_root.mkdir()
            _write_report(batch_root / "report.json", BatchReport(pages=pages))
            batches.append({"name": name, "branch": branch, "count": len(pages), "attempted": False})
    queue = {"catalog_hashes": hashes, "total_pages": len(candidates), "order": list(ORDER),
             "previously_imported_pages_needing_audit": len(receipts),
             "batches": batches, "paused": False, "pause_reason": None, "answerable": False}
    _save_queue(root, queue)
    return queue


def _write_json(destination: Path, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if destination.exists() and destination.read_text(encoding="utf-8") == text:
        return
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(4):
        try:
            temporary.replace(destination)
            break
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.1 * 2 ** attempt)


def _save_queue(root: Path, queue: dict) -> None:
    _write_json(root / "queue.json", queue)


def queue_status(root: Path, queue: dict) -> dict:
    counts = {key: 0 for key in ("queued", "downloaded", "imported", "imported_with_section_gaps", "prepared", "failed")}
    errors = []
    for batch in queue["batches"]:
        report = BatchReport.model_validate_json((root / batch["name"] / "report.json").read_text(encoding="utf-8"))
        for page in report.pages:
            state = ("imported_with_section_gaps" if page.status == "imported" and page.unmapped_skill_sections
                     else page.status)
            counts[state] += 1
            if page.error or page.unmapped_skill_sections:
                errors.append({"branch": page.target.branch, "title": page.target.title,
                               "error": page.error, "unmapped_skill_sections": page.unmapped_skill_sections})
    result = {"total_candidate_pages": queue["total_pages"], "counts": counts,
              "paused": queue["paused"], "pause_reason": queue["pause_reason"],
              "full_import_complete": counts["imported"] == queue["total_pages"],
              "answerable": False, "issues": errors}
    (root / "status.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def run_queue(root: Path, catalog_dir: Path, database: Path, games: list[Game], *,
              fetcher: Callable[[BatchTarget], bytes] | None = None, max_batches: int = 1, resume: bool = False) -> dict:
    if max_batches < 0:
        raise ValueError("批次数不能为负；0 表示处理到队列结束或网络暂停")
    queue = prepare_queue(root, catalog_dir, database)
    if queue["paused"] and not resume:
        return queue_status(root, queue)
    retry_name = queue.get("retry_batch") if resume else None
    queue["paused"], queue["pause_reason"] = False, None
    candidates = load_candidates(catalog_dir)
    done = 0
    for batch in queue["batches"]:
        if batch["attempted"] and batch["name"] != retry_name:
            continue
        if max_batches and done >= max_batches:
            break
        batch_root = root / batch["name"]
        previous = BatchReport.model_validate_json((batch_root / "report.json").read_text(encoding="utf-8"))
        report = run_batch([page.target for page in previous.pages], batch_root, games,
                           database=database, fetcher=fetcher, candidates=candidates)
        batch["attempted"] = True
        done += 1
        blocked = next((page for page in report.pages if page.status == "failed" and page.download_error
                        and any(f" {code}:" in page.download_error for code in (403, 429, 567))), None)
        if blocked is not None or report.import_error:
            queue["paused"] = True
            queue["pause_reason"] = blocked.download_error if blocked else report.import_error
            queue["retry_batch"] = batch["name"]
            _save_queue(root, queue)
            break
        queue["retry_batch"] = None
        _save_queue(root, queue)
    return queue_status(root, queue)


def main() -> None:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=workspace / "data/local/full-import.local")
    parser.add_argument("--catalog-dir", type=Path, default=workspace / "data/local/sgs-catalog.local")
    parser.add_argument("--database", type=Path, default=workspace / "data/local/knowledge.local/gamewise.sqlite3")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--resume", action="store_true", help="经检查后恢复上次网络暂停的批次")
    parser.add_argument("--max-batches", type=int, default=1, help="默认一批；0 表示持续推进，遇拒绝立即停")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    games = TypeAdapter(list[Game]).validate_json((Path(__file__).resolve().parents[1] / "catalog.json").read_text(encoding="utf-8"))
    queue = prepare_queue(args.root, args.catalog_dir, args.database)
    def paced_fetch(target):
        time.sleep(3)
        return fetch_page(target)
    status = (queue_status(args.root, queue) if args.plan_only else
              run_queue(args.root, args.catalog_dir, args.database, games,
                        fetcher=paced_fetch if args.fetch else None, max_batches=args.max_batches, resume=args.resume))
    print(json.dumps({key: value for key, value in status.items() if key != "issues"}, ensure_ascii=False, indent=2))
    if status["paused"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
