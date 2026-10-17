"""The parser cases that have actually bitten, as a regression test.

Every case here is a real reply shape observed from a model, not an invented
one. The first is the one that made a well-formed answer look like an agent
failure.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import pytest
from backends import _coerce, _last_json_object

CASES = [
    (
        "braces inside the escaped args string",
        'codex\n{"action":"say","args":"{\\"text\\":\\"I cannot see anything, so I cannot count the cups.\\"}"}\ntokens used\n1234',
        {"action": "say", "args": {"text": "I cannot see anything, so I cannot count the cups."}},
    ),
    (
        "plain nested object instead of a string",
        '{"action":"forward","args":{"metres":0.8}}',
        {"action": "forward", "args": {"metres": 0.8}},
    ),
    (
        "preamble containing its own JSON without an action",
        '{"model":"gpt"}\nthinking...\n{"action":"turn","args":"{\\"degrees\\": -45}"}',
        {"action": "turn", "args": {"degrees": -45}},
    ),
    (
        "empty args",
        '{"action":"pick","args":""}',
        {"action": "pick", "args": {}},
    ),
    (
        "a bare number where JSON was asked for",
        '{"action":"turn","args":"90"}',
        {"action": "turn", "args": {"degrees": 90.0, "metres": 90.0}},
    ),
    (
        "uppercase action name",
        '{"action":"FINISH","args":"{}"}',
        {"action": "finish", "args": {}},
    ),
]


@pytest.mark.parametrize(("raw", "want"), [(raw, want) for _, raw, want in CASES], ids=[name for name, _, _ in CASES])
def test_parser_case(raw: str, want: dict) -> None:
    obj = _last_json_object(raw)
    got = _coerce(obj) if obj else None
    assert got == want


def test_reply_with_no_action_returns_none() -> None:
    assert _last_json_object('{"note":"no action here"}') is None
