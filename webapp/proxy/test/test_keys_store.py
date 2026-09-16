# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""keys_store edits .env line by line and never hands a key back. Clobbering .env
would take a robot off the Innate proxy, so the untouched-lines rule is pinned."""

import stat

import keys_store
import pytest
from conftest import make_app_root, serve, sync

TEMPLATE = """# innate-os .env
INNATE_SERVICE_KEY=svc-key-1234567890
# GEMINI_API_KEY=
# OPENAI_API_KEY=
LLM_MODEL=google:gemini-3.6-flash
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in (*keys_store.KEYS, keys_store.SERVICE_KEY, keys_store.KEYS_ENV_FILE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    monkeypatch.setattr(keys_store, "SYSTEM_ENV_PATH", tmp_path / "etc-innate.env")
    path = tmp_path / ".env"
    path.write_text(TEMPLATE)
    return path


def test_set_fills_the_placeholder_in_place_and_touches_nothing_else(env):
    ok, _ = keys_store.apply({"GEMINI_API_KEY": "AIza-gemini-secret-9876"}, [])
    assert ok
    assert env.read_text() == TEMPLATE.replace("# GEMINI_API_KEY=", "GEMINI_API_KEY=AIza-gemini-secret-9876")


def test_set_replaces_an_active_line_and_drops_a_shadowing_duplicate(env):
    env.write_text(TEMPLATE + "OPENAI_API_KEY=old\nOPENAI_API_KEY=older\n")
    ok, _ = keys_store.apply({"OPENAI_API_KEY": "sk-new-key-000000"}, [])
    assert ok
    text = env.read_text()
    assert text.count("OPENAI_API_KEY=") == 2  # the template placeholder line and the one active line
    assert "OPENAI_API_KEY=sk-new-key-000000" in text and "old" not in text


def test_a_key_the_template_never_mentioned_is_appended(env):
    ok, _ = keys_store.apply({"ANTHROPIC_API_KEY": "sk-ant-abcdefgh"}, [])
    assert ok
    assert env.read_text() == TEMPLATE + "ANTHROPIC_API_KEY=sk-ant-abcdefgh\n"


def test_clear_leaves_the_placeholder(env):
    keys_store.apply({"GEMINI_API_KEY": "AIza-gemini-secret-9876"}, [])
    ok, _ = keys_store.apply({}, ["GEMINI_API_KEY"])
    assert ok
    assert env.read_text() == TEMPLATE
    assert not keys_store.read_status()["keys"]["GEMINI_API_KEY"]["set"]


def test_status_carries_a_hint_never_the_value(env):
    keys_store.apply({"GEMINI_API_KEY": "AIza-gemini-secret-9876"}, [])
    status = keys_store.read_status()
    assert status["keys"]["GEMINI_API_KEY"] == {"set": True, "hint": "…9876", "source": "file"}
    assert status["keys"]["ANTHROPIC_API_KEY"] == {"set": False, "hint": "", "source": "file"}
    assert status["service_key"] is True
    assert "secret" not in repr(status)


def test_the_service_key_is_seen_in_the_system_env_file(env, monkeypatch):
    env.write_text("# GEMINI_API_KEY=\n")
    keys_store.SYSTEM_ENV_PATH.write_text("INNATE_SERVICE_KEY=provisioned\n")
    assert keys_store.read_status()["service_key"] is True


def test_unknown_names_and_unkeylike_values_are_refused(env):
    assert keys_store.apply({"INNATE_SERVICE_KEY": "x"}, [])[0] is False  # provisioned, not typed
    assert keys_store.apply({"GEMINI_API_KEY": "  "}, [])[0] is False
    assert keys_store.apply({"GEMINI_API_KEY": 'a"b'}, [])[0] is False
    assert env.read_text() == TEMPLATE


def test_a_value_the_loader_would_misread_is_quoted(env):
    keys_store.apply({"LLM_API_KEY": "with space#hash"}, [])
    assert 'LLM_API_KEY="with space#hash"' in env.read_text()
    assert keys_store.read_status()["keys"]["LLM_API_KEY"]["hint"] == "…hash"


def test_a_new_env_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path / "fresh"))
    monkeypatch.setattr(keys_store, "SYSTEM_ENV_PATH", tmp_path / "none")
    ok, _ = keys_store.apply({"OPENAI_API_KEY": "sk-abcdefghij"}, [])
    assert ok
    path = tmp_path / "fresh" / ".env"
    assert path.read_text() == "OPENAI_API_KEY=sk-abcdefghij\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@sync
