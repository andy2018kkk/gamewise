"""全目录低频采集并逐页导入待审库；原始下载和解析结果分别保留。"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from pydantic import TypeAdapter

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import Game
from experiments.batch_bwiki import BatchReport, fetch_page, run_batch
from experiments.bwiki_relations import load_candidates
from experiments.download_catalog import run_downloads, worker_lock
from experiments.import_catalog import _write_json


class CachedImporter:
    """只消费已登记快照，不联网；单页事务避免一页冲突阻塞整批。"""

    def __init__(self, root: Path, catalog_dir: Path, database: Path, games: list[Game]):
        self.root, self.database, self.games = root, database, games
        self.candidates = load_candidates(catalog_dir)
        self.results: dict[str, dict] = {}
        self.status_path = root / "import-status.json"
        if self.status_path.exists():
            previous = json.loads(self.status_path.read_text(encoding="utf-8"))
            self.results = previous["pages"]

    def __call__(self, batch_root: Path) -> None:
        raw_report = BatchReport.model_validate_json((batch_root / "report.json").read_text(encoding="utf-8"))
        for page in raw_report.pages:
            if page.sha256 is None or not Path(page.snapshot or "").is_file():
                continue
            previous = self.results.get(page.target.key, {})
            if previous.get("complete") and previous.get("sha256") == page.sha256:
                continue
            destination = self.root / "ingested" / page.target.key
            report = run_batch([page.target], destination, self.games, database=self.database,
                               candidates=self.candidates, cache_report=batch_root / "report.json")
            result = report.pages[0]
            self.results[page.target.key] = {
                "branch": page.target.branch, "title": page.target.title, "sha256": page.sha256,
                "imported": result.status == "imported",
                "complete": result.status == "imported" and not result.unmapped_skill_sections,
                "characters": result.character_count, "skills": result.skill_count,
                "unmapped_skill_sections": result.unmapped_skill_sections,
                "excluded_skill_sections": result.excluded_skill_sections,
                "error": report.import_error or result.error,
                "report": str((destination / "report.json").resolve()),
            }
            self.save()

    def save(self) -> dict:
        totals = {}
        if self.database.exists():
            with KnowledgeRepository(self.database, readonly=True) as repository:
                totals = repository.counts()
        result = {"updated_at_utc": datetime.now(timezone.utc).isoformat(),
                  "processed_pages": len(self.results),
                  "imported_pages": sum(p["imported"] for p in self.results.values()),
                  "complete_pages": sum(p["complete"] for p in self.results.values()),
                  "database_totals": totals, "answerable": False,
                  "scope": "已采集页面的待审导入；完整覆盖须与候选目录核对",
                  "pages": self.results}
        _write_json(self.status_path, result)
        return result


def main() -> None:
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=workspace / "data/local/full-import.local")
    parser.add_argument("--catalog-dir", type=Path, default=workspace / "data/local/sgs-catalog.local")
    parser.add_argument("--database", type=Path, default=workspace / "data/local/knowledge.local/gamewise.sqlite3")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--wait-on-567", action="store_true", help="HTTP 567 持续等待后重试")
    args = parser.parse_args()
    games = TypeAdapter(list[Game]).validate_json((workspace / "backend/catalog.json").read_text(encoding="utf-8"))
    def paced_fetch(target):
        time.sleep(10)
        return fetch_page(target, attempts=1)
    with worker_lock(args.root):
        worker = {"pid": os.getpid(), "state": "running", "mode": "download_and_import",
                  "started_at_utc": datetime.now(timezone.utc).isoformat()}
        _write_json(args.root / "worker.json", worker)
        try:
            importer = CachedImporter(args.root, args.catalog_dir, args.database, games)
            result = run_downloads(args.root, args.catalog_dir, args.database,
                                   fetcher=paced_fetch if args.fetch else None,
                                   resume=args.resume, max_requests=args.max_requests,
                                   retry_delays=(30, 60), wait_on_567=args.wait_on_567, on_batch=importer)
            imported = importer.save()
            complete = result["raw_download_complete"] and imported["complete_pages"] == result["total_candidate_pages"]
            worker["state"] = "paused" if result["paused"] else ("completed" if complete else "incomplete")
        except BaseException as error:
            worker.update(state="failed", error_type=type(error).__name__)
            _write_json(args.root / "worker.json", worker)
            raise
        _write_json(args.root / "worker.json", worker)
    print(json.dumps({"downloaded_pages": result["downloaded_pages"],
                      "imported_pages": imported["imported_pages"],
                      "complete_pages": imported["complete_pages"], "state": worker["state"],
                      "database_totals": imported["database_totals"], "answerable": False}, ensure_ascii=False))
    if result["paused"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
