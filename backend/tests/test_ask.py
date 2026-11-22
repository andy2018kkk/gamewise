"""使用脚本模型验证循环、预算、隔离和停止，不访问外部服务。"""

import asyncio
import json

import pytest

from app.providers.llm import ModelProviderError, ScriptedModel
from app.schemas.ask import FinalAnswerTurn, ToolCallTurn
from app.schemas.query import Game
from app.services.ask import AskService
from app.services.import_characters import import_character_files
from app.services.knowledge_tools import KnowledgeTools
from test_knowledge_store import save_batch


@pytest.fixture
def database(tmp_path):
    db = tmp_path / 'db.sqlite3'
    games = [Game(game_id=key, name=key) for key in ('a', 'b')]
    import_character_files(db, [save_batch(tmp_path, 'one', 'a'),
                               save_batch(tmp_path, 'two', 'b', text='另一服独立正文')], games)
    return db


def call(number=1, name='search_records', **arguments):
    return ToolCallTurn(call_id=str(number), name=name, arguments=json.dumps(arguments))


def answer_from_last(history):
    records = history[-1].response.results[0].records
    return FinalAnswerTurn(status='answered', answer=records[0].skill_text,
                           citation_ids=[records[0].citation_id])


def run(database, steps, *, preview=True, **limits):
    service = AskService(ScriptedModel(steps), lambda: KnowledgeTools(database, ['a'], preview=preview), **limits)
    return asyncio.run(service.ask('合成武将的技能是什么？'))


def test_search_result_returns_to_model_and_only_program_fills_sources(database):
    result = run(database, [call(character='合成武将'), answer_from_last])
    assert result.status == 'preview_answer' and not result.answerable
    assert result.answer == '准备阶段，摸2张牌。'
    assert len(result.trace) == 1 and len(result.citations) == 1
    assert result.citations[0].game_id == 'a' and result.citations[0].source_id == 'one'
    assert result.trace[0].elapsed_ms >= 0


def test_model_can_read_catalog_then_choose_content_query(database):
    def query_catalog_result(history):
        catalog = history[0].response.results[0].catalog
        assert catalog.characters == ['合成武将']
        return call(2, character=catalog.characters[0], game_ids=[catalog.game_id])

    result = run(database, [call(name='discover_catalog'), query_catalog_result, answer_from_last])
    assert result.status == 'preview_answer'
    assert [h.call.name for h in result.trace] == ['discover_catalog', 'search_records']


def test_lookup_can_continue_after_no_match_instead_of_hardcoded_one_call(database):
    def try_another_filter(history):
        assert history[-1].response.results[0].status == 'no_match_in_loaded_data'
        return call(2, skill_name='合成技能')

    result = run(database, [call(character='没有此样本'), try_another_filter, answer_from_last])
    assert result.status == 'preview_answer' and len(result.trace) == 2


@pytest.mark.parametrize('status', ['needs_clarification', 'insufficient_evidence'])
def test_explicit_non_answer_can_stop_without_unnecessary_queries(database, status):
    result = run(database, [FinalAnswerTurn(status=status, answer='请补充需要查询的对象。')])
    assert result.status == status and not result.trace


@pytest.mark.parametrize('first', [
    call(name='run_sql', sql='SELECT 1'),
    call(preview=True, character='合成武将'),
    call(game_ids=['b'], character='合成武将'),
])
def test_recoverable_tool_errors_return_to_model_but_do_not_bypass_limits(database, first):
    def correct(history):
        assert history[-1].response.error is not None
        return call(2, character='合成武将')

    result = run(database, [first, correct, answer_from_last])
    assert result.status == 'preview_answer' and len(result.trace) == 2
    assert {r.game_id for r in result.citations} == {'a'}


def test_tool_limit_also_counts_failed_attempts(database):
    result = run(database, [call(name='unknown'), call(2, character='合成武将')], max_tool_calls=1)
    assert result.error.code == 'tool_limit' and len(result.trace) == 1


def test_model_round_limit_is_separate_from_tool_limit(database):
    result = run(database, [call(character='合成武将')], max_rounds=1)
    assert result.error.code == 'round_limit'


def test_query_budget_exhausted_still_allows_evidence_insufficiency_response(database):
    class BudgetAwareModel:
        async def respond(self, *, question, tools, history):
            if not history:
                assert tools
                return call(name='unknown')
            assert tools == []
            return FinalAnswerTurn(status='insufficient_evidence', answer='查询预算用尽，未取得可用证据。')
    service = AskService(BudgetAwareModel(), lambda: KnowledgeTools(database, ['a']), max_tool_calls=1)
    result = asyncio.run(service.ask('问题'))
    assert result.status == 'insufficient_evidence' and len(result.trace) == 1


def test_repeated_request_is_detected_even_if_json_key_order_changes(database):
    second = ToolCallTurn(call_id='2', name='search_records', arguments='{"mode":"经典","character":"合成武将"}')
    result = run(database, [call(character='合成武将', mode='经典'), second])
    assert result.error.code == 'repeated_call' and len(result.trace) == 1


