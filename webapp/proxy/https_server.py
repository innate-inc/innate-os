#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""HTTPS front door for the Innate webapp.

Serves the static app AND proxies /ws to the local rosbridge (rws) over one
TLS port, so a single self-signed certificate acceptance gives the browser a
secure origin for everything — which is what unlocks WebSerial (leader-arm
teleop) without serving the app from the operator's laptop.

The read-only episode/run media endpoints live in media_routes.py and the
settings read/write endpoints in settings_routes.py; this module is the server
itself (TLS, static, routing, the /ws relay), built on aiohttp. It serves the
same app on both a TLS port (HTTPS) and a cleartext port (HTTP). The
secure-origin features (WebSerial leader-arm) need HTTPS; the arm panel offers a
one-click switch rather than an automatic bounce. A self-signed certificate is
generated on first run (10 years) under ~/.innate-webapp-tls/ via openssl.

Static files are served with aiohttp's FileResponse: a single stat() yields the
mtime+size ETag, a matching If-None-Match returns a bodyless 304 before the file
is read, and Range is honoured. Everything is no-cache — the browser revalidates
every load, but revalidation is a cheap conditional request. The sim 3D assets come off
container-image layers, whose mtimes are whatever the build produced; the host-side
geometry is stamped with one install mtime (see ensure_sim_assets) so the validator does
not degrade to size-only. Text assets are gzipped on the way out and the bytes kept in a
small mtime-keyed cache, so the zero-build app ships ~4x fewer bytes without a build step
(see _gzipped). The dynamic JSON in media_routes (joints, run logs) is still uncompressed —
it needs its own path, since there is no file identity to key a cache on.

Run:        python3 proxy/https_server.py        # https://<robot>:443 + http://<robot>:80
Persist:    launched on boot in the `console-webapp` tmux window
            (innate-os/scripts/launch_ros_in_tmux.sh).
