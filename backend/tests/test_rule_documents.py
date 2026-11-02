"""规则整理的正文边界、表格与列表完整性、来源指纹及重复输出验证。"""

import hashlib
import json

import pytest

from app.schemas.rule import RuleRecord
from app.services.data_processor import DataProcessor
from app.services.rule_documents import prepare_rule_page, process_rule_pages


def sample(tmp_path, html=None):
    html = html or '''<nav>导航广告</nav><section class="normal"><h2>伤害流程</h2>
<h3>伤害开始</h3><p>若伤害被防止，则终止结算。<strong>不能继续。</strong></p>
<ul><li>因技能造成伤害。</li><li>因连环传导造成伤害。</li></ul>
<table><tr><th>方式</th><th>数值</th></tr><tr><td>伤害</td><td>2</td></tr></table>
<script>恶意指令</script><h3>伤害之后</h3><p>注意：受到一次伤害与受到1点伤害不同。</p>
</section><footer>页脚导航</footer>'''
    path = tmp_path / "raw.html"
    path.write_text(html, encoding="utf-8")
    receipt = {"status": "downloaded", "path": str(path), "url": "https://gltjk.com/sanguosha/rules/flow/damage.html",
               "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "collected_at_utc": "2026-10-29T10:00:00+00:00"}
    return path, receipt


def test_article_keeps_rules_and_table_cells_but_excludes_navigation(tmp_path):
    path, receipt = sample(tmp_path)
    document, extractor = prepare_rule_page(path, receipt)
    result = DataProcessor(RuleRecord, exact_fields=("text",)).process(document, extractor)
    assert len(result.records) == 2
    assert all(r.data.chapter == "伤害流程" for r in result.records)
    first = result.records[0]
    assert "若伤害被防止，则终止结算。不能继续。" in first.data.text
    assert "因连环传导造成伤害。" in first.data.text
    assert "方式 | 数值" in first.data.text and "伤害 | 2" in first.data.text
    assert "导航" not in first.data.text and "恶意指令" not in first.data.text
    assert "受到一次伤害与受到1点伤害不同" in result.records[1].data.text
    assert first.evidence[0].verbatim_text == first.data.text
    assert receipt["url"] in first.evidence[0].locator
    assert result.all_blocks_accounted_for and result.review_status == "pending"
    assert result.source.version_ids == []


@pytest.mark.parametrize("html", ["<p>只有导航</p>", '<section class="normal"><h2>缺少结束</h2><p>正文</p>',
                                 '<section class="normal"><p>没有标题</p></section>',
                                 '<section class="normal"><h2>空正文</h2></section>'])
def test_invalid_article_never_becomes_success(tmp_path, html):
    path, receipt = sample(tmp_path, html)
    with pytest.raises(ValueError):
        prepare_rule_page(path, receipt)


def test_snapshot_change_is_not_silently_accepted(tmp_path):
    path, receipt = sample(tmp_path)
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="指纹"):
        prepare_rule_page(path, receipt)


def test_nested_heading_retains_character_context(tmp_path):
    path, receipt = sample(tmp_path, '<section class="normal"><h2>魏势力</h2><h3>曹操</h3>'
                           '<h4>奸雄</h4><p>旧版技能。</p><h3>司马懿</h3><h4>反馈</h4><p>另一技能。</p></section>')
    document, extractor = prepare_rule_page(path, receipt)
    result = DataProcessor(RuleRecord).process(document, extractor)
    assert [r.data.heading for r in result.records] == ["魏势力 / 曹操 / 奸雄", "魏势力 / 司马懿 / 反馈"]


def test_default_processes_every_registered_page_and_exports_readable_text(tmp_path):
    path, receipt = sample(tmp_path)
    second = {**receipt, "url": "https://gltjk.com/sanguosha/rules/flow/game.html"}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"pages": [receipt, second]}), encoding="utf-8")
    out = tmp_path / "processed"
    report = process_rule_pages(manifest, out)
    assert report["pages"] == 2 and report["records"] == 4
    assert len(report["inventory"]) == 2
    assert "若伤害被防止" in (out / "flow-damage.md").read_text(encoding="utf-8")
    assert report == json.loads((out / "report.json").read_text(encoding="utf-8"))


def test_processing_is_repeatable_and_does_not_modify_raw_or_create_database(tmp_path):
    path, receipt = sample(tmp_path)
    original = path.read_bytes()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"pages": [receipt]}), encoding="utf-8")
    out = tmp_path / "processed"
    first = process_rule_pages(manifest, out, ("flow/damage.html",))
    output = (out / "flow-damage.json").read_bytes()
    assert process_rule_pages(manifest, out, ("flow/damage.html",)) == first
    assert (out / "flow-damage.json").read_bytes() == output
    assert path.read_bytes() == original and not list(tmp_path.rglob("*.sqlite3"))
    assert first["pages"] == 1 and first["records"] == 2 and not first["answerable"]
