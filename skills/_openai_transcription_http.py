"""
HTTP helpers for OpenAI audio transcription via Innate proxy.

Not a skill module (leading underscore: excluded from skill_loader glob).

Root cause addressed: default httpx ``Accept-Encoding`` includes ``br`` and ``zstd``.
If ``brotli`` / ``zstandard`` are not installed, httpx uses IdentityDecoder for those
encodings and ``response.content`` stays compressed — JSON parse then sees binary garbage.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

# Prefer gzip/deflate only so servers do not pick br/zstd when decoders may be absent.
TRANSCRIPTION_REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}


def _safe_preview(raw: bytes, limit: int = 400) -> str:
    if not raw:
        return "(empty body)"
    try:
        return raw[:limit].decode("utf-8", errors="replace")
    except Exception:
        return repr(raw[: min(limit, 200)])


def _dict_from_utf8_json_bytes(blob: bytes) -> dict[str, Any] | None:
    try:
        s = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def transcription_json_from_response(resp: Any, logger: Any) -> tuple[dict[str, Any] | None, str | None]:
    """
    Parse OpenAI-style ``{"text": "..."}`` from an httpx response.

    Returns (data, None) on success, (None, error_message) on failure.
    """
    raw = resp.content or b""
    ct = (resp.headers.get("content-type") or "").lower()
    ce = (resp.headers.get("content-encoding") or "").lower()

    if resp.status_code != 200:
        return None, f"Transcription HTTP {resp.status_code}: {_safe_preview(raw)}"

    # 1) Normal path: httpx already decoded gzip/deflate; body is JSON text.
    parsed = _dict_from_utf8_json_bytes(raw)
    if parsed is not None:
        return parsed, None

    # 2) gzip body without httpx decode (mislabeled / edge proxy).
    if len(raw) >= 2 and raw[:2] == b"\x1f\x8b":
        try:
            parsed = _dict_from_utf8_json_bytes(gzip.decompress(raw))
            if parsed is not None:
                logger.info("[transcription_http] Parsed JSON after manual gzip decompress")
                return parsed, None
        except OSError:
            pass

    # 3) brotli (advertised or blind try — fails fast on non-br data).
    try:
        import brotli

        try:
            parsed = _dict_from_utf8_json_bytes(brotli.decompress(raw))
            if parsed is not None:
                logger.info("[transcription_http] Parsed JSON after manual brotli decompress")
                return parsed, None
        except Exception:
            pass
    except ImportError:
        pass

    # 4) zstd
    try:
        import zstandard as zstd

        try:
            dctx = zstd.ZstdDecompressor()
            parsed = _dict_from_utf8_json_bytes(dctx.decompress(raw))
            if parsed is not None:
                logger.info("[transcription_http] Parsed JSON after manual zstd decompress")
                return parsed, None
        except Exception:
            pass
    except ImportError:
        pass

    head = raw[:48].hex()
    return (
        None,
        "Transcription response was not JSON. "
        f"content-type={ct!r} content-encoding={ce!r} head_hex={head} preview={_safe_preview(raw, 200)!r}",
    )
