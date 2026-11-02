"""从两服已有孙坚快照提取武将属性预览；不联网、不自动审核。"""

import argparse
import hashlib
import json
from pathlib import Path
import re

from app.schemas.character import CharacterRecord, CharacterSkill
from app.services.data_processor import DataProcessor, EvidenceBlock, RuleExtractor, SourceDocument
from experiments.preview_bwiki import BRANCHES, DEFAULT_SECTIONS, SAMPLE_BRANCHES, Element, SampleTree, is_skill_label, extract_skills
from experiments.process_bwiki import prepare_sample


def excluded_skill_section(section: str) -> bool:
    """用户当前明确排除的规则章节；其他未知章节仍列待核对。"""
    return "自走棋" in section or section == "统率版本"


def resolve_section_characters(branch: str, subject: str, sections: list[str],
                               explicit: dict[str, str]) -> dict[str, str]:
    """十周年在本体页面内收录界限突破；按明确章节含义命名，保留经典模式。"""
    result = dict(explicit)
    if branch == "sgs" and not subject.startswith("界"):
        for section in sections:
            if section in {"界限突破", "界限突破版本"}:
                result.setdefault(section, f"界{subject}")
    return result


def inspect_skill_sections(path: Path, branch: str) -> list[str]:
    """列出含技能标签的章节，供覆盖核对；不把章节名直接当模式。"""
    payload = json.loads(path.read_bytes())
    tree = SampleTree(payload["parse"]["text"]["*"])
    tree.feed(tree.html)
    tree.close()
    if len(tree.stack) != 1:
        raise ValueError("HTML 未闭合")
    section, found = "", []
    for node in tree.elements:
        if node.tag == "h2":
            section = node.text().strip()
        if is_skill_label(node, branch) and section and section not in found:
            found.append(section)
    return found


def _label_value(nodes: list[Element], label: str) -> tuple[str | None, Element | None]:
    labels = [node for node in nodes if "gold-title-color" in (node.attrs.get("class") or "").split()
              and node.text().strip() == label]
    if len(labels) > 1:
        values = [_label_value([node], label) for node in labels]
        if len({value for value, _ in values}) != 1:
            raise ValueError(f"同一范围出现多个{label}，拒绝猜测")
        return values[0]
    if not labels:
        return None, None
    node = labels[0]
    if node.parent is None:
        raise ValueError("属性标签缺少相邻内容")
    siblings = node.parent.children
    before = siblings[:siblings.index(node)]
    if any((child.text() if isinstance(child, Element) else child).strip() for child in before):
        raise ValueError("属性行结构变化，标签不在内容之前")
    value = " ".join("".join(child.text() if isinstance(child, Element) else child
                             for child in siblings[siblings.index(node) + 1:]).split())
    return value or None, node.parent


def _hp_value(nodes: list[Element]) -> tuple[str | None, Element | None]:
    candidates = []
    for node in nodes:
        style = re.sub(r"\s+", "", node.attrs.get("style") or "").lower()
        if node.tag != "span" or re.search(r"(?:^|;)display:none(?:;|$)", style) is None or node.parent is None:
            continue
        parent = node.parent
        icons = [item for item in nodes if parent.start < item.start < parent.end and item.tag == "img"
                 and any(word in (item.attrs.get("alt") or "") for word in ("勾玉", "阴阳鱼"))]
        if icons:
            raw = " ".join(node.text().split())
            if not raw:
                raise ValueError("体力图标的原始表示为空")
            candidates.append((raw, parent))
    # 图标的 @ 占位符与同一容器中的数值是两种显示编码，并非两个体力。
    # 只在明确“×数值”与相邻隐藏数值一致时合并，真正冲突仍报错。
    filtered = []
    for raw, parent in candidates:
        if raw == "@":
            display = re.fullmatch(r"@\s*×\s*(\d+(?:\.\d+)?)", parent.text().strip())
            matches = [(value, container) for value, container in candidates
                       if display and value == display.group(1) and container is parent.parent]
            if len(matches) != 1:
                raise ValueError("体力图标占位符缺少一致的数值表示")
            continue
        filtered.append((raw, parent))
    candidates = filtered
    if len({raw for raw, _ in candidates}) > 1:
        raise ValueError("同一模式有多个体力表示，拒绝猜测")
    return candidates[0] if candidates else (None, None)


