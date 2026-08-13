"""Compute ground-truth prop visibility for every frame of a captured dataset.

Rebuilds the identical scene, re-poses the robot at each frame, and raycasts
from the main camera to each placed prop (center and top). A prop is visible
in a frame when it lies inside the camera frustum and a ray reaches it before
hitting anything else. Writes visibility.json next to the manifest:

    {"frames":   {"12": {"banana": {"dist": 0.7, "az_deg": 4.1}}, ...},
     "memories": {"14": {...}}}

Run from sim/:  uv run benchmarks/spatial_memory/annotate.py --dataset benchmarks/spatial_memory/dataset/home_tour
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import _paths  # noqa: F401
import mujoco
import numpy as np
from apartment import SCENE
from capture import build_scene, teleport
from mars_sim_driver.constants import CAMERA_FOVY
from mars_sim_driver.core import VirtualMars

FOVX_DEG = math.degrees(2 * math.atan(math.tan(math.radians(CAMERA_FOVY / 2)) * 640 / 480))
_RAY_START_OFFSET = 0.08  # start past the head shell so the ray doesn't hit the robot itself


def _top_z(model: mujoco.MjModel, data: mujoco.MjData, body_id: int) -> float:
    """Highest world z over the body's geom AABBs at its current pose."""
    adr, num = model.body_geomadr[body_id], model.body_geomnum[body_id]
    tops = []
    for gid in range(adr, adr + num):
        rot = data.geom_xmat[gid].reshape(3, 3)
        aabb_center, half = model.geom_aabb[gid, :3], model.geom_aabb[gid, 3:]
        tops.append(float(data.geom_xpos[gid][2] + rot[2] @ aabb_center + np.abs(rot[2]) @ half))
    return max(tops)


def visible_props(sim: VirtualMars, cam_id: int) -> dict[str, dict]:
    mujoco.mj_forward(sim.model, sim.data)
    cam_pos = sim.data.cam_xpos[cam_id]
    cam_mat = sim.data.cam_xmat[cam_id].reshape(3, 3)  # camera looks along -z, y up
    out: dict[str, dict] = {}
    geomid_out = np.zeros(1, dtype=np.int32)
    for name, pose in sim.object_poses().items():
        body_id = sim.model.body(name).id
        center = np.array(pose[:3])
        # 2cm under the exact top so the ray lands on the surface instead of grazing the edge
        top = np.array([center[0], center[1], max(_top_z(sim.model, sim.data, body_id) - 0.02, center[2])])
        for point in (center, top):
            to_prop = point - cam_pos
            dist = float(np.linalg.norm(to_prop))
            if dist < _RAY_START_OFFSET * 2:
                continue
            local = cam_mat.T @ to_prop
            az = math.degrees(math.atan2(local[0], -local[2]))
            el = math.degrees(math.atan2(local[1], -local[2]))
            if abs(az) > FOVX_DEG / 2 or abs(el) > CAMERA_FOVY / 2 or local[2] > 0:
                continue
            direction = to_prop / dist
            start = cam_pos + direction * _RAY_START_OFFSET
            hit = mujoco.mj_ray(sim.model, sim.data, start, direction, None, 1, -1, geomid_out)
            if geomid_out[0] < 0 or hit < 0:
                continue
            if sim.model.geom_bodyid[geomid_out[0]] == body_id:
                out[name] = {"dist": round(dist, 2), "az_deg": round(az, 1)}
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=Path("benchmarks/spatial_memory/dataset/home_tour"))
    args = ap.parse_args()
    root = args.dataset.resolve()

    scene = json.loads((root / "scene.json").read_text())
    if scene.get("capture", {}).get("mode") == "drive":
        print(
            "WARNING: drive-mode capture — the physics tour can nudge floor props, but this\n"
            "annotation raycasts the pristine scene: prop-visibility labels assume undisturbed\n"
            "props and may be wrong for any prop the robot brushed."
        )

    sim = VirtualMars()
    build_scene(sim)
    cam_id = sim.model.camera("main").id

    frames = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
    memories = json.loads((root / "data/spatial_memory/sim_apartment/index.json").read_text())["memories"]

    def annotate(records: list[dict], key: str) -> dict[str, dict]:
        result = {}
        for r in records:
            teleport(sim, r["x"], r["y"], r.get("yaw", r.get("theta")))
            seen = visible_props(sim, cam_id)
            if seen:
                result[str(r[key])] = seen
        return result

    payload = {"frames": annotate(frames, "i"), "memories": annotate(memories, "id")}
    (root / "visibility.json").write_text(json.dumps(payload, indent=1))
    per_prop: dict[str, int] = {}
    for seen in payload["memories"].values():
        for prop in seen:
            per_prop[prop] = per_prop.get(prop, 0) + 1
    print(
        f"frames with props: {len(payload['frames'])}/{len(frames)}, memories: {len(payload['memories'])}/{len(memories)}"
    )
    print("memory sightings per prop:", dict(sorted(per_prop.items())), "| placed:", [p.prop for p in SCENE])


if __name__ == "__main__":
    main()
