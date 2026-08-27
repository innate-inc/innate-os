#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Seed the build context for sim/Dockerfile.assets. CI is always a clean
# checkout, and sim/assets, sim/viewer/public and sim/viewer/assets are all
# gitignored, so something has to put those files on disk before docker reads
# them.
#
# AUTHORED inputs only -- they exist in no repo and nothing can derive them, so
# each is downloaded from a pinned, checksummed release asset, ONE FILE PER
# ASSET: the apartment geometry the pipeline consumes, the props' source meshes
# and textures, and the glTF exports the viewer loads. The DERIVED geometry
# (decomposition, room exports, nav map) is not seeded -- sim/tools derives all
# of it from these during the build, collision hulls included.
#
# TEMPORARY: these belong in version control (LFS) or a release of THIS repo.
#
# Usage: python3 ci/seed_asset_context.py   (from the repo root)
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SIM = ROOT / "sim"

# Where sim/Dockerfile.assets expects each file -> (url, sha256). The digest is
# the whole point: these are the only bytes in the image that nothing in git
# describes, so an unverified download would let the payload change silently
# under a tag that claims to be content-addressed. sim-geometry-v1 exists ONLY
# to host them: one file per asset, nothing derived.
_RAW_RELEASE = "https://github.com/innate-inc/innate-sim-assets/releases/download/sim-geometry-v1"
RAW_FILES = {
    SIM / "viewer" / "assets" / "apartment_obj" / "apartment.obj": (
        f"{_RAW_RELEASE}/apartment.obj",
        "79286a12485ea63253bf006c17d288101b6fcec2a01cfe28a3957e0c6b38143e",
    ),
    SIM / "viewer" / "public" / "models" / "appartement.glb": (
        f"{_RAW_RELEASE}/appartement.glb",
        "807ff2613f4b3aaf1fad39645fee19e10cd560296e1a2f59133daa46f8678c38",
    ),
    # The browser's prop models -- the glTF exports the viewer loads. Their
    # MuJoCo counterparts are the authored meshes under assets/objects and
    # assets/humans below. Decomposed object props get derived *_hulls.f32
    # files; residents use their visual mesh's convex hull directly.
    SIM / "viewer" / "public" / "models" / "human.glb": (
        f"{_RAW_RELEASE}/human.glb",
        "e089c1eb6624fa881773bd08542c16aa13afbe2bb6323216d00c3e377b668ec8",
    ),
    SIM / "viewer" / "public" / "models" / "resident_alex.glb": (
        f"{_RAW_RELEASE}/resident_alex.glb",
        "2f7f877bf0ad4b063cf18e2912fbf5e6d236a1d7d034c356ab935238bd15ae98",
    ),
    SIM / "viewer" / "public" / "models" / "resident_blake.glb": (
        f"{_RAW_RELEASE}/resident_blake.glb",
        "7c9d211fa89f5d75ff985c483879a606062d4c3064023c0d67326e386aa74a9b",
    ),
    SIM / "viewer" / "public" / "models" / "resident_casey.glb": (
        f"{_RAW_RELEASE}/resident_casey.glb",
        "2ddd1b74d7477932a996feeb82272cbe3f55e35799f229979bbb2024ae0f890b",
    ),
    SIM / "viewer" / "public" / "models" / "labrador.glb": (
        f"{_RAW_RELEASE}/labrador.glb",
        "6537d99636d0175bc42dfa63c8999d4b6376f9032a7f8d5563d7bbf807a2965a",
    ),
    SIM / "viewer" / "public" / "models" / "soccer_ball.glb": (
        f"{_RAW_RELEASE}/soccer_ball.glb",
        "9ed6bab6ae9dc99e55eeeb2db0f607a3aa282138cbf5ffa83ad6d0f6ec673828",
    ),
    SIM / "assets" / "humans" / "casual_man.obj": (
        f"{_RAW_RELEASE}/casual_man.obj",
        "d45337ab71f50e2213b41544a468fbbc82a811a20ee40f7fd3ec6eefa799849d",
    ),
    SIM / "assets" / "humans" / "casual_man_basecolor.png": (
        f"{_RAW_RELEASE}/casual_man_basecolor.png",
        "c742c793985222cdc84d073abca1101b06270f502a377105c03c3827a4d834c6",
    ),
    SIM / "assets" / "humans" / "resident_alex.obj": (
        f"{_RAW_RELEASE}/resident_alex.obj",
        "f33ba7ea12590b8b16428f967534ee8d3ac63de3b8a9964312dbbf66590e0b21",
    ),
    SIM / "assets" / "humans" / "resident_alex_basecolor.png": (
        f"{_RAW_RELEASE}/resident_alex_basecolor.png",
        "f2ddce768302f0762311f148ac56d1c517b623b988272a25a7728058d9c07a16",
    ),
    SIM / "assets" / "humans" / "resident_blake.obj": (
        f"{_RAW_RELEASE}/resident_blake.obj",
        "d87c2d9b0e8366afa9201e3b3a35383b48d16a6c38da4edf03fc9be1b47bfa0c",
    ),
    SIM / "assets" / "humans" / "resident_blake_basecolor.png": (
        f"{_RAW_RELEASE}/resident_blake_basecolor.png",
        "bd3ff333ad7a50b7451b23e51fc01ac9f7a8242a120670f680b6aae7dbeba334",
    ),
    SIM / "assets" / "humans" / "resident_casey.obj": (
        f"{_RAW_RELEASE}/resident_casey.obj",
        "4c7ccecb4f1e640a27fcb812080879877e6d5cae7a33ea8cd8a2b26a12a02cb6",
    ),
    SIM / "assets" / "humans" / "resident_casey_basecolor.png": (
        f"{_RAW_RELEASE}/resident_casey_basecolor.png",
        "b1a927b6684d2de00db65ff834bf6f8889cbf99e637fe3d26709cf45ae360698",
    ),
    SIM / "assets" / "objects" / "labrador.obj": (
        f"{_RAW_RELEASE}/labrador.obj",
        "5254d274ee5f0d8f85d09694cb22a77ef6dc5b8cf7c051771d746f562ecbdbd7",
    ),
    SIM / "assets" / "objects" / "labrador_basecolor.png": (
        f"{_RAW_RELEASE}/labrador_basecolor.png",
        "0d4e542e5827849ecc2bd85fbb20984bbb9d67a47494421755400668a6e70e11",
    ),
    SIM / "assets" / "objects" / "soccer_ball.obj": (
        f"{_RAW_RELEASE}/soccer_ball.obj",
        "e176b750bfc3bbe41a96cdfdf0459e60b9f8ca8c703c5dbde808fc2f6f959859",
    ),
    SIM / "assets" / "objects" / "soccer_ball_basecolor.png": (
        f"{_RAW_RELEASE}/soccer_ball_basecolor.png",
        "ea698ee6e7e3cc318a72cfd4ebe51393abf1cb6a8cf9b2cac1559abf8c6c8099",
    ),
}


def _download(url: str, dest: Path, expected_sha256: str) -> None:
    """Fetch straight to `dest`, hashing the stream as it lands -- no second
    read of the file and no whole copy in memory."""
    print(f"  fetching {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=600) as resp, open(dest, "wb") as out:  # noqa: S310
        while chunk := resp.read(1 << 20):
            sha.update(chunk)
            out.write(chunk)
    if sha.hexdigest() != expected_sha256:
        dest.unlink(missing_ok=True)
        sys.exit(f"checksum mismatch for {url}: got {sha.hexdigest()}, expected {expected_sha256}")


def seed_raw() -> None:
    """Everything nothing can derive, each file straight to its final path."""
    print("raw inputs:")
    for dest, (url, sha256) in RAW_FILES.items():
        _download(url, dest, sha256)
        print(f"  seeded {dest.relative_to(ROOT)}")


def main() -> None:
    seed_raw()
    subprocess.run(["du", "-sh", "sim/assets", "sim/viewer/public", "sim/viewer/assets"], check=False)


if __name__ == "__main__":
    main()
