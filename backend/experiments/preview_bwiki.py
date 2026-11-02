"""BWIKI 已展开正文的离线小样本实验，不是通用爬虫或正式导入器。"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import quote

from app.schemas.source import SourceBatch

BRANCHES = {"sgs": ("十周年", "sanguosha-shizhounian"), "sgsol": ("OL", "sanguosha-ol"),
            "msgs": ("移动版", "sanguosha-mobile")}
SAMPLE_BRANCHES = ("sgs", "sgsol")
DEFAULT_SECTIONS = {"sgs": ["经典版本", "国战版本"], "sgsol": ["经典版本", "国战版本", "1V1版本"],
                    "msgs": ["经典", "国战", "1V1"]}
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None]
    start: int
    line: int
    end: int = 0
    children: list["Element | str"] = field(default_factory=list)
    parent: "Element | None" = field(default=None, repr=False)

    def text(self) -> str:
        if self.tag in {"script", "style"}:
            return ""
        if self.tag == "br":
            return " "
        return "".join(child.text() if isinstance(child, Element) else child for child in self.children)


class SampleTree(HTMLParser):
    """为定位保留字符范围；仅处理本次 API 返回的完整 HTML 片段。"""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.html = html
        self.root = Element("root", {}, 0, 1)
        self.stack = [self.root]
        self.elements: list[Element] = []
        self.offsets = [0]
        for line in html.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def position(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, dict(attrs), self.position(), self.getpos()[0], parent=self.stack[-1])
        self.stack[-1].children.append(node)
        self.elements.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)
        else:
            node.end = self.html.find(">", node.start) + 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            raise ValueError("HTML 标签未配对，需要人工核对")
        self.stack.pop().end = self.html.find(">", self.position()) + 1

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _mobile_skill_area(node: Element) -> bool:
    """共享排版的技能与台词标签相同，须核对所在面板的直接标题。"""
    ancestor = node.parent
    while ancestor is not None:
        headers = [child for child in ancestor.children if isinstance(child, Element)
                   and "middle-grey-side-transparen-bg" in (child.attrs.get("class") or "").split()]
        if headers:
            if len(headers) != 1 or headers[0].text().strip() != "技能":
                return False
            panel = ancestor.parent
            while panel is not None:
                if "character-lines-and-skills-section" in (panel.attrs.get("class") or "").split():
                    return True
                panel = panel.parent
            return False
        ancestor = ancestor.parent
    return False


def is_skill_label(node: Element, branch: str) -> bool:
    """章节发现和正文提取共用；三服均可能使用旧标签或新式技能面板。"""
    classes = (node.attrs.get("class") or "").split()
    if branch in {"sgs", "sgsol"} and "技能标签" in classes:
        return True
    return (branch in BRANCHES and "basic-info-row-label" in classes
            and _mobile_skill_area(node))


def extract_skills(payload: dict, branch: str, sections: list[str], subject: str, *,
                   allow_condition_variants: bool = False) -> list[dict]:
    if branch not in BRANCHES or not sections or len(set(sections)) != len(sections):
        raise ValueError("必须选择支持的分支与非空、不重复的章节")
    if "error" in payload:
        raise ValueError("API 返回错误，不能作为无技能或无资料处理")
    parsed = payload.get("parse", {})
    html = parsed.get("text", {}).get("*")
    revision = parsed.get("revid")
    if parsed.get("title") != subject or not isinstance(html, str):
        raise ValueError("页面标题不符或缺少展开的 HTML；wikitext 模板入口不能代替正文")
    if type(revision) is not int or revision <= 0:
        raise ValueError("缺少有效页面修订编号")
    tree = SampleTree(html)
    tree.feed(html)
    tree.close()
    if len(tree.stack) != 1:
        raise ValueError("HTML 未闭合")
    section = ""
    result = []
    seen = set()
    historical_counts = dict.fromkeys(sections, 0)
    for node in tree.elements:
        if node.tag == "h2":
            section = node.text().strip()
        if not is_skill_label(node, branch) or section not in sections:
            continue
        ancestor = node.parent
        historical = False
        while ancestor is not None:
            if "histroy-skill" in (ancestor.attrs.get("class") or "").split():
                historical = True
                break
            ancestor = ancestor.parent
        if historical:
            historical_counts[section] += 1
            continue
        siblings = [child for child in node.parent.children if isinstance(child, Element)]
        if len(siblings) != 2 or siblings[0] is not node:
            raise ValueError("技能行结构变化，拒绝猜测正文")
        body = siblings[1]
        name = node.text().strip()
        if not name:
            # 图片技能名没有文字节点，使用同一技能链接的明确 title。
            titles = {element.attrs.get("title") for element in tree.elements
                      if element.tag == "a" and node.start < element.start < node.end
                      and (element.attrs.get("href") or "").startswith(f"/{branch}/")}
            if len(titles) == 1:
                name = next(iter(titles)) or ""
        text = body.text().strip()
        condition_variant = allow_condition_variants and section in {"使命成功", "使命失败", "巅峰"}
        if not name or not text or ((section, name) in seen and not condition_variant):
            raise ValueError("技能为空或同一章节有重复技能，需要核对历史文本")
        links = [element.attrs.get("href") or "" for element in tree.elements
                 if element.tag == "a" and node.start < element.start < node.end]
        if not links or any(not link.startswith(f"/{branch}/") for link in links):
            raise ValueError("技能链接与指定分支不符，拒绝跨服混用")
        seen.add((section, name))
        result.append({
            "branch": branch, "branch_label": BRANCHES[branch][0],
            "subject": subject, "section": section, "skill": name, "text": text,
            "revision_id": revision, "source_line": node.parent.line,
            "source_html": html[node.parent.start:node.parent.end],
            "source_url": f"https://wiki.biligame.com/{branch}/{quote(subject)}#{quote(section)}",
        })
    if set(sections) - {item["section"] for item in result}:
        raise ValueError("指定章节没有提取到技能，拒绝静默遗漏")
    for item in result:
        item["historical_rows_excluded"] = historical_counts[item["section"]]
    return result


def write_preview(input_dir: Path, subject: str) -> None:
    """只读两份本地 API 快照，输出待审对照；不比较事实真伪。"""
    items, sources, snapshots = [], [], []
    markdown_path = input_dir / "preview.md"
    for branch in SAMPLE_BRANCHES:
        path = input_dir / f"{branch}-rendered.json"
        raw = path.read_bytes()
        sections = ["经典版本", "国战版本"] + (["1V1版本"] if branch == "sgsol" else [])
        extracted = extract_skills(json.loads(raw), branch, sections, subject)
        items.extend(extracted)
        sources.append({
            "source_id": f"bwiki-{branch}-preview", "title": f"{BRANCHES[branch][0]}：{subject}（待审实验）",
            "game_id": BRANCHES[branch][1], "version_ids": [], "topic": "武将技能提取实验",
            "content_path": str(markdown_path.resolve()), "source_type": "web",
            "source_locator": f"https://wiki.biligame.com/{branch}/{quote(subject)}",
            "collected_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date(),
            "content_hash": hashlib.sha256(raw).hexdigest(), "review_status": "pending",
            "usage_note": f"本地审核；站点默认 CC BY-NC-SA 4.0，正式准入待确认。{LICENSE_URL}",
            "review_note": "模式不是补丁版本；平台、更新适用期与事实尚待审核。",
        })
        snapshots.append({"branch": branch, "path": str(path.resolve()),
                          "revision_id": extracted[0]["revision_id"],
                          "api_url": f"https://wiki.biligame.com/{branch}/api.php?action=parse&page={quote(subject)}&prop=text%7Crevid&format=json"})
    batch = SourceBatch.model_validate({"sources": sources})
    report = {"status": "pending_review", "subject": subject, "snapshots": snapshots,
              "source_batch": batch.model_dump(mode="json"), "items": items}
    markdown = [
        f"# {subject}：十周年与 OL 技能提取预览", "",
        "状态：待审实验，不参与正式问答。以下是页面原文提取，不是模型生成的规则解释。",
        "页面内的经典、国战、1V1 标签按原样保留，不当作补丁版本；不推断同服所有模式通用。",
        "两服经典版文本在本次快照中相同，仍分别保留出处，不能据此共享整个规则库。",
        "页面修订号不是游戏版本，也不能单独冻结其引用的模板；实际依据是本地展开正文及 SHA-256。", "",
        f"署名：BWIKI 三国杀及三国杀 OL 社区贡献者；站点默认许可：[CC BY-NC-SA 4.0]({LICENSE_URL})。",
        "处理方式：去掉显示标签、按分支和章节整理，未改写技能原文。具体来源和历史记录见每条链接。", "",
    ]
    for item in items:
        markdown.extend([
            f"## {item['branch_label']} / {item['section']} / {item['skill']}", "",
            item["text"], "",
            f"[原页面]({item['source_url']})；页面修订号 {item['revision_id']}；展开 HTML 第 {item['source_line']} 行起。",
            f"[贡献历史](https://wiki.biligame.com/{item['branch']}/index.php?title={quote(subject)}&action=history)", "",
            "原始技能行（用于对照提取是否遗漏）：", "", "```html", item["source_html"], "```", "",
        ])
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    (input_dir / "preview.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Extracted {len(items)} skill records; pending review; {markdown_path.resolve()}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("input_dir", type=Path)
    cli.add_argument("subject", help="应与两个页面的标题一致")
    args = cli.parse_args()
    write_preview(args.input_dir, args.subject)