def test_duplicate_call_identifier_is_not_reused(database):
    result = run(database, [call(name='discover_catalog'), call(character='合成武将')])
    assert result.error.code == 'duplicate_call_id'


def test_cumulative_evidence_budget_stops_before_next_model_step(database):
    result = run(database, [call(character='合成武将'), answer_from_last], max_evidence_characters=1)
    assert result.error.code == 'evidence_budget' and result.answer is None


@pytest.mark.parametrize('turn,code', [
    (FinalAnswerTurn(status='answered', answer='编造引用', citation_ids=['invented']), 'invalid_citation'),
    (FinalAnswerTurn(status='answered', answer='来源https://invented.example/'), 'invalid_model_output'),
])
def test_model_cannot_finish_with_missing_invented_or_self_generated_sources(database, turn, code):
    result = run(database, [turn])
    assert result.error.code == code and result.answer is None


def test_duplicate_final_citations_are_rejected(database):
    def duplicate(history):
        record = history[-1].response.results[0].records[0]
        return FinalAnswerTurn(status='answered', answer='正文', citation_ids=[record.citation_id] * 2)
    assert run(database, [call(character='合成武将'), duplicate]).error.code == 'invalid_citation'


def test_pending_formal_mode_can_only_report_missing_approval(database):
    def stop(history):
        assert history[-1].response.results[0].status == 'pending_review'
        assert not history[-1].response.results[0].records
        return FinalAnswerTurn(status='insufficient_evidence', answer='当前资料仍待审。')
    result = run(database, [call(character='合成武将'), stop], preview=False)
    assert result.status == 'insufficient_evidence' and not result.citations


def test_citations_from_a_previous_question_are_not_accepted(database):
    first = run(database, [call(character='合成武将'), answer_from_last])
    second = run(database, [FinalAnswerTurn(status='answered', answer='旧结果',
                                          citation_ids=[first.citations[0].citation_id])])
    assert second.error.code == 'invalid_citation'


def test_model_service_failure_is_not_reported_as_no_evidence(database):
    class FailedModel:
        async def respond(self, **kwargs):
            raise ModelProviderError('模拟服务不可用')
    service = AskService(FailedModel(), lambda: KnowledgeTools(database, ['a']))
    result = asyncio.run(service.ask('问题'))
    assert result.error.code == 'model_error' and result.status == 'error'


def test_model_timeout(database):
    class SlowModel:
        async def respond(self, **kwargs):
            await asyncio.sleep(1)
    service = AskService(SlowModel(), lambda: KnowledgeTools(database, ['a']), timeout_seconds=.01)
    assert asyncio.run(service.ask('问题')).error.code == 'model_timeout'


def test_tool_timeout(database):
    import time
    class SlowTools(KnowledgeTools):
        def invoke(self, name, arguments):
            time.sleep(.03)
            return super().invoke(name, arguments)
    service = AskService(ScriptedModel([call(character='合成武将')]),
                         lambda: SlowTools(database, ['a']), timeout_seconds=.01)
    assert asyncio.run(service.ask('问题')).error.code == 'tool_timeout'


def test_storage_failure_is_not_reported_as_missing_game_content(tmp_path):
    result = run(tmp_path / 'missing.db', [call(character='合成武将')])
    assert result.error.code == 'storage_error' and result.status == 'error'


def test_invalid_model_action_is_rejected(database):
    result = run(database, [lambda _: {'kind': 'arbitrary_python', 'code': 'print(1)'}])
    assert result.error.code == 'invalid_model_output'


def test_blank_question_never_calls_model(database):
    service = AskService(ScriptedModel([]), lambda: KnowledgeTools(database, ['a']))
    assert asyncio.run(service.ask('  ')).error.code == 'invalid_question'


def test_model_cannot_mutate_retained_tool_results(database):
    def tamper(history):
        record = history[-1].response.results[0].records[0]
        citation = record.citation_id
        record.skill_text = '篡改正文'
        return FinalAnswerTurn(status='answered', answer='仅核对引用来源', citation_ids=[citation])
    result = run(database, [call(character='合成武将'), tamper])
    assert result.trace[0].response.results[0].records[0].skill_text == '准备阶段，摸2张牌。'
    assert result.citations[0].skill_text == '准备阶段，摸2张牌。'


def test_missing_citation_preserves_answer_without_inventing_support(database):
    result = run(database, [FinalAnswerTurn(status="answered", answer="这是尚未提供引用的回答。")])
    assert result.error is None and result.answer == "这是尚未提供引用的回答。"
    assert result.citations == [] and not result.answerable
    assert any("未提供具体引用" in note for note in result.notes)


def test_cumulative_size_limit_can_be_disabled_explicitly(database):
    result = run(database, [call(character='合成武将'), answer_from_last], max_evidence_characters=None)
    assert result.error is None and result.answer == '准备阶段，摸2张牌。'
