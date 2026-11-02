"""合成 HTML 验证分支、模式、技能正文与台词隔离；不联网。"""

from copy import deepcopy
import json

import pytest

from experiments.preview_bwiki import extract_skills, write_preview


def page(branch="sgs"):
    return {"parse": {"title": "合成武将", "revid": 123, "text": {"*": f'''
<div><h2>经典版本</h2>
<div><div class="技能标签"><a href="/{branch}/skill">合成技能</a></div><div>准备阶段，若你已受伤，选择一名其他角色，摸2张牌。</div></div>
<div><div>合成技能</div><div>这里是台词，不是规则。</div></div>
<h2>1V1版本</h2>
<div><div class="技能标签"><a href="/{branch}/skill">合成技能</a></div><div>准备阶段，若你已受伤，令对手摸2张牌。</div></div></div>'''}}}


def test_keeps_condition_quantity_section_and_exact_html():
    payload = page()
    item, = extract_skills(payload, "sgs", ["经典版本"], "合成武将")
    assert item["text"] == "准备阶段，若你已受伤，选择一名其他角色，摸2张牌。"
    assert "台词" not in item["text"]
    assert item["section"] == "经典版本"
    assert item["source_html"] in payload["parse"]["text"]["*"]
    assert item["source_line"] == 3
    assert item["revision_id"] == 123


def mobile_page():
    return {"parse": {"title": "合成武将", "revid": 123, "text": {"*": '''
<h2>经典</h2><div class="character-lines-and-skills-section">
<div><div class="middle-grey-side-transparen-bg bold-font">技能</div><div><div>
<div class="basic-info-row-label"><a href="/msgs/skill">合成技能</a></div>
<div>准备阶段，摸2张牌。<br />完整条件保留。</div></div></div></div>
<div><div class="middle-grey-side-transparen-bg bold-font">台词</div><div><div>
<div class="basic-info-row-label"><a href="/msgs/skill">合成技能</a></div>
<div>这是台词，不能当规则。</div></div></div></div></div>'''}}}


@pytest.mark.parametrize("branch", ["msgs", "sgsol", "sgs"])
def test_shared_panel_skill_and_voice_with_same_classes_are_separated(branch):
    payload = mobile_page()
    payload["parse"]["text"]["*"] = payload["parse"]["text"]["*"].replace("/msgs/", f"/{branch}/")
    item, = extract_skills(payload, branch, ["经典"], "合成武将")
    assert item["text"] == "准备阶段，摸2张牌。 完整条件保留。"
    assert item["section"] == "经典" and f"/{branch}/" in item["source_url"]
    assert "台词" not in item["text"]


def test_image_skill_label_uses_local_link_title_but_requires_unambiguous_name():
    payload = mobile_page()
    html = payload["parse"]["text"]["*"]
    html = html.replace('<a href="/msgs/skill">合成技能</a>',
                        '<a href="/msgs/skill" title="合成技能"><img alt="图标.png" /></a>')
    payload["parse"]["text"]["*"] = html
    item, = extract_skills(payload, "msgs", ["经典"], "合成武将")
    assert item["skill"] == "合成技能"
    assert item["text"] == "准备阶段，摸2张牌。 完整条件保留。"
    payload["parse"]["text"]["*"] = html.replace(' title="合成技能"', '')
    with pytest.raises(ValueError, match="技能为空"):
        extract_skills(payload, "msgs", ["经典"], "合成武将")


@pytest.mark.parametrize("branch", ["msgs", "sgsol"])
@pytest.mark.parametrize("mutation", ["wrong_link", "no_skill_header", "outside_panel"])
def test_shared_panel_unknown_layout_is_not_silently_treated_as_skills(mutation, branch):
    payload = mobile_page()
    html = payload["parse"]["text"]["*"].replace("/msgs/", f"/{branch}/")
    if mutation == "wrong_link":
        html = html.replace(f"/{branch}/skill", "/sgs/skill")
    elif mutation == "no_skill_header":
        html = html.replace(">技能</div>", ">未知标题</div>")
    else:
        html = html.replace("character-lines-and-skills-section", "unknown-panel")
    payload["parse"]["text"]["*"] = html
    with pytest.raises(ValueError):
        extract_skills(payload, branch, ["经典"], "合成武将")


