#!/usr/bin/env python3
"""Serve the 3D reward preview, including the real robot and built viewer bundle."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
MOUNTS = {
    "/sim-viewer/": ROOT / "sim/viewer/dist-lib",
    "/robot/": ROOT / "ros2_ws/src/mars_bot/mars_sim",
    "/": ROOT / "webapp",
}


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        route = unquote(urlsplit(path).path)
        for prefix, base in MOUNTS.items():
            if route.startswith(prefix):
                target = (base / route[len(prefix) :]).resolve()
                return str(target if target.is_relative_to(base) else base / "__not_found__")
        return str(ROOT / "__not_found__")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if not (MOUNTS["/sim-viewer/"] / "sim-session.js").is_file():
        parser.error("Build the viewer first: npm run build:lib --prefix sim/viewer")
    print(f"http://127.0.0.1:{args.port}/debug/plant-reward.html", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
