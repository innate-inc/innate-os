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

In simulator mode, environment switching crosses a narrow file control plane:
the proxy may create one bounded request in a writable mailbox and may only
read the controller's catalog/job snapshots from a separate read-only mount.
It never receives a Docker socket or shells out on behalf of those routes.

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
import errno
import gzip
import json
import logging
import math
import mimetypes
import os
import posixpath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import NoReturn
from urllib.parse import urlsplit

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
ACTIVE_ENVIRONMENT_PATH = ROOT.parent / "sim" / "assets" / ".active-environment.json"
SIM_VIEWER_ROUTES = {
    "/sim-viewer/": SIM_VIEWER_ROOT / "dist-lib",
    "/models/": SIM_VIEWER_ROOT / "public" / "models",
    # scene.ts declares `loader.packages = { mars_sim: "/robot" }`, so
    # `package://mars_sim/meshes/base.STL` resolves here by itself.
    "/robot/": ROOT.parent / "ros2_ws" / "src" / "mars_bot" / "mars_sim",
    # Collision hulls for the SimSession's "collisions" debug overlay.
    "/physics/": SIM_VIEWER_ROOT / "public" / "physics",
}

ENVIRONMENT_NO_STORE = {"Cache-Control": "no-store, max-age=0"}

# The host launcher owns the simulator lifecycle. The webapp gets a deliberately
# tiny filesystem control plane instead of access to Docker or a host shell:
# requests is the one writable bind mount, status is a separate read-only one.
SIM_ENVIRONMENT_REQUESTS_DIR = Path("/run/innate-sim-control/requests")
SIM_ENVIRONMENT_STATUS_DIR = Path("/run/innate-sim-control/status")
SIM_ENVIRONMENT_TIME = time.time
SIM_ENVIRONMENT_UUID = uuid.uuid4

ENVIRONMENT_ID_RE = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SWITCH_STATES = {"queued", "running", "ready", "failed"}
SWITCH_PHASES = {
    "queued",
    "validating",
    "stopping_runtime",
    "activating",
    "starting_physics",
    "starting_ros",
    "waiting_ros",
    "waiting_sim",
    "rolling_back",
    "ready",
    "failed",
}
CONTROL_REQUEST_MAX_BYTES = 4096
CONTROL_STATUS_MAX_BYTES = 64 * 1024
CONTROL_HEARTBEAT_MAX_AGE_S = 5.0
CONTROL_REQUEST_MAX_AGE_S = 300.0
CONTROL_JOB_STATUS_GRACE_S = 2.0


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


def _environment_asset_path(value: object, *, expect_directory: bool) -> "Path | None":
    """Resolve one descriptor path inside sim/viewer/public, fail closed."""
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        return None
    public = _safe_resolve(SIM_VIEWER_ROOT / "public")
    target = _safe_resolve(SIM_VIEWER_ROOT / "public" / Path(*relative.parts))
    if public is None or target is None or not target.is_relative_to(public):
        return None
    exists = target.is_dir() if expect_directory else target.is_file()
    return target if exists else None


