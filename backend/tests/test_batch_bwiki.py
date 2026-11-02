"""小批抓取、续跑和待审入库；注入合成响应，不访问网络。"""

import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import Game
from experiments.batch_bwiki import BatchTarget, fetch_page, run_batch
from experiments.bwiki_relations import CatalogCandidate


def games():
    return [Game(game_id="sanguosha-shizhounian", name="十周年", category="sanguosha"),
            Game(game_id="sanguosha-ol", name="OL", category="sanguosha")]


@pytest.mark.parametrize("blocked_attempts", [1, 4])
def test_report_save_handles_temporary_lock_without_losing_previous_report(tmp_path, monkeypatch, blocked_attempts):
    from experiments.batch_bwiki import BatchReport, _write_report

    path = tmp_path / "report.json"
    path.write_text("previous report", encoding="utf-8")
    original_replace = Path.replace
    attempts = []

    def replace(source, destination):
        attempts.append(destination)
        if len(attempts) <= blocked_attempts:
            raise PermissionError("file in use")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("experiments.batch_bwiki.time.sleep", lambda _: None)
    if blocked_attempts == 4:
        with pytest.raises(PermissionError):
            _write_report(path, BatchReport(pages=[]))
        assert path.read_text(encoding="utf-8") == "previous report"
    else:
        _write_report(path, BatchReport(pages=[]))
        assert BatchReport.model_validate_json(path.read_text(encoding="utf-8")).pages == []
        assert len(attempts) == 2


def target(page_id=1, branch="sgsol"):
    return BatchTarget(branch=branch, page_id=page_id, title=f"合成武将{page_id}")


def response(item):
    html = f'''<div><p class="gold-title-color">势力</p>吴</div><h2>经典版本</h2>
<div><div class="gold-title-color">武将包</div><div>合成包</div></div>
<div class="技能标签行"><div class="技能标签"><a href="/{item.branch}/skill">合成技能</a></div>
<div>准备阶段，摸2张牌。</div></div><h2>统率版本</h2><p>本次未支持的模式</p>'''
    return json.dumps({"parse": {"pageid": item.page_id, "title": item.title, "revid": 123,
                                 "text": {"*": html}}}, ensure_ascii=False).encode("utf-8")


def test_mission_supplement_preserves_condition_without_creating_another_character(tmp_path):
    item = target(branch="msgs")
    def fetch(_):
        payload = json.loads(response(item))
        html = payload["parse"]["text"]["*"]
        # 使用真实移动版面板边界，同时保留一条同名而效果不同的条件技能。
        def panel(text):
            return ('<div class="character-lines-and-skills-section"><div>'
                    '<div class="middle-grey-side-transparen-bg">技能</div><div><div>'
                    '<div class="basic-info-row-label"><a href="/msgs/skill">合成技能</a></div>'
                    f'<div>{text}</div></div></div></div></div>')
        payload["parse"]["text"]["*"] = ('<h2>经典</h2>' + panel('准备阶段，摸2张牌。') + '<h2>使命成功</h2>'
                                        + panel('成功1后改为摸3张牌。') + panel('成功2后改为摸4张牌。'))
        return json.dumps(payload, ensure_ascii=False).encode('utf-8')

    report = run_batch([item], tmp_path / "batch",
        [Game(game_id="sanguosha-mobile", name="移动版", category="sanguosha")], fetcher=fetch)
    page, = report.pages
    assert page.character_count == 1 and page.skill_count == 1
    supplement, second = page.skill_supplements
    assert supplement.character == item.title and supplement.mode == "经典"
    assert supplement.condition == "使命成功" and supplement.skill_text == "成功1后改为摸3张牌。"
    assert second.skill_name == supplement.skill_name and second.skill_text == "成功2后改为摸4张牌。"
    assert not supplement.repeats_base
    assert page.unmapped_skill_sections == ["使命成功"]  # 主技能查询尚未纳入补充，不能抹掉缺口。


