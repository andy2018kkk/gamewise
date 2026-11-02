"""离线整理已登记的 GitBook 规则页面，复用统一处理类，不联网或写数据库。"""

from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from app.schemas.rule import RuleRecord
from app.schemas.source import SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, RuleExtractor, SourceDocument

RULESET_ID = "sanguosha-rules-3.0"


class RuleArticleParser(HTMLParser):
    """只读取 normal 正文区，按标题分段，保留列表与表格的文本顺序。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.articles = 0
        self.suppressed = 0
        self.heading_parts: list[str] | None = None
        self.heading = ""
        self.title = ""
        self.heading_level = 0
        self.heading_path: list[tuple[int, str]] = []
        self.line = 0
        self.parts: list[str] = []
        self.records: list[tuple[str, str, int]] = []

    def flush(self) -> None:
        text = "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip())
        if text:
            if not self.heading:
                raise ValueError("正文缺少章节标题")
            self.records.append((self.heading, text, self.line))
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "section":
            if self.depth:
                self.depth += 1
            elif "normal" in (attributes.get("class") or "").split():
                self.depth = 1
                self.articles += 1
        if not self.depth:
            return
        if tag in {"script", "style"}:
            self.suppressed += 1
        if self.suppressed:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.heading_parts = []
            self.heading_level = int(tag[1])
            self.line = self.getpos()[0]
        elif tag in {"p", "li", "tr", "br", "blockquote"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag == "img" and attributes.get("alt"):
            self.parts.append(attributes["alt"] or "")

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
            return
        if self.suppressed:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_parts is not None:
            title = " ".join("".join(self.heading_parts).split())
            if not self.title:
                self.title = title
            while self.heading_path and self.heading_path[-1][0] >= self.heading_level:
                self.heading_path.pop()
            self.heading_path.append((self.heading_level, title))
            self.heading = " / ".join(value for _, value in self.heading_path)
            self.heading_parts = None
        elif tag in {"p", "li", "tr", "blockquote"}:
            self.parts.append("\n")
        elif tag == "section":
            self.depth -= 1
            if not self.depth:
                self.flush()

    def handle_data(self, data: str) -> None:
        if self.depth and not self.suppressed:
            # HTML 排版缩进不是正文换行；段落、列表及表格行由标签确定边界。
            data = re.sub(r"\s+", " ", data)
            if self.heading_parts is not None:
                self.heading_parts.append(data)
            else:
                self.parts.append(data)


def prepare_rule_page(path: Path, receipt: dict) -> tuple[SourceDocument, RuleExtractor]:
    raw = path.read_bytes()
    if receipt["status"] != "downloaded" or hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
        raise ValueError("规则快照指纹不一致或尚未下载")
    parser = RuleArticleParser()
    parser.feed(raw.decode("utf-8"))
    parser.close()
    if parser.articles != 1 or parser.depth or parser.heading_parts is not None or not parser.records:
        raise ValueError("规则正文结构不完整或不是单一 GitBook 正文区")
    chapter = parser.title
    blocks, rows = [], []
    for index, (heading, text, line) in enumerate(parser.records, 1):
        block_id = f"section-{index}"
        blocks.append(EvidenceBlock(block_id=block_id, text=text, verbatim_text=text,
                                    locator=f"{receipt['url']}；HTML第{line}行；{heading}"))
        rows.append({"data": {"ruleset_id": RULESET_ID, "chapter": chapter,
                              "heading": heading, "text": text}, "block_ids": [block_id]})
    relative = urlsplit(receipt["url"]).path.removeprefix("/sanguosha/rules/")
    category = ("历史武将说明" if relative.startswith("card/hero") else
                "更新历史与附录" if relative.startswith("appendix/") else "基础规则参考")
    source = SourceRecord(
        source_id="gltjk-" + urlsplit(receipt["url"]).path.removeprefix("/sanguosha/rules/").replace("/", "-"),
        title=chapter, game_id="sanguosha", version_ids=[], topic=category,
        content_path=str(path.resolve()), source_type="web", source_locator=receipt["url"],
        collected_at=datetime.fromisoformat(receipt["collected_at_utc"]).date(), content_hash=receipt["sha256"],
        usage_note="本地已下载规则素材整理；使用与分发范围尚未确认。",
        review_note="规则集3.0历史资料；包含旧武将示例，不自动映射到当前三服规则。",
    )
    return SourceDocument(source=source, blocks=tuple(blocks)), RuleExtractor(lambda evidence: rows)


def process_rule_pages(manifest_path: Path, output_dir: Path,
                       relative_paths: tuple[str, ...] | None = None) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = "https://gltjk.com/sanguosha/rules/"
    if relative_paths is None:
        if any(not p["url"].startswith(base) for p in manifest["pages"]):
            raise ValueError("规则清单包含未知来源")
        relative_paths = tuple(p["url"].removeprefix(base) for p in manifest["pages"])
    if not relative_paths or len(set(relative_paths)) != len(relative_paths):
        raise ValueError("规则页面选择为空或重复")
    processor = DataProcessor(RuleRecord, exact_fields=("text",))
    prepared = []
    for relative in relative_paths:
        matches = [p for p in manifest["pages"] if p["url"] == f"https://gltjk.com/sanguosha/rules/{relative}"]
        if len(matches) != 1:
            raise ValueError(f"规则页面登记缺失或重复：{relative}")
        receipt = matches[0]
        document, extractor = prepare_rule_page(Path(receipt["path"]), receipt)
        result = processor.process(document, extractor)
        prepared.append((relative, result))
    output_dir.mkdir(parents=True, exist_ok=True)
    review = ["# 全量规则正文（待审）", "", "规则集3.0历史资料，不自动表示当前三服通用。", ""]
    inventory = []
    count = 0
    for relative, result in prepared:
        filename = (relative.replace("/", "-").removesuffix(".html") or "home") + ".json"
        (output_dir / filename).write_text(processor.render(result), encoding="utf-8")
        markdown = [f"# {result.source.title}", "", f"来源：{result.source.source_locator}", "",
                    "规则集3.0历史资料，未自动映射到当前三服。", ""]
        review.extend([f"## {result.source.title}", "", f"来源：{result.source.source_locator}", ""])
        for record in result.records:
            section = [f"### {record.data.heading}", "", record.data.text, "",
                       f"定位：{record.evidence[0].locator}", ""]
            review.extend(section)
            markdown.extend(section)
        md_name = Path(filename).with_suffix(".md").name
        (output_dir / md_name).write_text("\n".join(markdown), encoding="utf-8")
        inventory.append({"source_url": result.source.source_locator, "title": result.source.title,
                          "category": result.source.topic,
                          "json": filename, "markdown": md_name, "records": len(result.records),
                          "raw_sha256": result.source.content_hash})
        count += len(result.records)
    (output_dir / "review.md").write_text("\n".join(review), encoding="utf-8")
    report = {"pages": len(prepared), "records": count, "review_status": "pending",
              "answerable": False, "output_dir": str(output_dir.resolve()), "inventory": inventory}
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
