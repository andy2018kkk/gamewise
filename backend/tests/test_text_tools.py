"""合成文件验证统一文本工具与虚拟模型，不联网、不下载或调用收费模型。"""

import asyncio
from datetime import date
import hashlib
import json

import pytest

from app.providers.llm import ScriptedModel
from app.schemas.ask import FinalAnswerTurn, ToolCallTurn
from app.schemas.source import SourceBatch, SourceRecord
from app.schemas.tools import TextEvidence, TextScopeInfo
from app.services.ask import AskService
from app.services.knowledge_tools import KnowledgeTools
from app.services.text_library import TextCollection, TextLibrary, load_text_library


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "analysis"
    root.mkdir()
    body = "# 主公\n主公生存与配合是合成分析观点。\n" + "后续完整条件与例外。" * 750
    raw, text = root / "raw.txt", root / "text.txt"
    raw.write_text(body, encoding="utf-8")
    text.write_text(body, encoding="utf-8")
    source = SourceRecord(source_id="analysis-a01-original", title="合成作者的历史观点", game_id="reference",
        topic="analysis_reference_original", content_path=str(raw), source_type="web",
        source_locator="https://example.com/analysis", collected_at=date(2026, 11, 17),
        content_hash=hashlib.sha256(raw.read_bytes()).hexdigest(), review_note="观点，非官方规则")
    (root / "sources.json").write_text(SourceBatch(sources=[source]).model_dump_json(), encoding="utf-8")
    (root / "download-report.json").write_text(json.dumps([dict(id="A01", status="user_text_archived",
        text_path=str(text), text_sha256=hashlib.sha256(text.read_bytes()).hexdigest(), coverage="合成资料，无真实覆盖")]), encoding="utf-8")
    return TextLibrary([TextCollection(TextScopeInfo(scope_id="analysis-test", record_type="analysis",
        game_id="reference", applicability="仅测试，未映射任何服"), root)])


def tools(tmp_path, library, **kwargs):
    return KnowledgeTools(tmp_path / "not-needed.sqlite3", ["a"], text_library=library, preview=True, **kwargs)


def search(session, **overrides):
    arguments = dict(record_type="analysis", scope_id="analysis-test", query="主公生存", top_k=1)
    arguments.update(overrides)
    return session.invoke("search_records", json.dumps(arguments))


def test_wrong_text_filters_get_actionable_recovery_without_echoing_input(tmp_path, library):
    session = tools(tmp_path, library)
    response = search(session, game_ids=["sensitive-input"])
    assert response.error.code == "invalid_arguments"
    assert "discover_catalog" in response.error.message and "game_ids" in response.error.message
    assert "sensitive-input" not in response.error.message
    denied = search(session, scope_id="invented")
    assert denied.error.code == "scope_denied" and "discover_catalog" in denied.error.message
    assert not denied.results
    assert search(session).results[0].records  # 改成正确参数后仍可获取证据，未放宽校验。


def test_text_results_honor_program_budget_and_report_remaining_candidates(tmp_path, library):
    session = tools(tmp_path, library, max_records=1)
    response = search(session, query="后续完整条件", top_k=5)
    assert len(response.results[0].records) == 1
    assert response.results[0].has_more
    assert response.results[0].records[0].text  # 未伪称候选全读或删改正文。


def test_catalog_search_and_context_reuse_real_file_without_sql(tmp_path, library):
    session = tools(tmp_path, library)
    catalog = session.invoke("discover_catalog", '{"record_type":"analysis"}')
    assert catalog.results[0].scope.scope_id == "analysis-test"
    assert not catalog.results[0].records
    hit = search(session).results[0].records[0]
    assert isinstance(hit, TextEvidence) and len(hit.text) <= 350
    assert hit.record_type == "analysis" and not hit.answerable
    assert "未映射任何服" in hit.limitations[0]
    response = session.invoke("read_evidence", json.dumps(dict(citation_id=hit.citation_id)))
    context = response.results[0].records[0]
    assert len(context.text) == 6000 and context.truncated
    assert context.text.startswith(hit.text) and context.citation_id != hit.citation_id
    assert session.resolve_citation(context.citation_id) == context