def _obtained_skills(payload: dict, branch: str, subject: str,
                     base_skills: list[CharacterSkill], sections: list[str]) -> list[tuple[CharacterSkill, EvidenceBlock]]:
    """只补原技能明确提及且效果唯一的新技能；现有技能的状态变化不覆盖。"""
    if not sections:
        return []
    def name_key(name: str) -> str:
        return name.lstrip("☆★ \u00a0").strip()

    known = {name_key(skill.skill_name) for skill in base_skills}
    base_text = "\n".join(skill.skill_text for skill in base_skills)
    candidates: dict[str, list[dict]] = {}
    for item in extract_skills(payload, branch, sections, subject, allow_condition_variants=True):
        name = name_key(item["skill"])
        if name and name not in known and name in base_text:
            candidates.setdefault(name, []).append(item)
    result = []
    for name, items in candidates.items():
        # 同名条件技能若效果不同，不能挑第一份或丢掉条件来冒充完整资料。
        if len({" ".join(item["text"].split()) for item in items}) != 1:
            continue
        item = items[0]
        block = EvidenceBlock(
            block_id=f"supplement:{item['section']}:{item['source_line']}:{name}",
            text=f"武将：{subject}\n来源章节：{item['section']}\n原始技能名：{item['skill']}\n技能：{name}\n技能原文：{item['text']}",
            verbatim_text=item["text"],
            locator=f"{item['source_url']}；展开 HTML 第 {item['source_line']} 行；同文出处：" +
                    "、".join(f"{entry['section']}第{entry['source_line']}行" for entry in items),
        )
        result.append((CharacterSkill(skill_name=name, skill_text=item["text"]), block))
    return result


def prepare_character_sample(
    path: Path, branch: str, subject: str, *, sections: list[str] | None = None,
    section_characters: dict[str, str] | None = None,
) -> tuple[SourceDocument, RuleExtractor]:
    if branch not in BRANCHES:
        raise ValueError("不支持此分区")
    selected_sections = list(DEFAULT_SECTIONS[branch]) if sections is None else sections
    identities = resolve_section_characters(branch, subject, selected_sections, section_characters or {})
    if set(identities) - set(selected_sections) or any(not name.strip() for name in identities.values()):
        raise ValueError("章节身份配置必须属于本次选择且武将名非空")
    for section in selected_sections:
        if excluded_skill_section(section):
            raise ValueError(f"{section}暂不收录，保留覆盖提示")
    mode_sections = {"经典", "经典版本", "国战", "国战版本", "1V1", "1V1版本"}
    if any(section not in mode_sections and section not in identities for section in selected_sections):
        raise ValueError("非模式章节必须显式配置具体武将身份，不能猜测系列")
    # 先沿用已测试的标题、修订号、技能正文及跨服链接校验。
    original, _ = prepare_sample(path, branch, subject, sections=sections)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != original.source.content_hash:
        raise ValueError("读取期间快照发生变化，需要重新处理")
    payload = json.loads(raw)
    tree = SampleTree(payload["parse"]["text"]["*"])
    tree.feed(tree.html)
    tree.close()
    if len(tree.stack) != 1:
        raise ValueError("HTML 未闭合")
    modes = list(DEFAULT_SECTIONS[branch]) if sections is None else sections
    headings = [node for node in tree.elements if node.tag == "h2"]
    selected = [node for node in headings if node.text().strip() in modes]
    if len(selected) != len(modes):
        raise ValueError("模式章节缺失或重复")
    global_nodes = [node for node in tree.elements if node.start < min(heading.start for heading in selected)]
    global_faction = _label_value(global_nodes, "势力")
    blocks = {block.block_id: block for block in original.blocks}
    supplement_sections = ([s for s in inspect_skill_sections(path, branch)
                            if s in {"使命成功", "使命失败", "巅峰"}] if branch == "msgs" else [])
    rows = []
    seen_identities = set()

    def attribute_block(label: str, value: str | None, node: Element | None) -> str | None:
        if value is None or node is None:
            return None
        block_id = f"attribute:{label}:{node.start}"
        blocks[block_id] = EvidenceBlock(
            block_id=block_id, text=f"{label}：{value}",
            locator=f"{original.source.source_locator}；快照 {path.name} 展开 HTML 第 {node.line} 行",
        )
        return block_id

    for section in modes:
        character = identities.get(section, subject)
        # 国战、1V1 单列，其余已确认具体身份的系列属于经典。
        if section in {"国战", "国战版本"}:
            mode = "国战" if branch == "msgs" else "国战版本"
        elif section in {"1V1", "1V1版本"}:
            mode = "1V1" if branch == "msgs" else "1V1版本"
        else:
            mode = DEFAULT_SECTIONS[branch][0]
        if (character, mode) in seen_identities:
            raise ValueError("多个章节映射到同一武将模式，需核对具体身份")
        seen_identities.add((character, mode))
        heading = next(node for node in selected if node.text().strip() == section)
        end = next((node.start for node in headings if node.start > heading.start), len(tree.html))
        nodes = [node for node in tree.elements if heading.start <= node.start < end]
        faction = _label_value(nodes, "势力")
        if faction[1] is None:
            faction = global_faction
        packages = [(label, *_label_value(nodes, label)) for label in ("武将包", "国战包")]
        packages = [item for item in packages if item[1] is not None]
        if len(packages) > 1:
            raise ValueError("同一模式同时出现多个包分类，需要核查")
        package_kind, package_name, package_node = packages[0] if packages else (None, None, None)
        hp_raw, hp_node = _hp_value(nodes)
        skill_blocks = [block for block in original.blocks if block.block_id.startswith(f"{section}:")]
        # 技能文本已由旧适配器保留为完整 verbatim_text，不重新摘要或补齐。
        skills = [CharacterSkill(
            skill_name=block.block_id.removeprefix(f"{section}:"), skill_text=block.verbatim_text,
        ) for block in skill_blocks]
        if section == "经典" and character == subject:
            for skill, block in _obtained_skills(payload, branch, subject, skills, supplement_sections):
                skills.append(skill)
                skill_blocks.append(block)
                blocks[block.block_id] = block
        data = CharacterRecord(
            character=character, mode=mode, package_kind=package_kind, package_name=package_name,
            faction=faction[0], hp_raw=hp_raw, skills=skills,
        )
        context_id = f"context:{section}"
        blocks[context_id] = EvidenceBlock(
            block_id=context_id, text=(f"武将：{character}\n模式：{mode}" if section == mode and character == subject
                                     else f"页面：{subject}\n来源章节：{section}\n按已确认映射整理：\n武将：{character}\n模式：{mode}"),
            locator=f"{original.source.source_locator}；快照 {path.name} 展开 HTML 第 {heading.line} 行",
        )
        ids = [context_id, *(block.block_id for block in skill_blocks)]
        for label, value, node in ((package_kind or "包", package_name, package_node),
                                   ("势力", *faction), ("体力原始表示", hp_raw, hp_node)):
            block_id = attribute_block(label, value, node)
            if block_id is not None:
                ids.append(block_id)
        rows.append({"data": data.model_dump(), "block_ids": ids})

    document = SourceDocument(source=original.source.model_copy(update={"topic": "武将属性提取实验"}),
                              blocks=tuple(blocks.values()))
    return document, RuleExtractor(lambda evidence: rows)


