"""Episode + training-run read endpoints for the webapp front door.

Pure, read-only HTTP handlers served by https_server.py (over TLS) and by the
plain-HTTP media listener. Every file access is fenced to the skill roots below
(path-traversal guards). Split out of https_server.py.
"""

import asyncio
import fnmatch
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs

from aiohttp import web
from util import threaded

# Downloaded training-run results larger than this aren't served as a single
# blob (the log viewer wants text, not gigabytes).
MAX_LOG_BYTES = 8 * 1024 * 1024

# Roots the /episode* routes may serve from. Only workspace/custom_skills:
# the pre-0.6 in-place locations ($INNATE_OS_ROOT/skills, ~/skills) are no
# longer scanned by the brain, so serving from them was dead surface.
_INNATE_OS_ROOT = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
SKILLS_ROOTS = ((Path(_INNATE_OS_ROOT) / "workspace" / "custom_skills").resolve(),)


def _under_skills_root(p: Path) -> bool:
    """True if p is inside an allowed skill root (path-traversal fence)."""
    return any(p.is_relative_to(root) for root in SKILLS_ROOTS)


# Saved navigation maps (mode_manager's maps dir): <name>.yaml + its image.
MAPS_DIR = (Path(_INNATE_OS_ROOT) / "data" / "maps").resolve()
# mode_manager's save_map name validation, mirrored.
_MAP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# brain_client's in-progress mapping session stages memories under this
# literal — dot-prefixed exactly so no saved map name can collide with it.
_MAPPING_SESSION = ".mapping"


def _plain(status: int, reason: str, text: str) -> web.Response:
    return web.Response(status=status, reason=reason, body=text.encode(), content_type="text/plain")


def _resolve_under_root(rel: str):
    """Resolve a client-supplied skill directory, refusing anything that escapes
    the skills root (path-traversal guard, mirrors static_response)."""
    if not rel:
        return None
    try:
        p = Path(rel).resolve()
    except (OSError, ValueError):
        return None
    return p if _under_skills_root(p) else None


def _safe_resolve(p: Path):
    """Path.resolve() that returns None instead of raising on illegal bytes (e.g.
    a NUL byte in a query param), so malformed input becomes a 404, not a 500."""
    try:
        return p.resolve()
    except (OSError, ValueError):
        return None


async def episode_response(request: web.Request) -> web.StreamResponse:
    """GET /episode?dir=<skill_dir>&id=<n>&camera=<cam> → episode MP4.

    FileResponse honours Range natively (seek + stream only the requested
    bytes), so a scrubbing <video> never slurps the whole multi-MB file. Cheap
    (just a fenced lookup), so it stays on the loop rather than a thread."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    cam = (qs.get("camera") or [""])[0]
    if base is None or not eid or not cam:
        return _plain(404, "Not Found", "not found")
    mp4 = _safe_resolve(base / "data" / f"episode_{eid}_{cam}.mp4")
    if mp4 is None or not _under_skills_root(mp4) or mp4.suffix != ".mp4" or not mp4.is_file():
        return _plain(404, "Not Found", "no such episode video")
    return web.FileResponse(mp4, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache"})


def _make_thumb(mp4_path: Path, cache_path: Path, width: int = 240) -> None:
    """Decode one representative frame from *mp4_path* and write a small JPEG to
    *cache_path* (atomically). Runs in a thread — cv2 is blocking."""
    import cv2  # available in the robot's system python (with video support)

    cap = cv2.VideoCapture(str(mp4_path))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, 400)  # ~0.4s in for a settled frame
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        raise RuntimeError("could not read a frame")

    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("jpeg encode failed")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".jpg.tmp")
    tmp.write_bytes(buf.tobytes())
    os.replace(str(tmp), str(cache_path))


# In-flight thumbnail generations, keyed by cache path, so a burst of lazy <img>
# loads on a gallery page doesn't spawn N redundant cv2 decoders for the same
# frame: the first request generates, the rest await the same lock and then read
# the now-cached file.
_thumb_locks: dict[str, asyncio.Lock] = {}


async def thumb_response(request: web.Request) -> web.Response:
    """GET /episode/thumb?dir=<skill_dir>&id=<n>&camera=<cam> → cached JPEG of a
    frame from the episode MP4 (generated on first request, then served static)."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    cam = (qs.get("camera") or ["camera_1"])[0]
    if base is None or not eid:
        return _plain(404, "Not Found", "not found")
    mp4 = _safe_resolve(base / "data" / f"episode_{eid}_{cam}.mp4")
    if mp4 is None or not _under_skills_root(mp4) or not mp4.is_file():
        return _plain(404, "Not Found", "no such episode video")
    # Cache beside data/ (not inside it) so thumbnails are never uploaded to the cloud.
    cache = _safe_resolve(base / "thumbs" / f"episode_{eid}_{cam}.jpg")
    if cache is None or not cache.is_relative_to(base):
        return _plain(404, "Not Found", "no such thumbnail")
    try:
        if not cache.is_file():
            lock = _thumb_locks.setdefault(str(cache), asyncio.Lock())
            try:
                async with lock:
                    if not cache.is_file():  # another request may have generated it while we waited
                        await asyncio.to_thread(_make_thumb, mp4, cache)
            finally:
                # finally, so a failed _make_thumb doesn't leak the lock entry.
                _thumb_locks.pop(str(cache), None)
        data = await asyncio.to_thread(cache.read_bytes)
    except Exception as err:  # noqa: BLE001
        return _plain(500, "Internal Server Error", f"thumb failed: {err}")
    return web.Response(status=200, body=data, headers={"Content-Type": "image/jpeg", "Cache-Control": "max-age=86400"})


