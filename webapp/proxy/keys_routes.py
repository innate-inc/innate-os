# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""API-key endpoints (GET / POST /keys.json), thin adapters over keys_store.

GET reports set/not-set per key and whether the Innate service key is present;
POST sets or clears keys. The POST route is only registered when the webapp is
writable (not the public demo); GET says so, so the page can hide the fields.
"""

import asyncio

from aiohttp import ContentTypeError, web


async def keys_get(request: web.Request) -> web.Response:
    import keys_store

    status = await asyncio.to_thread(keys_store.read_status)
    status["readonly"] = bool(request.app.get("readonly"))
    return web.json_response(status, headers={"Cache-Control": "no-cache"})


async def keys_apply(request: web.Request) -> web.Response:
    import keys_store

    try:
        req = await request.json()
        sets = req.get("sets", {}) or {}
        clears = req.get("clears", []) or []
        if not isinstance(sets, dict) or not isinstance(clears, list):
            raise ValueError
    except (ContentTypeError, ValueError, AttributeError, TypeError):
        return web.json_response({"ok": False, "message": "malformed request"}, status=400)
    try:
        ok, message = await asyncio.to_thread(keys_store.apply, sets, clears)
    except Exception as exc:  # noqa: BLE001 — disk error, etc.
        ok, message = False, f"key update failed: {exc}"
    return web.json_response({"ok": ok, "message": message})
