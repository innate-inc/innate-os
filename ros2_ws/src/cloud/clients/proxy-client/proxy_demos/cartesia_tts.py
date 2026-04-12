"""Demo: Cartesia TTS via the service proxy — speak text and play it.

Usage (from repo root)::

    python -m proxy_demos.cartesia_tts "Hello!"
    python -m proxy_demos.cartesia_tts

Env vars:
    INNATE_PROXY_URL    — proxy service URL
    INNATE_AUTH_URL     — auth service URL (for OIDC JWT exchange)
    INNATE_SERVICE_KEY  — robot service key
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from innate_proxy import ProxyClient

VOICE_ID = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cartesia TTS demo (via proxy)")
    parser.add_argument("text", nargs="*", help="Text to synthesise")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Kill audio player as soon as download finishes",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    quick: bool = args.quick
    text = (
        " ".join(args.text)
        or "Hello! This is a test of Cartesia text to speech through the proxy."
    )

    print(f'▶ Synthesising: "{text}"')

    request_json = {
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": VOICE_ID},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
    }

    player_cmd = _find_player()

    import httpx

    async with ProxyClient() as client:
        url = f"{client.proxy_url}/v1/services/cartesia/tts/bytes"
        headers = client._get_auth_headers()

        t_start = time.perf_counter()

        async with httpx.AsyncClient(timeout=60.0) as http:
            async with http.stream(
                "POST", url, json=request_json, headers=headers
            ) as stream:
                stream.raise_for_status()
                t_first_byte = time.perf_counter()

                # Start the audio player with stdin pipe so it plays
                # immediately as chunks arrive — no temp file needed.
                player_proc: Optional[subprocess.Popen[bytes]] = None
                if player_cmd is not None:
                    player_proc = subprocess.Popen(player_cmd, stdin=subprocess.PIPE)

                total_size = 0
                chunk_count = 0

                # Decouple download from playback: the async loop
                # downloads at full network speed into a queue, and
                # a background task drains chunks to the player's
                # stdin via a thread (so blocking pipe writes don't
                # throttle the download).
                queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

                async def _player_writer() -> None:
                    """Drain queue → player stdin in a thread."""
                    assert player_proc is not None
                    assert player_proc.stdin is not None
                    while True:
                        chunk = await queue.get()
                        if chunk is None:
                            break
                        await asyncio.to_thread(player_proc.stdin.write, chunk)
                    player_proc.stdin.close()

                writer_task: Optional[asyncio.Task[None]] = None
                if player_proc is not None:
                    writer_task = asyncio.create_task(_player_writer())

                async for chunk in stream.aiter_bytes():
                    chunk_count += 1
                    total_size += len(chunk)
                    if writer_task is not None:
                        await queue.put(chunk)

                t_done = time.perf_counter()

                # Signal the writer that the download is complete,
                # then wait for it to finish flushing to the player.
                if writer_task is not None:
                    await queue.put(None)
                    await writer_task

    print(f"  ── profile ──────────────────────")
    print(f"  TTFB (first byte)  {t_first_byte - t_start:.3f}s")
    print(f"  transfer           {t_done - t_first_byte:.3f}s")
    print(f"  total request      {t_done - t_start:.3f}s")
    print(f"  response size      {total_size:,} bytes")
    print(f"  chunks             {chunk_count}")
    print(f"  ✔ received {total_size:,} bytes of audio")

    if player_proc is not None:
        if quick:
            player_proc.kill()
            print("  ⏩ killed player (--quick)")
        else:
            print(f"▶ Waiting for {player_cmd[0]} to finish…")
            player_proc.wait()
    else:
        print("⚠ No audio player found — audio was streamed but not played.")

    print("✔ Done.")


def _find_player() -> Optional[List[str]]:
    """Return a command list for the first available audio player."""
    candidates: List[List[str]] = [
        ["aplay", "-q", "-t", "wav", "-"],
        ["paplay"],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


if __name__ == "__main__":
    asyncio.run(main())
