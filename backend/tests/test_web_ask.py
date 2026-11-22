"""网页边界与通用问答装配测试；全部替身，不联网。"""
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.ask_runtime import QuestionRun
from app.main import app
from app.providers.llm import ModelProviderError
from app.schemas.ask import AskError, AskResult
from app.schemas.tools import ToolRecord
from app.schemas.web_ask import WebAnswer
from app.services import web_ask


def test_question_only_contract(monkeypatch):
    runner = AsyncMock(return_value=WebAnswer(status="preview_answer", answer="回答[1]"))
    monkeypatch.setattr("app.main.answer_web_question", runner)
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "OL刘备技能是什么"})
    assert response.status_code == 200
    assert response.json()["answer"] == "回答[1]"
    runner.assert_awaited_once_with("OL刘备技能是什么", history=())


def test_invalid_inputs_never_call_model(monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr("app.main.answer_web_question", runner)
    with TestClient(app) as client:
        for body in ({"question": "  "}, {"question": 123}, {},
                     {"question": "你好", "api_key": "forbidden"},
                     {"question": "你好", "database": "other.db"}):
            assert client.post("/api/ask", json=body).status_code == 422
    runner.assert_not_awaited()


def test_missing_config_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(web_ask, "ROOT", tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "刘备"})
    assert response.json()["status"] == "configuration_error"
    assert str(tmp_path) not in response.text


def setup_local(monkeypatch, tmp_path, result):
    (tmp_path / ".env").write_text("GROQ_API_KEY=secret-test-not-real", encoding="utf-8")
    database = tmp_path / "data/local/knowledge.local/gamewise.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    monkeypatch.setattr(web_ask, "ROOT", tmp_path)
    monkeypatch.setattr(web_ask, "load_text_library", lambda _: None)
    monkeypatch.setattr(web_ask.CharacterFamilies, "from_config", lambda _: None)
    runner = AsyncMock(return_value=QuestionRun(result, (), 1.2))
    monkeypatch.setattr(web_ask, "run_question", runner)
    return runner


def test_sources_preserve_order_without_local_paths(monkeypatch, tmp_path):
    def record(number, locator):
        return ToolRecord(character="刘备", skill_name=f"技能{number}", mode="经典", skill_text="技能原文",
            record_id=str(number), game_id="sanguosha-ol", version_ids=[], source_id="s",
            source_locator=locator, content_path="private-file.json", content_hash=None,
            locators=[], citation_id=f"c{number}")
    result = AskResult(status="preview_answer", answer="回答[1][2]", citations=[
        record(2, "https://example.com/2"), record(1, "C:/private/source.json")])
    runner = setup_local(monkeypatch, tmp_path, result)
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "自由提问"})
    data = response.json()
    assert [s["number"] for s in data["sources"]] == [1, 2]
    assert data["sources"][0]["title"].endswith("技能2")
    assert data["sources"][1]["url"] is None
    assert "private" not in response.text and "secret-test" not in response.text
    assert runner.call_args.args[1].model == "qwen/qwen3.8-27b"
    assert runner.call_args.kwargs["game_ids"] == web_ask.GAME_IDS


def test_runtime_failure_preserved(monkeypatch, tmp_path):
    message = ModelProviderError("not public", kind="rate_limit", http_status=429).public_message
    setup_local(monkeypatch, tmp_path, AskResult(status="error",
        error=AskError(code="model_error", message=message)))
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "你好"})
    assert response.json()["error"] == message
    assert response.json()["answer"] is None
