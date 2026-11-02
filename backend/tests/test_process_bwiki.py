"""验证来源特有的 BWIKI 样本能接入通用处理器；使用合成页面。"""

import json

import pytest

from app.services.data_processor import DataProcessor, ManualExtractor
from experiments.process_bwiki import SkillRecord, prepare_sample, run_samples


def save_page(path, branch, subject="合成武将"):
    sections = ["经典版本", "国战版本"] + (["1V1版本"] if branch == "sgsol" else [])
    html = "<div>" + "".join(
        f'<h2>{section}</h2><div><div class="技能标签"><a href="/{branch}/skill">合成技能</a></div>'
        '<div>准备阶段，若受伤，摸2张牌。</div></div>' for section in sections
    ) + "</div>"
    payload = {"parse": {"title": subject, "pageid": 100, "revid": 200, "text": {"*": html}}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_bwiki_rules_and_manual_use_same_processor_and_preserve_complete_skill(tmp_path):
    path = tmp_path / "sgs-rendered.json"
    save_page(path, "sgs")
    document, rule = prepare_sample(path, "sgs", "合成武将")
    processor = DataProcessor(SkillRecord, exact_fields=("skill_text",))
    ruled = processor.process(document, rule)
    rows = [{"data": item.data.model_dump(), "block_ids": [block.block_id for block in item.evidence]} for item in ruled.records]
    manual = processor.process(document, ManualExtractor(rows))
    assert manual.model_dump() == ruled.model_dump()
    assert len(ruled.records) == 2
    assert ruled.records[0].data.skill_text == "准备阶段，若受伤，摸2张牌。"
    assert ruled.source.game_id == "sanguosha-shizhounian"
    assert ruled.source.version_ids == []
    assert ruled.review_status == "pending"
    assert ruled.all_blocks_accounted_for


def test_two_sources_export_five_records_and_keep_branch_metadata(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for branch in ("sgs", "sgsol"):
        save_page(input_dir / f"{branch}-rendered.json", branch)
    output = tmp_path / "output"
    run_samples(input_dir, output, "合成武将")
    reports = [json.loads((output / f"{branch}-processed.json").read_text(encoding="utf-8")) for branch in ("sgs", "sgsol")]
    assert sum(len(report["records"]) for report in reports) == 5
    assert len({report["source"]["game_id"] for report in reports}) == 2
    assert all(report["review_status"] == "pending" for report in reports)
    assert all((output / f"{branch}-processed.md").exists() for branch in ("sgs", "sgsol"))


def test_invalid_second_source_does_not_write_partial_batch(tmp_path):
    save_page(tmp_path / "sgs-rendered.json", "sgs")
    (tmp_path / "sgsol-rendered.json").write_text('{"error":{"code":"bad"}}', encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        run_samples(tmp_path, output, "合成武将")
    assert not output.exists()


def test_adapter_refuses_unsupported_branch(tmp_path):
    path = tmp_path / "input.json"
    save_page(path, "sgs")
    with pytest.raises(ValueError, match="分区"):
        prepare_sample(path, "not-a-branch", "合成武将")
