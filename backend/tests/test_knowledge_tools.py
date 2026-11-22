"""模拟模型发出工具请求；无网络、真实模型或收费调用。"""

import json

import pytest

from app.schemas.query import Game
from app.services.import_characters import import_character_files
from app.services.knowledge_tools import KnowledgeTools
from test_knowledge_store import save_batch


@pytest.fixture
def tool_data(tmp_path):
    database = tmp_path / "db.sqlite3"
    games = [Game(game_id=game, name=game) for game in ("a", "b", "c")]
    paths = [save_batch(tmp_path, "one", "a"),
             save_batch(tmp_path, "two", "b", text="另一个服的技能效果", package="包乙")]
    import_character_files(database, paths, games)
    return database


@pytest.mark.parametrize("arguments", [
    {"character": "合成武将"}, {"skill_name": "合成技能"},
    {"package_name": "包甲"}, {"faction": "吴"},
    {"character": "合成武将", "skill_name": "合成技能", "mode": "经典"},
])
def test_same_tool_supports_forward_reverse_and_attribute_queries(tool_data, arguments):
    tools = KnowledgeTools(tool_data, ["a"], preview=True)
    response = tools.invoke("search_records", json.dumps(arguments))
    assert response.error is None and response.preview and not response.answerable
    result, = response.results
    assert result.game_id == "a" and result.status == "preview_available"
    record, = result.records
    assert record.skill_text == "准备阶段，摸2张牌。" and not record.answerable
    assert tools.resolve_citation(record.citation_id).record_id == record.record_id


def test_default_scopes_are_program_configuration_not_hardcoded(tool_data):
    response = KnowledgeTools(tool_data, ["b", "a"], preview=True).invoke(
        "search_records", '{"skill_name":"合成技能"}')
    assert {r.game_id for r in response.results} == {"a", "b"}
    assert {r.records[0].skill_text for r in response.results} == {"准备阶段，摸2张牌。", "另一个服的技能效果"}


def test_formal_mode_keeps_pending_body_and_citations_out(tool_data):
    tools = KnowledgeTools(tool_data, ["a"])
    response = tools.invoke("search_records", '{"character":"合成武将"}')
    assert response.results[0].status == "pending_review"
    assert response.results[0].records == [] and not response.answerable
    assert "准备阶段" not in response.model_dump_json()
    with pytest.raises(ValueError):
        tools.resolve_citation("invented")


@pytest.mark.parametrize("arguments", [
    '{}', '{"mode":"经典"}', '{"skill_name":""}', '{"skill_name":"   "}',
    '{"skill_name":1}', '{"game_ids":[],"skill_name":"合成技能"}',
    '{"skill_name":"合成技能","preview":true}',
    '{"sql":"SELECT * FROM sources"}', 'not-json',
])
def test_bad_arguments_are_not_executed(tool_data, arguments):
    response = KnowledgeTools(tool_data, ["a"]).invoke("search_records", arguments)
    assert response.error.code == "invalid_arguments" and not response.results


@pytest.mark.parametrize("games", [["b"], ["a", "a"], ["invented"]])
def test_model_cannot_expand_or_duplicate_allowed_scope(tool_data, games):
    response = KnowledgeTools(tool_data, ["a"], preview=True).invoke(
        "search_records", json.dumps({"game_ids": games, "skill_name": "合成技能"}))
    assert response.error.code == "scope_denied" and not response.results


def test_unknown_tool_and_storage_failure_are_technical_errors(tool_data, tmp_path):
    assert KnowledgeTools(tool_data, ["a"]).invoke("run_sql", '{}').error.code == "unknown_tool"
    response = KnowledgeTools(tmp_path / "missing.sqlite3", ["a"]).invoke(
        "search_records", '{"skill_name":"合成技能"}')
    assert response.error.code == "storage_error" and not response.results


def test_empty_scope_does_not_mean_game_skill_does_not_exist(tool_data):
    tools = KnowledgeTools(tool_data, ["a", "c"], preview=True)
    response = tools.invoke("search_records", '{"skill_name":"未收录技能"}')
    assert [r.status for r in response.results] == ["no_match_in_loaded_data", "not_loaded"]


