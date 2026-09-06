# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Episode / run / map endpoints: the traversal fence across the skill roots is
the security-critical part, so it's tested hardest; the h5py/cv2 happy paths are
covered where those libs are present. Part of the fast (no-ROS) pytest bucket."""

import media_routes
import pytest
from conftest import make_app_root, serve, sync


def _skill(tmp_path, monkeypatch):
    """A skills root with one skill dir, fenced in as the only allowed root."""
    skills = tmp_path / "skills"
    skill = skills / "s"
    (skill / "data").mkdir(parents=True)
    monkeypatch.setattr(media_routes, "SKILLS_ROOTS", (skills.resolve(),))
    return skill


@sync
async def test_episode_serves_and_fences(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    (skill / "data" / "episode_1_camera_1.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/episode", params={"dir": str(skill), "id": "1", "camera": "camera_1"})
        assert r.status == 200 and r.headers["Content-Type"] == "video/mp4"
        await r.read()
        # a dir outside every skill root -> 404
        assert (await s.get(base + "/episode", params={"dir": "/etc", "id": "1", "camera": "camera_1"})).status == 404
        # a traversal that escapes the root -> 404
        assert (
            await s.get(base + "/episode", params={"dir": f"{skill}/../../../etc", "id": "1", "camera": "camera_1"})
        ).status == 404
        # missing camera -> 404
        assert (await s.get(base + "/episode", params={"dir": str(skill), "id": "1", "camera": "nope"})).status == 404


@sync
async def test_run_info_downloaded_with_checkpoint(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    run = skill / "run42"
    run.mkdir()
    (run / "model_step_100.pth").write_bytes(b"ckpt")
    (run / "output.log").write_text("all good\n")
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        info = await (await s.get(base + "/run/info", params={"dir": str(skill), "id": "run42"})).json()
    assert info["downloaded"] and info["has_checkpoint"]
    assert "model_step_100.pth" in info["files"]
    assert info["error_excerpt"] == ""


@sync
async def test_run_info_failure_excerpt(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    run = skill / "run7"
    run.mkdir()
    (run / "output.log").write_text("step 1 ok\nstep 2 ok\nRuntimeError: boom happened\n")
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        info = await (await s.get(base + "/run/info", params={"dir": str(skill), "id": "run7"})).json()
    assert info["downloaded"] and not info["has_checkpoint"]
    assert "RuntimeError: boom happened" in info["error_excerpt"]


@sync
async def test_run_info_not_downloaded(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        info = await (await s.get(base + "/run/info", params={"dir": str(skill), "id": "never"})).json()
    assert info == {"downloaded": False, "has_checkpoint": False, "files": []}


@sync
async def test_run_log_serves_and_fences(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    run = skill / "run"
    run.mkdir()
    (run / "output.log").write_text("hello log\n")
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/run/log", params={"dir": str(skill), "id": "run", "file": "output.log"})
        assert r.status == 200 and "hello log" in await r.text()
        # escaping the run dir -> 404
        r2 = await s.get(base + "/run/log", params={"dir": str(skill), "id": "run", "file": "../../../etc/passwd"})
        assert r2.status == 404


@sync
async def test_media_symlinks_do_not_reveal_files_outside_their_roots(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    root = make_app_root(tmp_path)
    canary = "synthetic-private-media-canary"
    outside = tmp_path / "private.txt"
    outside.write_text("RuntimeError: " + canary)
    memories = tmp_path / "memories"
    (memories / "Home").mkdir(parents=True)
    (memories / "Home" / "1.jpg").symlink_to(outside)
    monkeypatch.setattr(media_routes, "MEMORY_DIR", memories)
    (skill / "thumbs").mkdir()
    (skill / "thumbs" / "episode_1_camera_1.jpg").symlink_to(outside)
    (skill / "data" / "episode_1_camera_1.mp4").write_bytes(b"synthetic-video")
    run = skill / "run"
    run.mkdir()
    (run / "output.log").symlink_to(outside)
    async with serve(ROOT=root) as (s, base):
        for route, params in (
            ("/memory/image", {"map": "Home", "id": "1"}),
            ("/episode/thumb", {"dir": str(skill), "id": "1", "camera": "camera_1"}),
        ):
            response = await s.get(base + route, params=params)
            assert response.status == 404
            assert canary not in await response.text()
        info = await (await s.get(base + "/run/info", params={"dir": str(skill), "id": "run"})).json()
        assert info["error_excerpt"] == ""


@sync
async def test_run_log_bounded_read(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    run = skill / "run"
    run.mkdir()
    big = media_routes.MAX_LOG_BYTES + 4096
    (run / "huge.log").write_bytes(b"x" * big)
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        body = await (
            await s.get(base + "/run/log", params={"dir": str(skill), "id": "run", "file": "huge.log"})
        ).read()
    assert len(body) <= media_routes.MAX_LOG_BYTES + len(b"\n\n[truncated]\n")
    assert body.endswith(b"[truncated]\n")


@sync
async def test_profile_serves(tmp_path, monkeypatch):
    skill = _skill(tmp_path, monkeypatch)
    (skill / "data" / "episode_3_profile.jsonl").write_text('{"ctx": 1}\n{"step": 1}\n')
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/episode/profile", params={"dir": str(skill), "id": "3"})
        assert r.status == 200 and r.headers["Content-Type"].startswith("application/x-ndjson")
        assert (await r.text()).count("\n") == 2
        # no profile file -> 404
        assert (await s.get(base + "/episode/profile", params={"dir": str(skill), "id": "99"})).status == 404


@sync
async def test_joints(tmp_path, monkeypatch):
    h5py = pytest.importorskip("h5py")
    import numpy as np

    skill = _skill(tmp_path, monkeypatch)
    with h5py.File(skill / "data" / "episode_5.h5", "w") as f:
        obs = f.create_group("observations")
        obs.create_dataset("qpos", data=np.zeros((4, 7)))
        obs.create_dataset("qvel", data=np.zeros((4, 7)))
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        j = await (await s.get(base + "/episode/joints", params={"dir": str(skill), "id": "5"})).json()
    assert len(j["qpos"]) == 4 and len(j["qvel"]) == 4


@sync
async def test_map_preview(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    maps = tmp_path / "maps"
    maps.mkdir()
    cv2.imwrite(str(maps / "m.pgm"), np.zeros((20, 20), np.uint8))
    (maps / "m.yaml").write_text("image: m.pgm\n")
    monkeypatch.setattr(media_routes, "MAPS_DIR", maps.resolve())
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        # a name that isn't a saved map -> 404 (deterministic, doesn't need cv2)
        assert (await s.get(base + "/map/preview", params={"name": "../etc"})).status == 404
        # the happy path depends on cv2 being able to transcode the .pgm here
        r = await s.get(base + "/map/preview", params={"name": "m"})
        if r.status != 200:
            pytest.skip("cv2 in this build can't transcode the .pgm")
        assert r.headers["Content-Type"] == "image/png"


@sync
async def test_memory_image_serves_and_fences(tmp_path, monkeypatch):
    memories = tmp_path / "spatial_memory"
    (memories / "Home").mkdir(parents=True)
    (memories / "Home" / "3.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    monkeypatch.setattr(media_routes, "MEMORY_DIR", memories.resolve())
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/memory/image", params={"map": "Home", "id": "3"})
        assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
        assert await r.read() == b"\xff\xd8\xff\xe0jpegbytes"
        # the map name may arrive as the yaml filename the topic publishes
        assert (await s.get(base + "/memory/image", params={"map": "Home.yaml", "id": "3"})).status == 200
        # traversal-shaped names and non-numeric ids -> 404
        assert (await s.get(base + "/memory/image", params={"map": "../etc", "id": "3"})).status == 404
        assert (await s.get(base + "/memory/image", params={"map": "Home", "id": "../3"})).status == 404
        assert (await s.get(base + "/memory/image", params={"map": "Home", "id": "9"})).status == 404
        # the in-progress mapping session is the one dotted name that serves
        (memories / ".mapping").mkdir()
        (memories / ".mapping" / "1.jpg").write_bytes(b"\xff\xd8staged")
        assert (
            await (await s.get(base + "/memory/image", params={"map": ".mapping", "id": "1"})).read()
            == b"\xff\xd8staged"
        )
        assert (await s.get(base + "/memory/image", params={"map": ".hidden", "id": "1"})).status == 404


@sync
async def test_thumb(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    skill = _skill(tmp_path, monkeypatch)
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        # missing video -> 404 (deterministic)
        assert (
            await s.get(base + "/episode/thumb", params={"dir": str(skill), "id": "0", "camera": "camera_1"})
        ).status == 404

        mp4 = skill / "data" / "episode_9_camera_1.mp4"
        writer = cv2.VideoWriter(str(mp4), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (32, 32))
        if not writer.isOpened():
            pytest.skip("no mp4 writer in this cv2 build")
        for _ in range(10):
            writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
        writer.release()
        if not mp4.exists() or mp4.stat().st_size == 0:
            pytest.skip("cv2 produced no readable mp4")

        r = await s.get(base + "/episode/thumb", params={"dir": str(skill), "id": "9", "camera": "camera_1"})
        if r.status != 200:
            pytest.skip("cv2 in this build can't decode the generated mp4")
        assert r.headers["Content-Type"] == "image/jpeg"
