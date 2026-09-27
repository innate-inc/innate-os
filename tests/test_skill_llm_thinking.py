"""Check skill reasoning settings at the provider request boundary, without ROS."""

import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws/src/cloud/clients/innate-llm"))
from innate_llm import Thinking  # noqa: E402


@pytest.mark.parametrize("thinking", [Thinking.DEFAULT, Thinking.LOW])
def test_skill_thinking_reaches_provider(monkeypatch, thinking):
    config = ModuleType("mars_bringup.config_loader")
    config.keys_env_path = lambda: None
    config.parse_key_value_env = lambda _: {}
    types = ModuleType("brain_client.skills.types")
    types.cancellable_sleep = lambda _: None
    monkeypatch.setitem(sys.modules, "mars_bringup.config_loader", config)
    monkeypatch.setitem(sys.modules, "brain_client.skills.types", types)
    spec = importlib.util.spec_from_file_location(
        "skill_llm_under_test", ROOT / "ros2_ws/src/brain/brain_client/brain_client/robot/llm.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = []

    def run(request, **kwargs):
        captured.append(request)
        return SimpleNamespace(message=SimpleNamespace(text=lambda: "seen"))

    kwargs = {} if thinking == Thinking.DEFAULT else {"thinking": thinking}
    llm = module.Llm("google:gemini-3.8-flash", **kwargs)
    llm._route = SimpleNamespace(provider=SimpleNamespace(run=run))
    assert llm.ask(base64.b64encode(b"jpeg").decode(), "Find the LEGO") == "seen"
    assert captured[0].thinking == thinking
    assert captured[0].temperature == 0.0
