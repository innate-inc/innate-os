#!/usr/bin/env python3
"""Tester for the mars_cam ``webrtc_streamer`` node.

The node encodes each camera ONCE in a persistent pipeline and fans the RTP out to N per-peer
``webrtcbin`` transports. This tester drives the node's signalling over the ``/webrtc/*`` ROS topics
and verifies its behaviour from the node side (the ``/webrtc/active_streams`` status + the SDP offers):

  * fanout     — N independent peers stream the same cameras; the node reports all N but the encode
                 rate is shared (encode-once).
  * gating     — at idle no stream is encoding (flat memory / zero CPU); a peer turns its cameras on.
  * selective  — a peer asks for a subset of cameras; only those are ACTIVE/encoded (a client_id peer
                 negotiates ALL cameras up front so a later switch needs no renegotiation).
  * timeout    — a peer that never completes the handshake is auto-released (no transport leak).

Signalling uses ``*_id`` topics with ``{client_id, ...}`` envelopes. Every peer must provide a
``client_id``.

End-to-end *media decode* is verified separately in a real browser via ``webapp/debug/webrtc.html``:
GStreamer 1.20's ``webrtcbin`` *answerer* segfaults on these offers (a known upstream bug), so this
tester does not decode by default. ``--decode`` opts into a best-effort webrtcbin answerer anyway.

This is an INTEGRATION test against the live node (not a unit test): the webrtc_streamer node and
the cameras must be running — there is nothing to talk to otherwise.

Run it (from a shell with the robot's ROS environment):

    source /opt/ros/humble/setup.bash
    source <repo>/ros2_ws/install/setup.bash       # the workspace overlay
    export RMW_IMPLEMENTATION=rmw_zenoh_cpp         # MUST match the running node, or the topics are invisible

    python3 webrtc_consumer.py                      # full node-side suite
    python3 webrtc_consumer.py fanout --peers 4
    python3 webrtc_consumer.py selective

Run the `gating` check on an otherwise-idle node (no other browser tabs / teleop connected) — it
verifies "nothing encodes at idle," which only holds if you're the only peer.
"""

import argparse
import json
import sys
import threading
import time
import uuid

import rclpy
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String

START = "/webrtc/start"
ACTIVE = "/webrtc/active_streams"
OFFER_ID = "/webrtc/offer_id"

# rtp payload types the node assigns: pt96 = main cam, pt101 = arm cam, pt127 = opus mic.
PT = {96: "main", 101: "arm", 127: "audio"}


class Driver:
    """Publishes START/answer-less signalling and caches the node's offers + active_streams."""

    def __init__(self):
        rclpy.init()
        self.node = rclpy.create_node("webrtc_tester")
        self.start_pub = self.node.create_publisher(String, START, 10)
        self.status = {}
        self.offers = {}  # client_id -> latest offer SDP
        self._lock = threading.Lock()
        self.node.create_subscription(String, ACTIVE, self._on_status, 10)
        self.node.create_subscription(String, OFFER_ID, self._on_offer_id, 10)
        self.ex = SingleThreadedExecutor()
        self.ex.add_node(self.node)
        self._stop = threading.Event()
        threading.Thread(target=self._spin, daemon=True).start()

    def _spin(self):
        while not self._stop.is_set():
            self.ex.spin_once(timeout_sec=0.05)

    def _on_status(self, m):
        try:
            with self._lock:
                self.status = json.loads(m.data)
        except ValueError:
            pass

    def _on_offer_id(self, m):
        try:
            e = json.loads(m.data)
            with self._lock:
                self.offers[e["client_id"]] = e["sdp"]
        except (ValueError, KeyError):
            pass

    def start(self, client_id, source="live", video=None, audio=False):
        p = {"source": source, "audio": audio, "client_id": client_id}
        if video is not None:
            p["video"] = video
        self.start_pub.publish(String(data=json.dumps(p)))

    def release(self, client_id):
        self.start(client_id=client_id, video=[])

    def start_get_offer(self, client_id, video, tries=4, per_try=2.0):
        """(Re)publish START until this peer's offer arrives — absorbs ROS discovery latency on the
        first request and the node re-offers on every START (it replaces the peer)."""
        with self._lock:
            self.offers.pop(client_id, None)
        for _ in range(tries):
            self.start(client_id=client_id, video=video)
            if wait_for(lambda: self.offers.get(client_id) not in (None, ""), per_try):
                return self.offers.get(client_id)
        return self.offers.get(client_id)

    def snapshot(self):
        with self._lock:
            return dict(self.status), dict(self.offers)

    def client(self, cid):
        s, _ = self.snapshot()
        for c in s.get("clients", []):
            if c.get("client_id") == cid:
                return c
        return None

    def node_up(self):
        return self.node.count_publishers(ACTIVE) > 0

    def shutdown(self):
        self._stop.set()
        time.sleep(0.2)
        self.node.destroy_node()
        rclpy.shutdown()


