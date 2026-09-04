"""Fixed sink-cabinet articulation and deterministic carving of baked assets.

The source apartment remains untouched. Cached OBJ variants subtract the
cabinet volume before adding hollow walls and a single passive hinge.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

CONFIG = Path(__file__).resolve().parents[5] / "sim/viewer/config/cabinet.json"


@lru_cache(maxsize=1)
def config():
    return json.loads(CONFIG.read_text())


def clip(poly, axis, bound, sign):
    inside, outside = [], []
    for a, b in zip(poly, poly[1:] + poly[:1], strict=True):
        da, db = sign * (a[axis] - bound), sign * (b[axis] - bound)
        (inside if da >= 0 else outside).append(a)
        if (da >= 0) != (db >= 0):
            v = a + da / (da - db) * (b - a)
            inside.append(v)
            outside.append(v)
    return inside, outside


def subtract(poly, spec):
    for axis in range(3):
        for bound, sign in [(spec["cutMin"][axis], 1), (spec["cutMax"][axis], -1)]:
            if not poly:
                return
            poly, outside = clip(poly, axis, bound, sign)
            if len(outside) >= 3:
                yield outside


def read_obj(path):
    vertices, uv, normals, faces = [], [], [], []
    for line in path.read_text().splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            vertices.append(list(map(float, p[1:4])))
        elif p[0] == "vt":
            uv.append(list(map(float, p[1:3])))
        elif p[0] == "vn":
            normals.append(list(map(float, p[1:4])))
        elif p[0] == "f":
            face = []
            for token in p[1:]:
                ids = token.split("/")
                row = vertices[int(ids[0]) - 1].copy()
                if uv:
                    row += uv[int(ids[1]) - 1]
                if normals:
                    row += normals[int(ids[2]) - 1]
                face.append(np.array(row))
            faces.extend([[face[0], face[i], face[i + 1]] for i in range(1, len(face) - 1)])
    return np.array(vertices), faces, bool(uv), bool(normals)


def write_obj(path, polygons, uv=False, normals=False):
    rows = [v for p in polygons for i in range(1, len(p) - 1) for v in (p[0], p[i], p[i + 1])]
    if not rows:
        return False
    lines = ["v " + " ".join(map(str, v[:3])) for v in rows]
    if uv:
        lines += ["vt " + " ".join(map(str, v[3:5])) for v in rows]
    if normals:
        lines += ["vn " + " ".join(map(str, v[5 if uv else 3 :])) for v in rows]

    def ref(i):
        return f"{i}/{i}/{i}" if uv and normals else f"{i}/{i}" if uv else f"{i}//{i}" if normals else str(i)

    lines += ["f " + " ".join(ref(j + 1) for j in range(i, i + 3)) for i in range(0, len(rows), 3)]
    path.write_text("\n".join(lines) + "\n")
    return True


def carve_assets(rooms, visuals):
    """Only carve the named kitchen. Empty/custom worlds stay unchanged."""
    s = config()
    name = s["room"]
    if name not in rooms:
        return rooms, visuals, False
    sources = [*rooms[name], *([visuals[name]] if name in visuals else [])]
    digest = hashlib.sha256(CONFIG.read_bytes() + Path(__file__).read_bytes())
    for p in sources:
        stat = p.stat()
        digest.update(f"{p}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    cache = sources[0].parent.parent / ".cabinet" / digest.hexdigest()[:16]
    manifest = cache / "manifest.json"
    if not manifest.exists():
        cache.mkdir(parents=True, exist_ok=True)
        output = []
        lo = np.array(s["cutMin"]) - np.array([0.008, 0.004, 0])
        hi = np.array(s["cutMax"]) + np.array([0.008, 0.005, 0.03])
        for source in rooms[name]:
            vertices, faces, _, _ = read_obj(source)
            if np.any(vertices.max(0) <= lo) or np.any(vertices.min(0) >= hi):
                output.append(str(source))
                continue
            # Convex hull minus box: split the convex polyhedron along each
            # cut plane, retain its outside piece, then continue with inside.
            # MuJoCo computes each mesh's convex hull, closing the cut faces.
            remaining = faces
            for axis in range(3):
                for bound, sign in [(lo[axis], 1), (hi[axis], -1)]:
                    inside, outside = [], []
                    for face in remaining:
                        a, b = clip(face, axis, bound, sign)
                        if len(a) >= 3:
                            inside.append(a)
                        if len(b) >= 3:
                            outside.append(b)
                    if outside:
                        verts = np.array([v for p in outside for v in p])
                        if np.linalg.matrix_rank(verts - verts[0], tol=1e-8) == 3:
                            target = cache / f"{source.stem}_{axis}_{sign}.obj"
                            write_obj(target, outside)
                            output.append(str(target))
                    # Close the retained inside with its convex hull before
                    # the next plane, otherwise later pieces can miss corners.
                    if inside:
                        verts = np.unique(np.array([v for p in inside for v in p]), axis=0)
                        cap = verts[np.abs(verts[:, axis] - bound) < 1e-8]
                        if len(cap) >= 3:
                            axes = [i for i in range(3) if i != axis]
                            center = cap.mean(0)
                            angles = np.arctan2(cap[:, axes[1]] - center[axes[1]], cap[:, axes[0]] - center[axes[0]])
                            inside.append(list(cap[np.argsort(angles)]))
                        remaining = inside
                    else:
                        remaining = []
        visual = None
        if name in visuals:
            _, faces, uv, normals = read_obj(visuals[name])
            target = cache / f"{name}.obj"
            write_obj(target, [p for f in faces for p in subtract(f, s)], uv, normals)
            panel = []
            for face in faces:
                poly = face
                for axis in range(3):
                    low = s["hinge"][2] - s["thickness"] - 0.001 if axis == 2 else s["cutMin"][axis]
                    for bound, sign in [(low, 1), (s["cutMax"][axis], -1)]:
                        if poly:
                            poly, _ = clip(poly, axis, bound, sign)
                if len(poly) >= 3:
                    transformed = []
                    for row in poly:
                        row = row.copy()
                        row[:3] -= s["hinge"]
                        row[:3] = row[[0, 2, 1]] * np.array([1, -1, 1])
                        if normals:
                            k = 5 if uv else 3
                            row[k : k + 3] = row[[k, k + 2, k + 1]] * np.array([1, -1, 1])
                        transformed.append(row)
                    panel.append(transformed)
            write_obj(cache / "cabinet_door.obj", panel, uv, normals)
            texture = target.with_suffix(".png")
            if not texture.exists():
                texture.symlink_to(visuals[name].with_suffix(".png").resolve())
            visual = str(target)
        manifest.write_text(json.dumps({"hulls": output, "visual": visual}))
    result = json.loads(manifest.read_text())
    rooms = dict(rooms)
    visuals = dict(visuals)
    rooms[name] = [Path(p) for p in result["hulls"]]
    if result["visual"]:
        visuals[name] = Path(result["visual"])
    return rooms, visuals, True


def bodies_xml(visual_group, textured=False):
    s = config()
    x, y, z = s["hinge"]
    w, h = s["width"], s["height"]
    t = s["thickness"]

    def box(name, size, pos, color, mass=0):
        # Config is Y-up; these bodies are already in MuJoCo Z-up.
        sx, sy, sz = size
        px, py, pz = pos
        return f'<geom name="{name}" type="box" size="{sx / 2} {sz / 2} {sy / 2}" pos="{px} {-pz} {py}" rgba="{color}" mass="{mass}" group="{visual_group}" friction="1 .01 .001" margin="0.001"/>'

    frame = []
    for i, dx in enumerate([0.009, w - 0.009]):
        frame.append(box(f"cabinet_side_{i}", [0.018, h, 0.55], [x + dx, y + h / 2, z - 0.30], ".65 .60 .53 1"))
    frame.append(box("cabinet_floor", [w, 0.018, 0.55], [x + w / 2, y + 0.009, z - 0.30], ".65 .60 .53 1"))
    frame.append(box("cabinet_back", [w, h, 0.018], [x + w / 2, y + h / 2, z - 0.566], ".65 .60 .53 1"))
    door = box("cabinet_panel", [w, h, t], [w / 2, h / 2, -t / 2 - 0.0015], ".223 .205 .188 1", 2.0)
    if textured:
        door += f'<geom name="cabinet_finish" type="mesh" mesh="vis_cabinet_door" material="mat_{s["room"]}" contype="0" conaffinity="0" mass="0" group="{visual_group}"/>'
    hx = s["handleX"]
    hy = s["handleHeight"] - y
    hz = s["handleClearance"] + s["handleRadius"]
    length = s["handleLength"]
    radius = s["handleRadius"]

    def capsule(name, a, b):
        return f'<geom name="{name}" type="capsule" fromto="{a[0]} {-a[2]} {a[1]} {b[0]} {-b[2]} {b[1]}" size="{radius}" mass=".025" rgba=".246 .235 .212 1" friction="1.5 .01 .001" margin="0.001" group="{visual_group}"/>'

    door += capsule("cabinet_handle", [hx, hy - length / 2, hz], [hx, hy + length / 2, hz])
    for i, dy in enumerate([-length / 2, length / 2]):
        door += capsule(f"cabinet_handle_mount_{i}", [hx, hy + dy, 0], [hx, hy + dy, hz])
    return (
        "".join(frame)
        + f'<body name="{s["name"]}" pos="{x} {-z} {y}"><joint name="cabinet_hinge" type="hinge" axis="0 0 -1" limited="true" range="0 {s["maxAngle"]}" damping=".4" frictionloss=".08"/>{door}</body>'
    )


def pose(model, data):
    try:
        bid = model.body(config()["name"]).id
    except KeyError:
        return {}
    return {config()["name"]: [*data.xpos[bid].tolist(), *data.xquat[bid].tolist()]}
