"""小批 BWIKI 采集与待审导入；复用现有解析器，显式 --fetch 才联网。"""

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.repositories.knowledge import ImportReport
from app.schemas.character import CharacterRecord
from app.schemas.query import Game
from app.schemas.source import NonBlankText
from app.services.data_processor import DataProcessor
from app.services.import_characters import import_character_files
from experiments.preview_bwiki import BRANCHES, SampleTree, extract_skills
from experiments.preview_characters import (excluded_skill_section, inspect_skill_sections,
                                            prepare_character_sample, resolve_section_characters)
from experiments.bwiki_relations import CatalogCandidate, RelatedPage, discover_related, load_candidates


class BatchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    branch: Literal["sgs", "sgsol", "msgs"]
    page_id: Annotated[int, Field(strict=True, gt=0)]
    title: NonBlankText
    section_characters: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)
    # 单页人工核对的排除项及理由，不扩展成全站同名章节规则。
    section_exclusions: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.branch}-{self.page_id}"

    @property
    def api_url(self) -> str:
        return f"https://wiki.biligame.com/{self.branch}/api.php?" + urlencode({
            "action": "parse", "page": self.title, "prop": "text|revid", "format": "json",
        })


class DownloadEvent(BaseModel):
    attempt: int
    outcome: Literal["success", "http_error", "retry_wait"]
    http_status: int | None = None
    wait_seconds: float | None = None


class SkillSupplement(BaseModel):
    """同一武将的条件技能原文；保留在审阅报告，不冒充独立模式或主技能。"""

    character: NonBlankText
    mode: NonBlankText
    condition: Literal["使命成功", "使命失败"]
    skill_name: NonBlankText
    skill_text: NonBlankText
    source_url: NonBlankText
    source_line: int
    repeats_base: bool


class PageResult(BaseModel):
    target: BatchTarget
    status: Literal["queued", "downloaded", "prepared", "imported", "failed"] = "queued"
    snapshot: str | None = None
    sha256: str | None = None
    collected_at: str | None = None
    revision: int | None = None
    sections: list[str] = Field(default_factory=list)
    other_sections: list[str] = Field(default_factory=list)
    processed: str | None = None
    character_count: int = 0
    skill_count: int = 0
    error: str | None = None
    download_error: str | None = None
    historical_rows_excluded: int = 0
    unmapped_skill_sections: list[str] = Field(default_factory=list)
    skill_supplements: list[SkillSupplement] = Field(default_factory=list)
    excluded_skill_sections: list[str] = Field(default_factory=list)
    related_pages: list[RelatedPage] = Field(default_factory=list)
    related_pages_checked: bool = False
    availability_status: str = "待确认"
    availability_basis: str = "尚未核对上线状态"
    download_attempts: int = 0
    download_events: list[DownloadEvent] = Field(default_factory=list)


class BatchReport(BaseModel):
    status: Literal["running", "completed", "partial", "failed"] = "running"
    coverage: Literal["selected_sections_only"] = "selected_sections_only"
    pages: list[PageResult]
    import_report: ImportReport | None = None
    import_error: str | None = None
    answerable: Literal[False] = False


def fetch_page(target: BatchTarget, *, attempts: int = 2) -> bytes:
    """最多两次尝试；网络失败保留为错误，不当作无技能。"""
    if attempts not in {1, 2}:
        raise ValueError("底层请求只支持一次或两次尝试")
    request = Request(target.api_url, headers={"User-Agent": "GameWise-local-study/0.1"})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=25) as response:
                content = response.read(5_000_001)
            if len(content) > 5_000_000:
                raise ValueError("页面响应超过本批大小限制")
            return content
        except HTTPError as error:
            if error.code in {403, 429, 567} or attempt == attempts - 1 or error.code < 500:
                raise
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(1)
    raise RuntimeError("网络重试未返回结果")


def _write_report(path: Path, report: BatchReport) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    # Windows 文件被短暂占用时重试；持续失败仍抛错，保留旧记录。
    for attempt in range(4):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.1 * 2 ** attempt)


