"""Settings read + write endpoints (GET / POST /settings.json).

Thin adapters over settings_store, served by https_server.py. The webapp owns the
catalog (knobs/defaults/docs); these only report and apply the override values.
Deliberately not on /settings itself: that path is the SPA route for the settings
page, so the API lives at /settings.json to leave it free.
"""

import asyncio

from aiohttp import ContentTypeError, web

DEFAULT_MIC_SOURCE = "browser"  # input_manager_node's own stt_mic_source default


async def settings_get(request: web.Request) -> web.Response:
    """GET /settings.json -> the live override values from config/settings.yaml."""
    import settings_store

    def read():
        return {"overrides": settings_store.read_overrides(), "exists": settings_store.settings_path().is_file()}

    return web.json_response(await asyncio.to_thread(read), headers={"Cache-Control": "no-cache"})


def mic_source() -> str:
    """The saved stt_mic_source — what input_manager_node hears through once it
    has restarted on this file."""
    import settings_store

    node = settings_store.read_overrides().get("input_manager_node")
    params = node.get("ros__parameters") if isinstance(node, dict) else None
    source = params.get("stt_mic_source") if isinstance(params, dict) else None
    return str(source or DEFAULT_MIC_SOURCE)


async def settings_apply(request: web.Request) -> web.Response:
    """POST /settings.json {sets, clears} -> apply each to config/settings.yaml and
    ack {ok, message}. Serialised under settings_store's lock so two tabs can't
    clobber each other's read-modify-write."""
    import settings_store

    try:
        req = await request.json()
        sets = req.get("sets", []) or []
        clears = req.get("clears", []) or []
    except (ContentTypeError, ValueError, AttributeError, TypeError):
        # ContentTypeError: wrong/missing Content-Type; ValueError: malformed body.
        return web.json_response({"ok": False, "message": "malformed request"}, status=400)
    try:
        ok, message = await asyncio.to_thread(settings_store.apply_changes, sets, clears)
    except Exception as exc:  # noqa: BLE001 — malformed payload, disk error, etc.
        ok, message = False, f"settings update failed: {exc}"
    return web.json_response({"ok": ok, "message": message})
