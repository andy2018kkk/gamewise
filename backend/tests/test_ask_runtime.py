"""通用入口接收不同问题，不联网，不套用固定人物测试案例。"""
import asyncio
import json

import pytest

from app.ask_runtime import run_question
from synthetic_data import build_synthetic_database
from app.groq_preview import main
from app.providers.groq import GroqSettings
from app.schemas.ask import FinalAnswerTurn, ToolCallTurn


@pytest.mark.parametrize("character,expected_status", [("测试武将甲", "preview_available"), ("另一人物", "no_match_in_loaded_data")])
def test_runtime_passes_user_question_and_uses_generic_tools(tmp_path, monkeypatch, character, expected_status):
    import app.ask_runtime as runtime
    captured = []
    class FakeModel:
        def __init__(self, settings, client, **options):
            assert "刘备" not in options["instructions"] and "曹操" not in options["instructions"]
            assert options["allowed_game_ids"] == ["synthetic-a"]
            self.usage = []
        async def respond(self, *, question, tools, history):
            captured.append(question)
            if not history:
                definition = next(t for t in tools if t["name"] == "get_character_skills")
                assert "enum" not in definition["parameters"]["properties"]["characters"]["items"]
                return ToolCallTurn(call_id="read", name="get_character_skills",
                    arguments=json.dumps({"characters": [character]}))
            assert history[0].response.results[0].status == expected_status
            return FinalAnswerTurn(status="partial", answer="仅验证入口，非真实模型答案。")
    monkeypatch.setattr(runtime, "GroqModel", FakeModel)
    database = build_synthetic_database(tmp_path)
    question = character + "的技能是什么？"
    run = asyncio.run(run_question(question, GroqSettings(api_key="fake", model="qwen/qwen3.8-27b"),
        database=database, game_ids=["synthetic-a"]))
    assert captured == [question, question]
    assert run.result.answer and run.result.error is None
    assert run.result.trace[0].call.name == "get_character_skills"


@pytest.mark.parametrize("arguments", [
    ["--question", "新问题", "--run-remote"],
    ["--question", " ", "--game", "a", "--run-remote"],
    ["--question", "新问题", "--game", "a", "--case", "ol-comparison", "--run-remote"],
])
def test_question_cli_rejects_missing_scope_or_mixed_fixed_case_before_reading_key(monkeypatch, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("非法输入不应读取密钥或调用模型")
    monkeypatch.setattr(GroqSettings, "from_file", forbidden)
    monkeypatch.setattr("sys.argv", ["preview", *arguments])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize("question", ["界刘备和神曹操谁适合做主公？", "失去体力与伤害有什么区别？"])
def test_cli_passes_new_question_to_runtime_without_saved_context(tmp_path, monkeypatch, question):
    from types import SimpleNamespace
    from app.schemas.ask import AskResult
    import app.groq_preview as entry
    captured = []
    async def fake_run(actual_question, settings, **options):
        captured.append((actual_question, options))
        return SimpleNamespace(result=AskResult(status="insufficient_evidence", answer="离线入口测试"),
                               elapsed_seconds=0, usage=[])
    monkeypatch.setattr(entry, "run_question", fake_run)
    monkeypatch.setattr(GroqSettings, "from_file", lambda path, **kwargs: GroqSettings(api_key="fake", model=kwargs.get("model") or "qwen/qwen3.8-27b"))
    monkeypatch.setattr(entry, "load_text_library", lambda path: None)
    monkeypatch.setattr(entry.CharacterFamilies, "from_config", lambda path: None)
    monkeypatch.setattr("sys.argv", ["preview", "--run-remote", "--question", question,
        "--game", "sanguosha-ol", "--output-dir", str(tmp_path)])
    entry.main()
    assert len(captured) == 1 and captured[0][0] == question
    assert set(captured[0][1]) == {"database", "game_ids", "text_library", "character_families"}
    assert captured[0][1]["game_ids"] == ["sanguosha-ol"]
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["question"] == question and report["result"]["trace"] == []