def _load_active_environment() -> "tuple[dict[str, object], dict[str, Path]] | None":
    """Validated public descriptor plus its resolved viewer assets."""
    try:
        descriptor = json.loads(ACTIVE_ENVIRONMENT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
        return None
    if not isinstance(descriptor.get("id"), str) or not isinstance(descriptor.get("fingerprint"), str):
        return None
    viewer = descriptor.get("viewer")
    if not isinstance(viewer, dict):
        return None
    collision_dir = _environment_asset_path(viewer.get("collision_dir"), expect_directory=True)
    if collision_dir is None:
        return None

    resolved = {"collision_dir": collision_dir}
    viewer_type = viewer.get("type")
    if viewer_type == "glb":
        model = _environment_asset_path(viewer.get("model"), expect_directory=False)
        if model is None:
            return None
        resolved["model"] = model
    elif viewer_type == "split-glb":
        manifest = _environment_asset_path(viewer.get("manifest"), expect_directory=False)
        base_dir = _environment_asset_path(viewer.get("base_dir"), expect_directory=True)
        if manifest is None or base_dir is None:
            return None
        resolved["manifest"] = manifest
        resolved["base_dir"] = base_dir
    else:
        return None
    return descriptor, resolved


def _raise_environment_unavailable() -> NoReturn:
    """Keep an old proxy's missing route distinct from a broken active pack.

    A new simulator proxy knows that the generic route exists. If its selected
    descriptor or referenced assets are unavailable, report a retryable service
    failure so a new viewer cannot mistake that condition for the legacy proxy
    contract and silently load the apartment instead.
    """
    if WEBAPP_SIM_CONTROLS:
        raise web.HTTPServiceUnavailable(
            text="active simulator environment unavailable",
            headers=ENVIRONMENT_NO_STORE,
        )
    raise web.HTTPNotFound(text="not found", headers=ENVIRONMENT_NO_STORE)


def _bind_environment_fingerprint(request: web.Request, fingerprint: str) -> None:
    """Bind an unversioned URL once; reject a URL bound to an old pack.

    Redirecting a stale URL to the latest descriptor would let a page that
    already loaded pack A's room layout attach pack B's meshes mid-stream.
    """
    requested = request.query.get("fingerprint")
    if requested == fingerprint:
        return
    if requested is not None:
        raise web.HTTPPreconditionFailed(text="simulator environment changed", headers=ENVIRONMENT_NO_STORE)
    query = dict(request.query)
    query["fingerprint"] = fingerprint
    raise web.HTTPTemporaryRedirect(
        location=str(request.rel_url.with_query(query)),
        headers=ENVIRONMENT_NO_STORE,
    )


async def sim_environment_manifest(request: web.Request) -> web.Response:
    loaded = _load_active_environment()
    if loaded is None:
        _raise_environment_unavailable()
    descriptor, _resolved = loaded
    return web.json_response(descriptor, headers=ENVIRONMENT_NO_STORE)


async def sim_environment_asset(request: web.Request) -> web.StreamResponse:
    loaded = _load_active_environment()
    if loaded is None:
        _raise_environment_unavailable()
    descriptor, resolved = loaded
    fingerprint = descriptor.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise web.HTTPNotFound(text="not found")

    clean = request.path
    target: Path | None = None
    if clean == "/sim-environment/scene.glb":
        target = resolved.get("model")
    elif clean == "/sim-environment/layout.json":
        target = resolved.get("manifest")
    else:
        for prefix, root_key in (
            ("/sim-environment/rooms/", "base_dir"),
            ("/sim-environment/collisions/", "collision_dir"),
        ):
            if not clean.startswith(prefix):
                continue
            base = resolved.get(root_key)
            if base is None:
                break
            target = _safe_resolve(base / clean[len(prefix) :])
            if target is None or not target.is_file() or not target.is_relative_to(base):
                target = None
            break
    if target is None or not target.is_file():
        raise web.HTTPNotFound(text="not found")
    _bind_environment_fingerprint(request, fingerprint)
    return await _serve_static(target, request)


class _ControlStateMissing(Exception):
    """A requested controller snapshot does not exist."""


class _ControlStateInvalid(Exception):
    """The controller filesystem contract is missing, unsafe, or malformed."""


def _strict_json_loads(raw: bytes) -> object:
    """RFC JSON only: UTF-8, no duplicate keys, and no NaN/Infinity."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object)


def _control_json(body: dict[str, object], *, status: int = 200) -> web.Response:
    return web.json_response(body, status=status, headers=ENVIRONMENT_NO_STORE)


def _control_error(status: int, message: str, **details: object) -> web.Response:
    return _control_json({"ok": False, "error": message, **details}, status=status)


def _control_child(root: Path, *parts: str) -> Path:
    """A fixed-name child whose parent cannot escape its injected mount."""
    if any(not part or Path(part).name != part or "/" in part or "\\" in part for part in parts):
        raise _ControlStateInvalid
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise _ControlStateInvalid
        target = root.joinpath(*parts)
        resolved_parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ControlStateInvalid from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise _ControlStateInvalid
    return target


def _read_control_json(path: Path, *, max_bytes: int = CONTROL_STATUS_MAX_BYTES) -> dict[str, object]:
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _ControlStateMissing from exc
    except OSError as exc:
        raise _ControlStateInvalid from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise _ControlStateInvalid
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > max_bytes:
            raise _ControlStateInvalid
        parsed = _strict_json_loads(raw)
    except (OSError, RecursionError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _ControlStateInvalid from exc
    finally:
        os.close(fd)
    if not isinstance(parsed, dict):
        raise _ControlStateInvalid
    return parsed


def _is_timestamp(value: object) -> bool:
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_safe_text(
    value: object,
    *,
    max_length: int,
    allow_empty: bool = False,
    allow_newlines: bool = False,
) -> bool:
    if not isinstance(value, str) or len(value) > max_length or (not value and not allow_empty):
        return False
    return not any(ord(char) < 0x20 and (not allow_newlines or char not in "\n\t") for char in value)


def _is_environment_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 64 and ENVIRONMENT_ID_RE.fullmatch(value) is not None


def _is_job_id(value: object) -> bool:
    if not isinstance(value, str) or JOB_ID_RE.fullmatch(value) is None:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _environment_summary(value: object, *, fingerprint: bool) -> dict[str, str]:
    expected = {"id", "display_name", *(("fingerprint",) if fingerprint else ())}
    if not isinstance(value, dict) or set(value) != expected:
        raise _ControlStateInvalid
    environment_id = value.get("id")
    display_name = value.get("display_name")
    if not _is_environment_id(environment_id) or not _is_safe_text(display_name, max_length=160):
        raise _ControlStateInvalid
    result = {"id": environment_id, "display_name": display_name}
    if fingerprint:
        environment_fingerprint = value.get("fingerprint")
        if not _is_safe_text(environment_fingerprint, max_length=256):
            raise _ControlStateInvalid
        result["fingerprint"] = environment_fingerprint
    return result


def _require_controller_healthy(now: float) -> None:
    if not _is_timestamp(now):
        raise _ControlStateInvalid
    heartbeat_path = _control_child(SIM_ENVIRONMENT_STATUS_DIR, "heartbeat.json")
    heartbeat = _read_control_json(heartbeat_path, max_bytes=4096)
    if set(heartbeat) != {"schema_version", "pid", "updated_at"} or heartbeat.get("schema_version") != 1:
        raise _ControlStateInvalid
    pid = heartbeat.get("pid")
    updated_at = heartbeat.get("updated_at")
    if type(pid) is not int or not 0 < pid < 2**31 or not _is_timestamp(updated_at):
        raise _ControlStateInvalid
    age = now - updated_at
    if age < -1.0 or age > CONTROL_HEARTBEAT_MAX_AGE_S:
        raise _ControlStateInvalid


def _load_environment_catalog(now: float) -> dict[str, object]:
    _require_controller_healthy(now)
    catalog_path = _control_child(SIM_ENVIRONMENT_STATUS_DIR, "catalog.json")
    catalog = _read_control_json(catalog_path)
    if set(catalog) != {"schema_version", "active", "environments"} or catalog.get("schema_version") != 1:
        raise _ControlStateInvalid
    raw_environments = catalog.get("environments")
    if not isinstance(raw_environments, list) or len(raw_environments) > 256:
        raise _ControlStateInvalid
    environments = [_environment_summary(item, fingerprint=False) for item in raw_environments]
    environment_ids = [item["id"] for item in environments]
    if environment_ids != sorted(environment_ids) or len(environment_ids) != len(set(environment_ids)):
        raise _ControlStateInvalid
    raw_active = catalog.get("active")
    active = None if raw_active is None else _environment_summary(raw_active, fingerprint=True)
    if active is not None:
        installed = next((item for item in environments if item["id"] == active["id"]), None)
        if installed is None or installed["display_name"] != active["display_name"]:
            raise _ControlStateInvalid
    return {"schema_version": 1, "active": active, "environments": environments}


def _validate_switch_request(value: dict[str, object], now: float) -> dict[str, object]:
    if set(value) != {"schema_version", "job_id", "environment_id", "created_at"}:
        raise _ControlStateInvalid
    job_id = value.get("job_id")
    environment_id = value.get("environment_id")
    created_at = value.get("created_at")
    if value.get("schema_version") != 1 or not _is_job_id(job_id) or not _is_environment_id(environment_id):
        raise _ControlStateInvalid
    if not _is_timestamp(created_at):
        raise _ControlStateInvalid
    age = now - created_at
    if age < -1.0 or age > CONTROL_REQUEST_MAX_AGE_S:
        raise _ControlStateInvalid
    return {
        "schema_version": 1,
        "job_id": job_id,
        "environment_id": environment_id,
        "created_at": created_at,
    }


def _validate_switch_job(value: dict[str, object], expected_job_id: str) -> dict[str, object]:
    required = {
        "schema_version",
        "job_id",
        "target",
        "state",
        "phase",
        "progress",
        "message",
        "started_at",
        "updated_at",
    }
    optional = {"fingerprint", "finished_at", "recovered_environment", "error"}
    if not required.issubset(value) or set(value) - required - optional:
        raise _ControlStateInvalid
    job_id = value.get("job_id")
    state = value.get("state")
    phase = value.get("phase")
    progress = value.get("progress")
    message = value.get("message")
    started_at = value.get("started_at")
    updated_at = value.get("updated_at")
    if value.get("schema_version") != 1 or job_id != expected_job_id or not _is_job_id(job_id):
        raise _ControlStateInvalid
    if state not in SWITCH_STATES or phase not in SWITCH_PHASES:
        raise _ControlStateInvalid
    if type(progress) is not int or not 0 <= progress <= 100:
        raise _ControlStateInvalid
    if not _is_safe_text(message, max_length=2048, allow_empty=True, allow_newlines=True):
        raise _ControlStateInvalid
    if not _is_timestamp(started_at) or not _is_timestamp(updated_at) or updated_at < started_at:
        raise _ControlStateInvalid
    result: dict[str, object] = {
        "schema_version": 1,
        "job_id": job_id,
        "target": _environment_summary(value.get("target"), fingerprint=False),
        "state": state,
        "phase": phase,
        "progress": progress,
        "message": message,
        "started_at": started_at,
        "updated_at": updated_at,
    }
    if "fingerprint" in value:
        if not _is_safe_text(value["fingerprint"], max_length=256):
            raise _ControlStateInvalid
        result["fingerprint"] = value["fingerprint"]
    if "finished_at" in value:
        finished_at = value["finished_at"]
        if not _is_timestamp(finished_at) or finished_at < started_at:
            raise _ControlStateInvalid
        result["finished_at"] = finished_at
    if "recovered_environment" in value:
        result["recovered_environment"] = _environment_summary(value["recovered_environment"], fingerprint=True)
    if "error" in value:
        if not _is_safe_text(value["error"], max_length=4096, allow_empty=True, allow_newlines=True):
            raise _ControlStateInvalid
        result["error"] = value["error"]
    return result


def _load_switch_job(job_id: str) -> dict[str, object]:
    job_path = _control_child(SIM_ENVIRONMENT_STATUS_DIR, "jobs", f"{job_id}.json")
    return _validate_switch_job(_read_control_json(job_path), job_id)


def _request_is_same_origin(request: web.Request) -> bool:
    origins = request.headers.getall("Origin", [])
    if len(origins) != 1:
        return False
    try:
        supplied = urlsplit(origins[0])
        expected = urlsplit(f"{request.scheme}://{request.host}")
        if supplied.username is not None or supplied.password is not None:
            return False
        if supplied.path not in ("", "/") or supplied.query or supplied.fragment:
            return False
        supplied_port = supplied.port or (443 if supplied.scheme.lower() == "https" else 80)
        expected_port = expected.port or (443 if request.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return False
    return (
        supplied.scheme.lower() == request.scheme.lower()
        and supplied.hostname is not None
        and expected.hostname is not None
        and supplied.hostname.rstrip(".").lower() == expected.hostname.rstrip(".").lower()
        and supplied_port == expected_port
    )


def _write_switch_request(value: dict[str, object]) -> None:
    current_path = _control_child(SIM_ENVIRONMENT_REQUESTS_DIR, "current.json")
    payload = (json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(payload) > CONTROL_REQUEST_MAX_BYTES:
        raise _ControlStateInvalid
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        # The proxy runs as root in the container, while the host controller
        # runs as the invoking user on native Linux. The non-secret mailbox
        # payload must therefore be host-readable across the bind mount.
        fd = os.open(current_path, flags, 0o644)
    except FileExistsError:
        raise
    except OSError as exc:
        raise _ControlStateInvalid from exc
    created = os.fstat(fd)
    wrote = False
    try:
        # os.open's mode is filtered through the container's umask; normalize
        # it explicitly so a hardened 0077 umask cannot recreate the Linux
        # cross-UID read failure this mode is intended to prevent.
        os.fchmod(fd, 0o644)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[count:]
        os.fsync(fd)
        wrote = True
    except OSError as exc:
        raise _ControlStateInvalid from exc
    finally:
        os.close(fd)
        if not wrote:
            try:
                visible = current_path.lstat()
                if (visible.st_dev, visible.st_ino) == (created.st_dev, created.st_ino):
                    current_path.unlink()
            except OSError:
                pass


def _existing_switch(now: float, catalog: dict[str, object]) -> dict[str, object]:
    current_path = _control_child(SIM_ENVIRONMENT_REQUESTS_DIR, "current.json")
    current = _validate_switch_request(
        _read_control_json(current_path, max_bytes=CONTROL_REQUEST_MAX_BYTES),
        now,
    )
    try:
        job = _load_switch_job(current["job_id"])
    except _ControlStateMissing:
        # The controller polls every 250 ms. A valid, fresh mailbox precedes its
        # first queued snapshot and is already authoritative enough to reject a
        # double-click without misreporting the controller as unhealthy.
        if now - current["created_at"] > CONTROL_JOB_STATUS_GRACE_S:
            raise _ControlStateInvalid from None
        target = next(
            (item for item in catalog["environments"] if item["id"] == current["environment_id"]),
            None,
        )
        if target is None:
            raise _ControlStateInvalid from None
        return {"job_id": current["job_id"], "state": "queued", "target": target}
    if job["target"]["id"] != current["environment_id"]:
        raise _ControlStateInvalid
    if job["state"] not in {"queued", "running"}:
        # Terminal status is published before the controller removes current;
        # this tiny inconsistent window is not permission to overwrite it.
        raise _ControlStateInvalid
    return {"job_id": job["job_id"], "state": job["state"], "target": job["target"]}


async def sim_environments_catalog(request: web.Request) -> web.Response:
    if not WEBAPP_SIM_CONTROLS:
        return _control_error(404, "not found")
    try:
        catalog = _load_environment_catalog(SIM_ENVIRONMENT_TIME())
    except (_ControlStateMissing, _ControlStateInvalid):
        return _control_error(503, "simulator environment controller unavailable")
    return _control_json(catalog)


async def sim_environment_switch(request: web.Request) -> web.Response:
    if not WEBAPP_SIM_CONTROLS:
        return _control_error(404, "not found")
    requested_by = request.headers.getall("X-Requested-By", [])
    if requested_by != ["innate-webapp"]:
        return _control_error(403, "missing X-Requested-By header")
    if not _request_is_same_origin(request):
        return _control_error(403, "request origin does not match this simulator")
    if request.content_type != "application/json":
        return _control_error(415, "Content-Type must be application/json")
    if request.content_length is not None and request.content_length > CONTROL_REQUEST_MAX_BYTES:
        return _control_error(413, "request body too large")
    raw = await request.content.read(CONTROL_REQUEST_MAX_BYTES + 1)
    if len(raw) > CONTROL_REQUEST_MAX_BYTES:
        return _control_error(413, "request body too large")
    try:
        body = _strict_json_loads(raw)
    except (RecursionError, UnicodeError, ValueError, json.JSONDecodeError):
        return _control_error(400, "invalid JSON body")
    if not isinstance(body, dict) or set(body) != {"id"} or not _is_environment_id(body.get("id")):
        return _control_error(400, "body must contain exactly one valid environment id")

    now = SIM_ENVIRONMENT_TIME()
    try:
        catalog = _load_environment_catalog(now)
    except (_ControlStateMissing, _ControlStateInvalid):
        return _control_error(503, "simulator environment controller unavailable")
    target = next((item for item in catalog["environments"] if item["id"] == body["id"]), None)
    if target is None:
        return _control_error(404, "environment is not installed")

    job_id = str(SIM_ENVIRONMENT_UUID()).lower()
    if not _is_job_id(job_id):
        return _control_error(503, "simulator environment controller unavailable")
    mailbox = {
        "schema_version": 1,
        "job_id": job_id,
        "environment_id": target["id"],
        "created_at": now,
    }
    for attempt in range(2):
        try:
            _write_switch_request(mailbox)
            break
        except FileExistsError:
            try:
                busy = _existing_switch(now, catalog)
            except _ControlStateMissing:
                # The controller can remove a terminal mailbox between O_EXCL
                # and our inspection. Retry once rather than exposing that
                # harmless cleanup race as a controller failure.
                if attempt == 0:
                    continue
                return _control_error(503, "simulator environment controller state is inconsistent")
            except _ControlStateInvalid:
                return _control_error(503, "simulator environment controller state is inconsistent")
            return _control_error(409, "an environment switch is already in progress", **busy)
        except _ControlStateInvalid:
            return _control_error(503, "simulator environment controller unavailable")
    else:
        return _control_error(503, "simulator environment controller unavailable")
    return _control_json({"job_id": job_id, "state": "queued", "target": target}, status=202)


async def sim_environment_switch_status(request: web.Request) -> web.Response:
    if not WEBAPP_SIM_CONTROLS:
        return _control_error(404, "not found")
    job_id = request.match_info["job_id"]
    if not _is_job_id(job_id):
        return _control_error(400, "invalid environment switch job id")
    try:
        _require_controller_healthy(SIM_ENVIRONMENT_TIME())
        job = _load_switch_job(job_id)
    except _ControlStateMissing:
        return _control_error(404, "environment switch job not found")
    except _ControlStateInvalid:
        return _control_error(503, "simulator environment controller state is invalid")
    return _control_json(job)


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
    app.router.add_get("/sim-environments.json", sim_environments_catalog)
    app.router.add_post("/sim-environment/switch", sim_environment_switch)
    app.router.add_get("/sim-environment/switch/{job_id}", sim_environment_switch_status)
    app.router.add_get("/sim-environment/manifest.json", sim_environment_manifest)
    app.router.add_get("/sim-environment/scene.glb", sim_environment_asset)
    app.router.add_get("/sim-environment/layout.json", sim_environment_asset)
    app.router.add_get("/sim-environment/rooms/{tail:.*}", sim_environment_asset)
    app.router.add_get("/sim-environment/collisions/{tail:.*}", sim_environment_asset)
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
