"""启动配置回归：临时文件和参数替身，不读真实密钥、不联网。"""
from types import SimpleNamespace
import pytest
from app.providers.groq import GroqSettings
from app.groq_preview import main


def cli_args(config, selected_model=None):
    return SimpleNamespace(run_remote=True, question="任意问题", games=["sanguosha-ol"],
        env_file=config, model=selected_model, output_dir=config.parent / "out",
        case="ol-comparison", resume_report=None, finalize_only=False)

@pytest.mark.parametrize("env_model,selected_model,expected", [
    (None, "qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),
    ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),
    ("qwen/qwen3.8-27b", None, "qwen/qwen3.8-27b"),
])
def test_model_selection_precedence_without_changing_local_file(tmp_path, env_model, selected_model, expected):
    path = tmp_path / ".env"
    content = 'GROQ_API_KEY="fake-secret"\n'
    if env_model is not None:
        content += 'GROQ_MODEL=' + env_model + '\n'
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    settings = GroqSettings.from_file(path, model=selected_model)
    assert settings.model == expected
    assert settings.api_key.get_secret_value() == "fake-secret"
    assert path.read_bytes() == original
    assert "fake-secret" not in repr(settings)


def test_model_missing_from_both_inputs_is_explicit_and_does_not_expose_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GROQ_API_KEY=fake-secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="--model") as error:
        GroqSettings.from_file(path)
    assert "fake-secret" not in str(error.value)


def test_explicit_model_does_not_bypass_required_local_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GROQ_MODEL=openai/gpt-oss-120b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqSettings.from_file(path, model="qwen/qwen3.8-27b")


@pytest.mark.parametrize("env_model,selected_model,expected", [
    (None, "qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),
    ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "qwen/qwen3.8-27b"),
    ("qwen/qwen3.8-27b", None, "qwen/qwen3.8-27b"),
])
def test_cli_resolves_model_before_running_any_question(tmp_path, monkeypatch, env_model, selected_model, expected):
    config = tmp_path / ".env"
    config.write_text("GROQ_API_KEY=fake-private-key\n" + ("GROQ_MODEL=" + env_model + "\n" if env_model else ""), encoding="utf-8")
    original = config.read_bytes()
    resolve = GroqSettings.from_file
    captured = []
    class SettingsLoaded(RuntimeError):
        pass
    def resolve_and_stop(path, *, model=None):
        settings = resolve(path, model=model)
        captured.append(settings.model)
        assert settings.api_key.get_secret_value() == "fake-private-key"
        raise SettingsLoaded
    monkeypatch.setattr(GroqSettings, "from_file", resolve_and_stop)
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: cli_args(config, selected_model))
    with pytest.raises(SettingsLoaded):
        main()
    assert captured == [expected] and config.read_bytes() == original


@pytest.mark.parametrize("config_text,selected_model", [
    ("GROQ_API_KEY=fake-private-key\n", None),
    ("GROQ_MODEL=openai/gpt-oss-120b\n", "qwen/qwen3.8-27b"),
])
def test_cli_missing_settings_stops_without_exposing_config(tmp_path, monkeypatch, capsys, config_text, selected_model):
    config = tmp_path / ".env"
    config.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: cli_args(config, selected_model))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "--model" in message and "GROQ_API_KEY" in message
    assert "fake-private-key" not in message
