"""Validate the sink cabinet and optionally record a physics replay for Three.js.

From repo root: sim/.venv/bin/python sim/sandbox/cabinet_demo.py --record
Then: cd sim/viewer && npm exec vite -- --host 127.0.0.1 --port 5187
Open http://127.0.0.1:5187/cabinet-demo.html .
The replay uses external hinge torque; it is not an autonomous robot grasp.
"""

import argparse
import json
import math
from pathlib import Path

import _driver_pkg  # noqa: F401
import mujoco
import numpy as np
from mars_sim_driver import cabinet
from mars_sim_driver.core import VirtualMars


def validate(sim):
    m, d = sim.model, sim.data
    bid = m.body(cabinet.config()["name"]).id
    j = m.joint("cabinet_hinge")
    q, v = j.qposadr[0], j.dofadr[0]
    # Every five degrees, including both stops: no static geometry intersects
    # the panel or handle (the old baked cabinet hulls used to fill this space).
    for deg in range(0, 101, 5):
        d.qpos[q] = math.radians(deg)
        mujoco.mj_forward(m, d)
        for c in d.contact:
            if bid in (m.geom_bodyid[c.geom1], m.geom_bodyid[c.geom2]):
                assert c.dist > -0.0001, (deg, m.geom(c.geom1).name, m.geom(c.geom2).name, c.dist)
    sim.reset()
    sim.step(0.5)
    assert abs(d.qpos[q]) < 0.001, "closed door drifts"
    # External force opens the passive joint and respects its upper stop.
    d.xfrc_applied[bid, 5] = -1
    sim.step(3)
    assert math.radians(99) < d.qpos[q] < math.radians(101), d.qpos[q]
    assert np.isfinite(d.qpos).all()
    sim.reset()
    assert d.qpos[q] == 0 and d.qvel[v] == 0
    assert np.allclose(sim.object_poses()[cabinet.config()["name"]][:3], [-1.639, -0.3474, 0.102])
    assert sim.set_cabinet_open(True)
    sim.step(4)
    assert math.radians(85) < d.qpos[q] < math.radians(95)
    assert sim._cabinet_target is None
    sim.set_cabinet_open(False)
    sim.step(4)
    assert abs(d.qpos[q]) < math.radians(2)
    sim.set_cabinet_open(True)
    sim.step(0.2)
    sim.reset()
    assert sim._cabinet_target is None and d.qpos[q] == 0
    try:
        sim.set_cabinet_open("false")
    except ValueError:
        pass
    else:
        raise AssertionError("non-boolean cabinet command accepted")
    # The real URDF reaches handle height with a level wrist and bent elbow.
    # joint1 points out the robot's right side, avoiding its head/arm guard.
    angles = {
        "joint1": -math.pi / 2,
        "joint2": -0.40998245,
        "joint3": -0.3,
        "joint4": 0.70998245,
        "joint5": 0,
        "joint6": 0.8727,
        "joint6M": -0.8727,
    }
    for n, a in angles.items():
        d.qpos[m.joint("robot_" + n).qposadr[0]] = a
    mujoco.mj_forward(m, d)
    ee = d.xpos[m.body("robot_ee_link").id]
    assert abs(ee[2] - cabinet.config()["handleHeight"]) < 0.001, ee
    # 16mm bar, 40mm finger clearance behind it, 120mm vertical grasp segment.
    assert cabinet.config()["handleRadius"] * 2 < 0.025
    sim.reset()
    print("PASS: 21 collision-free angles, passive opening, hinge stop, rest/reset, 30 cm arm reach.", flush=True)


def record(sim, path):
    m, d = sim.model, sim.data
    j = m.joint("cabinet_hinge")
    q, v = j.qposadr[0], j.dofadr[0]
    frames = []
    for i in range(600):
        t = i / 60
        if t < 1:
            target = 0
        elif t < 4:
            target = math.pi / 2 * (0.5 - 0.5 * math.cos(math.pi * (t - 1) / 3))
        elif t < 6:
            target = math.pi / 2
        elif t < 9:
            target = math.pi / 2 * (0.5 + 0.5 * math.cos(math.pi * (t - 6) / 3))
        else:
            target = 0
        # Apply a bounded external torque to the passive hinge. No qpos
        # animation: all recorded poses come from the full apartment solver.
        d.xfrc_applied[m.body(cabinet.config()["name"]).id, 5] = -np.clip(
            8 * (target - d.qpos[q]) - 1.3 * d.qvel[v], -0.8, 0.8
        )
        sim.step(1 / 60)
        frames.append({"t": round(i / 60, 5), "angle": float(d.qpos[q]), "objects": cabinet.pose(m, d)})
    assert max(f["angle"] for f in frames) > math.radians(85)
    assert abs(frames[-1]["angle"]) < math.radians(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"source": "MuJoCo full apartment; externally driven passive hinge", "frames": frames},
            separators=(",", ":"),
        )
    )
    print(f"Recorded {len(frames)} physics frames: {path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    sim = VirtualMars(render_wh=(160, 120))
    validate(sim)
    if args.record:
        record(sim, Path(__file__).resolve().parents[1] / "viewer/public/cabinet-replay.json")
