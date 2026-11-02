"""离线整理攻略清单的小样本；实验工具，不是正式导入器。"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

from app.schemas.source import SourceBatch

SOURCE_URL = "https://zkjellberg.github.io/dark-souls-3-cheat-sheet/"
RAW_URL = "https://raw.githubusercontent.com/ZKjellberg/dark-souls-3-cheat-sheet/gh-pages/index.html"


@dataclass
class PreviewItem:
    item_id: str
    section: str
    source_line: int
    source_html: str
    text: str
    classes: str
    linked_urls: list[str]


class ChecklistParser(HTMLParser):
    """只识别此类清单的 h3 与 li[data-id]，遇到未知嵌套拒绝处理。"""

    def __init__(self, html: str, item_ids: list[str]) -> None:
        super().__init__(convert_charrefs=True)
        if not item_ids or len(set(item_ids)) != len(item_ids):
            raise ValueError("必须选择非空且不重复的条目编号")
        self.html = html
        self.wanted = set(item_ids)
        self.items: list[PreviewItem] = []
        self.heading_parts: list[str] | None = None
        self.section = ""
        self.current: PreviewItem | None = None
        self.parts: list[str] = []
        self.start_offset = 0
        self.suppressed: list[str] = []
        self.line_offsets = [0]
        for line in html.splitlines(keepends=True):
            self.line_offsets.append(self.line_offsets[-1] + len(line))

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.suppressed.append(tag)
        if self.suppressed:
            return
        if self.current is not None:
            if tag in {"li", "table", "ul", "ol"}:
                raise ValueError("样本含嵌套列表或表格，需单独设计处理方式")
            if tag == "a" and (href := attributes.get("href")):
                url = urljoin(SOURCE_URL, href)
                if urlparse(url).scheme in {"http", "https"}:
                    self.current.linked_urls.append(url)
            if tag == "br":
                self.parts.append(" ")
        if tag == "h3":
            self.heading_parts = []
        if tag == "li" and attributes.get("data-id") in self.wanted:
            item_id = attributes["data-id"]
            if any(item.item_id == item_id for item in self.items):
                raise ValueError("网页内的样本编号重复")
            if not self.section:
                raise ValueError("样本缺少章节标题")
            self.current = PreviewItem(
                item_id=item_id, section=self.section, source_line=self.getpos()[0],
                source_html="", text="", classes=attributes.get("class") or "",
                linked_urls=[],
            )
            self.start_offset = self.source_offset()
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
            return
        if tag == "h3" and self.heading_parts is not None:
            self.section = " ".join("".join(self.heading_parts).split())
            self.heading_parts = None
        if tag == "li" and self.current is not None:
            self.current.text = " ".join("".join(self.parts).split())
            if not self.current.text:
                raise ValueError("样本正文为空")
            closing_end = self.html.find(">", self.source_offset()) + 1
            self.current.source_html = self.html[self.start_offset:closing_end]
            self.items.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.suppressed:
            return
        if self.heading_parts is not None:
            self.heading_parts.append(data)
        if self.current is not None:
            self.parts.append(data)


def extract_items(html: str, item_ids: list[str]) -> list[PreviewItem]:
    parser = ChecklistParser(html, item_ids)
    parser.feed(html)
    parser.close()
    if parser.current is not None:
        raise ValueError("样本条目未闭合")
    missing = set(item_ids) - {item.item_id for item in parser.items}
    if missing:
        raise ValueError("未找到样本条目：" + ", ".join(sorted(missing)))
    return parser.items


def write_preview(input_path: Path, output_dir: Path, item_ids: list[str]) -> None:
    """读取本地快照，不联网、不翻译、不补写游戏事实。"""
    raw = input_path.read_bytes()
    items = extract_items(raw.decode("utf-8-sig"), item_ids)
    now = datetime.now(timezone.utc)
    markdown_path = output_dir / "preview.md"
    batch = SourceBatch.model_validate({"sources": [{
        "source_id": "ds3-cheatsheet-preview",
        "title": "黑魂 3 攻略清单整理实验（待审）",
        "game_id": "dark-souls-3",
        "version_ids": [],
        "topic": "流程清单提取实验",
        "content_path": str(markdown_path.resolve()),
        "source_type": "web",
        "source_locator": SOURCE_URL,
        "collected_at": datetime.fromtimestamp(input_path.stat().st_mtime, timezone.utc).date(),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "usage_note": "本地小样本审核；仓库标注 MIT，第三方来源及正式使用范围仍待确认；不分发、不发送 LLM。",
        "review_status": "pending",
        "review_note": "平台、补丁、DLC 适用性及事实尚未审核，不参与问答。",
    }]})
    markdown = [
        "# 真实攻略整理预览（待审）", "",
        "仅供核对提取是否忠实；不是正式知识库。英文正文未翻译，避免引入改写。",
        "平台、补丁、DLC 适用范围未知；网页里的其他链接未读取，不作为额外证据。",
        f"来源：{SOURCE_URL}", f"快照下载地址：{RAW_URL}",
        f"本地原文件：{input_path.resolve()}", "",
    ]
    for item in items:
        markdown.extend([
            f"## {item.section} / {item.item_id}", "",
            f"原文件第 {item.source_line} 行起；原始类别：`{item.classes}`。", "",
            "原始 HTML：", "", "```html", item.source_html, "```", "",
            "提取正文（保持原文）：", "", item.text, "",
        ])
    report = {
        "experiment_status": "pending_review",
        "processed_at": now.isoformat(),
        "snapshot_path": str(input_path.resolve()),
        "raw_url": RAW_URL,
        "source_batch": batch.model_dump(mode="json"),
        "items": [asdict(item) for item in items],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    (output_dir / "preview.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"已提取 {len(items)} 条；状态 pending；预览：{markdown_path.resolve()}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("input", type=Path, help="已保存的 HTML 文件")
    cli.add_argument("output", type=Path, help="本地审核输出目录")
    cli.add_argument("item_ids", nargs="+", help="明确选择的 data-id")
    args = cli.parse_args()
    write_preview(args.input, args.output, args.item_ids)
