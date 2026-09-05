"""Provider parity and multi-turn Gemini function-call contract."""

import copy
import json
import time
from contextlib import contextmanager

import pytest
from innate_skills.arm.cabinet_agent_policy import SYSTEM, TOOL, CabinetPolicy
from innate_skills.arm.cabinet_gemini_transport import GeminiCabinetTransport


def reply():
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "gemini-call",
                            "extra_content": {"google": {"thought_signature": "opaque"}},
                            "function": {
                                "name": "cabinet_action",
                                "arguments": json.dumps(
                                    {
                                        "action": "base_step",
                                        "values": [-0.03, 0, 0],
                                        "note": "Grasp held; continue pulling.",
                                    }
                                ),
                            },
                        }
                    ],
                },
            }
        ]
    }


class Client:
    def __init__(self, data=None):
        self.data = reply() if data is None else data
        self.requests = []
        self.status_code = 200

    def is_available(self):
        return True

    @contextmanager
    def request_stream(self, service, endpoint, **kwargs):
        assert (service, endpoint) == ("gemini", "/v1/chat/completions")
        self.requests.append(copy.deepcopy(kwargs["json"]))
        yield self

    def read(self):
        return json.dumps(self.data).encode()

    def close(self):
        pass


def test_same_observations_tools_and_history_with_gemini_signatures(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = Client()
    policy = CabinetPolicy(model="gemini-3.8-flash", transport=GeminiCabinetTransport(client))
    for step in range(3):
        call, action = policy.decide(
            {"step": step, "grip_commanded": True}, {"head": "aA==", "wrist": "dw=="}, time.sleep
        )
        assert action[:2] == ("base_step", (-0.03, 0, 0))
        policy.result(call, "Measured 0.028m backward; observe again")
    body = client.requests[-1]
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    assert body["tools"][0]["function"]["parameters"] == TOOL["parameters"]
    assert body["model"] == "gemini-3.8-flash"
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 4096
    assert "service_tier" not in body
    assert body["tool_choice"]["function"]["name"] == "cabinet_action"
    assert sum(p["type"] == "image_url" for m in body["messages"] if m["role"] == "user" for p in m["content"]) == 4
    assistants = [m for m in body["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 2
    assert all(m == reply()["choices"][0]["message"] for m in assistants)
    assert any(m.get("tool_call_id") == "gemini-call" for m in body["messages"])


@pytest.mark.parametrize("mode", ["truncated", "parallel", "no_tool"])
def test_failed_model_output_does_not_produce_action(mode):
    data = reply()
    if mode == "truncated":
        data["choices"][0]["finish_reason"] = "length"
    elif mode == "parallel":
        data["choices"][0]["message"]["tool_calls"] *= 2
    else:
        data["choices"][0]["message"]["tool_calls"] = []
    policy = CabinetPolicy(model="gemini-3.8-flash", transport=GeminiCabinetTransport(Client(data)))
    with pytest.raises((ValueError, RuntimeError)):
        policy.decide({}, {}, time.sleep)