def _prepare_page(page: PageResult, root: Path, fetcher: Callable[[BatchTarget], bytes] | None,
                  candidates: list[CatalogCandidate] | None = None) -> None:
    target = page.target
    if candidates is not None:
        candidate = next((item for item in candidates if item.branch == target.branch
                          and item.page_id == target.page_id and item.title == target.title), None)
        if candidate is None:
            raise ValueError("当前页编号或标题不在指定候选目录中")
        page.availability_status, page.availability_basis = candidate.availability_status, candidate.availability_basis
    # 新的章节配置可引用旧批次已登记的快照，不复制原件或改变来源定位。
    snapshot = Path(page.snapshot) if page.snapshot else root / "raw" / f"{target.key}.json"
    page.snapshot = str(snapshot.resolve())
    if snapshot.exists():
        raw = snapshot.read_bytes()
        if page.sha256 is None or hashlib.sha256(raw).hexdigest() != page.sha256:
            raise ValueError("已有快照缺少可信记录或指纹不一致，未覆盖")
    else:
        if page.sha256 is not None:
            raise FileNotFoundError("已有采集记录的快照缺失，未自动替换")
        if fetcher is None:
            raise FileNotFoundError("缺少本地快照；需显式允许抓取")
        raw = fetcher(target)
        page.download_error = None
        snapshot.write_bytes(raw)
        page.sha256 = hashlib.sha256(raw).hexdigest()
        page.collected_at = datetime.now(timezone.utc).isoformat()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("API 返回错误，不能作为无资料处理")
    parsed = payload.get("parse", {})
    if parsed.get("pageid") != target.page_id or parsed.get("title") != target.title:
        raise ValueError("页面编号或标题与候选清单不符")
    html = parsed.get("text", {}).get("*")
    if not isinstance(html, str):
        raise ValueError("缺少展开 HTML")
    tree = SampleTree(html)
    tree.feed(html)
    tree.close()
    if len(tree.stack) != 1:
        raise ValueError("HTML 未闭合")
    headings = [node.text().strip() for node in tree.elements if node.tag == "h2"]
    if candidates is not None:
        page.related_pages = discover_related(tree, target.branch, target.page_id, target.title, candidates)
        page.related_pages_checked = True
    skill_sections = inspect_skill_sections(snapshot, target.branch)
    identities = resolve_section_characters(target.branch, target.title, skill_sections, target.section_characters)
    if set(target.section_characters) - set(skill_sections):
        raise ValueError("章节身份配置未对应本页含技能章节")
    if set(target.section_exclusions) - set(skill_sections):
        raise ValueError("排除配置未对应本页含技能章节")
    if set(target.section_exclusions) & set(target.section_characters):
        raise ValueError("同一章节不能同时配置身份与排除")
    page.excluded_skill_sections = [heading for heading in skill_sections
                                    if excluded_skill_section(heading) or heading in target.section_exclusions]
    if set(target.section_characters) & set(page.excluded_skill_sections):
        raise ValueError("自走棋、统率版本暂不收录，不能通过身份映射启用")
    known_modes = {"经典", "经典版本", "国战", "国战版本", "1V1", "1V1版本"}
    page.sections = [heading for heading in headings if heading in skill_sections and
                     (heading in known_modes or heading in identities)
                     and heading not in page.excluded_skill_sections]
    page.unmapped_skill_sections = [heading for heading in skill_sections
                                   if heading not in page.sections and heading not in page.excluded_skill_sections]
    page.other_sections = [heading for heading in headings if heading not in page.sections]
    if not page.sections:
        if "无武将" in tree.root.text() and "技能数据" in tree.root.text():
            raise ValueError("源页面明确缺少技能数据，保留目录缺口")
        raise ValueError(f"没有受支持的模式章节：{headings}")
    extracted = extract_skills(payload, target.branch, page.sections, target.title)
    # 使命状态仅作当前武将经典技能的条件补充；不改动已入库技能或版本。
    mission_sections = [s for s in skill_sections if s in {"使命成功", "使命失败"}]
    if target.branch == "msgs" and "经典" in page.sections and mission_sections:
        base = {(item["skill"], item["text"]) for item in extracted if item["section"] == "经典"}
        page.skill_supplements = [SkillSupplement(
            character=target.title, mode="经典", condition=item["section"],
            skill_name=item["skill"], skill_text=item["text"], source_url=item["source_url"],
            source_line=item["source_line"], repeats_base=(item["skill"], item["text"]) in base,
        ) for item in extract_skills(payload, target.branch, mission_sections, target.title,
                                    allow_condition_variants=True)]
    page.historical_rows_excluded = sum({item["section"]: item["historical_rows_excluded"]
                                         for item in extracted}.values())
    document, extractor = prepare_character_sample(snapshot, target.branch, target.title, sections=page.sections,
                                                   section_characters=identities)
    if page.availability_status in {"已上线", "未上线"}:
        document.source.review_note = (f"{document.source.review_note or ''} 上线状态：{page.availability_status}；"
                                       f"依据：{page.availability_basis}。上线状态与技能审核分开。")
    processor = DataProcessor(CharacterRecord, verbatim_fields=("character", "mode"))
    result = processor.process(document, extractor)
    processed = root / "processed" / f"{target.key}-characters.json"
    processed.write_text(processor.render(result), encoding="utf-8")
    page.processed = str(processed.resolve())
    page.revision = parsed["revid"]
    page.character_count = len(result.records)
    page.skill_count = sum(len(record.data.skills) for record in result.records)
    page.status = "prepared"


