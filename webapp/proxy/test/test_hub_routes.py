# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hugging Face publish endpoints: the skill-root fence, the preconditions, and the cross-site guard."""

import hub_publish
import keys_store
import media_routes
import pytest
from conftest import make_app_root, serve, sync


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
async def test_a_page_from_another_site_cannot_publish(tmp_path, robot):
    body = {"dir": str(robot), "repo_id": "me/mars-s"}
    async with serve(ROOT=make_app_root(tmp_path)) as (s, base):
        foreign = {"Origin": "https://evil.example"}
        assert (await s.post(base + "/hub/publish", json=body, headers=foreign)).status == 403
        assert (await s.post(base + "/hub/setup", headers=foreign)).status == 403
        own = await s.post(base + "/hub/publish", json=body, headers={"Origin": base})
        assert own.status == 400 and "token" in (await own.json())["message"]
