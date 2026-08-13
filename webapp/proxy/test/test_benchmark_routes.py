# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""/benchmark reverse proxy: sim-gated, forwards verbatim, fails as 502."""

import contextlib

from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import make_app_root, serve, sync


@contextlib.asynccontextmanager
async def fake_benchmark_upstream():
    async def health(request):
        return web.json_response({"ok": True, "gemini_key": False})

    async def run(request):
        return web.json_response({"echo": await request.json(), "q": request.query_string})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/api/run", run)
    srv = TestServer(app)
    await srv.start_server()
    try:
        yield f"http://127.0.0.1:{srv.port}"
    finally:
        await srv.close()


@sync
async def test_forwards_get_and_post(tmp_path):
    async with fake_benchmark_upstream() as upstream:
        async with serve(ROOT=make_app_root(tmp_path), WEBAPP_SIM_CONTROLS=True, BENCHMARK_URL=upstream) as (
            session,
            base,
        ):
            resp = await session.get(f"{base}/benchmark/health")
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "gemini_key": False}

            resp = await session.post(f"{base}/benchmark/api/run?x=1", json={"kind": "capture"})
            assert resp.status == 200
            assert (await resp.json()) == {"echo": {"kind": "capture"}, "q": "x=1"}


@sync
async def test_upstream_down_is_502(tmp_path):
    async with serve(ROOT=make_app_root(tmp_path), WEBAPP_SIM_CONTROLS=True, BENCHMARK_URL="http://127.0.0.1:9") as (
        session,
        base,
    ):
        resp = await session.get(f"{base}/benchmark/health")
        assert resp.status == 502
        assert "unreachable" in (await resp.json())["error"]


@sync
async def test_absent_on_robot(tmp_path):
    """Without the sim flag the path falls through to the SPA shell — the
    robot webapp exposes no /benchmark surface at all."""
    async with serve(ROOT=make_app_root(tmp_path), WEBAPP_SIM_CONTROLS=False) as (session, base):
        resp = await session.get(f"{base}/benchmark/health")
        assert resp.status == 200
        assert "SHELL" in (await resp.text())