# name -> (image mtime_ns, png bytes). Maps only change on save/overwrite, so
# a stale hit is impossible (mtime keys the entry) and the dict stays tiny.
_map_png_cache: dict[str, tuple[int, bytes]] = {}


def _render_map_png(image_path: Path, max_px: int = 480) -> bytes:
    """Decode a saved map image (.pgm) and re-encode it as a small PNG."""
    import cv2  # available in the robot's system python

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError("could not decode map image")
    h, w = img.shape[:2]
    scale = max_px / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("png encode failed")
    return buf.tobytes()


@threaded
def map_preview_response(request: web.Request) -> web.Response:
    """GET /map/preview?name=<base or file.yaml> → PNG preview of a saved map,
    so the Nav sidebar can show a map without switching to it. The image path
    comes from the map's own yaml (map_saver writes `image: <file>`), fenced to
    the maps directory."""
    qs = parse_qs(request.query_string)
    name = (qs.get("name") or [""])[0]
    if name.endswith(".yaml"):
        name = name[:-5]
    if not _MAP_NAME_RE.match(name):
        return _plain(404, "Not Found", "no such map")
    yaml_path = MAPS_DIR / f"{name}.yaml"
    if not yaml_path.is_file():
        return _plain(404, "Not Found", "no such map")
    image_rel = ""
    try:
        for line in yaml_path.read_text().splitlines():
            if line.startswith("image:"):
                image_rel = line.split(":", 1)[1].strip()
                break
    except OSError as err:
        return _plain(500, "Internal Server Error", f"failed to read map yaml: {err}")
    image_path = _safe_resolve(MAPS_DIR / image_rel) if image_rel else None
    if image_path is None or not image_path.is_relative_to(MAPS_DIR) or not image_path.is_file():
        return _plain(404, "Not Found", "map has no image")
    try:
        mtime = image_path.stat().st_mtime_ns
        cached = _map_png_cache.get(name)
        if cached is None or cached[0] != mtime:
            cached = (mtime, _render_map_png(image_path))
            _map_png_cache[name] = cached
        data = cached[1]
    except Exception as err:  # noqa: BLE001 — surface a clean 500 to the client
        return _plain(500, "Internal Server Error", f"map preview failed: {err}")
    return web.Response(status=200, body=data, headers={"Content-Type": "image/png", "Cache-Control": "no-cache"})


# The robot's per-map spatial memory (brain_client/memory): one JPEG per
# remembered viewpoint, written by the brain node.
MEMORY_DIR = (Path(_INNATE_OS_ROOT) / "data" / "spatial_memory").resolve()