def test_missing_source_skill_data_is_reported_without_inventing_skills(tmp_path):
    item = target()
    payload = {"parse": {"pageid": item.page_id, "title": item.title, "revid": 123,
                         "text": {"*": '<h2>经典版本</h2><div>无武将合成武将的经典技能数据</div>'}}}
    report = run_batch([item], tmp_path / "batch", games(),
                       fetcher=lambda _: json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    page, = report.pages
    assert page.status == "failed" and page.skill_count == 0
    assert "源页面明确缺少技能数据" in page.error


def test_successful_batch_imports_and_reports_unhandled_sections(tmp_path):
    root, database = tmp_path / "batch", tmp_path / "db.sqlite3"
    report = run_batch([target(), target(2, "sgs")], root, games(), database=database, fetcher=response)
    assert report.status == "completed"
    assert [page.status for page in report.pages] == ["imported", "imported"]
    assert all(page.sections == ["经典版本"] and page.other_sections == ["统率版本"] for page in report.pages)
    assert report.import_report.totals["sanguosha_skills"] == 2
    assert (root / "report.json").exists() and (root / "review.md").exists()
    assert all(page.sha256 and page.collected_at for page in report.pages)
    with KnowledgeRepository(database, readonly=True) as repository:
        result = repository.search_records(character="合成武将1")
        assert result[1].records[0].review_status == "pending" and not result[1].records[0].answerable


def test_failed_page_is_recorded_successful_page_really_imports(tmp_path):
    def fetcher(item):
        return response(item) if item.page_id == 1 else b'{"error":{"code":"missingtitle"}}'

    report = run_batch([target(), target(2)], tmp_path / "batch", games(),
                       database=tmp_path / "db.sqlite3", fetcher=fetcher)
    assert report.status == "partial"
    assert report.pages[0].status == "imported" and report.pages[1].status == "failed"
    assert "API" in report.pages[1].error
    assert report.import_report.totals["sources"] == 1


def test_resume_reuses_unchanged_snapshot_without_network_or_duplicates(tmp_path):
    root, database = tmp_path / "batch", tmp_path / "db.sqlite3"
    first = run_batch([target()], root, games(), database=database, fetcher=response)
    snapshot = root / "raw" / "sgsol-1.json"
    original = snapshot.read_bytes()

    def no_network(item):
        pytest.fail("续跑不应重新请求已保存快照")

    second = run_batch([target()], root, games(), database=database, fetcher=no_network)
    assert second.status == "completed"
    assert all(value == 0 for value in second.import_report.added.values())
    assert second.import_report.totals == first.import_report.totals
    assert snapshot.read_bytes() == original


def test_modified_cache_is_reported_not_overwritten(tmp_path):
    root = tmp_path / "batch"
    run_batch([target()], root, games(), fetcher=response)
    snapshot = root / "raw" / "sgsol-1.json"
    snapshot.write_bytes(b"changed")
    report = run_batch([target()], root, games(), fetcher=lambda item: pytest.fail("不应覆盖被改动快照"))
    assert report.status == "failed" and "指纹" in report.pages[0].error
    assert snapshot.read_bytes() == b"changed"


@pytest.mark.parametrize("mutation", ["wrong_id", "wrong_title", "no_supported_section"])
def test_wrong_page_or_unknown_mode_never_imported(tmp_path, mutation):
    def fetcher(item):
        payload = json.loads(response(item))
        if mutation == "wrong_id":
            payload["parse"]["pageid"] = 999
        elif mutation == "wrong_title":
            payload["parse"]["title"] = "其他武将"
        else:
            payload["parse"]["text"]["*"] = "<h2>未知模式</h2>"
        return json.dumps(payload).encode("utf-8")

    database = tmp_path / "db.sqlite3"
    report = run_batch([target()], tmp_path / "batch", games(), database=database, fetcher=fetcher)
    assert report.status == "failed" and report.pages[0].error
    assert not database.exists()


def test_manifest_cannot_be_reused_for_different_targets(tmp_path):
    run_batch([target()], tmp_path / "batch", games(), fetcher=response)
    with pytest.raises(ValueError, match="清单"):
        run_batch([target(2)], tmp_path / "batch", games(), fetcher=response)


def test_duplicate_targets_and_offline_behavior(tmp_path):
    with pytest.raises(ValueError):
        run_batch([target(), target()], tmp_path / "duplicate", games(), fetcher=response)
    report = run_batch([target()], tmp_path / "offline", games())
    assert report.status == "failed" and "快照" in report.pages[0].error


def test_import_failure_does_not_claim_pages_were_imported(tmp_path, monkeypatch):
    def fail_import(*args, **kwargs):
        raise ValueError("合成导入冲突")

    monkeypatch.setattr("experiments.batch_bwiki.import_character_files", fail_import)
    report = run_batch([target()], tmp_path / "batch", games(), database=tmp_path / "db.sqlite3", fetcher=response)
    assert report.status == "failed" and "冲突" in report.import_error
    assert report.pages[0].status == "prepared" and report.import_report is None


def test_offline_resume_keeps_previous_network_failure_reason(tmp_path):
    def rejected(item):
        raise HTTPError(item.api_url, 567, "synthetic rejection", None, None)

    first = run_batch([target()], tmp_path / "batch", games(), fetcher=rejected)
    assert "567" in first.pages[0].error
    second = run_batch([target()], tmp_path / "batch", games())
    assert "567" in second.pages[0].download_error
    assert "567" in second.pages[0].error and second.status == "failed"


def test_network_timeout_has_finite_retries(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("experiments.batch_bwiki.urlopen", timeout)
    monkeypatch.setattr("experiments.batch_bwiki.time.sleep", lambda seconds: None)
    with pytest.raises(TimeoutError):
        fetch_page(target())
    assert calls == [25, 25]


@pytest.mark.parametrize("code", [403, 429, 567])
def test_site_rejection_stops_remaining_network_requests(tmp_path, monkeypatch, code):
    calls = []

    def blocked(*args, **kwargs):
        calls.append(code)
        raise HTTPError("https://wiki.biligame.com/sgsol/api.php", code, "synthetic rejection", None, None)

    monkeypatch.setattr("experiments.batch_bwiki.urlopen", blocked)
    report = run_batch([target(), target(2)], tmp_path / "batch", games(), fetcher=fetch_page)
    assert calls == [code]
    assert report.pages[0].status == "failed" and "HTTPError" in report.pages[0].error
    assert report.pages[1].status == "failed" and "暂停" in report.pages[1].error


def series_response(item):
    payload = json.loads(response(item))
    payload["parse"]["text"]["*"] += (f'<h2>界限突破版本</h2><div><div class="技能标签">'
        f'<a href="/{item.branch}/skill">合成技能</a></div><div>界武将独有正文。</div></div>'
        f'<h2>自走棋</h2><div><div class="技能标签"><a href="/{item.branch}/skill">合成技能</a></div>'
        '<div>自走棋正文不入库。</div></div>')
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_batch_series_mapping_and_repeat_import(tmp_path):
    item = target().model_copy(update={"section_characters": {"界限突破版本": "界合成武将1"}})
    root, database = tmp_path / "batch", tmp_path / "db.sqlite3"
    first = run_batch([item], root, games(), database=database, fetcher=series_response)
    assert first.status == "completed" and first.pages[0].character_count == 2
    assert first.pages[0].excluded_skill_sections == ["自走棋"]
    assert first.pages[0].unmapped_skill_sections == []
    with KnowledgeRepository(database, readonly=True) as repository:
        rows = repository.search_records(character="界合成武将1")[1].records
        assert len(rows) == 1 and rows[0].mode == "经典版本" and rows[0].skill_text == "界武将独有正文。"
    second = run_batch([item], root, games(), database=database)
    assert second.import_report.totals == first.import_report.totals
    assert all(value == 0 for value in second.import_report.added.values())


def test_unmapped_skill_section_is_a_coverage_gap(tmp_path):
    report = run_batch([target()], tmp_path / "batch", games(), fetcher=series_response)
    assert report.status == "partial"
    assert report.pages[0].unmapped_skill_sections == ["界限突破版本"]
    assert report.pages[0].excluded_skill_sections == ["自走棋"]
    assert report.pages[0].character_count == 1


def test_commander_skill_section_is_explicitly_excluded(tmp_path):
    def fetcher(item):
        return series_response(item).decode('utf-8').replace('界限突破版本', '统率版本').encode('utf-8')
    report = run_batch([target()], tmp_path / "batch", games(), fetcher=fetcher)
    assert report.status == "completed"
    assert report.pages[0].excluded_skill_sections == ["统率版本", "自走棋"]
    assert report.pages[0].unmapped_skill_sections == []
    assert report.pages[0].character_count == 1


def test_reviewed_supplement_exclusion_is_page_specific_and_keeps_raw(tmp_path):
    def fetcher(item):
        return series_response(item).decode('utf-8').replace('界限突破版本', '巅峰').encode('utf-8')
    reviewed = target().model_copy(update={"section_exclusions": {"巅峰": "用户确认是技能补充说明，本批不收录"}})
    report = run_batch([reviewed, target(2)], tmp_path / "batch", games(), fetcher=fetcher)
    first, second = report.pages
    assert first.unmapped_skill_sections == [] and "巅峰" in first.excluded_skill_sections
    assert second.unmapped_skill_sections == ["巅峰"] and "巅峰" not in second.excluded_skill_sections
    assert report.status == "partial" and first.character_count == second.character_count == 1
    assert "巅峰" in (tmp_path / "batch/raw/sgsol-1.json").read_text(encoding="utf-8")
    assert "用户确认是技能补充说明" in (tmp_path / "batch/review.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("availability", ["已上线", "未上线"])
def test_confirmed_availability_is_kept_separate_from_content_review(tmp_path, availability):
    candidates = [CatalogCandidate(branch="sgsol", page_id=1, title="合成武将1", source_group="人物甲",
                                  availability_status=availability, availability_basis="用户已实地核对")]
    report = run_batch([target()], tmp_path / "batch", games(), fetcher=response, candidates=candidates)
    page = report.pages[0]
    payload = json.loads(Path(page.processed).read_text(encoding="utf-8"))
    assert page.availability_status == availability
    assert availability in payload["source"]["review_note"]
    assert "用户已实地核对" in payload["source"]["review_note"]
    assert payload["source"]["review_status"] == "pending" and not report.answerable


def test_failed_download_does_not_erase_confirmed_availability(tmp_path):
    def rejected(item):
        raise HTTPError(item.api_url, 567, "synthetic rejection", None, None)
    candidates = [CatalogCandidate(branch="sgsol", page_id=1, title="合成武将1", source_group="人物甲",
                                  availability_status="未上线", availability_basis="用户确认敬请期待"),
                  CatalogCandidate(branch="sgsol", page_id=2, title="合成武将2", source_group="人物甲",
                                   availability_status="已上线", availability_basis="用户确认已上线")]
    report = run_batch([target(), target(2)], tmp_path / "batch", games(), fetcher=rejected, candidates=candidates)
    assert report.pages[0].status == "failed"
    assert report.pages[0].availability_status == "未上线"
    assert "用户确认敬请期待" in report.pages[0].availability_basis
    assert report.pages[1].status == "failed" and report.pages[1].availability_status == "已上线"


@pytest.mark.parametrize("exclusions,mappings", [({"不存在的章节": "已审核"}, {}),
                                                ({"界限突破版本": "已审核"}, {"界限突破版本": "界合成武将1"})])
def test_invalid_review_exclusion_cannot_silently_drop_content(tmp_path, exclusions, mappings):
    item = target().model_copy(update={"section_exclusions": exclusions, "section_characters": mappings})
    report = run_batch([item], tmp_path / "batch", games(), fetcher=series_response)
    assert report.status == "failed" and report.pages[0].error


def test_linked_series_are_reported_but_not_automatically_fetched(tmp_path):
    calls = []
    def fetcher(item):
        calls.append(item.page_id)
        payload = json.loads(response(item))
        payload["parse"]["text"]["*"] += '<a href="/sgsol/神合成武将1">神合成武将1</a>'
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    candidates = [CatalogCandidate(branch="sgsol", page_id=1, title="合成武将1", source_group="人物甲"),
                  CatalogCandidate(branch="sgsol", page_id=2, title="神合成武将1", source_group="人物甲")]
    report = run_batch([target()], tmp_path / "batch", games(), fetcher=fetcher, candidates=candidates)
    assert calls == [1] and report.status == "partial"
    related, = report.pages[0].related_pages
    assert related.linked_from_page and related.status == "not_in_batch"
    assert "未列入本批，尚未采集正文" in (tmp_path / "batch/review.md").read_text(encoding="utf-8")


def test_related_page_is_processed_as_its_own_explicit_target(tmp_path):
    candidates = [CatalogCandidate(branch="sgsol", page_id=i, title=f"合成武将{i}", source_group="人物甲")
                  for i in (1, 2)]
    report = run_batch([target(), target(2)], tmp_path / "batch", games(),
                       database=tmp_path / "db.sqlite3", fetcher=response, candidates=candidates)
    assert report.status == "completed"
    assert all(page.related_pages[0].status == "imported" for page in report.pages)
    assert report.import_report.totals["sources"] == 2
    assert report.import_report.totals["sanguosha_characters"] == 2


def test_changed_identity_configuration_cannot_silently_resume(tmp_path):
    root = tmp_path / "batch"
    run_batch([target()], root, games(), fetcher=series_response)
    changed = target().model_copy(update={"section_characters": {"界限突破版本": "界合成武将1"}})
    with pytest.raises(ValueError, match="清单"):
        run_batch([changed], root, games())


def test_new_configuration_reuses_registered_snapshot_and_only_adds_new_series(tmp_path):
    root, database = tmp_path / "old", tmp_path / "db.sqlite3"
    old = run_batch([target()], root, games(), database=database, fetcher=series_response)
    old_snapshot = (root / "raw/sgsol-1.json").read_bytes()
    item = target().model_copy(update={"section_characters": {"界限突破版本": "界合成武将1"}})
    new = run_batch([item], tmp_path / "new", games(), database=database, cache_report=root / "report.json")
    assert new.import_report.added == {"games": 0, "sources": 0, "source_snapshots": 0,
                                       "sanguosha_characters": 1, "sanguosha_skills": 1}
    assert new.pages[0].snapshot == old.pages[0].snapshot
    assert (root / "raw/sgsol-1.json").read_bytes() == old_snapshot
    again = run_batch([item], tmp_path / "new", games(), database=database)
    assert all(value == 0 for value in again.import_report.added.values())