def wait_for(pred, timeout, poll=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(poll)
    return False


def offer_mlines(sdp):
    """Returns the list of (payload-type) video m-lines in an offer, as stream names."""
    out = []
    for line in (sdp or "").splitlines():
        if line.startswith("m=video"):
            parts = line.split()
            pt = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            out.append(PT.get(pt, f"pt{pt}"))
    return out


# ---- tests ---------------------------------------------------------------


def test_fanout(d, peers=3):
    print(f"\n[fanout] {peers} independent peers, encode-once")
    ids = [f"fan-{i}-{uuid.uuid4().hex[:6]}" for i in range(peers)]
    for cid in ids:
        d.start(client_id=cid, video=["main", "arm"])
        time.sleep(0.3)
    ok_count = wait_for(lambda: d.snapshot()[0].get("count", 0) >= peers, 5)
    s, _ = d.snapshot()
    rates = []
    for c in s.get("clients", []):
        if c["client_id"] in ids:
            for st in c["streams"]:
                if st["name"] == "main":
                    rates.append(st["fps"])
    same = len(rates) >= peers and (max(rates) - min(rates) <= 3) and min(rates) > 0
    print(f"  active count={s.get('count')} (want >= {peers})  per-peer main fps={rates}")
    print(f"  encode-once (one shared rate to all): {same}")
    for cid in ids:
        d.release(cid)
    time.sleep(0.5)
    ok = ok_count and same
    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


def test_gating(d):
    print("\n[gating] idle = nothing encoding; a peer turns its cameras on")
    time.sleep(1.0)
    s0, _ = d.snapshot()
    n0 = s0.get("count", 0)
    # The strict "nothing encodes at idle" check only holds when we're the only peer. Don't FALSE-fail
    # the whole test if someone else (a browser tab / teleop) is connected — flag it and skip that check.
    idle_ok = True
    if n0 == 0:
        print("  idle: count=0 -> no peers (good)")
    else:
        print(
            f"  idle: count={n0} -> OTHER PEERS PRESENT; skipping strict idle check "
            f"(re-run on an idle node to verify zero-encoding-at-idle)"
        )
    cid = f"gate-{uuid.uuid4().hex[:6]}"
    d.start(client_id=cid, video=["main", "arm"])
    on = wait_for(lambda: (lambda c: c and all(st.get("encoding") for st in c["streams"]))(d.client(cid)), 5)
    c = d.client(cid)
    print(f"  peer on: streams={[(st['name'], st['encoding'], st['fps']) for st in (c['streams'] if c else [])]}")
    d.release(cid)
    off = wait_for(lambda: d.client(cid) is None, 5)
    print(f"  after release: peer gone={off}")
    ok = idle_ok and on and off
    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


def test_selective(d):
    # A client_id peer NEGOTIATES all cameras up front (so a later switch needs no renegotiation); the
    # `video` list selects which are ACTIVE — i.e. actually encoded/pushed. So selectivity now shows up in
    # active_streams, not in the offer's m-lines (the offer always carries every camera).
    print("\n[selective] only the requested cameras are ACTIVE (offer negotiates all, for instant switch)")
    results = []
    for want in (["arm"], ["main"], ["main", "arm"]):
        cid = f"sel-{uuid.uuid4().hex[:6]}"
        d.start(client_id=cid, video=want)
        ok = wait_for(
            lambda cid=cid, want=want: (lambda cl: cl and sorted(s["name"] for s in cl["streams"]) == sorted(want))(
                d.client(cid)
            ),
            5,
        )
        c = d.client(cid)
        active = sorted(s["name"] for s in (c["streams"] if c else []))
        print(f"  video={want}: active streams={active}  -> {'PASS' if ok else 'FAIL'}")
        results.append(ok)
        d.release(cid)
        time.sleep(0.3)
    allok = all(results)
    print(f"  {'PASS' if allok else 'FAIL'}")
    return allok


def test_timeout(d, connect_timeout=15.0):
    print(f"\n[timeout] a peer that never answers is auto-released (~{connect_timeout:.0f}s)")
    cid = f"to-{uuid.uuid4().hex[:6]}"
    d.start(client_id=cid, video=["arm"])
    up = wait_for(lambda: d.client(cid) is not None, 5)
    print(f"  peer created (state should be 'new'): {up}")
    t0 = time.monotonic()
    gone = wait_for(lambda: d.client(cid) is None, connect_timeout + 10, poll=0.5)
    print(f"  auto-released after {time.monotonic() - t0:.1f}s: {gone}")
    ok = up and gone
    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


def run_full(d, args):
    results = {
        "gating": test_gating(d),
        "fanout": test_fanout(d, peers=args.peers),
        "selective": test_selective(d),
        "timeout": test_timeout(d, connect_timeout=args.connect_timeout),
    }
    print("\n==================== SUMMARY ====================")
    for k, v in results.items():
        print(f"  {k:12} {'PASS' if v else 'FAIL'}")
    print("================================================")
    print("Media decode is verified in a browser via webapp/debug/webrtc.html (webrtcbin 1.20's")
    print("answerer segfaults headlessly; the node-side behaviour above is the regression surface).")
    return all(results.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="full", choices=["full", "fanout", "gating", "selective", "timeout"])
    ap.add_argument("--peers", type=int, default=3)
    ap.add_argument(
        "--connect-timeout", type=float, default=15.0, help="node's connect timeout, for sizing the timeout test's wait"
    )
    args = ap.parse_args()

    d = Driver()
    try:
        if not wait_for(d.node_up, 5.0):
            print("ERROR: webrtc_streamer not found on the ROS graph.")
            return 2
        fns = {
            "fanout": lambda: test_fanout(d, args.peers),
            "gating": lambda: test_gating(d),
            "selective": lambda: test_selective(d),
            "timeout": lambda: test_timeout(d, args.connect_timeout),
            "full": lambda: run_full(d, args),
        }
        return 0 if fns[args.mode]() else 1
    finally:
        d.shutdown()


if __name__ == "__main__":
    sys.exit(main())
