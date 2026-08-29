"""Run the navigation benchmark: teleport, instruct, watch, score.

Metrics mirror the closed-loop eval harness so the numbers are comparable:
success (stopped inside the goal radius), oracle success (ever inside it),
and nav error (distance at the stop).
"""
import argparse, json, math, os, socket, struct, sys, threading, time
from websockets.sync.client import connect

WORLD_PORT, ROSBRIDGE = 8799, "ws://127.0.0.1:9090"
ACTION, ACTION_TYPE = "/innate_nav/navigate", "innate_cloud_msgs/action/NavigateInstruction"


class World:
    def __init__(self, host="127.0.0.1", port=WORLD_PORT):
        self.sock = socket.create_connection((host, port), timeout=15.0)
    def rpc(self, **req):
        p = json.dumps(req).encode()
        self.sock.sendall(struct.pack(">I", len(p)) + p)
        n = struct.unpack(">I", self._read(4))[0]
        return json.loads(self._read(n))
    def _read(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c: raise ConnectionError("world server closed")
            b += c
        return b
    def set_pose(self, x, y, yaw_deg):
        return self.rpc(op="set_pose", x=float(x), y=float(y), yaw=math.radians(yaw_deg))
    def pose(self):
        return self.rpc(op="state")["pose"]


class Ros:
    """One rosbridge connection per scenario — no state carried between runs."""
    def __init__(self):
        self.ws = connect(ROSBRIDGE, max_size=None)
        self.odom = None
        self.result = None
        self._stop = False
        threading.Thread(target=self._rx, daemon=True).start()
        self.ws.send(json.dumps({"op": "subscribe", "topic": "/odom",
                                 "type": "nav_msgs/msg/Odometry"}))
    def _rx(self):
        try:
            for raw in self.ws:
                if self._stop: return
                m = json.loads(raw)
                op = m.get("op")
                if op == "publish" and m["topic"] == "/odom":
                    p = m["msg"]["pose"]["pose"]
                    q = p["orientation"]
                    self.odom = (p["position"]["x"], p["position"]["y"],
                                 2.0 * math.atan2(q["z"], q["w"]))
                elif op in ("action_result", "send_action_goal_response"):
                    if m.get("result") is not None or op == "action_result":
                        self.result = m
        except Exception:
            pass
    def send_goal(self, instruction, goal_id, server=""):
        self.ws.send(json.dumps({
            "op": "send_action_goal", "id": goal_id, "action": ACTION,
            "action_type": ACTION_TYPE, "args": {"instruction": instruction, "server": server},
            "feedback": False, "goal_id": goal_id}))
    def cancel(self, goal_id):
        try:
            self.ws.send(json.dumps({"op": "cancel_action_goal", "id": goal_id,
                                     "action": ACTION, "goal_id": goal_id}))
        except Exception:
            pass
    def close(self):
        self._stop = True
        try: self.ws.close()
        except Exception: pass


def run_one(world, sc, timeout_s, server=""):
    sx, sy, syaw = sc["spawn"]
    gx, gy = sc["goal"]
    world.set_pose(sx, sy, syaw)
    time.sleep(1.5)                      # let the physics settle after a teleport

    ros = Ros()
    t0 = time.time()
    while ros.odom is None and time.time() - t0 < 15: time.sleep(0.1)
    if ros.odom is None:
        ros.close()
        return dict(id=sc["id"], outcome="no_odom", success=False, oracle=False)

    goal_id = f"bench_{sc['id']}_{int(t0)}"
    ros.send_goal(sc["instruction"], goal_id, server)
    traj, best = [], float("inf")
    start = time.time()
    while time.time() - start < timeout_s:
        if ros.odom:
            x, y, _ = ros.odom
            if not traj or (x - traj[-1][0])**2 + (y - traj[-1][1])**2 > 1e-4:
                traj.append((x, y))
            best = min(best, math.hypot(x - gx, y - gy))
        if ros.result is not None: break
        time.sleep(0.1)
    timed_out = ros.result is None
    if timed_out:
        ros.cancel(goal_id)
        time.sleep(2.0)
    dur = time.time() - start
    x, y, _ = ros.odom if ros.odom else (sx, sy, 0)
    ros.close()

    final = math.hypot(x - gx, y - gy)
    length = sum(math.dist(traj[i], traj[i+1]) for i in range(len(traj)-1)) if len(traj) > 1 else 0.0
    return dict(id=sc["id"], instruction=sc["instruction"], turn=sc["turn"],
                final_xy=[round(x, 3), round(y, 3)],
                path_m=sc["path_m"], spawn=sc["spawn"], goal=sc["goal"],
                final_dist_m=round(final, 3), best_dist_m=round(best, 3),
                travelled_m=round(length, 2), duration_s=round(dur, 1),
                outcome="timeout" if timed_out else "stopped",
                success=bool(final <= sc["radius"]), oracle=bool(best <= sc["radius"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="/tmp/bench/scenarios.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--timeout", type=float, default=75.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--server", default="", help="policy server for this run; empty = the robot's own")
    a = ap.parse_args()

    scen = json.load(open(a.scenarios))
    if a.limit: scen = scen[:a.limit]
    results = []
    for i, sc in enumerate(scen, 1):
        try:
            # Fresh connection per scenario: one held across a 35 min sweep
            # went stale and cost the last four runs of a checkpoint.
            r = run_one(World(), sc, a.timeout, a.server)
        except Exception as exc:                       # one bad scenario must not end the sweep
            r = dict(id=sc["id"], outcome=f"error: {exc!r}", success=False, oracle=False)
        results.append(r)
        print(f"  [{i:>2}/{len(scen)}] {sc['id']} {sc['turn']:8} "
              f"{r.get('outcome','?'):8} final {r.get('final_dist_m','-')} m "
              f"{'OK' if r.get('success') else '  '}", flush=True)
        json.dump({"label": a.label, "results": results}, open(a.out, "w"), indent=1)
    ok = sum(r["success"] for r in results)
    orc = sum(r["oracle"] for r in results)
    print(f"\n{a.label}: success {ok}/{len(results)} ({ok/len(results):.0%}), "
          f"oracle {orc}/{len(results)} ({orc/len(results):.0%})")


if __name__ == "__main__":
    main()
