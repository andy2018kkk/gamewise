"""用合成 HTML 验证武将属性按页面及模式提取。"""

import hashlib
import json
from urllib.parse import unquote

import pytest

from experiments.preview_characters import prepare_character_sample, run_preview
from app.schemas.character import CharacterRecord
from app.services.data_processor import DataProcessor


@pytest.mark.parametrize("section,character", [("界限突破版本", "界合成武将"), ("谋系列", "谋合成武将"),
                                              ("闪系列", "闪合成武将")])
def test_confirmed_series_maps_to_classic_without_merging_identity(tmp_path, section, character):
    path = tmp_path / "page.json"
    payload = save_character(path)
    payload["parse"]["text"]["*"] += (
        f'<h2>{section}</h2><div><div class="技能标签"><a href="/sgsol/skill">合成技能</a></div>'
        '<div>此系列独有的技能正文。</div></div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    document, extractor = prepare_character_sample(path, "sgsol", "合成武将",
        sections=["经典版本", section, "国战版本", "1V1版本"], section_characters={section: character})
    result = DataProcessor(CharacterRecord, verbatim_fields=("character", "mode")).process(document, extractor)
    assert [(r.data.character, r.data.mode) for r in result.records] == [
        ("合成武将", "经典版本"), (character, "经典版本"), ("合成武将", "国战版本"), ("合成武将", "1V1版本")]
    assert result.records[1].data.skills[0].skill_text == "此系列独有的技能正文。"
    assert "其他角色" in result.records[0].data.skills[0].skill_text
    assert any(section in unquote(b.locator) for b in result.records[1].evidence if b.verbatim_text)


@pytest.mark.parametrize("section", ["界限突破", "界限突破版本"])
def test_shizhounian_explicit_limit_break_section_resolves_without_per_character_config(tmp_path, section):
    path = tmp_path / "page.json"
    payload = save_character(path, branch="sgs")
    payload["parse"]["text"]["*"] += (
        f'<h2>{section}</h2><div><div class="技能标签"><a href="/sgs/skill">合成技能</a></div>'
        '<div>界限突破后的正文。</div></div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    document, extractor = prepare_character_sample(path, "sgs", "合成武将",
        sections=["经典版本", section, "国战版本"])
    result = DataProcessor(CharacterRecord, verbatim_fields=("character", "mode")).process(document, extractor)
    assert [(r.data.character, r.data.mode) for r in result.records] == [
        ("合成武将", "经典版本"), ("界合成武将", "经典版本"), ("合成武将", "国战版本")]
    assert result.records[1].data.skills[0].skill_text == "界限突破后的正文。"
    assert result.records[0].data.skills[0].skill_text != result.records[1].data.skills[0].skill_text