"""

import asyncio
import gzip
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import ssl
import struct
import subprocess
import sys
import threading
from pathlib import Path

import aiohttp
from aiohttp import web
from media_routes import (
    episode_response,
    joints_response,
    map_preview_response,
    memory_image_response,
    profile_response,
    run_info_response,
    run_log_response,
    thumb_response,
)
from settings_routes import settings_apply, settings_get

HTTPS_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 443
# Cleartext HTTP listener serving the SAME app as the TLS front door, so the site
# is reachable over http://<robot> too. The secure-origin features (WebSerial
# leader-arm) need HTTPS; the arm panel offers a one-click switch rather than an
# automatic bounce — the self-signed cert means any upgrade costs a warning
# click-through anyway, and the browser caches that acceptance. Native clients
# (the mobile app) that can't accept the self-signed cert also reach /episode*
# here. 0 disables it.
# NOTE: 80/443 are privileged — bind needs root or cap_net_bind_service.
HTTP_PORT = int(os.environ.get("INNATE_HTTP_PORT", "80"))

ROOT = Path(__file__).resolve().parent.parent
# The sim launch sets this so the webapp's sim-only debug controls (Reset
# Position + FPS/queue) surface without editing the committed (robot-default)
# config.json. Overlaid onto /config.json at request time.
WEBAPP_SIM_CONTROLS = os.environ.get("WEBAPP_SIM_CONTROLS", "").strip().lower() in ("1", "true", "yes")
CERT_DIR = Path.home() / ".innate-webapp-tls"
ROSBRIDGE_URL = "ws://127.0.0.1:9090"

# /worldstate -> the sim world server's observer stream: host of
# VIRTUAL_MARS_REMOTE (else in-container), on the port the launcher published.
_WORLD_HOST = os.environ.get("VIRTUAL_MARS_REMOTE", "").strip().partition(":")[0] or "127.0.0.1"
WORLD_STATE_PORT = int(os.environ.get("INNATE_WORLD_STATE_PORT", "").strip() or "8800")
WORLD_STATE_URL = f"ws://{_WORLD_HOST}:{WORLD_STATE_PORT}"
# /sim/reset -> the world server's driver RPC (its --port, default 8799).
WORLD_RPC_PORT = 8799

# Ping both legs of every relay so a peer that vanishes without a FIN (a robot's
# WiFi dropping mid-teleop) is reaped in ~heartbeat seconds instead of lingering
# until the kernel's TCP timeout and leaking upstream rosbridge subscriptions.
# aiohttp derives the pong deadline as heartbeat/2, so this allows 30s: at 20s
# the 10s deadline was closing healthy sockets that share WiFi with the streams.
WS_HEARTBEAT = 60.0

WS_KEEPALIVE = 20.0
_KEEPALIVE_FRAME = json.dumps({"op": "keepalive"})


def _quiet_benign_disconnects() -> None:
    """Drop the traceback aiohttp logs when a client vanishes mid-request —
    browser reloads, cert-warning preconnects, and port scanners routinely reset
    the socket, surfacing as ConnectionResetError under 'aiohttp.server'. Filter
    just those out while keeping every other error."""

    class _DropReset(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            exc = record.exc_info[1] if record.exc_info else None
            return not isinstance(exc, ConnectionResetError)

    logging.getLogger("aiohttp.server").addFilter(_DropReset())


def ensure_cert() -> tuple[Path, Path]:
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip() or "robot"
    ips = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    sans = [f"DNS:{hostname}.local", f"DNS:{hostname}", "DNS:localhost"] + [f"IP:{ip}" for ip in ips if ":" not in ip]
    openssl_cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        "3650",
        "-nodes",
        "-subj",
        f"/CN={hostname}.local",
        "-addext",
        f"subjectAltName={','.join(sans)}",
    ]
    try:
        subprocess.run(openssl_cmd, check=True, capture_output=True)
    except FileNotFoundError:
        # Fail loudly — otherwise the unit just respawns and a "site won't load"
        # symptom hides the real cause (no openssl on PATH).
        print("FATAL: openssl not found — cannot generate the HTTPS certificate.", file=sys.stderr, flush=True)
        raise
    except subprocess.CalledProcessError as exc:
        # capture_output swallows openssl's stderr; print it so each restart says why.
        print(
            f"FATAL: openssl failed to generate the HTTPS certificate:\n{exc.stderr.decode(errors='replace')}",
            file=sys.stderr,
            flush=True,
        )
        raise
    key.chmod(0o600)
    print(f"generated self-signed cert for {', '.join(sans)}")
    return cert, key


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".png": "image/png",
    ".md": "text/plain; charset=utf-8",
    ".tgz": "application/gzip",
    ".mp4": "video/mp4",
    # Sim viewer assets.
    ".glb": "model/gltf-binary",
    ".obj": "text/plain; charset=utf-8",
    ".urdf": "application/xml",
    ".stl": "application/octet-stream",
}

# Simulation viewer (sim/viewer): the SimSession bundle plus the 3D assets it
# fetches at their canonical absolute paths. Served only where the directories
# exist -- the image-mounted ones only in the sim container or a dev checkout.
# /robot is the exception: a robot does have the tracked ROS package, so it
# answers there too, with that robot's own description.
SIM_VIEWER_ROOT = ROOT.parent / "sim" / "viewer"
SIM_VIEWER_ROUTES = {
    "/sim-viewer/": SIM_VIEWER_ROOT / "dist-lib",
    "/models/": SIM_VIEWER_ROOT / "public" / "models",
    # scene.ts declares `loader.packages = { mars_sim: "/robot" }`, so
    # `package://mars_sim/meshes/base.STL` resolves here by itself.
    "/robot/": ROOT.parent / "ros2_ws" / "src" / "mars_bot" / "mars_sim",
    # Collision hulls for the SimSession's "collisions" debug overlay.
    "/physics/": SIM_VIEWER_ROOT / "public" / "physics",
}


def _content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _safe_resolve(path: Path) -> "Path | None":
    """Path.resolve() that returns None instead of raising on illegal bytes — a
    percent-decoded NUL in the URL, which aiohttp hands us decoded — so malformed
    input becomes a 404, not a 500 (mirrors media_routes._safe_resolve)."""
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


# Text assets are gzipped on the way out — the app is zero-build, so there are no
# precompressed build siblings to sendfile, and the alternative is shipping ~1.8 MB
# of source per cold load. Compressed bytes are cached in memory keyed by the file's
# identity, so each file is deflated once per edit, not once per request — which is
# why this isn't aiohttp's enable_compression: that re-deflates every response on
# the event loop shared with the /ws teleop relay, and can't give the gzip
# representation its own ETag/304 path.
COMPRESSIBLE = {".js", ".css", ".html", ".json", ".svg", ".md", ".urdf", ".obj", ".stl"}
GZIP_MIN_BYTES = 1024  # below this the gzip framing eats the saving
GZIP_MAX_BYTES = 4 * 1024 * 1024  # a mesh this large stays a sendfile rather than a cache entry
GZIP_CACHE_BUDGET = 24 * 1024 * 1024  # the whole app compresses to well under this; the cap bounds a Jetson's RSS

# (path, mtime_ns, size) -> gzipped bytes. Insertion-ordered, evicted oldest-first.
# _gzipped runs on executor threads (asyncio.to_thread), and a cold load is many
# concurrent misses — the byte counter and the eviction loop race without the lock.
_GZIP_CACHE: "dict[tuple[str, int, int], bytes]" = {}
_gzip_cache_bytes = 0
_gzip_lock = threading.Lock()


def _gzipped(path: Path, stat: os.stat_result) -> bytes:
    """The file's gzipped bytes, from the cache when its identity is unchanged.

    mtime=0 keeps the output byte-identical across calls, so a cache miss after a
    restart cannot change what a revalidating browser is holding."""
    global _gzip_cache_bytes
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _gzip_lock:
        hit = _GZIP_CACHE.get(key)
    if hit is not None:
        return hit
    body = gzip.compress(path.read_bytes(), compresslevel=6, mtime=0)
    with _gzip_lock:
        if key not in _GZIP_CACHE:  # a concurrent miss compressed it too; count it once
            _GZIP_CACHE[key] = body
            _gzip_cache_bytes += len(body)
        while _gzip_cache_bytes > GZIP_CACHE_BUDGET and len(_GZIP_CACHE) > 1:
            _gzip_cache_bytes -= len(_GZIP_CACHE.pop(next(iter(_GZIP_CACHE))))
    return body


def _matches_etag(header: str, etag: str) -> bool:
    return any(t.strip().removeprefix("W/") in (etag, "*") for t in header.split(","))


def _accepts_gzip(header: str) -> bool:
    """Whether Accept-Encoding allows gzip. An explicit gzip token outranks a
    wildcard, and q=0 on the winning token is a rejection — a substring test
    would read `gzip;q=0` as acceptance and serve bytes the client can't decode."""
    q: dict[str, float] = {}
    for token in header.split(","):
        coding, _, params = token.partition(";")
        params = params.strip()
        try:
            weight = float(params[2:]) if params.startswith("q=") else 1.0
        except ValueError:
            weight = 0.0
        q[coding.strip().lower()] = weight
    winner = q.get("gzip", q.get("*"))
    return winner is not None and winner > 0