@threaded
def memory_image_response(request: web.Request) -> web.Response:
    """GET /memory/image?map=<base or file.yaml>&id=<n> → the remembered JPEG of
    one spatial-memory viewpoint. A refresh overwrites the file in place, so
    clients append the memory's stamp as ?v= and this can be cached hard."""
    qs = parse_qs(request.query_string)
    name = (qs.get("map") or [""])[0]
    if name.endswith(".yaml"):
        name = name[:-5]
    memory_id = (qs.get("id") or [""])[0]
    # The map-name charset (or the session literal) and a numeric id make the
    # joined path traversal-proof.
    if (name != _MAPPING_SESSION and not _MAP_NAME_RE.match(name)) or not memory_id.isdigit():
        return _plain(404, "Not Found", "no such memory")
    try:
        target = _safe_resolve(MEMORY_DIR / name / f"{int(memory_id)}.jpg")
        if target is None or not target.is_relative_to(MEMORY_DIR):
            return _plain(404, "Not Found", "no such memory")
        data = target.read_bytes()
    except OSError:
        return _plain(404, "Not Found", "no such memory")
    return web.Response(
        status=200, body=data, headers={"Content-Type": "image/jpeg", "Cache-Control": "max-age=86400, immutable"}
    )


@threaded
def joints_response(request: web.Request) -> web.Response:
    """GET /episode/joints?dir=<skill_dir>&id=<n> → qpos/qvel/timestamps JSON,
    read straight from the (possibly image-stripped) HDF5 — joints are kept."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    if base is None or not eid:
        return _plain(404, "Not Found", "not found")
    h5 = _safe_resolve(base / "data" / f"episode_{eid}.h5")
    if h5 is None or not _under_skills_root(h5) or h5.suffix != ".h5" or not h5.is_file():
        return _plain(404, "Not Found", "no such episode")
    try:
        import h5py  # available in the robot's system python

        with h5py.File(str(h5), "r") as f:
            obs = f["observations"]
            qpos = obs["qpos"][:].tolist() if "qpos" in obs else []
            qvel = obs["qvel"][:].tolist() if "qvel" in obs else []
            ts = []
            if "timestamps" in f and "arm" in f["timestamps"]:
                ts = f["timestamps"]["arm"][:].tolist()
        freq = 0
        meta = base / "data" / "dataset_metadata.json"
        if meta.is_file():
            freq = json.loads(meta.read_text()).get("data_frequency", 0)
        payload = json.dumps({"qpos": qpos, "qvel": qvel, "timestamps": ts, "data_frequency": freq}).encode()
    except Exception as err:  # noqa: BLE001 — surface a clean 500 to the client
        return _plain(500, "Internal Server Error", f"failed to read joints: {err}")
    return web.Response(
        status=200, body=payload, headers={"Content-Type": "application/json", "Cache-Control": "no-cache"}
    )


@threaded
def profile_response(request: web.Request) -> web.Response:
    """GET /episode/profile?dir=<skill_dir>&id=<n> → the episode's persisted
    inference-profile trace (JSONL written by profile_recorder next to the
    HDF5): one context line, then one per-step sample per line. 404 when the
    episode predates profile recording or wasn't a learned-skill rollout."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    if base is None or not eid:
        return _plain(404, "Not Found", "not found")
    jsonl = _safe_resolve(base / "data" / f"episode_{eid}_profile.jsonl")
    if jsonl is None or not _under_skills_root(jsonl) or jsonl.suffix != ".jsonl" or not jsonl.is_file():
        return _plain(404, "Not Found", "no profile for this episode")
    try:
        data = jsonl.read_bytes()
    except OSError as err:
        return _plain(500, "Internal Server Error", f"failed to read profile: {err}")
    return web.Response(
        status=200, body=data, headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"}
    )


# A run's own exception line, e.g. "RuntimeError: stack expects each tensor to
# be equal size...". Anchored to the exception-name shape rather than a bare
# "error" substring so progress lines mentioning errors don't match; the FATAL
# branch is case-insensitive (scoped flag) since tools spell it every way.
_ERROR_LINE_RE = re.compile(
    r"^\s*(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Interrupt)\b.*|^\s*(?i:FATAL(?:\s+error)?)\b.*"
)
_TAIL_BYTES = 128 * 1024  # errors live at the end; don't read multi-MB logs whole


