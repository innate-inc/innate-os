# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Benchmark: proxy vs direct Cartesia TTS — short & long text, 20 runs each.

Usage (from repo root)::

    python -m proxy_demos.bench_tts
    python -m proxy_demos.bench_tts --runs 10

Reads from .env: CARTESIA_API_KEY, INNATE_PROXY_URL, INNATE_SERVICE_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

from innate_proxy import ProxyClient

load_dotenv()

VOICE_ID = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"

SHORT_TEXT = "Hello from the robot!"
LONG_TEXT = (
    "Hello from the robot! This is a long audio, so it will take a bunch of "
    "time to stream hopefully. The quick brown fox jumps over the lazy dog."
)

REQUEST_TEMPLATE = {
    "model_id": "sonic-3.6",
    "voice": {"mode": "id", "id": VOICE_ID},
    "output_format": {
        "container": "wav",
        "encoding": "pcm_s16le",
        "sample_rate": 44100,
    },
}


@dataclass
class RunResult:
    ttfb: float
    transfer: float
    total: float
    size: int
    chunks: int


@dataclass
class BenchGroup:
    label: str
    results: list[RunResult] = field(default_factory=list)

    def add(self, r: RunResult) -> None:
        self.results.append(r)

    def summary(self) -> dict[str, str]:
        if not self.results:
            return {}
        ttfbs = [r.ttfb for r in self.results]
        transfers = [r.transfer for r in self.results]
        totals = [r.total for r in self.results]
        sizes = [r.size for r in self.results]
        chunks = [r.chunks for r in self.results]
        return {
            "TTFB": (
                f"{statistics.mean(ttfbs):.3f}s  (σ {statistics.stdev(ttfbs):.3f})"
                if len(ttfbs) > 1
                else f"{ttfbs[0]:.3f}s"
            ),
            "transfer": (
                f"{statistics.mean(transfers):.3f}s  (σ {statistics.stdev(transfers):.3f})"
                if len(transfers) > 1
                else f"{transfers[0]:.3f}s"
            ),
            "total": (
                f"{statistics.mean(totals):.3f}s  (σ {statistics.stdev(totals):.3f})"
                if len(totals) > 1
                else f"{totals[0]:.3f}s"
            ),
            "size": f"{statistics.mean(sizes):,.0f} bytes",
            "chunks": f"{statistics.mean(chunks):.0f}",
        }


async def _tts_request(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    text: str,
) -> RunResult:
    """Single streaming TTS request, returns timing stats."""
    body = {**REQUEST_TEMPLATE, "transcript": text}

    t_start = time.perf_counter()

    async with http.stream("POST", url, json=body, headers=headers) as stream:
        stream.raise_for_status()
        t_first_byte = time.perf_counter()

        total_size = 0
        chunk_count = 0
        async for chunk in stream.aiter_bytes():
            chunk_count += 1
            total_size += len(chunk)

    t_done = time.perf_counter()

    return RunResult(
        ttfb=t_first_byte - t_start,
        transfer=t_done - t_first_byte,
        total=t_done - t_start,
        size=total_size,
        chunks=chunk_count,
    )


def _print_table(groups: list[BenchGroup]) -> None:
    """Pretty-print a comparison table."""
    col_w = 36
    header = "  " + "".join(g.label.ljust(col_w) for g in groups)
    print(header)
    print("  " + "─" * col_w * len(groups))

    summaries = [g.summary() for g in groups]
    keys = list(summaries[0].keys()) if summaries[0] else []
    for key in keys:
        row = f"  {key:<12}"
        for s in summaries:
            row += s.get(key, "—").ljust(col_w)
        print(row)
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark proxy vs direct TTS")
    parser.add_argument("--runs", type=int, default=20, help="Runs per combination")
    args = parser.parse_args()
    runs: int = args.runs

    # ── Validate credentials ──────────────────────────────────────
    api_key = os.environ.get("CARTESIA_API_KEY", "")
    if not api_key:
        print("✗ CARTESIA_API_KEY not set")
        sys.exit(1)

    # ProxyClient handles auth (service key → JWT via OIDC)
    proxy_client = ProxyClient()
    if not proxy_client.is_available():
        print("✗ INNATE_PROXY_URL / INNATE_SERVICE_KEY not set")
        sys.exit(1)

    direct_headers = {
        "Authorization": f"Bearer {api_key}",
        "Cartesia-Version": "2025-04-16",
    }
    proxy_tts_url = f"{proxy_client.proxy_url}/v1/services/cartesia/tts/bytes"

    # ── Run benchmark ─────────────────────────────────────────────
    scenarios = [
        ("short", SHORT_TEXT),
        ("long", LONG_TEXT),
    ]

    for label, text in scenarios:
        proxy_group = BenchGroup(label=f"proxy ({label})")
        direct_group = BenchGroup(label=f"direct ({label})")

        print(f"\n{'═' * 60}")
        print(f"  {label.upper()} TEXT  ({len(text)} chars, {runs} runs)")
        print(f'  "{text[:60]}{"…" if len(text) > 60 else ""}"')
        print(f"{'═' * 60}\n")

        # Use persistent connections (one client per target) to
        # avoid measuring TLS handshake on every run.
        async with (
            httpx.AsyncClient(timeout=60.0) as http_proxy,
            httpx.AsyncClient(timeout=60.0) as http_direct,
        ):
            for i in range(runs):
                # Alternate: proxy first on even, direct first on odd
                if i % 2 == 0:
                    first = ("proxy", http_proxy, proxy_tts_url, {}, proxy_group)
                    second = (
                        "direct",
                        http_direct,
                        CARTESIA_URL,
                        direct_headers,
                        direct_group,
                    )
                else:
                    first = (
                        "direct",
                        http_direct,
                        CARTESIA_URL,
                        direct_headers,
                        direct_group,
                    )
                    second = ("proxy", http_proxy, proxy_tts_url, {}, proxy_group)

                for name, http, url, headers, group in [first, second]:
                    # Refresh proxy auth headers each run (JWT may rotate)
                    if name == "proxy":
                        headers = proxy_client._get_auth_headers()
                    r = await _tts_request(http, url, headers, text)
                    group.add(r)
                    tag = "P" if name == "proxy" else "D"
                    sys.stdout.write(
                        f"  [{i + 1:2d}/{runs}] {tag}  "
                        f"TTFB {r.ttfb:.3f}s  "
                        f"xfer {r.transfer:.3f}s  "
                        f"total {r.total:.3f}s  "
                        f"({r.size:,}B, {r.chunks}ch)\n"
                    )
                    sys.stdout.flush()

        print()
        _print_table([proxy_group, direct_group])

    print("✔ Benchmark complete.")
    await proxy_client.close_async()


if __name__ == "__main__":
    asyncio.run(main())
