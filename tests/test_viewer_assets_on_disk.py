# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The viewer's assets reach the container as bind mounts, not image mounts.

`type: image` mounts broke twice in a year -- moby#51687 made every one of
them fail with 'file name too long', and Compose 5 hands the daemon a manifest
digest where an image ID belongs -- so the launcher installs those directories
onto the host and compose binds them. These tests hold that line: the mount
kind in the compose file, and the marker that stops a warm start re-fetching
tens of megabytes.
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "sim" / "launcher"))

import runtime  # noqa: E402

COMPOSE_FILE = REPO_ROOT / "sim" / "docker-compose.dev.yml"
VIEWER_MOUNTS = ("sim/viewer/public", "sim/viewer/dist-lib")


@pytest.fixture
def compose_volumes():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    return compose["services"]["innate"]["volumes"]


def test_no_image_mounts_anywhere(compose_volumes):
    """The feature, not just its two uses: a third one would break the same way."""
    image_mounts = [v for v in compose_volumes if isinstance(v, dict) and v.get("type") == "image"]
    assert image_mounts == []


@pytest.mark.parametrize("path", VIEWER_MOUNTS)
def test_the_viewer_directories_are_bound_read_only(compose_volumes, path):
    binds = [v for v in compose_volumes if isinstance(v, str) and f"/{path}:" in v]
    assert len(binds) == 1, f"{path} should be bind-mounted exactly once"
    assert binds[0].endswith(":ro"), "the container never writes these; the launcher owns them"


def _fake_layer(monkeypatch, tmp_path, subtree, digest):
    """Stand in for the registry: one manifest, one layer, one subtree."""
    monkeypatch.setattr(runtime.oci, "manifest_for_image", lambda image: {"layers": [{"digest": digest}]})
    monkeypatch.setattr(runtime.oci, "split_ref", lambda image: ("innate-inc/thing", "tag"))
    monkeypatch.setattr(runtime.oci, "anon_token", lambda repo: "token")
    fetches: list[str] = []

    def fetch_layer(repo, layer_digest, handle, token, *, label=""):
        fetches.append(layer_digest)
        handle.write(b"x")

    def safe_extract(blob, dest):
        (Path(dest) / subtree).mkdir(parents=True, exist_ok=True)
        (Path(dest) / subtree / "file.txt").write_text("contents")

    monkeypatch.setattr(runtime.oci, "fetch_layer", fetch_layer)
    monkeypatch.setattr(runtime.oci, "safe_extract", safe_extract)
    return fetches


def test_a_layer_is_installed_once(monkeypatch, tmp_path):
    """The second call must not spend the download again -- this runs on every
    `up`, and the bundle layer is tens of megabytes."""
    digest = "sha256:" + "a" * 64
    fetches = _fake_layer(monkeypatch, tmp_path, "bundle", digest)
    destination, marker = tmp_path / "dist-lib", tmp_path / ".dist-lib-tag"

    for _ in range(2):
        runtime.install_layer_subtree("ghcr.io/x/y:tag", 0, "bundle", destination, marker, label="bundle")

    assert len(fetches) == 1
    assert (destination / "file.txt").read_text() == "contents"
    assert marker.read_text().split() == [digest, "ghcr.io/x/y:tag"]


def test_a_deleted_directory_is_reinstalled(monkeypatch, tmp_path):
    """The marker records what this host meant to install, not what survived:
    a hand-deleted directory must come back rather than be asserted present."""
    digest = "sha256:" + "b" * 64
    fetches = _fake_layer(monkeypatch, tmp_path, "viewer", digest)
    destination, marker = tmp_path / "public", tmp_path / ".public-tag"

    runtime.install_layer_subtree("ghcr.io/x/y:tag", 0, "viewer", destination, marker, label="viewer")
    runtime.shutil.rmtree(destination)
    runtime.install_layer_subtree("ghcr.io/x/y:tag", 0, "viewer", destination, marker, label="viewer")

    assert len(fetches) == 2
    assert (destination / "file.txt").exists()


def test_viewer_refresh_preserves_local_environment_packs(monkeypatch, tmp_path):
    """Licensed packs are built beside published assets and must survive an
    asset-image refresh, while stale files from the old published layer do not."""
    destination = tmp_path / "public"
    marker = destination / ".installed-tag"
    local_scene = destination / "local-environments" / "low-poly-town" / "scene.glb"
    local_scene.parent.mkdir(parents=True)
    local_scene.write_bytes(b"licensed town")
    (destination / "stale-published-file.txt").write_text("old")

    digest = "sha256:" + "c" * 64
    _fake_layer(monkeypatch, tmp_path, "viewer", digest)
    runtime.install_layer_subtree(
        "ghcr.io/x/y:new-tag",
        0,
        "viewer",
        destination,
        marker,
        label="viewer",
        preserve_subtrees=("local-environments",),
    )

    assert local_scene.read_bytes() == b"licensed town"
    assert not (destination / "stale-published-file.txt").exists()
    assert (destination / "file.txt").read_text() == "contents"
