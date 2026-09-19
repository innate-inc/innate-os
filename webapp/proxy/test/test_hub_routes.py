# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hugging Face publish endpoints: the skill-root fence, the preconditions the dialog reports,
and a whole job driven by a stand-in converter that speaks the real progress protocol."""

import asyncio
import json
import os
import stat

import hub_publish
import hub_routes
import keys_store
import media_routes
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import make_app_root, serve, sync

CONVERTER_OK = """#!/bin/sh
[ -n "$HF_TOKEN" ] || { echo "no token in the environment"; exit 3; }
echo '{"event": "start", "total": 2}'
echo 'some ordinary log line'
echo '{"event": "episode", "index": 1, "total": 2, "frames": 10}'
sleep 0.3
echo '{"event": "episode", "index": 2, "total": 2, "frames": 12}'
echo '{"event": "push"}'
echo '{"event": "done", "url": "https://huggingface.co/datasets/me/mars-s", "message": "Published 2 episodes"}'
"""
CONVERTER_FAILS = """#!/bin/sh
echo '{"event": "start", "total": 1}'
echo 'Traceback: the Hub said 403 Forbidden'
exit 1
"""


@pytest.fixture
def robot(tmp_path, monkeypatch):
    """A skills root with one recorded skill, a keys file, and no job left over from another test."""
    skills = tmp_path / "skills"
    skill = skills / "s"
    (skill / "data").mkdir(parents=True)
    monkeypatch.setattr(media_routes, "SKILLS_ROOTS", (skills.resolve(),))
    monkeypatch.setenv(keys_store.KEYS_ENV_FILE, str(tmp_path / ".env"))
    monkeypatch.setattr(keys_store, "SYSTEM_ENV_PATH", tmp_path / "no-system.env")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv(hub_publish.VENV_ENV, str(tmp_path / "venv"))
    monkeypatch.setattr(hub_publish, "_job", None)
    return skill


def install_converter(tmp_path, script: str) -> None:
    path = tmp_path / "venv" / "bin" / "mars2lerobot"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


async def wait_for_job(session, base, skill) -> dict:
    for _ in range(100):
        status = await (await session.get(base + "/hub/publish", params={"dir": str(skill)})).json()
        if status["job"] and not status["job"]["running"]:
            return status["job"]
        await asyncio.sleep(0.05)
    raise AssertionError("job never finished")


@sync
async def test_status_reports_what_is_missing(tmp_path, robot):
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        status = await (await s.get(base + "/hub/publish", params={"dir": str(robot)})).json()
        assert status == {"readonly": False, "token": False, "env_ready": False, "job": None, "published": None}
        outside = await (await s.get(base + "/hub/publish", params={"dir": "/etc"})).json()
        assert outside["published"] is None


@sync
async def test_publish_refuses_until_everything_is_in_place(tmp_path, robot):
    body = {"dir": str(robot), "repo_id": "me/mars-s"}
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        assert (await s.post(base + "/hub/publish", json={**body, "dir": "/etc"})).status == 404
        assert (await s.post(base + "/hub/publish", json={**body, "dir": f"{robot}/../../../etc"})).status == 404
        assert (await s.post(base + "/hub/publish", json={**body, "repo_id": "no-owner"})).status == 400
        assert (await s.post(base + "/hub/publish", data="not json")).status == 400
        no_token = await s.post(base + "/hub/publish", json=body)
        assert no_token.status == 400 and "token" in (await no_token.json())["message"]
        keys_store.apply({"HF_TOKEN": "hf_test"}, [])
        no_env = await s.post(base + "/hub/publish", json=body)
        assert no_env.status == 409 and "not installed" in (await no_env.json())["message"]


@sync
async def test_a_publish_job_runs_to_done_and_reports_progress(tmp_path, robot):
    install_converter(tmp_path, CONVERTER_OK)
    keys_store.apply({"HF_TOKEN": "hf_test"}, [])
    (robot / "data" / "dataset_metadata.json").write_text(
        json.dumps({"lerobot_export": {"repo_id": "me/mars-s", "episode_ids": [0, 1], "pushed": True}})
    )
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        started = await s.post(base + "/hub/publish", json={"dir": str(robot), "repo_id": "me/mars-s", "private": True})
        assert started.status == 202
        again = await s.post(base + "/hub/publish", json={"dir": str(robot), "repo_id": "me/mars-s"})
        assert again.status == 409
        job = await wait_for_job(s, base, robot)
        assert job["stage"] == "done" and job["progress"] == 1.0 and job["error"] == ""
        assert job["url"] == "https://huggingface.co/datasets/me/mars-s"
        assert job["episode"] == 2 and job["total"] == 2
        status = await (await s.get(base + "/hub/publish", params={"dir": str(robot)})).json()
        assert status["token"] and status["env_ready"]
        assert status["published"] == {"repo_id": "me/mars-s", "episodes": 2, "pushed": True}
        assert "hf_test" not in json.dumps(status)


@sync
async def test_a_failing_job_surfaces_the_converters_last_words(tmp_path, robot):
    install_converter(tmp_path, CONVERTER_FAILS)
    keys_store.apply({"HF_TOKEN": "hf_test"}, [])
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        assert (await s.post(base + "/hub/publish", json={"dir": str(robot), "repo_id": "me/mars-s"})).status == 202
        job = await wait_for_job(s, base, robot)
        assert job["stage"] == "error" and "403 Forbidden" in job["error"]


@sync
async def test_whoami_lists_the_account_and_writable_orgs(tmp_path, robot, monkeypatch):
    async def fake_hub(request):
        if request.headers.get("Authorization") != "Bearer hf_good":
            return web.json_response({"error": "Invalid credentials"}, status=401)
        orgs = [{"name": "innate-inc", "roleInOrg": "write"}, {"name": "read-only-org", "roleInOrg": "read"}]
        return web.json_response({"name": "daviddobas", "orgs": orgs})

    hub = web.Application()
    hub.router.add_get("/api/whoami-v2", fake_hub)
    upstream = TestServer(hub)
    await upstream.start_server()
    monkeypatch.setattr(hub_routes, "WHOAMI_URL", f"http://127.0.0.1:{upstream.port}/api/whoami-v2")
    try:
        async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
            assert (await (await s.get(base + "/hub/whoami")).json())["ok"] is False  # no token yet
            keys_store.apply({"HF_TOKEN": "hf_bad"}, [])
            rejected = await (await s.get(base + "/hub/whoami")).json()
            assert rejected["ok"] is False and "rejected" in rejected["message"]
            keys_store.apply({"HF_TOKEN": "hf_good"}, [])
            account = await (await s.get(base + "/hub/whoami")).json()
            assert account == {"ok": True, "name": "daviddobas", "orgs": ["innate-inc"]}
    finally:
        await upstream.close()


FAKE_UV = """#!/bin/sh
echo "$@" >> "$UV_LOG"
"""
LOCK = """version = 1

