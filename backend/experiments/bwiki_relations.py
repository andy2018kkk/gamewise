"""离线核对目录与页面链接；只发现候选，不递归抓取或推断技能。"""

import csv
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from experiments.preview_bwiki import BRANCHES, SampleTree


class CatalogCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    branch: str
    page_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    source_group: str = ""
    availability_status: str = "待确认"
    availability_basis: str = "尚未核对上线状态"


class RelatedPage(BaseModel):
    branch: str
    page_id: int
    title: str
    source_url: str
    linked_from_page: bool
    catalog_group: str
    status: str = "not_in_batch"
    relation_status: str = "目录关联候选，语义待审"
    availability_status: str = "待确认"


def load_candidates(directory: Path) -> list[CatalogCandidate]:
    result = []
    for branch in BRANCHES:
        with (directory / f"{branch}-candidates.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["branch"] != branch:
                    raise ValueError("候选目录分区不符")
                result.append(CatalogCandidate(branch=branch, page_id=int(row["page_id"]),
                    title=row["title"], source_group=row["source_groups"].strip(),
                    availability_status=row.get("availability_status") or "待确认",
                    availability_basis=row.get("availability_basis") or "尚未核对上线状态"))
    keys = [(item.branch, item.page_id) for item in result]
    titles = [(item.branch, item.title) for item in result]
    if len(set(keys)) != len(keys) or len(set(titles)) != len(titles):
        raise ValueError("候选目录编号或同服标题重复")
    return result


def discover_related(
    tree: SampleTree, branch: str, page_id: int, title: str, candidates: list[CatalogCandidate],
) -> list[RelatedPage]:
    source = next((item for item in candidates if item.branch == branch and item.page_id == page_id
                   and item.title == title), None)
    if source is None:
        raise ValueError("当前页编号或标题不在指定候选目录中")
    # 未解析的空关联不能靠名字前缀补齐。
    if not source.source_group:
        return []
    linked_titles = set()
    for node in tree.elements:
        if node.tag != "a":
            continue
        href = node.attrs.get("href") or ""
        parts = urlsplit(href)
        if parts.scheme not in {"", "https"} or parts.netloc not in {"", "wiki.biligame.com"} or parts.query:
            continue
        prefix = f"/{branch}/"
        if parts.path.startswith(prefix):
            linked_titles.add(unquote(parts.path[len(prefix):]))
    return [RelatedPage(branch=branch, page_id=item.page_id, title=item.title,
            source_url=f"https://wiki.biligame.com/{branch}/{quote(item.title)}",
            linked_from_page=item.title in linked_titles, catalog_group=source.source_group,
            availability_status=item.availability_status)
            for item in candidates if item.branch == branch and item.page_id != page_id
            and item.source_group == source.source_group]