def test_identical_attributes_from_two_portraits_do_not_create_two_characters(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    html = payload["parse"]["text"]["*"]
    extra = ('<div><div class="gold-title-color">武将包</div><div>合成包</div></div>'
             '<div><img alt="勾玉" /><span style="display:none">4</span></div>')
    payload["parse"]["text"]["*"] = html.replace('<h2>经典版本</h2>', '<h2>经典版本</h2>' + extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    document, extractor = prepare_character_sample(path, "sgsol", "合成武将", sections=["经典版本"])
    result = DataProcessor(CharacterRecord, verbatim_fields=("character", "mode")).process(document, extractor)
    assert len(result.records) == 1
    assert result.records[0].data.hp_raw == "4"
    assert result.records[0].data.package_name == "合成包"


def test_two_sections_cannot_silently_become_one_character_mode(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    payload["parse"]["text"]["*"] += ('<h2>另一系列</h2><div><div class="技能标签">'
                                        '<a href="/sgsol/skill">合成技能</a></div><div>另一正文</div></div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="同一武将模式"):
        prepare_character_sample(path, "sgsol", "合成武将", sections=["经典版本", "另一系列"],
                                 section_characters={"另一系列": "合成武将"})


def test_unknown_series_requires_identity_and_autochess_is_excluded(tmp_path):
    path = tmp_path / "page.json"
    save_character(path)
    with pytest.raises(ValueError, match="显式配置"):
        prepare_character_sample(path, "sgsol", "合成武将", sections=["界限突破版本"])
    with pytest.raises(ValueError, match="自走棋暂不收录"):
        prepare_character_sample(path, "sgsol", "合成武将", sections=["自走棋"],
                                 section_characters={"自走棋": "合成武将"})
    with pytest.raises(ValueError, match="统率版本暂不收录"):
        prepare_character_sample(path, "sgsol", "合成武将", sections=["统率版本"],
                                 section_characters={"统率版本": "合成武将"})


def test_preview_reports_unhandled_skill_sections(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    payload["parse"]["text"]["*"] += ('<h2>待核系列</h2><div><div class="技能标签">'
                                        '<a href="/sgsol/skill">另一技能</a></div><div>未处理正文</div></div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    run_preview(tmp_path, tmp_path / "out", "合成武将", branch="sgsol", snapshot_name="page.json")
    assert "未处理的含技能章节：待核系列" in (tmp_path / "out/review.md").read_text(encoding="utf-8")


def save_character(path, branch="sgsol", *, faction="吴", package="合成包", hp="4", missing=False):
    modes = ["经典版本", "国战版本"] + (["1V1版本"] if branch == "sgsol" else [])
    chunks = [f'<div><p class="gold-title-color">势力</p>{faction}</div>']
    for mode in modes:
        raw = "@×2.5" if mode == "国战版本" else hp
        kind = "国战包" if mode == "国战版本" else "武将包"
        attributes = "" if missing else (
            f'<div><span style="display:none;"> {raw}</span><img alt="吴勾玉.png" /></div>'
            f'<div><div class="gold-title-color">{kind}</div><div><a>{package}</a></div></div>'
        )
        body = "令对手摸两张牌。" if mode == "1V1版本" else "令其他角色摸两张牌。"
        chunks.append(f'<h2>{mode}</h2>{attributes}<div>'
                      f'<div class="技能标签"><a href="/{branch}/skill">合成技能</a></div>'
                      f'<div>{body}</div></div>')
    chunks.append('<h2>自走棋</h2><div><p class="gold-title-color">势力</p>其他势力</div>'
                  '<div><p class="gold-title-color">体力</p>8</div>')
    payload = {"parse": {"title": "合成武将", "revid": 123, "text": {"*": "".join(chunks)}}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def result_for(path, branch="sgsol"):
    document, extractor = prepare_character_sample(path, branch, "合成武将")
    return DataProcessor(CharacterRecord, verbatim_fields=("character", "mode")).process(document, extractor)


def test_attributes_and_skills_stay_in_their_own_modes(tmp_path):
    path = tmp_path / "page.json"
    save_character(path)
    result = result_for(path)
    classic, national, duel = [record.data for record in result.records]
    assert len(result.records) == 3
    assert classic.package_kind == "武将包" and classic.package_name == "合成包"
    assert national.package_kind == "国战包"
    assert [classic.hp_raw, national.hp_raw, duel.hp_raw] == ["4", "@×2.5", "4"]
    assert all(record.data.faction == "吴" for record in result.records)
    assert "对手" in duel.skills[0].skill_text
    assert "其他角色" in classic.skills[0].skill_text
    assert all("initial_hp" not in record.data.model_dump() and "max_hp" not in record.data.model_dump()
               for record in result.records)
    assert all(record.data.skills[0].tags is None for record in result.records)


def test_provenance_includes_each_attribute_and_skill_location(tmp_path):
    path = tmp_path / "page.json"
    save_character(path)
    before = path.read_bytes()
    result = result_for(path)
    assert result.source.content_hash == hashlib.sha256(before).hexdigest()
    assert result.source.game_id == "sanguosha-ol" and result.source.version_ids == []
    assert result.review_status == "pending"
    assert result.all_blocks_accounted_for
    for record in result.records:
        texts = "\n".join(block.text for block in record.evidence)
        assert record.data.package_name in texts
        assert record.data.faction in texts
        assert record.data.hp_raw in texts
        assert record.data.skills[0].skill_text in texts
        assert all("HTML 第" in block.locator for block in record.evidence)
    assert path.read_bytes() == before


def test_icon_multiplier_and_hidden_number_are_one_hp_value(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    old = '<div><span style="display:none;"> 4</span><img alt="吴勾玉.png" /></div>'
    new = ('<div><span class="bold-font"><span style="display:none;">@</span>'
           '<img alt="吴勾玉.png" />×8</span><span style="display:none;"> 8</span></div>')
    payload["parse"]["text"]["*"] = payload["parse"]["text"]["*"].replace(old, new)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = result_for(path)
    assert [r.data.hp_raw for r in result.records] == ["8", "@×2.5", "8"]


@pytest.mark.parametrize("text_value", ["9", "@"])
def test_icon_multiplier_disagreeing_or_missing_number_is_not_accepted(tmp_path, text_value):
    path = tmp_path / "page.json"
    payload = save_character(path)
    old = '<div><span style="display:none;"> 4</span><img alt="吴勾玉.png" /></div>'
    new = ('<div><span><span style="display:none;">@</span><img alt="吴勾玉.png" />×8</span>'
           f'<span style="display:none;"> {text_value}</span></div>')
    payload["parse"]["text"]["*"] = payload["parse"]["text"]["*"].replace(old, new)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        result_for(path)


def test_missing_attributes_remain_unknown_without_other_mode_fallback(tmp_path):
    path = tmp_path / "page.json"
    save_character(path, missing=True)
    result = result_for(path)
    assert all(record.data.hp_raw is None and record.data.package_name is None for record in result.records)
    assert all(record.data.faction == "吴" for record in result.records)


def test_mode_specific_faction_overrides_page_value(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    payload["parse"]["text"]["*"] = payload["parse"]["text"]["*"].replace(
        "<h2>1V1版本</h2>", '<h2>1V1版本</h2><div><p class="gold-title-color">势力</p>合成另一势力</div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = result_for(path)
    assert result.records[0].data.faction == "吴"
    assert result.records[2].data.faction == "合成另一势力"


def test_explicit_empty_mode_faction_stays_unknown(tmp_path):
    path = tmp_path / "page.json"
    payload = save_character(path)
    payload["parse"]["text"]["*"] = payload["parse"]["text"]["*"].replace(
        "<h2>1V1版本</h2>", '<h2>1V1版本</h2><div><p class="gold-title-color">势力</p></div>')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert result_for(path).records[2].data.faction is None


@pytest.mark.parametrize("mutation", ["duplicate_package", "duplicate_hp", "wrong_title", "no_revision", "bad_skill"])
def test_ambiguous_or_invalid_page_fails_instead_of_guessing(tmp_path, mutation):
    path = tmp_path / "page.json"
    payload = save_character(path)
    html = payload["parse"]["text"]["*"]
    if mutation == "duplicate_package":
        html = html.replace("<h2>经典版本</h2>", '<h2>经典版本</h2><div><div class="gold-title-color">武将包</div><div>另一个包</div></div>')
    elif mutation == "duplicate_hp":
        html = html.replace("<h2>经典版本</h2>", '<h2>经典版本</h2><div><span style="display:none">5</span><img alt="吴勾玉.png" /></div>')
    elif mutation == "wrong_title":
        payload["parse"]["title"] = "其他人物"
    elif mutation == "no_revision":
        del payload["parse"]["revid"]
    else:
        html = html.replace('/sgsol/skill', '/sgs/skill')
    payload["parse"]["text"]["*"] = html
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        result_for(path)


def test_two_sources_export_five_characters_without_touching_inputs(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    for branch in ("sgs", "sgsol"):
        save_character(inputs / f"{branch}-rendered.json", branch)
    outputs = tmp_path / "output"
    run_preview(inputs, outputs, "合成武将")
    reports = [json.loads((outputs / f"{branch}-characters.json").read_text(encoding="utf-8")) for branch in ("sgs", "sgsol")]
    assert [len(report["records"]) for report in reports] == [2, 3]
    assert all(report["review_status"] == "pending" for report in reports)
    assert "@×2.5" in (outputs / "review.md").read_text(encoding="utf-8")


def test_second_page_failure_does_not_export_partial_batch(tmp_path):
    save_character(tmp_path / "sgs-rendered.json", "sgs")
    (tmp_path / "sgsol-rendered.json").write_text("{}", encoding="utf-8")
    outputs = tmp_path / "output"
    with pytest.raises(ValueError):
        run_preview(tmp_path, outputs, "合成武将")
    assert not outputs.exists()


def test_page_specific_sections_do_not_require_other_modes(tmp_path):
    path = tmp_path / "page.json"
    save_character(path, hp="4/5")
    document, extractor = prepare_character_sample(path, "sgsol", "合成武将", sections=["经典版本"])
    result = DataProcessor(CharacterRecord).process(document, extractor)
    assert len(result.records) == 1
    assert result.records[0].data.mode == "经典版本"
    assert result.records[0].data.hp_raw == "4/5"


@pytest.mark.parametrize("sections", [[], ["经典版本", "经典版本"], ["没有的模式"]])
def test_explicit_invalid_sections_are_not_replaced_by_defaults(tmp_path, sections):
    path = tmp_path / "page.json"
    save_character(path)
    with pytest.raises(ValueError):
        prepare_character_sample(path, "sgsol", "合成武将", sections=sections)


def test_single_page_entry_writes_only_selected_subject_and_branch(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    save_character(inputs / "custom.json")
    outputs = tmp_path / "output"
    run_preview(inputs, outputs, "合成武将", branch="sgsol", sections=["经典版本"], snapshot_name="custom.json")
    assert {item.name for item in outputs.iterdir()} == {"sgsol-characters.json", "review.md"}
    report = json.loads((outputs / "sgsol-characters.json").read_text(encoding="utf-8"))
    assert report["records"][0]["data"]["character"] == "合成武将"
    assert len(report["records"]) == 1


def test_mobile_attributes_and_three_modes_share_existing_character_format(tmp_path):
    path = tmp_path / "mobile.json"
    chunks = ['<div><p class="gold-title-color">势力</p>吴</div>']
    for mode, kind, package in (("经典", "武将包", "合成经典包"), ("国战", "国战包", "合成国战包"),
                                ("1V1", "武将包", "合成单挑包")):
        chunks.append(f'''<h2>{mode}</h2>
<div><div class="gold-title-color">{kind}</div><div>{package}</div></div>
<div><span style="display:none">4</span><img alt="吴勾玉.png" /></div>
<div class="character-lines-and-skills-section"><div>
<div class="middle-grey-side-transparen-bg">技能</div><div><div>
<div class="basic-info-row-label"><a href="/msgs/skill">合成技能</a></div><div>原文-{mode}</div>
</div></div></div><div><div class="middle-grey-side-transparen-bg">台词</div><div><div>
<div class="basic-info-row-label"><a href="/msgs/skill">合成技能</a></div><div>这是台词</div>
</div></div></div></div>''')
    payload = {"parse": {"title": "合成武将", "revid": 123, "text": {"*": "".join(chunks)}}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    document, extractor = prepare_character_sample(path, "msgs", "合成武将")
    result = DataProcessor(CharacterRecord).process(document, extractor)
    assert result.source.game_id == "sanguosha-mobile" and result.review_status == "pending"
    assert [record.data.mode for record in result.records] == ["经典", "国战", "1V1"]
    assert [record.data.package_name for record in result.records] == ["合成经典包", "合成国战包", "合成单挑包"]
    assert all(record.data.faction == "吴" and record.data.hp_raw == "4" for record in result.records)
    assert all(record.data.skills[0].skill_text == f"原文-{record.data.mode}" for record in result.records)


@pytest.mark.parametrize("section", ["使命成功", "使命失败", "巅峰"])
def test_obtained_skill_is_added_to_same_character_with_original_evidence(tmp_path, section):
    from app.schemas.query import Game
    from app.services.import_characters import import_character_files
    from app.repositories.knowledge import KnowledgeRepository

    def panel(name, text):
        return ('<div class="character-lines-and-skills-section"><div>'
                '<div class="middle-grey-side-transparen-bg">技能</div><div><div>'
                f'<div class="basic-info-row-label"><a href="/msgs/skill">{name}</a></div>'
                f'<div>{text}</div></div></div></div></div>')

    path = tmp_path / "raw.json"
    html = '<h2>经典</h2>' + panel('原技能', '条件满足后，你获得技能“新技能”。')
    html += '<h2>国战</h2>' + panel('原技能', '国战原文，不获得额外技能。')
    html += f'<h2>{section}</h2>' + panel('☆\u00a0新技能', '出牌阶段限一次，摸一张牌。')
    html += panel('无关技能', '未被基础技能引用，不自动添加。')
    payload = {"parse": {"title": "合成武将", "revid": 1, "text": {"*": html}}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    document, extractor = prepare_character_sample(path, 'msgs', '合成武将', sections=['经典', '国战'])
    result = DataProcessor(CharacterRecord).process(document, extractor)
    assert len(result.records) == 2
    assert [s.skill_name for s in result.records[0].data.skills] == ['原技能', '新技能']
    assert [s.skill_name for s in result.records[1].data.skills] == ['原技能']
    assert any(section in b.text and b.verbatim_text == '出牌阶段限一次，摸一张牌。'
               for b in result.records[0].evidence)
    processed = tmp_path / 'characters.json'
    processed.write_text(result.model_dump_json(), encoding='utf-8')
    database = tmp_path / 'db.sqlite3'
    games = [Game(game_id='sanguosha-mobile', name='移动版')]
    first = import_character_files(database, [processed], games)
    second = import_character_files(database, [processed], games)
    assert first.totals['sanguosha_skills'] == 3 and not any(second.added.values())
    with KnowledgeRepository(database, readonly=True) as repository:
        rows = repository.search_records(game_ids=['sanguosha-mobile'], skill_name='新技能')
        assert len(rows[0].records) == 1 and rows[0].records[0].character == '合成武将'


def test_obtained_skill_conflicting_conditions_and_existing_names_are_not_overwritten():
    from experiments.preview_characters import _obtained_skills
    from app.schemas.character import CharacterSkill

    def row(name, text):
        return f'<div><div class="技能标签"><a href="/sgs/skill">{name}</a></div><div>{text}</div></div>'

    html = '<h2>巅峰</h2>' + row('新技能', '效果甲') + row('新技能', '效果乙') + row('原技能', '不能覆盖')
    payload = {"parse": {"title": "合成武将", "revid": 1, "text": {"*": html}}}
    base = [CharacterSkill(skill_name='原技能', skill_text='获得“新技能”。')]
    assert _obtained_skills(payload, 'sgs', '合成武将', base, ['巅峰']) == []
