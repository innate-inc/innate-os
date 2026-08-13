"""Capture a spatial-memory dataset from the simulated apartment.

Drops the benchmark props, walks the robot through every room (physics drive
or teleport-glide), and saves main-camera JPEGs with pose + timestamp:

    frames/NNNN.jpg     every captured frame
    manifest.jsonl      one record per frame: stamp, x, y, yaw, room, kind
    scene.json          settled prop poses, room registry, tour + capture meta
    data/               a production-format spatial memory ($INNATE_OS_ROOT
                        shape): maps/sim_apartment.pgm + spatial_memory/
                        sim_apartment/{index.json, N.jpg}, built by feeding
                        every frame through brain_client's plan_admission --
                        exactly what the robot's recorder would have kept.

Run from sim/:  uv run benchmarks/spatial_memory/capture.py --out benchmarks/spatial_memory/dataset/home_tour
Probe a pose:   uv run benchmarks/spatial_memory/capture.py --probe " -4.3,-2.1,90"
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import _paths  # noqa: F401  (sys.path: mars_sim_driver + brain_client)
import mujoco
from apartment import ROOMS, SCENE, TOUR, Stop, room_at
from mars_sim_driver.core import VirtualMars

from brain_client.memory.coverage import Coverage
from brain_client.memory.selection import plan_admission
from brain_client.memory.store import MemoryStore
from brain_client.state.map import Map

MAP_NAME = "sim_apartment.yaml"
MAP_SRC = _paths.SIM_DIR / "assets" / "map"
TURN_SPEED = 1.0  # rad/s, the base's MAX_YAW -- times turn frames in glide mode


def teleport(sim: VirtualMars, x: float, y: float, yaw: float) -> None:
    m, d = sim.model, sim.data
    for short, value in (("x", x), ("y", y), ("yaw", yaw)):
        jid = m.joint(f"robot_base_{short}").id
        d.qpos[m.jnt_qposadr[jid]] = value
        d.qvel[m.jnt_dofadr[jid]] = 0.0
    d.xfrc_applied[sim._base_id] = 0.0
    sim._hold = sim._still_since = None
    sim._cmd_vx = sim._cmd_wz = 0.0
    mujoco.mj_forward(m, d)


def wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def build_scene(sim: VirtualMars) -> list[dict]:
    for p in SCENE:
        if not sim.drop_prop_at(p.prop, p.x, p.y, math.radians(p.yaw_deg)):
            raise SystemExit(f"unknown prop {p.prop!r}")
    sim.step(3.0)  # let everything settle onto tables/floors
    settled = sim.object_poses()
    return [
        {
            "prop": p.prop,
            "room": p.room,
            "note": p.note,
            "dropped_at": [p.x, p.y],
            "settled": [round(v, 3) for v in settled[p.prop][:3]],
        }
        for p in SCENE
    ]


@dataclass
class Recorder:
    """Accumulates frames; sim time is the tour clock, stamps are anchored to
    wall clock at the end so the newest memory reads as 'just now'."""

    sim: VirtualMars
    out: Path
    frames: list[dict]

    def capture(self, t_sim: float, kind: str, stop: str) -> None:
        x, y, yaw = self.sim.pose()
        i = len(self.frames) + 1
        rel = f"frames/{i:04d}.jpg"
        (self.out / rel).write_bytes(self.sim.render_jpeg("main"))
        self.frames.append(
            {
                "i": i,
                "file": rel,
                "t_sim": round(t_sim, 2),
                "x": round(x, 3),
                "y": round(y, 3),
                "yaw": round(yaw, 4),
                "room": room_at(x, y),
                "kind": kind,
                "stop": stop,
            }
        )


def run_glide(sim: VirtualMars, rec: Recorder, speed: float, fps: float) -> float:
    """Teleport along the tour as if driving at `speed`, capturing at `fps`.
    Deterministic and collision-proof; physics only settles the props."""
    ds = speed / fps
    t = 0.0
    prev: Stop | None = None
    for stop in TOUR:
        if prev is not None:
            dist = math.hypot(stop.x - prev.x, stop.y - prev.y)
            heading = math.atan2(stop.y - prev.y, stop.x - prev.x)
            if stop.jump:
                teleport(sim, stop.x, stop.y, heading)
                t += dist / speed  # time passes as if driven; no frames
            else:
                steps = max(1, round(dist / ds))
                for k in range(1, steps + 1):
                    f = k / steps
                    teleport(sim, prev.x + f * (stop.x - prev.x), prev.y + f * (stop.y - prev.y), heading)
                    t += dist / steps / speed
                    rec.capture(t, "glide", stop.name)
        for look in stop.looks:
            target = math.radians(look)
            t += abs(wrap(target - sim.pose()[2])) / TURN_SPEED
            teleport(sim, stop.x, stop.y, target)
            rec.capture(t, "look", stop.name)
        prev = stop
    return t


def run_drive(sim: VirtualMars, rec: Recorder, speed: float, fps: float) -> float:
    """Drive the tour with real physics, capturing every 1/fps sim seconds.
    A leg that stops progressing (wedged on furniture) is finished by teleport."""
    period = 1.0 / fps
    state = {"t": 0.0, "next": period}

    def tick(dt: float, stop: Stop) -> None:
        state["t"] += dt
        if state["t"] >= state["next"]:
            rec.capture(state["t"], "drive", stop.name)
            state["next"] += period

    def turn_to(target: float, stop: Stop) -> None:
        while abs(err := wrap(target - sim.pose()[2])) > 0.08:
            sim.set_cmd_vel(0.0, max(-1.0, min(1.0, 2.5 * err)))
            sim.step(0.1)
            tick(0.1, stop)

    for stop in TOUR:
        if stop.jump:
            teleport(sim, stop.x, stop.y, sim.pose()[2])
        best = math.inf
        best_t = state["t"]
        while (dist := math.hypot(stop.x - sim.pose()[0], stop.y - sim.pose()[1])) > 0.3:
            if dist < best - 0.05:
                best, best_t = dist, state["t"]
            if state["t"] - best_t > 8.0:
                print(f"  [drive] wedged {dist:.2f}m from {stop.name}; teleporting the rest of the leg")
                teleport(sim, stop.x, stop.y, sim.pose()[2])
                break
            x, y, yaw = sim.pose()
            err = wrap(math.atan2(stop.y - y, stop.x - x) - yaw)
            sim.set_cmd_vel(speed if abs(err) < 0.6 else 0.0, max(-1.0, min(1.0, 2.5 * err)))
            sim.step(0.1)
            tick(0.1, stop)
        for look in stop.looks:
            turn_to(math.radians(look), stop)
            rec.capture(state["t"], "look", stop.name)
    sim.set_cmd_vel(0.0, 0.0)
    return state["t"]


def occupancy_map(sim: VirtualMars) -> Map:
    """The world as the recorder sees it on /map (recorder._on_map). Map.grid
    only reads raw_source.data, so a bare namespace stands in for the ROS
    message."""
    resolution = 0.05
    grid, origin_x, origin_y = sim.occupancy_grid(resolution)
    return Map(
        resolution=resolution,
        width=grid.shape[1],
        height=grid.shape[0],
        origin_x=origin_x,
        origin_y=origin_y,
        raw_source=SimpleNamespace(data=grid.ravel()),
    )


def build_store(out: Path, frames: list[dict], t_end_wall: float, duration: float, grid: Map) -> int:
    """Feed every frame through the production admission policy — grid and
    Coverage passed exactly as the robot's recorder does (recorder._record) —
    into a production-format MemoryStore under out/data/."""
    maps_dir = out / "data" / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    for f in MAP_SRC.iterdir():
        shutil.copy2(f, maps_dir / f.name)
    store = MemoryStore(out / "data")
    store.switch_map(MAP_NAME)
    coverage = Coverage()
    for frame in frames:
        stamp = t_end_wall - duration + frame["t_sim"]
        adm = plan_admission(store.snapshot().memories, frame["x"], frame["y"], frame["yaw"], stamp, grid, coverage)
        if not adm.record:
            continue
        jpeg = (out / frame["file"]).read_bytes()
        if adm.replace is not None:
            store.replace(adm.replace, frame["x"], frame["y"], frame["yaw"], stamp, jpeg)
            continue
        if adm.evict is not None:
            store.evict(adm.evict)
        store.add(frame["x"], frame["y"], frame["yaw"], stamp, jpeg)
    return len(store.snapshot().memories)


def probe(spec: str) -> None:
    sim = VirtualMars()
    build_scene(sim)
    x, y, yaw_deg = (float(v) for v in spec.split(","))
    teleport(sim, x, y, math.radians(yaw_deg))
    path = _paths.SIM_DIR / "benchmarks" / "spatial_memory" / "probe.jpg"
    path.write_bytes(sim.render_jpeg("main"))
    print(f"pose ({x}, {y}, {yaw_deg} deg) room={room_at(x, y)} -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("benchmarks/spatial_memory/dataset/home_tour"))
    ap.add_argument("--mode", choices=("glide", "drive"), default="glide")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--speed", type=float, default=0.35)
    ap.add_argument("--probe", help="x,y,yaw_deg -- render one frame and exit")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
        return

    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "frames").mkdir(parents=True)

    t0 = time.time()
    sim = VirtualMars()
    props = build_scene(sim)
    print(f"scene ready ({time.time() - t0:.1f}s): {', '.join(p['prop'] for p in props)}")

    rec = Recorder(sim, out, [])
    duration = (run_drive if args.mode == "drive" else run_glide)(sim, rec, args.speed, args.fps)
    t_end = time.time()
    print(f"tour: {len(rec.frames)} frames over {duration:.0f} sim-seconds ({time.time() - t0:.1f}s wall)")

    for frame in rec.frames:
        frame["stamp"] = round(t_end - duration + frame["t_sim"], 2)
    with (out / "manifest.jsonl").open("w") as fh:
        for frame in rec.frames:
            fh.write(json.dumps(frame) + "\n")

    kept = build_store(out, rec.frames, t_end, duration, occupancy_map(sim))
    (out / "scene.json").write_text(
        json.dumps(
            {
                "map": MAP_NAME,
                "rooms": {
                    n: {"bounds": [r.x0, r.y0, r.x1, r.y1], "description": r.description} for n, r in ROOMS.items()
                },
                "props": props,
                "tour": [{"name": s.name, "x": s.x, "y": s.y, "looks": list(s.looks)} for s in TOUR],
                "capture": {
                    "mode": args.mode,
                    "fps": args.fps,
                    "speed": args.speed,
                    "frames": len(rec.frames),
                    "memories_kept": kept,
                    "sim_duration_s": round(duration, 1),
                    "captured_at": t_end,
                },
            },
            indent=2,
        )
    )
    print(f"dataset at {out}: {len(rec.frames)} frames, {kept} admitted to the memory store")


if __name__ == "__main__":
    main()
