"""Offline strict, non-coplanar triangle-crossing audit; no physics mutation.

Not a clearance/CCD certificate: coplanar contact and neighboring triangles
sharing a vertex are excluded. Uses independent edge/triangle intersection,
not the contact solver's own gap residual. No IPC native dependency.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from numba import njit


@njit(cache=True)
def _crosses(p, q, a, b, c):
    ab, ac, direction = b - a, c - a, q - p
    h = np.cross(direction, ac)
    det = np.dot(ab, h)
    if abs(det) < 1e-14:
        return False
    s = p - a
    u = np.dot(s, h) / det
    if u <= 1e-7 or u >= 1 - 1e-7:
        return False
    cross = np.cross(s, ab)
    v = np.dot(direction, cross) / det
    t = np.dot(ac, cross) / det
    return v > 1e-7 and u + v < 1 - 1e-7 and 1e-7 < t < 1 - 1e-7


@njit(cache=True)
def count_crossings(vertices, faces):
    low = np.empty((len(faces), 3))
    high = np.empty_like(low)
    for f in range(len(faces)):
        for axis in range(3):
            low[f, axis] = min(vertices[faces[f, 0], axis], vertices[faces[f, 1], axis], vertices[faces[f, 2], axis])
            high[f, axis] = max(vertices[faces[f, 0], axis], vertices[faces[f, 1], axis], vertices[faces[f, 2], axis])
    count = 0
    for i in range(len(faces)):
        for j in range(i + 1, len(faces)):
            if np.any(low[i] > high[j]) or np.any(low[j] > high[i]):
                continue
            shared = False
            for a in faces[i]:
                for b in faces[j]:
                    if a == b:
                        shared = True
            if shared:
                continue
            hit = False
            for e in range(3):
                a, b, c = vertices[faces[j]]
                if _crosses(vertices[faces[i, e]], vertices[faces[i, (e + 1) % 3]], a, b, c):
                    hit = True
                a, b, c = vertices[faces[i]]
                if _crosses(vertices[faces[j, e]], vertices[faces[j, (e + 1) % 3]], a, b, c):
                    hit = True
            count += hit
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectory", type=Path)
    parser.add_argument("--stride", type=int, default=1)
    args = parser.parse_args()
    if args.trajectory.suffix == ".json":
        data = json.loads(args.trajectory.read_text())
        frames, faces = np.asarray(data["frames"]), np.asarray(data["faces"], dtype=np.int64)
    else:
        with np.load(args.trajectory) as data:
            frames, faces = data["vertices"], data["faces"]
    counts = [int(count_crossings(v, faces)) for v in frames[:: args.stride]]
    result = dict(
        frames=len(counts),
        frames_with_crossings=sum(n > 0 for n in counts),
        max_crossing_pairs=max(counts, default=0),
        total_crossing_pairs=sum(counts),
        counts=counts,
    )
    args.trajectory.with_suffix(".crossings.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "counts"}))
