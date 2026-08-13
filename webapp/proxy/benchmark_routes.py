"""Reverse proxy for the sim's host-side benchmark service.

/benchmark/{tail} forwards to the service the launcher runs next to the world
server (spatial-memory benchmark panel). Registered only when
WEBAPP_SIM_CONTROLS is set — a robot's webapp never exposes the path.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web

BENCHMARK_PORT = 8801
_HOP_HEADERS = ("Connection", "Keep-Alive", "Transfer-Encoding", "Content-Length")


def benchmark_url(world_host: str) -> str:
    return f"http://{world_host}:{BENCHMARK_PORT}"


def make_benchmark_proxy(base_url: str, client_key: web.AppKey[aiohttp.ClientSession]):
    async def benchmark_proxy(request: web.Request) -> web.Response:
        upstream = f"{base_url}/{request.match_info['tail']}"
        if request.query_string:
            upstream += f"?{request.query_string}"
        session = request.app[client_key]
        body = await request.read() if request.can_read_body else None
        try:
            async with session.request(request.method, upstream, data=body) as resp:
                payload = await resp.read()
                headers = {k: v for k, v in resp.headers.items() if k not in _HOP_HEADERS}
                return web.Response(status=resp.status, body=payload, headers=headers)
        except aiohttp.ClientError:
            return web.json_response({"error": "benchmark service unreachable"}, status=502)

    return benchmark_proxy