def _gzip_candidate(path: Path, request: web.Request) -> "os.stat_result | None":
    """The file's stat when this request should be answered gzipped, else None —
    Range wants offsets into the file itself, and everything else FileResponse
    serves better as-is."""
    if path.suffix not in COMPRESSIBLE or "Range" in request.headers:
        return None
    if not _accepts_gzip(request.headers.get("Accept-Encoding", "")):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat if GZIP_MIN_BYTES <= stat.st_size <= GZIP_MAX_BYTES else None


# A vendor filename that names its version, e.g. three.module.min.r160.js — the
# segment before the extension starts with a digit (an optional r/v prefix allowed).
_VENDOR_VERSIONED = re.compile(r"\.[rv]?\d[\w.]*\.\w+$")


async def _serve_static(path: Path, request: web.Request) -> web.StreamResponse:
    """Serve a file gzipped from the cache where that pays, else FileResponse
    (sendfile, native mtime+size ETag, bodyless 304, Range). no-cache except the
    immutable vendor libraries.

    A gzipped body is a different representation of the same file, so it carries
    its own ETag — a client that stops sending Accept-Encoding revalidates against
    the identity ETag and gets the plain file back, never a mislabelled body."""
    # public/vendor holds pinned libraries whose filenames carry their version
    # (three.module.min.r160.js): a bump is a new URL, so browsers may cache
    # these for good without `immutable` ever pinning a stale copy — which is
    # why an unversioned vendor file gets no-cache like everything else (the
    # zero-build app's "deploy" is a file edit).
    vendored = path.is_relative_to(ROOT / "public" / "vendor") and _VENDOR_VERSIONED.search(path.name)
    cache = "public, max-age=31536000, immutable" if vendored else "no-cache"
    headers = {"Content-Type": _content_type(path), "Cache-Control": cache}
    if path.suffix in COMPRESSIBLE:
        # Declared on the identity representation too — the two answers for one
        # URL must agree on Vary or an intermediary can't negotiate them.
        headers["Vary"] = "Accept-Encoding"
    stat = _gzip_candidate(path, request)
    if stat is None:
        return web.FileResponse(path, headers=headers)
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-gz"'
    headers |= {"ETag": etag}
    if _matches_etag(request.headers.get("If-None-Match", ""), etag):
        return web.Response(status=304, headers=headers)
    body = await asyncio.to_thread(_gzipped, path, stat)
    return web.Response(body=body, headers=headers | {"Content-Encoding": "gzip"})


