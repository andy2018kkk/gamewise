"""具名批量技能读取：范围、空项和引用按目标独立，失败不得发布半批证据。"""

import json
import asyncio

import pytest

from app.schemas.query import Game
from app.services.import_characters import import_character_files
from app.services.knowledge_tools import KnowledgeTools
from test_knowledge_store import save_batch


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "batch.sqlite3"
    games = [Game(game_id=g, name=g, category="sanguosha") for g in ("a", "b", "empty")]
    paths = [save_batch(tmp_path, "a-classic", text="甲服经典的完整技能。"),
             save_batch(tmp_path, "a-national", mode="国战", text="甲服国战的不同技能。"),
             save_batch(tmp_path, "a-other", character="另一武将", mode="经典版本", text="另一武将的技能。"),
             save_batch(tmp_path, "b-classic", "b", mode="经典版本", text="乙服经典的完整技能。")]
    import_character_files(path, paths, games)
    return path


def target(game="a", character="合成武将", mode="经典"):
    return {"game_id": game, "character": character, "mode": mode}


def query(session, targets, **other):
    return session.invoke("search_records", json.dumps({"record_type": "skill", "targets": targets, **other}))


def test_batch_matches_single_reads_and_keeps_branch_mode_and_citations(database):
    session = KnowledgeTools(database, ["a", "b"], preview=True)
    targets = [target(), target("b"), target(character="另一武将")]
    before = database.read_bytes()
    response = query(session, targets)
    assert response.error is None and len(response.results) == 3
    assert [r.target.model_dump() for r in response.results] == targets
    assert [r.records[0].skill_text for r in response.results] == [
        "甲服经典的完整技能。", "乙服经典的完整技能。", "另一武将的技能。"]
    for requested, result in zip(targets, response.results):
        single = session.invoke("search_records", json.dumps({"game_ids": [requested["game_id"]],
            "character": requested["character"], "mode": requested["mode"]}))
        assert result.records == single.results[0].records
        assert session.resolve_citation(result.records[0].citation_id) == result.records[0]
    assert database.read_bytes() == before


def test_missing_targets_keep_identity_without_losing_successes(database):
    session = KnowledgeTools(database, ["a", "empty"], preview=True)
    targets = [target(), target(character="未收录人物"), target("empty")]
    response = query(session, targets)
    assert [r.target.model_dump() for r in response.results] == targets
    assert [r.status for r in response.results] == ["preview_available", "no_match_in_loaded_data", "not_loaded"]
    assert len(response.results[0].records) == 1 and not response.results[1].records and not response.results[2].records


@pytest.mark.parametrize("targets,extra", [
    ([], {}), ([target()] * 2, {}), ([target(character=str(i)) for i in range(11)], {}),
    ([{"game_id": "a", "character": "合成武将"}], {}),
    ([target()], {"character": "合成武将"}), ([target()], {"game_ids": ["a"]}),
    ([target()], {"mode": "经典"}), ([target()], {"record_type": "analysis", "scope_id": "x", "query": "主公"}),
    ([{**target(), "sql": "SELECT 1"}], {}),
])
def test_invalid_batch_is_rejected_before_storage(tmp_path, targets, extra):
    response = query(KnowledgeTools(tmp_path / "missing.sqlite3", ["a"], preview=True), targets, **extra)
    assert response.error.code == "invalid_arguments" and not response.results


def test_one_unauthorized_target_rejects_entire_batch_before_storage(tmp_path):
    session = KnowledgeTools(tmp_path / "missing.sqlite3", ["a"], preview=True)
    response = query(session, [target(), target("b")])
    assert response.error.code == "scope_denied" and not response.results and not session._evidence


def test_formal_batch_does_not_publish_pending_skill_body(database):
    response = query(KnowledgeTools(database, ["a", "b"]), [target(), target("b")])
    assert [r.status for r in response.results] == ["pending_review", "pending_review"]
    assert all(not r.records and r.target is not None for r in response.results)


def test_over_budget_batch_publishes_no_partial_results_or_citations(database):
    session = KnowledgeTools(database, ["a", "b"], preview=True, max_characters=1)
    response = query(session, [target(), target("b")])
    assert response.error.code == "output_limit" and not response.results and not session._evidence


def test_per_target_record_limit_is_explicit_without_cutting_skill_text(database, tmp_path):
    extra = save_batch(tmp_path, "a-second-source", text="甲服另一个来源的完整技能。")
    import_character_files(database, [extra], [Game(game_id="a", name="a", category="sanguosha")])
    response = query(KnowledgeTools(database, ["a", "b"], preview=True, max_records=1), [target(), target("b")])
    assert response.results[0].has_more and not response.results[1].has_more
    assert response.results[0].records[0].skill_text == "甲服经典的完整技能。"




def test_batch_counts_as_one_tool_call_and_can_answer_with_both_sources(database):
    from app.providers.llm import ScriptedModel
    from app.schemas.ask import FinalAnswerTurn, ToolCallTurn
    from app.services.ask import AskService
    def answer(history):
        assert len(history) == 1
        left, right = (scope.records[0] for scope in history[0].response.results)
        return FinalAnswerTurn(status="answered", answer=left.skill_text + "[1]；" + right.skill_text + "[2]",
                               citation_ids=[left.citation_id, right.citation_id])
    model = ScriptedModel([ToolCallTurn(call_id="batch", name="search_records",
        arguments=json.dumps({"targets": [target(), target("b")]})), answer])
    result = asyncio.run(AskService(model, lambda: KnowledgeTools(database, ["a", "b"], preview=True),
                                   max_tool_calls=1).ask("比较两个服的技能"))
    assert result.status == "preview_answer" and len(result.trace) == 1
    assert {c.game_id for c in result.citations} == {"a", "b"}