def run_preview(
    input_dir: Path, output_dir: Path, subject: str = "孙坚", *, branch: str | None = None,
    sections: list[str] | None = None, snapshot_name: str | None = None,
    section_characters: dict[str, str] | None = None,
) -> None:
    if branch is None and (sections is not None or snapshot_name is not None or section_characters is not None):
        raise ValueError("指定章节或快照时必须明确分区")
    branches = (branch,) if branch is not None else SAMPLE_BRANCHES
    if any(value not in BRANCHES for value in branches):
        raise ValueError("不支持此分区")
    processor = DataProcessor(CharacterRecord, verbatim_fields=("character", "mode"))
    rendered = []
    review = [f"# {subject}：武将属性待审预览", "",
              "仅读取指定本地快照；规则适用期、资料使用及事实尚待审核。",
              "只处理本次配置的分区、武将和章节，不代表全部模式或系列。",
              "体力只保留页面原始表示；技能标签未核查。", ""]
    count = 0
    input_paths = []
    for branch in branches:
        input_path = input_dir / (snapshot_name or f"{branch}-rendered.json")
        input_paths.append(input_path.resolve())
        document, extractor = prepare_character_sample(input_path, branch, subject, sections=sections,
                                                       section_characters=section_characters)
        result = processor.process(document, extractor)
        rendered.append((output_dir / f"{branch}-characters.json", processor.render(result)))
        processed_sections = DEFAULT_SECTIONS[branch] if sections is None else sections
        omitted = [section for section in inspect_skill_sections(input_path, branch) if section not in processed_sections]
        review.extend([f"未处理的含技能章节：{'、'.join(omitted) or '无'}。自走棋、统率版本按当前范围暂不收录，其他遗漏须核对。", ""])
        for record in result.records:
            data = record.data
            review.extend([
                f"## {BRANCHES[branch][0]} / {data.character} / {data.mode}", "",
                f"- {data.package_kind or '包类型未知'}：{data.package_name or '未知'}",
                f"- 势力：{data.faction or '未知'}",
                f"- 体力原始表示：{data.hp_raw or '未知'}",
            ])
            for skill in data.skills:
                review.extend([f"- 技能：{skill.skill_name}（标签未核查）", "", skill.skill_text, ""])
            review.extend([f"[来源页面]({result.source.source_locator})", "",
                           *[f"- 依据：{block.locator}（{block.block_id}）" for block in record.evidence], ""])
        count += len(result.records)
    rendered.append((output_dir / "review.md", "\n".join(review)))
    # 两份均提取并校验后再导出；不宣称文件写入具备数据库事务。
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, text in rendered:
        if path.resolve() in input_paths:
            raise ValueError("输出不能覆盖原始快照")
        path.write_text(text, encoding="utf-8")
    print(f"Prepared {count} character-mode records; pending review; {output_dir.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--subject", default="孙坚")
    parser.add_argument("--branch", choices=tuple(BRANCHES))
    parser.add_argument("--section", action="append", dest="sections")
    parser.add_argument("--snapshot", dest="snapshot_name")
    parser.add_argument("--section-character", action="append", default=[], metavar="章节=武将",
                        help="经审核的章节身份映射，例如 界限突破版本=界刘备")
    args = parser.parse_args()
    identities = {}
    for pair in args.section_character:
        section, separator, character = pair.partition("=")
        if not separator or not section.strip() or not character.strip() or section in identities:
            parser.error("章节身份须为非空、不重复的 章节=武将")
        identities[section] = character
    run_preview(args.input_dir, args.output_dir, args.subject, branch=args.branch,
                sections=args.sections, snapshot_name=args.snapshot_name, section_characters=identities)