@pytest.mark.parametrize("changes,code", [
    ({"scope_id": "other"}, "scope_denied"),
    ({"record_type": "rule"}, "scope_denied"),
    ({"game_ids": ["b"]}, "invalid_arguments"),
    ({"query": " "}, "invalid_arguments"),
    ({"top_k": 6}, "invalid_arguments"),
    ({"path": "secret.txt"}, "invalid_arguments"),
])
def test_invalid_scope_or_arguments_do_not_escape_collection(tmp_path, library, changes, code):
    session = tools(tmp_path, library)
    response = search(session, **changes)
    assert response.error.code == code and not session._evidence


def test_formal_mode_never_reads_pending_text(tmp_path, library):
    (library.collections["analysis-test"].path / "text.txt").unlink()
    session = KnowledgeTools(tmp_path / "unused.db", ["a"], text_library=library)
    response = search(session)
    assert response.results[0].status == "pending_review"
    assert not response.results[0].records and not session._evidence


def test_cross_session_and_arbitrary_path_read_are_rejected(tmp_path, library):
    first = tools(tmp_path, library)
    hit = search(first).results[0].records[0]
    second = tools(tmp_path, library)
    assert second.invoke("read_evidence", json.dumps(dict(citation_id=hit.citation_id))).error.code == "invalid_citation"
    assert first.invoke("read_evidence", '{"path":"secret.txt"}').error.code == "invalid_arguments"


def test_output_budget_registers_no_invisible_citation(tmp_path, library):
    session = tools(tmp_path, library, max_characters=1)
    assert search(session).error.code == "output_limit"
    assert not session._evidence


def test_no_match_is_separate_from_missing_or_changed_file(tmp_path, library):
    session = tools(tmp_path, library)
    assert search(session, query="zzzzzz").results[0].status == "no_match_in_loaded_data"
    hit = search(session).results[0].records[0]
    (library.collections["analysis-test"].path / "text.txt").write_text("改写", encoding="utf-8")
    assert session.invoke("read_evidence", json.dumps(dict(citation_id=hit.citation_id))).error.code == "storage_error"
    assert search(session).error.code == "storage_error"


def test_manifest_path_cannot_read_outside_configured_directory(tmp_path, library):
    root = library.collections["analysis-test"].path
    path = root / "download-report.json"
    rows = json.loads(path.read_text())
    rows[0]["text_path"] = str(tmp_path / "outside.txt")
    (tmp_path / "outside.txt").write_text("主公", encoding="utf-8")
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert search(tools(tmp_path, library)).error.code == "storage_error"


def test_text_config_is_program_owned_and_relative_to_config_file(tmp_path, library):
    collection = library.collections["analysis-test"]
    config = tmp_path / "collections.json"
    config.write_text(json.dumps([dict(info=collection.info.model_dump(), path="analysis")]), encoding="utf-8")
    loaded = load_text_library(config)
    assert loaded.collections["analysis-test"].path == collection.path
    assert search(tools(tmp_path, loaded)).results[0].records
    data = json.loads(config.read_text())
    data[0]["unexpected"] = "not allowed"
    config.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_text_library(config)