@pytest.mark.parametrize("history_first", [True, False])
def test_explicit_history_panel_is_preserved_in_source_but_not_current_skills(history_first):
    current = '<div><div class="技能标签"><a href="/sgs/skill">同名技能</a></div><div>当前正文，摸2张牌。</div></div>'
    historical = '<div class="histroy-skill"><table><tr><td><div><div class="技能标签"><a href="/sgs/skill">同名技能</a></div><div>旧正文，摸1张牌。</div></div></td></tr></table></div>'
    html = '<h2>经典版本</h2>' + (historical + current if history_first else current + historical)
    payload = {"parse": {"title": "合成武将", "revid": 123, "text": {"*": html}}}
    before = deepcopy(payload)
    item, = extract_skills(payload, "sgs", ["经典版本"], "合成武将")
    assert item["text"] == "当前正文，摸2张牌。"
    assert item["historical_rows_excluded"] == 1
    assert payload == before


def test_history_without_main_body_is_not_used_as_fallback():
    html = '<h2>经典版本</h2><div class="histroy-skill"><div><div class="技能标签"><a href="/sgs/skill">技能</a></div><div>旧正文</div></div></div>'
    payload = {"parse": {"title": "合成武将", "revid": 123, "text": {"*": html}}}
    with pytest.raises(ValueError, match="没有提取到技能"):
        extract_skills(payload, "sgs", ["经典版本"], "合成武将")


def test_keeps_two_branches_and_mode_variants_separate():
    first = extract_skills(page(), "sgs", ["经典版本"], "合成武将")
    second = extract_skills(page("sgsol"), "sgsol", ["经典版本", "1V1版本"], "合成武将")
    assert first[0]["text"] == second[0]["text"]
    assert first[0]["branch"] != second[0]["branch"]
    assert first[0]["source_url"] != second[0]["source_url"]
    assert "对手" in second[1]["text"]
    assert "其他角色" in second[0]["text"]


@pytest.mark.parametrize("change", ["api_error", "wrong_title", "no_text", "bad_revision", "cross_branch", "duplicate", "broken_html", "empty_skill", "changed_row"])
def test_refuses_invalid_or_ambiguous_content(change):
    payload = deepcopy(page())
    parsed = payload["parse"]
    if change == "api_error":
        payload["error"] = {"code": "permissiondenied"}
    elif change == "wrong_title":
        parsed["title"] = "另一个武将"
    elif change == "no_text":
        parsed.pop("text")
    elif change == "bad_revision":
        parsed["revid"] = True
    else:
        html = parsed["text"]["*"]
        if change == "cross_branch":
            html = html.replace("/sgs/skill", "/sgsol/skill")
        elif change == "duplicate":
            html = html.replace("<h2>1V1版本</h2>", "")
        elif change == "broken_html":
            html += "<div>"
        elif change == "empty_skill":
            html = html.replace("准备阶段，若你已受伤，选择一名其他角色，摸2张牌。", "")
        else:
            html = html.replace('<div class="技能标签">', '<span>新增未知内容</span><div class="技能标签">')
        parsed["text"]["*"] = html
    with pytest.raises(ValueError):
        extract_skills(payload, "sgs", ["经典版本"], "合成武将")


def test_missing_mode_is_an_error_instead_of_silent_omission():
    with pytest.raises(ValueError, match="指定章节"):
        extract_skills(page(), "sgs", ["国战版本"], "合成武将")


def save_snapshots(directory):
    for branch in ("sgs", "sgsol"):
        payload = page(branch)
        html = payload["parse"]["text"]["*"]
        row = html[html.index('<div><div class="技能标签">'):html.index('<div><div>合成技能')]
        payload["parse"]["text"]["*"] = html.replace(
            "<h2>1V1版本</h2>", "<h2>国战版本</h2>" + row + "<h2>1V1版本</h2>",
        )
        (directory / f"{branch}-rendered.json").write_text(json.dumps(payload), encoding="utf-8")


def test_preview_preserves_two_pending_sources_and_five_separate_records(tmp_path):
    save_snapshots(tmp_path)
    write_preview(tmp_path, "合成武将")
    report = json.loads((tmp_path / "preview.json").read_text(encoding="utf-8"))
    assert len(report["items"]) == 5
    sources = report["source_batch"]["sources"]
    assert len({source["game_id"] for source in sources}) == 2
    assert all(source["review_status"] == "pending" and source["version_ids"] == [] for source in sources)
    assert all(len(source["content_hash"]) == 64 for source in sources)
    markdown = (tmp_path / "preview.md").read_text(encoding="utf-8")
    assert all(item["text"] in markdown and item["source_url"] in markdown for item in report["items"])


def test_failure_on_second_source_does_not_write_partial_preview(tmp_path):
    save_snapshots(tmp_path)
    (tmp_path / "sgsol-rendered.json").write_text('{"error": {"code": "timeout"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="API 返回错误"):
        write_preview(tmp_path, "合成武将")
    assert not (tmp_path / "preview.md").exists()
    assert not (tmp_path / "preview.json").exists()
