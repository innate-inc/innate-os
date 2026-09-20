#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Rewrite each arm link's <inertial> origin to its visual mesh's volume centroid.

The CAD export left every link's centre of mass at the link frame origin, which
sits ON that link's own joint axis -- so a gravity model built from the URDF
reads exactly zero torque at the head and badly under-reads the shoulder and
elbow. The meshes are the real geometry, so their centroids (uniform density
within a link) are a far better COM than the origin.

Masses and inertia tensors are left alone: the arm node only needs mass and COM,
and the tensors are placeholders that the sim is tuned around.

    python3 mesh_com.py            # print what would change
    python3 mesh_com.py --write    # rewrite mars.urdf in place
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

URDF = Path(__file__).resolve().parent.parent / "urdf" / "mars.urdf"
MESHES = Path(__file__).resolve().parent.parent / "meshes"

# base_link is the arm's ground: its COM carries no arm gravity torque, and the
# sim's base dynamics are tuned around the value in the file.
SKIP_LINKS = {"base_link"}


def mesh_centroid(path: Path) -> tuple[float, np.ndarray]:
    """(volume, volume centroid) of a binary STL, via the signed-tetrahedron sum."""
    raw = path.read_bytes()
    count = struct.unpack("<I", raw[80:84])[0]
    if 84 + count * 50 != len(raw):
        raise ValueError(f"{path.name}: not a binary STL ({count} triangles, {len(raw)} bytes)")
    records = np.frombuffer(raw[84:], dtype=np.uint8).reshape(count, 50)
    tris = records[:, 12:48].copy().view("<f4").reshape(count, 3, 3).astype(np.float64)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    volumes = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
    total = volumes.sum()
    if total <= 0:
        raise ValueError(f"{path.name}: non-positive volume {total}")
    return total, (volumes[:, None] * (a + b + c) / 4.0).sum(0) / total


def link_meshes(urdf_text: str) -> dict[str, Path]:
    out = {}
    for link in ET.fromstring(urdf_text).findall("link"):
        name = link.get("name")
        if name in SKIP_LINKS or link.find("inertial") is None:
            continue
        visual = link.find("visual/geometry/mesh")
        if visual is None:
            continue
        mesh = MESHES / Path(visual.get("filename")).name
        if mesh.is_file():
            out[name] = mesh
    return out


def rewrite(urdf_text: str, name: str, com: np.ndarray) -> str:
    """Replace the origin of `name`'s <inertial>, leaving the rest byte-identical."""
    block = re.search(rf'(<link name="{re.escape(name)}">.*?</link>)', urdf_text, re.DOTALL)
    if block is None:
        raise KeyError(name)
    inertial = re.search(r"(<inertial>\s*<origin xyz=\")([^\"]*)(\")", block.group(1), re.DOTALL)
    if inertial is None:
        raise KeyError(f"{name}: no <inertial><origin xyz=...>")
    xyz = " ".join(f"{v:.6f}" for v in com)
    patched = block.group(1).replace(inertial.group(0), inertial.group(1) + xyz + inertial.group(3), 1)
    return urdf_text.replace(block.group(1), patched, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite mars.urdf in place")
    args = parser.parse_args()

    text = URDF.read_text()
    for name, mesh in link_meshes(text).items():
        volume, com = mesh_centroid(mesh)
        print(f"{name:10s} {mesh.name:12s} vol={volume * 1e6:8.2f} cm^3  com={np.round(com, 6).tolist()}")
        text = rewrite(text, name, com)

    if not args.write:
        print("\n(dry run; pass --write to rewrite mars.urdf)")
        return 0
    URDF.write_text(text)
    print(f"\nwrote {URDF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
