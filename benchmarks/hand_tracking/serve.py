#!/usr/bin/env python3
"""Loopback-only recorder. Standard library only; no robot connection."""

import argparse
import base64
import hashlib
import json
import math
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PROTOCOL = json.loads((ROOT / "protocol.json").read_text())
MAX_BODY = 32 * 1024 * 1024
ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
}


def save_recording(root, body):
    meta = body["metadata"]
    session = str(uuid.UUID(meta["session"]))
    recording_id = str(uuid.UUID(meta["recording_id"]))
    clips = {c["id"]: c for c in PROTOCOL["clips"]}
    if meta["clip"] not in clips or meta["split"] not in ("calibration", "test_normal", "test_challenging"):
        raise ValueError("Unknown clip or split")
    if meta["hand"] not in ("right", "left"):
        raise ValueError("Choose a hand")
    if meta["protocol_version"] != PROTOCOL["version"]:
        raise ValueError("Protocol changed; reload the recorder")
    duration = float(meta["duration_ms"])
    if not math.isfinite(duration) or not 500 <= duration <= 60000:
        raise ValueError("Invalid duration")
    if not isinstance(meta.get("complete"), bool):
        raise ValueError("Missing completion status")
    mime = meta["mime"].split(";", 1)[0]
    if mime not in ("video/webm", "video/mp4"):
        raise ValueError("Unsupported video container")
    video = base64.b64decode(body["video_base64"], validate=True)
    if not video or len(video) > 24 * 1024 * 1024:
        raise ValueError("Empty or oversized recording")
    # Check container signatures; decoding/quality checks happen before benchmarking.
    if (mime == "video/webm" and not video.startswith(b"\x1aE\xdf\xa3")) or (
        mime == "video/mp4" and video[4:8] != b"ftyp"
    ):
        raise ValueError("Invalid video container")
    session_dir = root / session
    session_dir.mkdir(parents=True, exist_ok=True)
    destination = session_dir / recording_id
    digest = hashlib.sha256(video).hexdigest()
    if destination.exists():
        old = json.loads((destination / "metadata.json").read_text())
        if old["sha256"] == digest and all(old.get(k) == v for k, v in meta.items()):
            return {"saved": str(destination), "sha256": digest}  # idempotent retry
        raise ValueError("Recording ID already exists with different content")
    folder = Path(tempfile.mkdtemp(prefix=".upload-", dir=session_dir))
    try:
        filename = "video.webm" if mime == "video/webm" else "video.mp4"
        (folder / filename).write_bytes(video)
        stored = dict(
            meta,
            video=filename,
            sha256=digest,
            bytes=len(video),
            saved_at=datetime.now(timezone.utc).isoformat(),
            instruction=clips[meta["clip"]]["instruction"],
        )
        (folder / "metadata.json").write_text(json.dumps(stored, indent=2) + "\n")
        folder.rename(destination)
    finally:
        if folder.exists():
            shutil.rmtree(folder)
    return {"saved": str(destination), "sha256": digest}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type="application/json"):
        payload = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def local_request(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in allowed and (
            origin is None or origin in {f"http://{host}" for host in allowed}
        )

    def do_GET(self):
        if not self.local_request():
            return self.reply(403, {"error": "Local access only"})
        path = urlparse(self.path).path
        if path in ASSETS:
            name, mime = ASSETS[path]
            return self.reply(200, (ROOT / name).read_bytes(), mime)
        if path == "/api/config":
            return self.reply(200, {"protocol": PROTOCOL, "output": str(self.server.data_dir)})
        return self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.local_request():
            return self.reply(403, {"error": "Local access only"})
        if self.path != "/api/recordings":
            return self.reply(404, {"error": "Not found"})
        if self.headers.get("Content-Type") != "application/json":
            return self.reply(415, {"error": "Expected application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {"error": "Recording is too large"})
            body = json.loads(self.rfile.read(length))
            result = save_recording(self.server.data_dir, body)
        except (KeyError, TypeError, ValueError) as e:
            return self.reply(400, {"error": str(e)})
        except OSError:
            return self.reply(500, {"error": "Could not save to disk. Keep the review open and retry."})
        return self.reply(201, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.data_dir = args.data.resolve()
    print(f"Recorder: http://127.0.0.1:{server.server_port}\nRecordings: {server.data_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