def _write_review(root: Path, report: BatchReport) -> None:
    lines = ["# 小批资料待审清单", "", "按配置区分具体武将与模式；其他章节列出供核对，不能算整页或全服收齐。",
             "来源使用、规则适用期、技能标签和语义仍待审核；数据不能用于正式问答。", "",
             "| 服 | 武将 | 状态 | 已处理章节 | 模式记录 | 技能 |", "| --- | --- | --- | --- | ---: | ---: |"]
    for page in report.pages:
        lines.append(f"| {BRANCHES[page.target.branch][0]} | {page.target.title} | {page.status} | "
                     f"{'、'.join(page.sections)} | {page.character_count} | {page.skill_count} |")
    for page in report.pages:
        lines.extend(["", f"## {BRANCHES[page.target.branch][0]} / {page.target.title}", "",
                      f"其他章节：{'、'.join(page.other_sections) or '无'}", ""])
        lines.extend([f"待映射的技能章节：{'、'.join(page.unmapped_skill_sections) or '无'}",
                      f"按当前范围排除：{'、'.join(page.excluded_skill_sections) or '无'}", ""])
        lines.extend([f"上线状态：{page.availability_status}；依据：{page.availability_basis}", ""])
        if page.skill_supplements:
            lines.extend(["技能条件补充（已提取供审阅，未并入数据库主技能，不另建武将或模式）：", ""])
            for item in page.skill_supplements:
                lines.extend([f"### {item.character} / {item.mode} / {item.condition} / {item.skill_name}", "",
                              "与基础技能正文相同。" if item.repeats_base else item.skill_text, "",
                              f"[来源]({item.source_url})；快照 HTML 第{item.source_line}行。", ""])
        for section, reason in page.target.section_exclusions.items():
            lines.extend([f"单页审核排除：{section}；依据：{reason}。原始快照保留。", ""])
        if not page.related_pages_checked:
            lines.extend(["关联页面：未核对候选目录，不能判断各系列齐全。", ""])
        for related in page.related_pages:
            state = {"not_in_batch": "未列入本批，尚未采集正文", "prepared": "已提取，待审核",
                     "imported": "已入待审库", "failed": "下载或处理失败"}.get(related.status, related.status)
            lines.append(f"- 关联候选 [{related.title}]({related.source_url})：{state}；"
                         f"{'当前页有跳转链接' if related.linked_from_page else '仅目录关联'}；关系语义待审。")
        if page.historical_rows_excluded:
            lines.extend([f"跳过明确历史区中的 {page.historical_rows_excluded} 条技能行，原始快照保留；主正文不代表已核定游戏规则适用期。", ""])
        if page.error:
            lines.extend([f"失败原因：{page.error}", ""])
        if page.status not in {"prepared", "imported"} or page.processed is None:
            continue
        payload = json.loads(Path(page.processed).read_text(encoding="utf-8"))
        lines.extend([f"来源：{payload['source']['source_locator']}", ""])
        for record in payload["records"]:
            data = record["data"]
            lines.extend([f"### {data['character']} / {data['mode']}", "", f"包：{data['package_name'] or '未知'}；"
                          f"势力：{data['faction'] or '未知'}；体力原始表示：{data['hp_raw'] or '未知'}", ""])
            for skill in data["skills"]:
                lines.extend([f"**{skill['skill_name']}**", "", skill["skill_text"], ""])
    if report.import_error:
        lines.extend(["", f"本批导入失败（未提交）：{report.import_error}"])
    (root / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_batch(
    targets: list[BatchTarget], root: Path, games: list[Game], *, database: Path | None = None,
    fetcher: Callable[[BatchTarget], bytes] | None = None,
    candidates: list[CatalogCandidate] | None = None,
    cache_report: Path | None = None,
) -> BatchReport:
    if not targets or len(targets) > 15 or len({target.key for target in targets}) != len(targets):
        raise ValueError("本批清单须为 1–15 个不重复页面")
    if len({(target.branch, target.title) for target in targets}) != len(targets):
        raise ValueError("清单存在同服重复标题")
    known_games = {game.game_id for game in games}
    if not games or len(known_games) != len(games) or any(BRANCHES[item.branch][1] not in known_games for item in targets):
        raise ValueError("游戏登记缺失或重复")
    report_path = root / "report.json"
    if report_path.exists():
        previous = BatchReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        if [page.target for page in previous.pages] != targets:
            raise ValueError("输出目录已有其他清单，不能覆盖")
        report = BatchReport(pages=previous.pages)
    else:
        cached = (BatchReport.model_validate_json(cache_report.read_text(encoding="utf-8"))
                  if cache_report is not None else None)
        pages = []
        for item in targets:
            previous_page = next((page for page in cached.pages if page.target.key == item.key
                                  and page.target.title == item.title), None) if cached else None
            pages.append(PageResult(target=item, snapshot=previous_page.snapshot,
                sha256=previous_page.sha256, collected_at=previous_page.collected_at,
                download_error=previous_page.download_error or
                    (previous_page.error if previous_page.error and previous_page.error.startswith("HTTPError:") else None))
                if previous_page else PageResult(target=item))
        report = BatchReport(pages=pages)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "processed").mkdir(parents=True, exist_ok=True)
    network_paused = False
    for index, page in enumerate(report.pages, 1):
        if page.error and page.error.startswith("HTTPError:") and page.download_error is None:
            page.download_error = page.error
        page.status, page.error = "queued", None
        page.historical_rows_excluded = 0
        page.sections, page.other_sections = [], []
        page.unmapped_skill_sections, page.excluded_skill_sections, page.related_pages = [], [], []
        page.skill_supplements = []
        page.related_pages_checked = False
        page.availability_status, page.availability_basis = "待确认", "尚未核对上线状态"
        if candidates is not None:
            known = next((item for item in candidates if item.branch == page.target.branch
                          and item.page_id == page.target.page_id and item.title == page.target.title), None)
            if known is not None:
                page.availability_status, page.availability_basis = known.availability_status, known.availability_basis
        page.processed, page.character_count, page.skill_count = None, 0, 0
        try:
            cached_path = Path(page.snapshot) if page.snapshot else root / "raw" / f"{page.target.key}.json"
            if network_paused and not cached_path.exists():
                raise ValueError("站点拒绝或限流后，本批已暂停剩余联网请求；未取得快照")
            _prepare_page(page, root, fetcher, candidates)
        except HTTPError as error:
            page.status, page.error = "failed", f"HTTPError: {error}"
            page.download_error = page.error
            if error.code in {403, 429, 567}:
                network_paused = True
        except (OSError, ValueError) as error:
            page.status, page.error = "failed", f"{type(error).__name__}: {error}"
            if page.download_error:
                page.error += f"；上次下载失败：{page.download_error}"
        _write_report(report_path, report)
        print(f"[{index}/{len(targets)}] {page.target.branch}/{page.target.title}: {page.status}", flush=True)
    ready = [page for page in report.pages if page.status == "prepared"]
    if database is not None and ready:
        try:
            report.import_report = import_character_files(database, [Path(page.processed) for page in ready], games)
            for page in ready:
                page.status = "imported"
        except (OSError, ValueError, sqlite3.Error) as error:
            report.import_error = f"{type(error).__name__}: {error}"
    failed = sum(page.status == "failed" for page in report.pages)
    statuses = {(page.target.branch, page.target.page_id): page.status for page in report.pages}
    for page in report.pages:
        for related in page.related_pages:
            related.status = statuses.get((related.branch, related.page_id), "not_in_batch")
    gaps = any(page.unmapped_skill_sections or any(item.status in {"not_in_batch", "failed"}
               for item in page.related_pages) for page in report.pages)
    report.status = ("failed" if report.import_error or not ready else ("partial" if failed or gaps else "completed"))
    _write_report(report_path, report)
    _write_review(root, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--fetch", action="store_true", help="允许按清单访问公开 API")
    parser.add_argument("--import", action="store_true", dest="do_import", help="将成功结果整批导入待审库")
    parser.add_argument("--catalog-dir", type=Path, help="离线核对同服目录关联与跳转链接；不自动抓取关联页")
    parser.add_argument("--cache-report", type=Path, help="新批次复用旧报告登记的原始快照，仍校验指纹")
    workspace = Path(__file__).resolve().parents[2]
    parser.add_argument("--database", type=Path, default=workspace / "data/local/knowledge.local/gamewise.sqlite3")
    parser.add_argument("--games-config", type=Path, default=Path(__file__).resolve().parents[1] / "catalog.json")
    args = parser.parse_args()
    targets = TypeAdapter(list[BatchTarget]).validate_json(args.targets.read_text(encoding="utf-8-sig"))
    games = TypeAdapter(list[Game]).validate_json(args.games_config.read_text(encoding="utf-8-sig"))

    def paced_fetch(target: BatchTarget) -> bytes:
        time.sleep(1)
        return fetch_page(target)

    report = run_batch(targets, args.output_dir, games, database=args.database if args.do_import else None,
                       fetcher=paced_fetch if args.fetch else None,
                       candidates=load_candidates(args.catalog_dir) if args.catalog_dir else None,
                       cache_report=args.cache_report)
    print(json.dumps({"status": report.status, "requested": len(report.pages),
                      "imported_pages": sum(page.status == "imported" for page in report.pages),
                      "failed_pages": sum(page.status == "failed" for page in report.pages),
                      "import_report": report.import_report.model_dump() if report.import_report else None},
                     ensure_ascii=False, indent=2))
    if report.status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