def test_known_promotion_is_not_searchable_or_in_context_and_originals_stay_unchanged(tmp_path, library):
    root = library.collections["analysis-test"].path
    promotion = "微信搜索‘’三国桃园‘’小程序，更多精彩内容等你来看~"
    body = "原创作者：合成作者\n" + promotion + "\n# 主公\n主公要考虑生存和配合。\n玩家在微信搜索牌名只是咨询，不改变技能效果。"
    for name in ("raw.txt", "text.txt"):
        (root / name).write_text(body, encoding="utf-8")
    manifest = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    manifest["sources"][0]["content_hash"] = hashlib.sha256((root / "raw.txt").read_bytes()).hexdigest()
    (root / "sources.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = json.loads((root / "download-report.json").read_text(encoding="utf-8"))
    report[0]["text_sha256"] = hashlib.sha256((root / "text.txt").read_bytes()).hexdigest()
    (root / "download-report.json").write_text(json.dumps(report), encoding="utf-8")
    before = {name: (root / name).read_bytes() for name in ("raw.txt", "text.txt")}
    session = tools(tmp_path, library)
    assert search(session, query="三国桃园").results[0].records == []
    hit = search(session).results[0].records[0]
    context = session.invoke("read_evidence", json.dumps(dict(citation_id=hit.citation_id))).results[0].records[0]
    assert promotion not in context.text
    assert "主公要考虑生存和配合。" in context.text
    assert "玩家在微信搜索牌名只是咨询" in context.text
    assert all((root / name).read_bytes() == content for name, content in before.items())


def test_virtual_model_receives_result_then_requests_context_and_cites_it(tmp_path, library):
    def read_last(history):
        hit = history[-1].response.results[0].records[0]
        return ToolCallTurn(call_id="2", name="read_evidence", arguments=json.dumps(dict(citation_id=hit.citation_id)))

    def finish(history):
        context = history[-1].response.results[0].records[0]
        assert context.truncated
        return FinalAnswerTurn(status="partial", answer="合成流程预览，完整章节超长，不能声称已读完。",
                               citation_ids=[context.citation_id])

    steps = [ToolCallTurn(call_id="1", name="search_records", arguments=json.dumps(dict(
        record_type="analysis", scope_id="analysis-test", query="主公生存", top_k=1))), read_last, finish]
    result = asyncio.run(AskService(ScriptedModel(steps), lambda: tools(tmp_path, library)).ask("合成问题"))
    assert result.status == "partial" and not result.answerable
    assert [exchange.call.name for exchange in result.trace] == ["search_records", "read_evidence"]
    assert isinstance(result.citations[0], TextEvidence) and result.citations[0].truncated


def test_rule_scope_reuses_index_filtering_and_original_context(tmp_path):
    np = pytest.importorskip("numpy")
    root = tmp_path / "index"
    root.mkdir()
    chunks = [dict(chunk_id="c", record_id="r", source_id="source", source_url="https://example.com/rule",
                   ruleset_id="rules-test", category="基础规则参考", heading="合成规则", text="主公规则。", start=0, end=5),
              dict(chunk_id="other", record_id="other", source_id="other", source_url="https://example.com/other",
                   ruleset_id="other", category="基础规则参考", heading="其他规则", text="主公规则。", start=0, end=5)]
    (root / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    np.save(root / "vectors.npy", np.ones((2, 1), dtype=np.float32), allow_pickle=False)
    (root / "index.json").write_text(json.dumps(dict(format=1, dimensions=1, model={},
        chunks_sha256=hashlib.sha256((root / "chunks.json").read_bytes()).hexdigest(),
        vectors_sha256=hashlib.sha256((root / "vectors.npy").read_bytes()).hexdigest())), encoding="utf-8")
    library = TextLibrary([TextCollection(TextScopeInfo(scope_id="rule-test", record_type="rule",
        game_id="reference", applicability="仅合成规则范围"), root, ruleset_id="rules-test")])
    session = tools(tmp_path, library)
    response = session.invoke("search_records", '{"record_type":"rule","scope_id":"rule-test","query":"主公","top_k":1}')
    hit, = response.results[0].records
    assert hit.record_id == "c" and hit.record_type == "rule"
    context = session.invoke("read_evidence", json.dumps(dict(citation_id=hit.citation_id)))
    assert context.results[0].records[0].text == "主公规则。"


def test_reference_topic_and_question_both_reach_search(tmp_path, library, monkeypatch):
    original = library.search
    calls = []
    def observed(scope_id, record_type, query, top_k, **options):
        calls.append((scope_id, record_type, query, options))
        return original(scope_id, record_type, query, top_k, **options)
    monkeypatch.setattr(library, "search", observed)
    tools = KnowledgeTools(tmp_path / "unused.sqlite3", ["a"], preview=True, text_library=library)
    topic = tools.invoke("search_reference", '{"kind":"策略","topic":"身份"}')
    assert topic.error is None and any(s.records for s in topic.results)
    assert "主公" in calls[-1][2] and calls[-1][1] == "analysis"
    tools = KnowledgeTools(tmp_path / "unused.sqlite3", ["a"], preview=True, text_library=library)
    custom = tools.invoke("search_reference", '{"kind":"策略","topic":"身份","query":"主公生存"}')
    assert custom.error is None and any(s.records for s in custom.results)
    assert calls[-1][2] == "主公生存" and calls[-1][3]["topic"] == "身份"
    assert custom.results[0].records[0].source_url == "https://example.com/analysis"


def test_split_tool_loop_uses_skills_and_reference_without_internal_scope_input(tmp_path, library):
    from synthetic_data import build_synthetic_database
    database = build_synthetic_database(tmp_path)
    def finish(history):
        assert [x.call.name for x in history] == ["get_character_skills", "search_reference"]
        assert history[0].response.results[0].records[0].character == "测试武将甲"
        assert any(s.records for s in history[1].response.results)
        return FinalAnswerTurn(status="partial", answer="按技能及身份观点分析的测试回答。")
    model = ScriptedModel([
        ToolCallTurn(call_id="skills", name="get_character_skills", arguments='{"characters":["测试武将甲"]}'),
        ToolCallTurn(call_id="strategy", name="search_reference", arguments='{"kind":"策略","topic":"身份"}'), finish])
    service = AskService(model, lambda: KnowledgeTools(database, ["synthetic-a"], preview=True, text_library=library))
    result = asyncio.run(service.ask("测试武将甲适合做主公吗？"))
    assert result.error is None and result.answer
    assert result.citations == [] and any("未提供具体引用" in note for note in result.notes)


def test_topic_changes_ranking_and_unknown_topic_has_no_silent_fallback(tmp_path, monkeypatch):
    from app.services.text_library import _Section
    from app.schemas.tools import TextPassage
    collection = TextCollection(TextScopeInfo(scope_id="articles", record_type="analysis", game_id="reference",
        applicability="合成测试"), tmp_path)
    library = TextLibrary([collection])
    sections = []
    for source, topics, text in [("identity", ["身份", "配合"], "主公身份需要生存和忠臣配合。"),
                                 ("value", ["收益", "强度"], "主公收益来自摸牌的资源价值与代价。")]:
        passage = TextPassage(record_id=source, source_id=source, source_url="https://example.com/" + source,
            scope_id="articles", game_id="reference", record_type="analysis", title=source, heading=source,
            text=text, start=0, end=len(text), context_hash="snapshot", limitations=[], topics=topics)
        sections.append(_Section(passage, text))
    monkeypatch.setattr(library, "_analysis_sections", lambda _: sections)
    identity = library.search("articles", "analysis", "主公", 2, topic="身份")
    value = library.search("articles", "analysis", "主公", 2, topic="收益")
    assert identity[0].source_id == "identity"
    assert value[0].source_id == "value" and value[0].topics == ["收益", "强度"]
    assert library.search("articles", "analysis", "主公", 2, topic="不存在的领域") == []


def test_repeated_reference_query_returns_no_duplicate_body_and_keeps_original_readable(tmp_path, library):
    session = tools(tmp_path, library)
    args = '{"kind":"策略","topic":"身份","query":"主公生存"}'
    first = session.invoke("search_reference", args)
    records = [r for s in first.results for r in s.records]
    assert records
    seen = {(r.record_id, r.start, r.end) for r in records}
    for _ in range(60):
        second = session.invoke("search_reference", args)
        assert second.error is None
        new = [r for s in second.results for r in s.records]
        if not new:
            break
        keys = {(r.record_id, r.start, r.end) for r in new}
        assert not seen.intersection(keys)
        seen.update(keys)
    else:
        pytest.fail("有限资料未能搜索完毕")
    assert any("未找到新的" in note for note in second.notes)
    original = session.invoke("read_evidence", json.dumps({"citation_id": records[0].citation_id}))
    assert original.error is None and original.results[0].records[0].text
