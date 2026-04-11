"""FastAPI server for the Training Manager web UI.

Serves the React SPA from the ``static/`` directory and mounts the
REST API routers for skills, datasets, and training.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from training_manager.api.datasets import router as datasets_router
from training_manager.api.skills import router as skills_router
from training_manager.api.training import router as training_router
from training_manager.log_capture import log_capture

logger = logging.getLogger("training_manager")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(skills_dir: str = "~/skills") -> FastAPI:
    resolved_skills_dir = str(Path(skills_dir).expanduser().resolve())

    app = FastAPI(title="Training Manager", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.skills_dir = resolved_skills_dir

    app.include_router(skills_router, prefix="/api/skills")
    app.include_router(datasets_router, prefix="/api/datasets")
    app.include_router(training_router, prefix="/api/training")

    @app.get("/api/logs")
    async def stream_logs() -> StreamingResponse:
        async def generate() -> Any:
            async for entry in log_capture.subscribe():
                data = json.dumps(entry.to_dict())
                yield f"data: {data}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            file_path = STATIC_DIR / full_path
            if file_path.is_file():
                return FileResponse(str(file_path))
            return FileResponse(str(STATIC_DIR / "index.html"))

    _install_log_capture()

    return app


def _install_log_capture() -> None:
    """Attach the shared log capture handler to relevant loggers."""
    for name in ("training_manager", "training_client", "training_client.src"):
        log = logging.getLogger(name)
        log.addHandler(log_capture)
        log.setLevel(logging.DEBUG)


def main() -> None:
    """Entry point for ``training-manager`` console script."""
    port = int(os.environ.get("PORT", "8080"))
    skills_dir = os.environ.get("SKILLS_DIR", "~/skills")

    app = create_app(skills_dir=skills_dir)

    host_ip = _get_lan_ip()
    print(f"\n  Training Manager running at:")
    print(f"    Local:   http://localhost:{port}")
    if host_ip:
        print(f"    Network: http://{host_ip}:{port}")
    print()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def _get_lan_ip() -> str | None:
    """Best-effort LAN IP detection."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == "__main__":
    main()