async def test_routes_report_and_write_but_never_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    monkeypatch.setattr(keys_store, "SYSTEM_ENV_PATH", tmp_path / "none")
    async with serve(ROOT=make_app_root(tmp_path)) as (session, base):
        r = await session.post(base + "/keys.json", json={"sets": {"ANTHROPIC_API_KEY": "sk-ant-secret-1234"}})
        assert (await r.json())["ok"] is True
        r = await session.get(base + "/keys.json")
        body = await r.json()
        assert body["keys"]["ANTHROPIC_API_KEY"] == {"set": True, "hint": "…1234", "source": "file"}
        assert body["readonly"] is False and "secret" not in await r.text()


@sync
async def test_the_readonly_demo_reports_keys_but_takes_none(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    monkeypatch.setattr(keys_store, "SYSTEM_ENV_PATH", tmp_path / "none")
    async with serve(ROOT=make_app_root(tmp_path), WEBAPP_READONLY=True) as (session, base):
        assert (await (await session.get(base + "/keys.json")).json())["readonly"] is True
        r = await session.post(base + "/keys.json", json={"sets": {"OPENAI_API_KEY": "sk-x"}})
        assert r.status in (404, 405)
        assert not (tmp_path / ".env").exists()


def test_an_identifier_is_reported_in_full_while_keys_never_are(env):
    keys_store.apply({"ANTHROPIC_WORKSPACE_ID": "wrkspc_01ABCDEF", "ANTHROPIC_API_KEY": "sk-ant-secret-1234"}, [])
    keys = keys_store.read_status()["keys"]
    assert keys["ANTHROPIC_WORKSPACE_ID"] == {
        "set": True,
        "hint": "wrkspc_01ABCDEF",
        "source": "file",
    }  # an id, not a credential
    assert keys["ANTHROPIC_API_KEY"] == {"set": True, "hint": "…1234", "source": "file"}


def test_a_bind_mounted_env_is_written_through_since_no_rename_can_replace_it(env, monkeypatch):
    # The sim mounts the host's .env onto this path; os.replace onto a mount point is EBUSY.
    monkeypatch.setattr(keys_store.os, "replace", _refuse)
    ok, _ = keys_store.apply({"ANTHROPIC_API_KEY": "sk-ant-through-the-mount"}, [])
    assert ok
    assert env.read_text() == TEMPLATE + "ANTHROPIC_API_KEY=sk-ant-through-the-mount\n"
    assert not list(env.parent.glob(".env.*.tmp"))  # the scratch file never survives


def _refuse(*_args, **_kwargs):
    raise OSError(16, "Device or resource busy")


def test_a_write_that_is_not_a_bind_mount_failure_leaves_the_file_alone(env, monkeypatch):
    # The fallback exists for EBUSY on a mount point; a full disk or a denied write must not
    # reach it, or a failed key save would truncate the service key with it.
    def deny(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(keys_store.os, "replace", deny)
    ok, message = keys_store.apply({"GEMINI_API_KEY": "AIza-would-be-lost"}, [])
    assert ok is False and "No space left" in message
    assert env.read_text() == TEMPLATE  # every existing key still there


def test_keys_passed_as_environment_count_as_the_public_demo_passes_them(env, monkeypatch):
    env.unlink()  # the demo image has no .env at all
    monkeypatch.setenv("INNATE_SERVICE_KEY", "isk-demo-key-0001")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-demo-9999")
    status = keys_store.read_status()
    assert status["service_key"] is True
    assert status["keys"]["ANTHROPIC_API_KEY"] == {"set": True, "hint": "…9999", "source": "environment"}
    assert not status["keys"]["GEMINI_API_KEY"]["set"]
    # The file cannot take away what the environment gave: say so instead of a false success.
    ok, message = keys_store.apply({}, ["ANTHROPIC_API_KEY"])
    assert not ok and "environment" in message
    env.write_text("ANTHROPIC_API_KEY=sk-ant-file-1234\n")
    assert keys_store.read_status()["keys"]["ANTHROPIC_API_KEY"]["source"] == "file"
    assert keys_store.apply({}, ["ANTHROPIC_API_KEY"])[0]
