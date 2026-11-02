"""复用已获取的两服快照验证通用处理类；不下载或正式入库。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from app.schemas.source import NonBlankText, SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, RuleExtractor, SourceDocument
from experiments.preview_bwiki import BRANCHES, DEFAULT_SECTIONS, SAMPLE_BRANCHES, extract_skills


class SkillRecord(BaseModel):
    """调用方定义的示例格式，不是写死在处理核心中的游戏结构。"""

    model_config = ConfigDict(extra="forbid")
    character: NonBlankText
    skill_name: NonBlankText
    mode: NonBlankText
    skill_text: NonBlankText


def prepare_sample(
    path: Path, branch: str, subject: str, *, sections: list[str] | None = None,
) -> tuple[SourceDocument, RuleExtractor]:
    if branch not in BRANCHES:
        raise ValueError("该样本适配器不支持此分区")
    raw = path.read_bytes()
    sections = list(DEFAULT_SECTIONS[branch]) if sections is None else sections
    # 复用已测试的页面规则确定证据边界，不重写第二套 HTML 解析器。
    rows = extract_skills(json.loads(raw), branch, sections, subject)
    blocks = tuple(EvidenceBlock(
        block_id=f"{row['section']}:{row['skill']}",
        text=f"武将：{subject}\n模式：{row['section']}\n技能：{row['skill']}\n技能原文：{row['text']}",
        verbatim_text=row["text"],
        locator=f"{row['source_url']}；快照 {path.name} 展开 HTML 第 {row['source_line']} 行",
    ) for row in rows)
    source = SourceRecord(
        source_id=f"bwiki-{branch}-{subject}", title=f"{BRANCHES[branch][0]}：{subject}",
        game_id=BRANCHES[branch][1], version_ids=[], topic="武将技能处理类验证",
        content_path=str(path.resolve()), source_type="web",
        source_locator=f"https://wiki.biligame.com/{branch}/{quote(subject)}",
        collected_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date(),
        content_hash=hashlib.sha256(raw).hexdigest(),
        usage_note="沿用已获准的本地样本审核；不向外部模型发送，正式使用范围仍待确认。",
        review_status="pending", review_note="未核定正式规则适用期；处理成功不等于资料审核通过。",
    )

    def convert(evidence: tuple[EvidenceBlock, ...]) -> list[dict[str, object]]:
        return [{"data": {
            "character": subject, "skill_name": row["skill"],
            "mode": row["section"], "skill_text": row["text"],
        }, "block_ids": [f"{row['section']}:{row['skill']}"]} for row in rows]

    return SourceDocument(source=source, blocks=blocks), RuleExtractor(convert)


def run_samples(input_dir: Path, output_dir: Path, subject: str = "孙坚") -> None:
    processor = DataProcessor(
        SkillRecord, verbatim_fields=("character", "skill_name", "mode"), exact_fields=("skill_text",),
    )
    rendered: list[tuple[Path, str]] = []
    count = 0
    for branch in SAMPLE_BRANCHES:
        document, extractor = prepare_sample(input_dir / f"{branch}-rendered.json", branch, subject)
        result = processor.process(document, extractor)
        count += len(result.records)
        rendered.extend([
            (output_dir / f"{branch}-processed.json", processor.render(result, "json")),
            (output_dir / f"{branch}-processed.md", processor.render(result, "markdown")),
        ])
    # 两份都完成校验后才开始导出；文件 I/O 故障不承诺数据库式事务。
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in rendered:
        path.write_text(content, encoding="utf-8")
    print(f"Processed {count} records from {len(SAMPLE_BRANCHES)} snapshots; pending; {output_dir.resolve()}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("input_dir", type=Path)
    cli.add_argument("output_dir", type=Path)
    cli.add_argument("--subject", default="孙坚")
    args = cli.parse_args()
    run_samples(args.input_dir, args.output_dir, args.subject)