async def static_handler(request: web.Request) -> web.StreamResponse:
    clean = request.path
    target = _safe_resolve(ROOT / clean.lstrip("/"))
    if target is None:
        raise web.HTTPNotFound(text="not found")
    if target.is_dir():
        target = target / "index.html"
    body_path = target if target.is_file() else None
    # SPA fallback: an extensionless route that maps to no file (e.g. /profiling,
    # /settings, or a deep link/refresh on any client-side route) is served the
    # app shell so the router can render it. Asset requests carry a suffix
    # (.js/.css/...), so a genuinely missing asset still 404s below. The
    # in-root guard keeps a traversal like /../secrets a 404, not the shell.
    if body_path is None and target.is_relative_to(ROOT) and "." not in clean.rsplit("/", 1)[-1]:
        body_path = ROOT / "index.html"
    # Refuse anything that escapes the app root (or the TLS keys, defensively).
    if body_path is None or not body_path.is_relative_to(ROOT) or body_path.suffix == ".pem":
        raise web.HTTPNotFound(text="not found")
    return await _serve_static(body_path, request)


async def sim_viewer_handler(request: web.Request) -> web.StreamResponse:
    clean = request.path
    for prefix, base in SIM_VIEWER_ROUTES.items():
        if not clean.startswith(prefix):
            continue
        target = _safe_resolve(base / clean[len(prefix) :])
        if target is None or not target.is_file() or not target.is_relative_to(base.resolve()):
            raise web.HTTPNotFound(text="not found")
        return await _serve_static(target, request)
    raise web.HTTPNotFound(text="not found")


async def config_handler(request: web.Request) -> web.Response:
    """Serve config.json with env-driven feature flags overlaid, so a deployment
    can flip flags without editing the committed file (the sim sets
    WEBAPP_SIM_CONTROLS=1)."""
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
    except Exception:
        cfg = {}
    if WEBAPP_SIM_CONTROLS:
        cfg["simControls"] = True
        # The 3D view prefers a direct loopback socket over this relay.
        cfg["worldStatePort"] = WORLD_STATE_PORT
    return web.json_response(cfg, headers={"Cache-Control": "no-cache"})