def test_conditions_intersect_and_never_fall_back_to_other_scope(tool_data):
    tools = KnowledgeTools(tool_data, ["a", "b"], preview=True)
    response = tools.invoke("search_records", '{"game_ids":["a"],"package_name":"包乙"}')
    result, = response.results
    assert result.game_id == "a" and not result.records


def test_output_limit_preserves_whole_skills_and_does_not_register_hidden_evidence(tool_data):
    tools = KnowledgeTools(tool_data, ["a"], preview=True, max_characters=1)
    response = tools.invoke("search_records", '{"skill_name":"合成技能"}')
    assert response.error.code == "output_limit" and not response.results
    assert not tools._evidence


def test_catalog_is_metadata_not_body_and_source_database_is_unchanged(tool_data):
    before = tool_data.read_bytes()
    tools = KnowledgeTools(tool_data, ["a"])
    response = tools.invoke("discover_catalog", '{"package_name":"包甲"}')
    assert response.results[0].catalog.characters == ["合成武将"]
    assert "准备阶段" not in response.model_dump_json()
    assert tool_data.read_bytes() == before


def test_citations_cannot_be_reused_in_another_session_or_modified_by_caller(tool_data):
    first, second = KnowledgeTools(tool_data, ["a"], preview=True), KnowledgeTools(tool_data, ["a"], preview=True)
    response = first.invoke("search_records", '{"skill_name":"合成技能"}')
    record = response.results[0].records[0]
    citation = record.citation_id
    record.skill_text = "外部改写"
    assert first.resolve_citation(citation).skill_text == "准备阶段，摸2张牌。"
    with pytest.raises(ValueError):
        second.resolve_citation(citation)


def test_result_cap_is_explicit_without_cutting_text(tool_data, tmp_path):
    import_character_files(tool_data, [save_batch(tmp_path, 'three', 'a', mode='国战')],
                           [Game(game_id=g, name=g) for g in ('a', 'b', 'c')])
    response = KnowledgeTools(tool_data, ['a'], preview=True, max_records=1).invoke(
        'search_records', '{"character":"合成武将"}')
    assert response.results[0].has_more and len(response.results[0].records) == 1
    assert response.results[0].records[0].skill_text == '准备阶段，摸2张牌。'


def test_definitions_expose_only_current_tools_and_never_preview_override():
    definitions = KnowledgeTools.definitions()
    assert [d['name'] for d in definitions] == ['discover_catalog', 'get_character_skills', 'search_reference', 'read_evidence']
    assert all('preview' not in d['parameters']['properties'] for d in definitions)
    assert all(d['parameters']['additionalProperties'] is False for d in definitions)


def test_named_skill_tool_matches_existing_query_and_keeps_server_labels(tool_data):
    tools = KnowledgeTools(tool_data, ["a", "b"], preview=True)
    actual = tools.invoke("get_character_skills", '{"characters":["合成武将"],"mode":"经典"}')
    expected = tools.invoke("search_records", '{"character":"合成武将","mode":"经典"}')
    assert actual.error is None
    assert [(s.game_id, [(r.record_id, r.skill_text) for r in s.records]) for s in actual.results] == [
        (s.game_id, [(r.record_id, r.skill_text) for r in s.records]) for s in expected.results]
    assert {s.game_id for s in actual.results} == {"a", "b"}
    denied = tools.invoke("get_character_skills", '{"characters":["合成武将"],"game_ids":["outside"]}')
    assert denied.error.code == "scope_denied"


def test_split_tools_reject_cross_type_fields_without_opening_database(tmp_path):
    tools = KnowledgeTools(tmp_path / "missing.sqlite3", ["a"], preview=True)
    assert tools.invoke("get_character_skills", '{"characters":["合成武将"],"query":"主公"}').error.code == "invalid_arguments"
    assert tools.invoke("search_reference", '{"kind":"策略","topic":"身份","scope_id":"invented"}').error.code == "invalid_arguments"
    assert tools.invoke("search_reference", '{"kind":"策略"}').error.code == "invalid_arguments"
