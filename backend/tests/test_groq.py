"""用HTTP替身检查Groq转换、错误与上下文，不读.env、不联网。"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.providers.groq import GroqModel, GroqSettings
from app.providers.llm import ModelProviderError
from app.schemas.ask import ToolCallTurn, ToolExchange
from app.schemas.tools import ToolResponse
from app.services.knowledge_tools import KnowledgeTools


def execute(handler, *, history=()):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake-test-key", model="test-model"), client,
                              instructions="测试说明", allowed_game_ids=["a"])
            turn = await model.respond(question="测试问题", tools=KnowledgeTools.definitions(), history=history)
            return turn, model.usage
    return asyncio.run(run())


def test_native_call_wraps_schema_and_returns_action_without_executing():
    def handler(request):
        data = json.loads(request.content)
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert data["tool_choice"] == "auto" and data["parallel_tool_calls"] is False
        assert data["tools"][0]["function"]["name"] == "discover_catalog"
        assert "properties" in data["tools"][0]["function"]["parameters"]
        assert "fake-test-key" not in json.dumps(data)
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "discover_catalog", "arguments": "{}"}}]}}], "usage": {"total_tokens": 12}})
    turn, usage = execute(handler)
    assert isinstance(turn, ToolCallTurn) and turn.call_id == "c1"
    assert usage[0].total_tokens == 12


def test_actual_tool_result_and_call_id_are_sent_back_and_final_is_parsed():
    history = (ToolExchange(call=ToolCallTurn(call_id="c1", name="discover_catalog", arguments="{}"),
                            response=ToolResponse(notes=["合成目录返回"])),)
    def handler(request):
        messages = json.loads(request.content)["messages"]
        assert messages[-2]["tool_calls"][0]["id"] == "c1"
        assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "c1"
        assert json.loads(messages[-1]["content"])["notes"] == ["合成目录返回"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"kind": "final", "status": "insufficient_evidence", "answer": "无资料", "citation_ids": []})}}]})
    turn, _ = execute(handler, history=history)
    assert turn.status == "insufficient_evidence"


@pytest.mark.parametrize("status", [401, 402, 429, 500, 302])
def test_error_body_and_key_are_not_exposed_and_no_retry(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="private-data fake-test-key", headers={"Location": "https://example.com"})
    with pytest.raises(ModelProviderError) as caught:
        execute(handler)
    assert str(status) in str(caught.value) and "private-data" not in str(caught.value)
    assert len(requests) == 1


def test_timeout_remains_timeout():
    def handler(request):
        raise httpx.ReadTimeout("private data", request=request)
    with pytest.raises(TimeoutError, match="超时"):
        execute(handler)


@pytest.mark.parametrize("payload,error", [
    ({"choices": []}, ValidationError),
    ({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, ModelProviderError),
    ({"choices": [{"finish_reason": "stop", "message": {"content": "不是约定JSON"}}]}, ValidationError),
    ({"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": []}}]}, ModelProviderError),
])
def test_invalid_or_incomplete_output_is_not_used_as_answer(payload, error):
    with pytest.raises(error):
        execute(lambda request: httpx.Response(200, json=payload))


def test_env_settings_hide_key_and_do_not_expand_shell(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# note\nGROQ_API_KEY="fake-secret"\nGROQ_MODEL=test-model\nIGNORED=$(command)\n', encoding="utf-8")
    settings = GroqSettings.from_file(path)
    assert settings.api_key.get_secret_value() == "fake-secret"
    assert "fake-secret" not in repr(settings)
    path.write_text("GROQ_MODEL=test-model", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少"):
        GroqSettings.from_file(path)


@pytest.mark.parametrize("body", [{"error": {"code": ["invalid"]}}, ["invalid"], {"error": {"code": "private-data"}}])
def test_unknown_error_payload_does_not_escape_sanitized_error(body):
    with pytest.raises(ModelProviderError, match="HTTP 400"):
        execute(lambda request: httpx.Response(400, json=body))


def test_adapter_and_existing_query_loop_use_actual_synthetic_sqlite_result(tmp_path):
    from app.groq_preview import build_synthetic_database
    from app.services.ask import AskService

    database = build_synthetic_database(tmp_path)
    requests = []
    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        if len(requests) == 1:
            message = {"tool_calls": [{"id": "query", "type": "function", "function": {
                "name": "search_records", "arguments": json.dumps({
                    "game_ids": ["synthetic-a"], "character": "测试武将甲"})}}]}
            reason = "tool_calls"
        else:
            record = json.loads(data["messages"][-1]["content"])["results"][0]["records"][0]
            assert "弃置1张" in record["skill_text"] and "摸3张" in record["skill_text"]
            assert "content_path" not in record and "content_hash" not in record
            assert record["source_id"] == "synthetic-source" and record["citation_id"]
            message = {"content": json.dumps({"kind": "final", "status": "answered",
                "answer": "虚构测试：" + record["skill_text"], "citation_ids": [record["citation_id"]]})}
            reason = "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": message}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client,
                              instructions="测试", allowed_game_ids=["synthetic-a"])
            return await AskService(model, lambda: KnowledgeTools(database, ["synthetic-a"], preview=True)).ask("查测试技能")
    result = asyncio.run(run())
    assert result.status == "preview_answer" and len(result.citations) == 1
    assert result.citations[0].source_id == "synthetic-source" and len(requests) == 2


@pytest.mark.parametrize("retry_after,success,expected_calls", [("0", True, 2), ("90", False, 1),
    ("nan", False, 1), ("-1", False, 1), (None, False, 1)])
def test_opt_in_rate_retry_only_obeys_valid_server_wait_within_budget(retry_after, success, expected_calls):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={} if retry_after is None else {"retry-after": retry_after})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"status": "insufficient_evidence", "answer": "无资料"})}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client, instructions="测试",
                              allowed_game_ids=["a"], max_rate_limit_retries=1)
            return await model.respond(question="问题", tools=KnowledgeTools.definitions(), history=())
    if success:
        assert asyncio.run(run()).status == "insufficient_evidence"
    else:
        with pytest.raises(ModelProviderError):
            asyncio.run(run())
    assert len(requests) == expected_calls


def test_rate_retry_cannot_loop_indefinitely():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "0"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client, instructions="测试",
                              allowed_game_ids=["a"], max_rate_limit_retries=1)
            await model.respond(question="问题", tools=KnowledgeTools.definitions(), history=())
    with pytest.raises(ModelProviderError):
        asyncio.run(run())
    assert len(requests) == 2


def test_total_deadline_can_cancel_rate_limit_wait():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "10"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client, instructions="测试",
                              allowed_game_ids=["a"], max_rate_limit_retries=1)
            await asyncio.wait_for(model.respond(question="问题", tools=[], history=()), timeout=.01)
    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert len(requests) == 1


def test_tool_failure_diagnostic_does_not_log_generation_or_private_values():
    from app.providers.groq import _tool_failure_details
    diagnostic = _tool_failure_details({"message": "attempted to call tool 'json' which was not in request.tools",
        "failed_generation": '<function=json {"private":"fake-secret"}</function>'})
    assert diagnostic == {"failure_reason": "unknown_tool", "failed_tool_name": "json"}
    assert "fake-secret" not in json.dumps(diagnostic)
    unknown = _tool_failure_details({"message": "tool 'fake_secret_name' was not offered"})
    assert unknown["failed_tool_name"] == "unregistered_or_unidentified"
    assert _tool_failure_details({"message": [], "failed_generation": {}}) == {"failure_reason": "unclassified"}


def test_terminal_action_converts_to_final_answer_not_knowledge_tool():
    def handler(request):
        definitions = json.loads(request.content)["tools"]
        assert [t["function"]["name"] for t in definitions][-1] == "json"
        assert len(KnowledgeTools.definitions()) == 3
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "end", "type": "function", "function": {"name": "json",
                "arguments": json.dumps({"status": "partial", "answer": "仅测试[1]", "citation_ids": ["c1"]})}}]}}]})
    turn, usage = execute(handler)
    assert turn.kind == "final" and turn.status == "partial" and turn.citation_ids == ["c1"]
    assert usage[0].elapsed_seconds >= 0


def test_invalid_terminal_action_is_not_accepted():
    payload = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "end",
        "type": "function", "function": {"name": "json", "arguments": '{"status":"invented"}'}}]}}]}
    with pytest.raises(ValidationError):
        execute(lambda request: httpx.Response(200, json=payload))


@pytest.mark.parametrize("args", [{"character": "孙坚"}, {"character": "刘备", "mode": "国战版本"},
                                   {"character": "曹操", "game_ids": ["sanguosha-mobile"]}])
def test_real_case_runner_cannot_expand_authorized_character_mode_or_branch(tmp_path, args):
    from app.groq_preview import _ComparisonTools, build_synthetic_database
    database = build_synthetic_database(tmp_path)
    session = _ComparisonTools(database, ["sanguosha-ol"], preview=True)
    response = session.invoke("search_records", json.dumps(args))
    assert response.error.code == "scope_denied" and not response.results


def test_request_larger_than_token_capacity_does_not_wait_or_retry():
    requests = []
    async def run():
        def handler(request):
            requests.append(request)
            return httpx.Response(429, headers={"retry-after": "0", "x-ratelimit-limit-tokens": "8000"},
                json={"error": {"code": "rate_limit_exceeded", "message": "Limit 8000, Requested 9000"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client, instructions="测试",
                allowed_game_ids=["a"], max_rate_limit_retries=1)
            with pytest.raises(ModelProviderError):
                await model.respond(question="问题", tools=[], history=())
            assert model.usage[-1].failure_reason == "request_exceeds_token_limit"
            assert model.usage[-1].requested_tokens == 9000
    asyncio.run(run())
    assert len(requests) == 1


def test_resume_revalidates_actual_content_and_accepts_only_fresh_session_citations(tmp_path):
    from app.groq_preview import _ReplayThenModel, build_synthetic_database
    from app.schemas.ask import FinalAnswerTurn
    database = build_synthetic_database(tmp_path)
    args = json.dumps({"character": "测试武将甲"})
    call = ToolCallTurn(call_id="old-call", name="search_records", arguments=args)
    old_response = KnowledgeTools(database, ["synthetic-a"], preview=True).invoke(call.name, args)
    new_response = KnowledgeTools(database, ["synthetic-a"], preview=True).invoke(call.name, args)
    old = ToolExchange(call=call, response=old_response)
    current = ToolExchange(call=call, response=new_response)
    assert old_response.results[0].records[0].citation_id != new_response.results[0].records[0].citation_id
    class FinalModel:
        async def respond(self, *, history, **kwargs):
            return FinalAnswerTurn(status="answered", answer="仅测试", citation_ids=[history[-1].response.results[0].records[0].citation_id])
    resumed = _ReplayThenModel((old,), FinalModel())
    first = asyncio.run(resumed.respond(question="同题", tools=[], history=()))
    assert first == call
    final = asyncio.run(resumed.respond(question="同题", tools=[], history=(current,)))
    assert final.citation_ids == [new_response.results[0].records[0].citation_id]
    current.response.results[0].records[0].skill_text = "变化的正文"
    with pytest.raises(ModelProviderError, match="变化"):
        asyncio.run(resumed.respond(question="同题", tools=[], history=(current,)))


def test_finalize_phase_only_exposes_terminal_action_and_allows_insufficient_evidence():
    def handler(request):
        payload = json.loads(request.content)
        assert [t["function"]["name"] for t in payload["tools"]] == ["json"]
        assert payload["tool_choice"] == {"type": "function", "function": {"name": "json"}}
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "stop", "type": "function", "function": {"name": "json",
                "arguments": json.dumps({"status": "insufficient_evidence", "answer": "证据不足"})}}]}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client, instructions="测试",
                              allowed_game_ids=["a"])
            return await model.respond(question="同题", tools=[], history=())
    assert asyncio.run(run()).status == "insufficient_evidence"