[[package]]
name = "lerobot"
version = "0.6.1"

[[package]]
name = "nvidia-cublas"
version = "13.1.0.3"

[[package]]
name = "torch"
version = "2.11.0"

[[package]]
name = "torchvision"
version = "0.26.0"

[[package]]
name = "triton"
version = "3.6.0"
"""


@sync
async def test_setup_installs_the_lean_environment_with_cpu_torch(tmp_path, robot, monkeypatch):
    uv = tmp_path / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    lerobot_dir = tmp_path / "lerobot"
    lerobot_dir.mkdir()
    (lerobot_dir / "uv.lock").write_text(LOCK)
    monkeypatch.setenv("UV_LOG", str(tmp_path / "uv.log"))
    monkeypatch.setattr(hub_publish, "LEROBOT_DIR", lerobot_dir)
    monkeypatch.setattr(hub_publish, "_uv_command", lambda: [str(uv)])
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        assert (await s.post(base + "/hub/setup")).status == 202
        job = await wait_for_job(s, base, robot)
    assert job["kind"] == "setup" and job["stage"] == "done"
    sync_call, pip_call = (tmp_path / "uv.log").read_text().splitlines()
    assert sync_call.startswith("sync --frozen --no-default-groups")
    for skipped in ("nvidia-cublas", "triton", "torch", "torchvision"):
        assert f"--no-install-package {skipped}" in sync_call
    assert "--no-install-package lerobot" not in sync_call
    assert "download.pytorch.org/whl/cpu torch==2.11.0 torchvision==0.26.0" in pip_call


@sync
async def test_a_readonly_webapp_cannot_publish(tmp_path, robot):
    async with serve(ROOT=make_app_root(tmp_path), WEBAPP_READONLY=True) as (s, base):
        status = await (await s.get(base + "/hub/publish", params={"dir": str(robot)})).json()
        assert status["readonly"] is True
        assert (await s.post(base + "/hub/publish", json={"dir": str(robot), "repo_id": "me/x"})).status in (404, 405)
        assert (await s.post(base + "/hub/setup")).status in (404, 405)


def test_token_value_is_layered_like_the_status(tmp_path, robot, monkeypatch):
    assert keys_store.value("HF_TOKEN") == ""
    monkeypatch.setenv("HF_TOKEN", "from-environment")
    assert keys_store.value("HF_TOKEN") == "from-environment"
    keys_store.apply({"HF_TOKEN": "from-file"}, [])
    assert keys_store.value("HF_TOKEN") == "from-file"
    assert keys_store.value("PATH") == "" and os.environ.get("PATH")
