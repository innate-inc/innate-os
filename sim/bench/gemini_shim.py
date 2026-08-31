#!/usr/bin/env python3
"""Stand in for the Innate proxy so grasping works without a service key.

WHY. `pick_any_object` finds the object and verifies the grasp through
`innate.gemini.make_client()`, which returns a ProxyClient when
INNATE_SERVICE_KEY is set, a `_DirectClient(GEMINI_BASE_URL)` when that is set
instead, and None when neither is -- and `execute()` fails on the first line
when it is None. That blocks the 22 of 45 challenges that need a pick, 13 of
the 17 in category 2 (see capabilities.py). Innate built the GEMINI_BASE_URL seam for exactly this case; it just needs
something at the other end.

WHY A SHIM AND NOT THE URL ON ITS OWN. `_DirectClient` sends no auth header and
appends a fixed `/v1/chat/completions`. Google's OpenAI-compatible surface
wants `Authorization: Bearer` and lives at `/v1beta/openai/chat/completions`.
Both disagree, so GEMINI_BASE_URL cannot point straight at Google.

THE CONSTRAINT THAT SHAPES EVERYTHING HERE. GEMINI_BASE_URL is read by BOTH
seams: `innate/gemini.py` for skill vision, and `brain/transport.py:35` for the
brain's own turns, which use the NATIVE Gemini API
(`/v1beta/models/{model}:streamGenerateContent?alt=sse`) and consume it as a
live SSE stream. Point that variable at anything that only speaks
OpenAI-compatible chat and the brain stops working entirely.

So this is a transparent key-injecting reverse proxy, not a translator:

  /v1/chat/completions  ->  /v1beta/openai/chat/completions   Authorization: Bearer
  anything else         ->  the same path, untouched          x-goog-api-key

Nothing else is rewritten. The model name is passed through as sent -- gemlib
asks for `gemini-3.5-flash`, which Google serves, so skill vision runs on the
same model it would through the proxy. Response bytes are relayed as they
arrive rather than buffered, because the brain's turn loop reads SSE
incrementally and buffering would change its timing.

  usage: gemini_shim.py [port]        (default 8099; needs GEMINI_API_KEY)

Then GEMINI_BASE_URL=http://host.docker.internal:<port> in .env, which is how
the container reaches a host process -- the webapp already talks to the host
world server the same way.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://generativelanguage.googleapis.com"
OPENAI_PATH = "/v1/chat/completions"  # what innate/gemini.py sends
OPENAI_UPSTREAM = "/v1beta/openai/chat/completions"  # where Google serves it
CHUNK = 8192

# Headers worth carrying upstream. Everything else is either hop-by-hop or
# ours to set: Host must not be the shim's, and auth is injected per route.
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "x-goog-upload-protocol")
# Relayed back untouched. Content-Length is deliberately absent: the body is
# re-framed as chunked, which carries its own length per chunk.
FORWARD_RESPONSE_HEADERS = ("content-type",)


class Handler(BaseHTTPRequestHandler):
    api_key = ""
    # HTTP/1.1 with CHUNKED framing, and this is not a style choice.
    #
    # Google serves the turn stream chunked, which is self-terminating: the
    # body ends with a zero-length chunk, so a stream that dies early is
    # DETECTABLE and httpx raises RemoteProtocolError. The brain fails the turn
    # and retries. Loud, and correct.
    #
    # An earlier version answered HTTP/1.0 with no length and no
    # transfer-encoding -- close-delimited, where "the connection closed" IS
    # the end-of-message signal. That makes a truncated body byte-identical to
    # a complete one. The brain would accept half a plan as the whole answer,
    # commit it to history, drive to the counter and never pick up the mug,
    # with nothing logged anywhere. The final chunk also carries usageMetadata,
    # so the turn would go unmetered and the cost figures would under-report.
    # Re-framing as chunked keeps the truncation visible; see
    # sim/bench/test_shim_truncation.py.
    protocol_version = "HTTP/1.1"
    # Idle timeout on a kept-alive connection, and the value is chosen against
    # the client's. HTTP/1.1 means httpx now POOLS connections to this shim,
    # so whichever side expires first decides who sees the close. transport.py
    # sets keepalive_expiry to 30s; anything near that here races it, and the
    # loser is a request sent down a socket the other end just dropped -- the
    # "Server disconnected without sending a response" this whole series is
    # about. Keeping ours comfortably longer means the client always retires
    # the connection first, on its own terms.
    timeout = 120

    def log_message(self, *_args) -> None:
        pass  # one deliberate line per request instead, in _relay

    def do_GET(self) -> None:  # noqa: N802
        self._relay("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._relay("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._relay("DELETE")

    def _fail(self, code: int, payload: bytes, headers_sent: bool, content_type: str = "application/json") -> None:
        """Report a failure without corrupting a response already in flight.

        If the headers went out, the body has begun and a status line can no
        longer be sent -- appending one writes `HTTP/1.0 502 ...` and a header
        block INTO the body. `_sse_chunks` drops every line that does not start
        with "data: ", so that junk vanished silently and the caller saw a
        short but apparently clean stream. Once the body is open the only
        honest signal is to stop writing and close WITHOUT the terminating
        chunk, which is precisely what a strict client reads as truncation.
        """
        try:
            if headers_sent:
                self.close_connection = True
                return
            self.send_response(code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # client already gone

    def _relay(self, method: str) -> None:
        started = time.time()
        first_at: float | None = None  # when the first upstream byte was relayed

        # A chunked request body has no Content-Length, and reading zero bytes
        # would forward an EMPTY body upstream while the caller believes it
        # sent the whole thing -- Google answers 400 and the caller has no idea
        # why. Refuse instead. Not reachable through httpx's json=/content=
        # bytes, but it is one `content=<file object>` away.
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._fail(411, b'{"error": "shim requires Content-Length"}', False)
            print(f"{method} {self.path.split('?')[0]} -> 411 (chunked request body)", flush=True)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            # Raised BEFORE the try below, so this used to escape _relay and
            # drop the connection with nothing logged.
            self._fail(400, b'{"error": "bad Content-Length"}', False)
            print(f"{method} {self.path.split('?')[0]} -> 400 (bad Content-Length)", flush=True)
            return
        body = self.rfile.read(length) if length else None

        if self.path.split("?")[0] == OPENAI_PATH:
            path, auth = OPENAI_UPSTREAM, ("Authorization", f"Bearer {self.api_key}")
        else:
            path, auth = self.path, ("x-goog-api-key", self.api_key)

        request = urllib.request.Request(UPSTREAM + path, data=body, method=method)
        for name in FORWARD_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value:
                request.add_header(name, value)
        request.add_header(*auth)

        sent = 0
        headers_sent = False  # once true, the body has begun and cannot carry a status
        try:
            with urllib.request.urlopen(request, timeout=180) as upstream:
                self.send_response(upstream.status)
                for name in FORWARD_RESPONSE_HEADERS:
                    value = upstream.headers.get(name)
                    if value:
                        self.send_header(name, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                headers_sent = True
                # read1, NOT read. `read(n)` on a buffered socket blocks until
                # it has n bytes or the stream ends, so an 8KB chunk size holds
                # a whole SSE response back -- and a turn here averages 11
                # output tokens, far under 8KB, so every one would arrive in a
                # single lump at the end. `read1` returns whatever has already
                # landed, which is what makes this a relay instead of a buffer.
                read = getattr(upstream, "read1", None) or upstream.read
                while True:
                    chunk = read(CHUNK)
                    if not chunk:
                        break
                    if first_at is None:
                        first_at = time.time() - started
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    sent += len(chunk)
                # The terminating chunk, written ONLY on a clean upstream read.
                # Its absence is the signal a strict client needs.
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            status = upstream.status
        except urllib.error.HTTPError as error:
            # Pass the real status and body through: callers branch on the
            # status (a 404 latches a feature off, a 403 means the key lacks
            # a scope) and swallowing it into a 502 would hide which.
            #
            # `error.read()` can itself raise (a non-200 whose declared
            # Content-Length is never delivered raises IncompleteRead), and a
            # raise inside an except clause is NOT caught by a sibling except
            # -- it escaped _relay entirely, dropping the connection with zero
            # bytes written. That produced exactly the "Server disconnected
            # without sending a response" this whole shim is meant to survive,
            # and skipped the per-request log line, so it was invisible.
            try:
                payload = error.read()
            except Exception:  # noqa: BLE001
                payload = b'{"error": "upstream error body was truncated"}'
            status = error.code
            self._fail(error.code, payload, headers_sent, error.headers.get("content-type", "application/json"))
            sent = len(payload)
        except Exception as error:  # noqa: BLE001 -- a shim never takes the stack down
            status = 502
            payload = (f'{{"error": "shim could not reach upstream: {type(error).__name__}"}}').encode()
            self._fail(502, payload, headers_sent)
            # Named in the log, because "502 0B" reads as "sent nothing" when
            # the useful fact is WHY, and whether the body had already begun.
            print(
                f"    upstream {type(error).__name__}"
                f"{' MID-BODY (client sees a truncated stream)' if headers_sent else ''}",
                flush=True,
            )
            sent = 0 if headers_sent else len(payload)

        # first-byte time as well as total: the gap between them is the only
        # way to see from a log whether this is relaying or accumulating.
        first = f"first {1000 * first_at:.0f}ms " if first_at is not None else ""
        print(
            f"{method} {self.path.split('?')[0]} -> {status} {sent}B "
            f"{first}total {1000 * (time.time() - started):.0f}ms",
            flush=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("port", nargs="?", type=int, default=8099)
    port = ap.parse_args().port
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY is not set; nothing to inject", file=sys.stderr)
        return 2
    Handler.api_key = key
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"gemini shim on :{port} -> {UPSTREAM}  ({OPENAI_PATH} -> {OPENAI_UPSTREAM}, everything else passed through)",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