def _failure_excerpt(run_dir) -> str:
    """Last exception-looking line from the run's logs, or "".

    Scans the tail of the same files the Logs modal prefers. jsonl lines are
    {"line": ..., "stream": ...}; plain logs are read as-is. The *last* match
    wins — a traceback's final line names the actual exception.
    """
    excerpt = ""
    for name in ("process_output.jsonl", "daemon.log", "output.log"):
        path = run_dir / name
        resolved = _safe_resolve(path)
        if resolved is None or not resolved.is_relative_to(run_dir) or not resolved.is_file():
            continue
        try:
            with open(resolved, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - _TAIL_BYTES))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for raw in tail.splitlines():
            line = raw
            if name.endswith(".jsonl"):
                try:
                    line = str(json.loads(raw).get("line", ""))
                except (json.JSONDecodeError, AttributeError):
                    continue
            if _ERROR_LINE_RE.match(line):
                excerpt = line.strip()
        if excerpt:
            return excerpt[:400]
    return ""


@threaded
def run_info_response(request: web.Request) -> web.Response:
    """GET /run/info?dir=<skill_dir>&id=<run_id> → downloaded?/has_checkpoint?/files.
    A run is 'successful' if its downloaded results contain a *_step_*.pth — the
    same check the training node uses to activate a checkpoint."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    rid = (qs.get("id") or [""])[0]
    if base is None or not rid:
        return _plain(404, "Not Found", "not found")
    run_dir = _safe_resolve(base / rid)
    if run_dir is None or not _under_skills_root(run_dir) or not run_dir.is_dir():
        # Not downloaded yet (or never will be).
        body = json.dumps({"downloaded": False, "has_checkpoint": False, "files": []}).encode()
        return web.Response(status=200, body=body, content_type="application/json")
    files = []
    has_ckpt = False
    truncated = False
    max_files = 2000  # bound the response — run dirs can hold many checkpoint shards
    try:
        for p in sorted(run_dir.rglob("*")):
            if not p.is_file():
                continue
            if fnmatch.fnmatch(p.name, "*_step_*.pth"):
                has_ckpt = True
            if len(files) < max_files:
                files.append(p.relative_to(run_dir).as_posix())
            else:
                truncated = True
    except OSError as err:
        return _plain(500, "Internal Server Error", f"failed to read run dir: {err}")
    # Failed run (no checkpoint): pull the actual exception line out of the
    # downloaded logs so the Training page can say WHY, not just "no checkpoint".
    error_excerpt = "" if has_ckpt else _failure_excerpt(run_dir)
    body = json.dumps(
        {
            "downloaded": True,
            "has_checkpoint": has_ckpt,
            "files": files,
            "truncated": truncated,
            "error_excerpt": error_excerpt,
        }
    ).encode()
    return web.Response(
        status=200, body=body, headers={"Content-Type": "application/json", "Cache-Control": "no-cache"}
    )


@threaded
def run_log_response(request: web.Request) -> web.Response:
    """GET /run/log?dir=<skill_dir>&id=<run_id>&file=<relpath> → a run log file
    as text/plain. Sandboxed to the run directory."""
    qs = parse_qs(request.query_string)
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    rid = (qs.get("id") or [""])[0]
    rel = (qs.get("file") or [""])[0]
    if base is None or not rid or not rel:
        return _plain(404, "Not Found", "not found")
    run_dir = _safe_resolve(base / rid)
    target = _safe_resolve(run_dir / rel) if run_dir else None
    if (
        run_dir is None
        or target is None
        or not _under_skills_root(run_dir)
        or not target.is_relative_to(run_dir)
        or not target.is_file()
    ):
        return _plain(404, "Not Found", "no such log file")
    try:
        # Bound the read: this route serves *any* file under the run dir (the
        # guard only checks containment + is_file()), including multi-GB .pth
        # checkpoints — never pull more than the cap into RAM. read() of a
        # too-large file returns the cap+1 so the truncation check below still
        # fires, but peak memory is bounded regardless of file size.
        with open(target, "rb") as fh:
            data = fh.read(MAX_LOG_BYTES + 1)
    except OSError as err:
        return _plain(500, "Internal Server Error", f"read failed: {err}")
    truncated = b""
    if len(data) > MAX_LOG_BYTES:
        data = data[:MAX_LOG_BYTES]
        truncated = b"\n\n[truncated]\n"
    body = data + truncated
    return web.Response(
        status=200, body=body, headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"}
    )
