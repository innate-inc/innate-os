# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hugging Face endpoints for the Datasets page, thin adapters over hub_publish.

GET  /hub/publish?dir=…  what the publish dialog needs: token set, environment ready, the
                         running job, what was published before.
GET  /hub/whoami         the token's account and the organizations it can write to.
POST /hub/publish        start publishing a skill.    (not registered on a read-only webapp)
POST /hub/setup          install the LeRobot environment, once.      (same)
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp
import hub_publish
from aiohttp import ContentTypeError, web
from media_routes import _resolve_under_root

HF_TOKEN = "HF_TOKEN"
WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
_NO_CACHE = {"Cache-Control": "no-cache"}


def _token() -> str:
    import keys_store

    return keys_store.value(HF_TOKEN)


async def publish_status(request: web.Request) -> web.Response:
    skill_dir = _resolve_under_root(request.query.get("dir", ""))
    token = await asyncio.to_thread(_token)
    return web.json_response(
        {
            "readonly": bool(request.app.get("readonly")),
            "token": bool(token),
            "env_ready": hub_publish.env_ready(),
            "job": hub_publish.current_job(),
            "published": hub_publish.published(skill_dir) if skill_dir is not None else None,
        },
        headers=_NO_CACHE,
    )


async def whoami(_request: web.Request) -> web.Response:
    token = await asyncio.to_thread(_token)
    if not token:
        return web.json_response({"ok": False, "message": "no Hugging Face token saved"}, headers=_NO_CACHE)
    try:
        # Its own session: this server runs as __main__, so https_server's shared-session key
        # imported from here would be a different object than the one the app was built with.
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
            session.get(WHOAMI_URL, headers={"Authorization": f"Bearer {token}"}) as response,
        ):
            if response.status == 401:
                return web.json_response({"ok": False, "message": "Hugging Face rejected the token"}, headers=_NO_CACHE)
            response.raise_for_status()
            account = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.json_response({"ok": False, "message": f"could not reach Hugging Face: {e}"}, headers=_NO_CACHE)
    orgs = [org["name"] for org in account.get("orgs", []) if org.get("roleInOrg") in ("admin", "write", "contributor")]
    return web.json_response({"ok": True, "name": account.get("name", ""), "orgs": orgs}, headers=_NO_CACHE)


async def publish_start(request: web.Request) -> web.Response:
    if _cross_site(request):
        return _refuse(403, "cross-site request refused")
    try:
        body = await request.json()
        skill_dir = _resolve_under_root(str(body["dir"]))
        repo_id = str(body["repo_id"]).strip()
        private = bool(body.get("private", True))
        include_failures = bool(body.get("include_failures", False))
    except (ContentTypeError, ValueError, KeyError, TypeError):
        return _refuse(400, "malformed request")
    if skill_dir is None or not (skill_dir / "data").is_dir():
        return _refuse(404, "no such dataset on this robot")
    if not hub_publish.REPO_ID_RE.match(repo_id):
        return _refuse(400, "the repository must look like owner/name")
    token = await asyncio.to_thread(_token)
    if not token:
        return _refuse(400, "save a Hugging Face token in Settings first")
    if not hub_publish.env_ready():
        return _refuse(409, "the LeRobot environment is not installed yet")
    try:
        hub_publish.start_publish(skill_dir, repo_id, private=private, include_failures=include_failures, token=token)
    except hub_publish.Busy:
        return _refuse(409, "another upload is still running")
    return web.json_response({"ok": True}, status=202)


async def setup_start(request: web.Request) -> web.Response:
    if _cross_site(request):
        return _refuse(403, "cross-site request refused")
    if not hub_publish.LEROBOT_DIR.is_dir():
        return _refuse(404, "this innate-os checkout has no lerobot/ folder; update the robot first")
    try:
        hub_publish.start_setup()
    except hub_publish.Busy:
        return _refuse(409, "another job is still running")
    return web.json_response({"ok": True}, status=202)


def _cross_site(request: web.Request) -> bool:
    """True for a browser POST from another site's page: request.json() takes any content type,
    so without this a page the operator merely visits could publish with the robot's token."""
    origin = request.headers.get("Origin")
    return origin is not None and urlsplit(origin).netloc != request.host


def _refuse(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "message": message}, status=status)