async def restart_handler(request: web.Request) -> web.Response:
    """GET /restart -> kick off `innate restart` (same as the CLI) so the robot
    comes back with the latest config/settings.yaml. The restart tears down the
    tmux session this proxy runs in, so we spawn it detached with a brief delay —
    that lets this 200 flush to the browser before the proxy is killed, and the
    systemd restart job completes regardless of the client dying.

    Once detached the restart runs blind (stdout/stderr discarded, no one waits),
    so the 200 can't confirm it succeeded. The one failure we *can* catch up front
    is `innate` not being on PATH — resolve it here and 500 instead of reporting a
    false success, and spawn the absolute path so the detached `bash -c` (which
    sources no rc files) resolves it the same way we just did."""
    if request.headers.get("X-Requested-By", "") != "innate-webapp":
        raise web.HTTPForbidden(text="missing X-Requested-By header")
    innate = shutil.which("innate")
    if innate is None:
        raise web.HTTPInternalServerError(text="restart failed: `innate` not found on PATH")
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep 1; exec {innate} restart"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as err:
        raise web.HTTPInternalServerError(text=f"restart failed: {err}") from err
    return web.json_response({"ok": True}, headers={"Cache-Control": "no-cache"})


# The Arm SDK page (/armsdk) drives the arm over rosbridge like every other
# page; the front door only serves its 3D view the same URDF + STL meshes the
# IK node solves against (the installed mars_sim share), read-only under
# /armsdk/model/.
MARS_MODEL_ROOT = ROOT.parent / "ros2_ws" / "install" / "mars_sim" / "share" / "mars_sim"


def _model_target(tail: str) -> "Path | None":
    """MARS_MODEL_ROOT / tail, with traversal blocked on the *requested* path.

    Deliberately does not resolve() the result. The sim builds the workspace
    with colcon --symlink-install (scripts/validate_sim_ros_install.zsh), which
    installs every model file as a symlink into ros2_ws/src — so resolving and
    then demanding containment 404s the whole model there. The robot installs
    real files (plain colcon build) and never hit it.

    Prefixing "/" before normpath collapses any leading "..", so the join
    cannot escape the base and the symlinks we do follow are the build's own.
    """
    rel = posixpath.normpath("/" + tail).lstrip("/")
    if not rel:
        return None
    try:
        target = MARS_MODEL_ROOT / rel
        return target if target.is_file() else None
    except (OSError, ValueError):  # illegal path bytes (e.g. a decoded NUL)
        return None


async def armsdk_model(request: web.Request) -> web.StreamResponse:
    target = _model_target(request.match_info["tail"])
    if target is None:
        raise web.HTTPNotFound(text="not found")
    return await _serve_static(target, request)


async def _pump(src: "web.WebSocketResponse | aiohttp.ClientWebSocketResponse", dst) -> None:
    """Relay every frame from src to dst until either side closes."""
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        else:  # CLOSE / CLOSING / ERROR — the iterator is about to stop anyway
            break


async def _keepalive(ws: web.WebSocketResponse) -> None:
    """Emit a frame the browser's *JavaScript* can see.

    Ping/pong never reaches page scripts, so without this an idle socket and a
    dead one are indistinguishable to rosClient, which reconnects on the gap.
    """
    while True:
        await asyncio.sleep(WS_KEEPALIVE)
        await ws.send_str(_KEEPALIVE_FRAME)


