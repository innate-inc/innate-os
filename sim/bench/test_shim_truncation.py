"""A truncated model stream must still LOOK truncated after the shim relays it.

THE HAZARD. Google serves the turn stream as HTTP/1.1 with
`Transfer-Encoding: chunked`. Chunked framing is self-terminating: the body
ends with a zero-length chunk, so a stream that dies early is DETECTABLE, and
httpx raises `RemoteProtocolError("peer closed connection without sending
complete message body")`. The brain then fails the turn and retries -- loudly,
correctly.

If a relay re-frames that response as close-delimited (HTTP/1.0, no
Content-Length, no Transfer-Encoding), "the connection closed" IS the
end-of-message signal. A truncated body becomes byte-identical to a complete
one. No exception reaches the brain; `_sse_chunks` yields the parts that did
arrive; `absorb()` commits them to history as if the model had finished.

Concretely: the model plans `navigate(counter)` then `pick_any_object(mug)`
then stops. The stream dies after the second event. The robot drives to the
counter, never picks up the mug, and its own conversation history says that was
the whole answer. Nothing is logged. The final chunk also carries
`usageMetadata`, so the turn is never metered and the benchmark's cost figure
under-reports it.

This drives the REAL handler against a fake upstream that dies mid-body, and
asserts the client can still tell. Runs offline; costs nothing.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import socket
import threading
from http.server import ThreadingHTTPServer

import gemini_shim
import pytest

EVENTS = [b'data: {"seg": 0}\n\n', b'data: {"seg": 1}\n\n']


class DyingUpstream(threading.Thread):
    """Serves chunked SSE and hangs up mid-body, without the final 0 chunk.

    Raw sockets rather than http.server: the whole point is emitting framing
    that a well-behaved server never would."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            for event in EVENTS:
                conn.sendall(f"{len(event):X}\r\n".encode() + event + b"\r\n")
            # and then it dies: no terminating "0\r\n\r\n"
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()  # accept() raises, run() returns


def raw_response(host: str, port: int, path: str) -> tuple[bytes, bytes]:
    """(header block, body bytes) straight off the socket, no client library.

    Asserting on the FRAMING rather than on some client's reaction is the whole
    point. `urllib` happily tolerates a chunked body that never terminates --
    it swallows the truncation the same way the shim did -- so using it as the
    control proves nothing. httpx/h11, which is what the brain actually uses,
    is strict. Rather than depend on which client is stricter, check the
    invariant directly: does the relayed response carry self-terminating
    framing, and is the terminator present only when the upstream really
    finished?
    """
    sock = socket.create_connection((host, port), timeout=20)
    try:
        sock.sendall(
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"Content-Length: 2\r\nContent-Type: application/json\r\n\r\n{{}}".encode()
        )
        buffer = b""
        while True:
            try:
                piece = sock.recv(65536)
            except OSError:
                break
            if not piece:
                break
            buffer += piece
    finally:
        sock.close()
    head, _, body = buffer.partition(b"\r\n\r\n")
    return head, body


@pytest.fixture(scope="module")
def relayed() -> tuple[bytes, bytes]:
    """(head, body) of one stream relayed by the real Handler from an upstream
    that died mid-body. Both servers are shut down and their sockets closed
    however the request went; the module globals the shim reads are restored."""
    saved_upstream, saved_key = gemini_shim.UPSTREAM, gemini_shim.Handler.api_key
    upstream = DyingUpstream()
    upstream.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gemini_shim.Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        gemini_shim.UPSTREAM = f"http://127.0.0.1:{upstream.port}"
        gemini_shim.Handler.api_key = "test-key-not-real"
        serving.start()
        yield raw_response("127.0.0.1", server.server_port, "/v1beta/models/m:streamGenerateContent?alt=sse")
    finally:
        server.shutdown()
        server.server_close()
        upstream.close()
        upstream.join(timeout=5)
        gemini_shim.UPSTREAM, gemini_shim.Handler.api_key = saved_upstream, saved_key


def _self_terminating(head: bytes) -> bool:
    lowered = head.lower()
    return b"transfer-encoding: chunked" in lowered or b"content-length:" in lowered


def test_both_sse_events_were_relayed(relayed) -> None:
    _, body = relayed
    assert body.count(b"data: ") == len(EVENTS)


def test_the_relayed_response_uses_self_terminating_framing(relayed) -> None:
    # Self-terminating framing is the whole defence. Close-delimited (HTTP/1.0,
    # no length, no transfer-encoding) makes truncation indistinguishable from
    # completion, because the close IS the terminator.
    head, _ = relayed
    assert _self_terminating(head), head.decode(errors="replace").splitlines()[0] if head else "no head"


def test_a_truncated_upstream_yields_no_terminating_chunk(relayed) -> None:
    # Upstream died without its final chunk, so ours must be missing too --
    # that absence is exactly what makes a strict client raise.
    head, body = relayed
    assert _self_terminating(head) and not body.rstrip().endswith(b"0"), repr(body[-24:])


def test_no_http_status_line_is_injected_into_the_relayed_body(relayed) -> None:
    # The mid-stream error path must not append a second status line into a
    # body whose headers were already flushed. `_sse_chunks` drops any line
    # not starting with "data: ", so injected junk vanishes without a trace --
    # which is what turns a relay failure into a silent one.
    _, body = relayed
    assert b"HTTP/1." not in body, repr(body[-120:])
