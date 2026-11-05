"""Provider setup persists usable defaults without leaking or losing saved keys."""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim" / "launcher"))
import config as launcher_config
import main as launcher
import setup_wizard as wizard


@pytest.fixture
def setup(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# OPENAI_API_KEY=\n# GEMINI_API_KEY=\n# INNATE_SERVICE_KEY=\n")
    monkeypatch.setattr(wizard, "ENV_PATH", env)
    monkeypatch.setattr(wizard, "SETTINGS_PATH", tmp_path / "settings.yaml")
    for key in (*launcher_config.SECRET_ENV_KEYS, *launcher_config.VENDOR_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    return env, {"raw_env": {}, "user_env": {}}


def test_installer_cli_saves_openai_and_selects_direct_model(setup, monkeypatch, capsys):
    env, config = setup
    wizard.apply_brain_backend(config, "innate", "old-service-secret")
    monkeypatch.setattr(launcher, "get_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["innate-sim", "setup", "--no-prefetch", "--backend", "openai"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-openai-test-secret\n"))
    assert launcher.main() == 0
    saved = launcher_config.parse_env_file(env)
    assert saved["OPENAI_API_KEY"] == "sk-openai-test-secret"
    assert saved["LLM_MODEL"] == "openai:gpt-5.4-mini"
    assert "INNATE_SERVICE_KEY" not in saved
    assert launcher_config.resolve_brain_backend(saved) == launcher_config.VENDOR_BACKEND
    output = capsys.readouterr().out
    assert "OPENAI_API_KEY" in output
    assert "sk-openai-test-secret" not in output
    assert "old-service-secret" not in output


def test_switch_gemini_openai_innate_none_and_restore(setup, monkeypatch):
    env, config = setup
    wizard.apply_brain_backend(config, "gemini", "gemini-test-secret")
    wizard.apply_brain_backend(config, "openai", "openai-test-secret")
    wizard.apply_brain_backend(config, "gemini", "gemini-test-secret")
    assert config["user_env"]["LLM_MODEL"].startswith("google:")
    wizard.apply_brain_backend(config, "innate", "service-test-secret")
    assert launcher_config.resolve_brain_backend(launcher_config.parse_env_file(env)) == launcher_config.INNATE_BACKEND
    wizard.apply_brain_backend(config, "none", "")
    assert launcher_config.resolve_brain_backend(launcher_config.parse_env_file(env)) == launcher_config.NO_BACKEND
    monkeypatch.setattr(wizard, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(wizard, "_prompt_choice", lambda *a, **kw: "2")
    monkeypatch.setattr(wizard, "_prompt_secret", lambda *a: pytest.fail("saved key should be restored"))
    wizard.configure_brain_backend(config)
    assert launcher_config.parse_env_file(env)["OPENAI_API_KEY"] == "openai-test-secret"
    assert config["user_env"]["LLM_MODEL"].startswith("openai:")


@pytest.mark.parametrize("key", ["", "   ", "\n"])
def test_empty_key_does_not_change_existing_config(setup, key):
    env, config = setup
    before = env.read_text()
    with pytest.raises(ValueError):
        wizard.apply_brain_backend(config, "openai", key)
    assert env.read_text() == before
    assert config == {"raw_env": {}, "user_env": {}}


def test_interactive_invalid_then_valid_key(setup, monkeypatch):
    env, config = setup
    monkeypatch.setattr(wizard, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(wizard, "_prompt_choice", lambda *a, **kw: "2")
    answers = iter(["", "openai-test-secret"])
    monkeypatch.setattr(wizard, "_prompt_secret", lambda *a: next(answers))
    wizard.configure_brain_backend(config)
    assert launcher_config.parse_env_file(env)["OPENAI_API_KEY"] == "openai-test-secret"


def test_preserve_same_vendor_model_and_explicit_settings(setup):
    env, config = setup
    settings = env.parent / "settings.yaml"
    settings.write_text('brain_client_node:\n  ros__parameters:\n    llm_model: "google:gemini-3.6-flash"\n')
    before = settings.read_text()
    wizard.write_env_value(env, "LLM_MODEL", "openai:gpt-5.4")
    config["user_env"]["LLM_MODEL"] = "openai:gpt-5.4"
    wizard.apply_brain_backend(config, "openai", "openai-test-secret")
    assert launcher_config.parse_env_file(env)["LLM_MODEL"] == "openai:gpt-5.4"
    assert settings.read_text() == before


def test_noninteractive_only_reports_without_mutation(setup, monkeypatch, capsys):
    env, config = setup
    wizard.apply_brain_backend(config, "openai", "openai-test-secret")
    before = env.read_text()
    monkeypatch.setattr(wizard, "is_interactive_terminal", lambda: False)
    wizard.configure_brain_backend(config)
    assert env.read_text() == before
    assert "OpenAI key detected" in capsys.readouterr().out