async def sim_reset(request: web.Request) -> web.Response:
    """Respawn the simulated robot at its spawn pose (sim "reset position").

    Talks the world server's driver RPC (4-byte big-endian length | JSON)
    directly rather than publishing /virtual_mars/reset. Respawning is
    simulator control, not a robot command, and the ROS route is a trap: the
    driver's handler holds its node-wide lock across the very same RPC, so
    every timer and subscription in it stalls until the call returns.
    """
    try:
        reader, writer = await asyncio.open_connection(_WORLD_HOST, WORLD_RPC_PORT)
    except OSError as err:
        return web.json_response({"ok": False, "error": str(err)}, status=503)
    try:
        payload = json.dumps({"op": "reset"}).encode()
        writer.write(struct.pack(">I", len(payload)) + payload)
        await writer.drain()
        (length,) = struct.unpack(">I", await reader.readexactly(4))
        reply = json.loads(await reader.readexactly(length))
    except (OSError, asyncio.IncompleteReadError, ValueError) as err:
        return web.json_response({"ok": False, "error": str(err)}, status=502)
    finally:
        writer.close()
    return web.json_response(reply)


async def ws_proxy(request: web.Request) -> web.WebSocketResponse:
    """Bidirectional relay: /ws <-> rosbridge, /worldstate <-> the sim world
    server's observer stream. max_msg_size=0 lifts aiohttp's default cap for the
    large point-cloud / world-state frames."""
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=WS_HEARTBEAT)
    await ws.prepare(request)
    worldstate = request.path == "/worldstate"
    upstream_url = WORLD_STATE_URL if worldstate else ROSBRIDGE_URL
    session = request.app[CLIENT]
    try:
        async with session.ws_connect(upstream_url, max_msg_size=0, heartbeat=WS_HEARTBEAT) as upstream:
            tasks = [asyncio.create_task(_pump(ws, upstream)), asyncio.create_task(_pump(upstream, ws))]
            # Not /worldstate: the sim viewer parses only world state.
            if not worldstate:
                tasks.append(asyncio.create_task(_keepalive(ws)))
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            # gather ALL tasks (finished + cancelled), so a pump whose send failed
            # on a peer reset doesn't surface as "exception never retrieved".
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as err:  # upstream down — close the client politely
        print(f"ws relay ended: {err}")
    finally:
        await ws.close()
    return ws


# One shared client for the WS-proxy upstreams, keyed with a typed AppKey.
CLIENT = web.AppKey("client", aiohttp.ClientSession)


async def _on_startup(app: web.Application) -> None:
    app[CLIENT] = aiohttp.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    await app[CLIENT].close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/sim/reset", sim_reset)
    app.router.add_get("/ws", ws_proxy)
    app.router.add_get("/worldstate", ws_proxy)
    app.router.add_get("/config.json", config_handler)
    app.router.add_get("/episode", episode_response)
    app.router.add_get("/episode/joints", joints_response)
    app.router.add_get("/episode/profile", profile_response)
    app.router.add_get("/episode/thumb", thumb_response)
    app.router.add_get("/map/preview", map_preview_response)
    app.router.add_get("/memory/image", memory_image_response)
    app.router.add_get("/run/info", run_info_response)
    app.router.add_get("/run/log", run_log_response)
    app.router.add_get("/settings.json", settings_get)
    app.router.add_post("/settings.json", settings_apply)
    app.router.add_get("/restart", restart_handler)
    # Before the catch-all; the bare /armsdk page route stays on the SPA shell.
    app.router.add_get("/armsdk/model/{tail:.*}", armsdk_model)
    # Prefix routes must precede the catch-all so /models/foo.glb doesn't fall to
    # the SPA shell — first matching resource wins in add order.
    for prefix in SIM_VIEWER_ROUTES:
        app.router.add_get(prefix + "{tail:.*}", sim_viewer_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def main() -> None:
    _quiet_benign_disconnects()
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=ctx).start()
    print(
        f"https front door on https://0.0.0.0:{HTTPS_PORT} "
        f"(app + /ws -> {ROSBRIDGE_URL} + /worldstate -> {WORLD_STATE_URL})"
    )
    if HTTP_PORT:
        # Same app over cleartext — no auto-upgrade; the arm panel offers a manual HTTPS switch.
        await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
        print(f"http listener on http://0.0.0.0:{HTTP_PORT} (full app)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
