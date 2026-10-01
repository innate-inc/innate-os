# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pinned skill models use the robot's server without shipping a private address."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brain_client.robot import llm


@pytest.mark.parametrize(
    "model, explicit, expected",
    [
        ("openai-chat:qwen", "", "http://robot-server/v1"),
        ("openai-chat:qwen", "http://override/v1", "http://override/v1"),
        ("google:gemini-3.5-flash", "", ""),
    ],
)
def test_pinned_skill_server(monkeypatch, model, explicit, expected):
    monkeypatch.setattr(llm, "_robot_default", llm.Llm(base_url="http://robot-server/v1"))
    monkeypatch.setattr(llm, "refresh_keys", lambda: None)
    route = SimpleNamespace(provider=object())
    configure = Mock(return_value=route)
    monkeypatch.setattr(llm, "configure", configure)
    skill_model = llm.Llm(model, base_url=explicit, extra_body="{}")
    assert skill_model.available
    assert configure.call_args.kwargs["base_url"] == expected
    assert configure.call_args.kwargs["extra_body"] == "{}"
    assert skill_model.available
    configure.assert_called_once()
