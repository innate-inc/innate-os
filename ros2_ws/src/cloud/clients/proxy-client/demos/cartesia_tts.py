"""Demo: Cartesia TTS via the service proxy — speak text and play it.

Usage (from ros2_ws/src/cloud/clients/proxy-client)::

    python -m demos.cartesia_tts "Hello!"
    python -m demos.cartesia_tts

Env vars:
    INNATE_PROXY_URL    — proxy service URL
    INNATE_AUTH_URL     — auth service URL (for OIDC JWT exchange)
    INNATE_SERVICE_KEY  — robot service key
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

from innate_proxy import ProxyClient

VOICE_ID = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"


async def main() -> None:
    text = (
        " ".join(sys.argv[1:])
        or "Hello! This is a test of Cartesia text to speech through the proxy."
    )

    print(f'▶ Synthesising: "{text}"')

    async with ProxyClient() as client:
        resp = await client.request(
            service_name="cartesia",
            endpoint="/tts/bytes",
            method="POST",
            json={
                "model_id": "sonic-2",
                "transcript": text,
                "voice": {"mode": "id", "id": VOICE_ID},
                "output_format": {
                    "container": "wav",
                    "encoding": "pcm_s16le",
                    "sample_rate": 44100,
                },
            },
        )
    audio: bytes = resp.content

    print(f"  ✔ received {len(audio)} bytes of audio")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio)
        path = f.name

    try:
        player = _find_player()
        if player is None:
            print(f"⚠ No audio player found. WAV saved at {path}")
            return
        print(f"▶ Playing with {player[0]}…")
        subprocess.run([*player, path], check=True)
    finally:
        os.unlink(path)

    print("✔ Done.")


def _find_player() -> Optional[List[str]]:
    """Return a command list for the first available audio player."""
    candidates: List[List[str]] = [
        ["aplay", "-q"],
        ["paplay"],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


if __name__ == "__main__":
    asyncio.run(main())
