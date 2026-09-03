# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Manifest-backed simulator environments shared by physics, viewer, and Nav2."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from config import StackError

DEFAULT_ENVIRONMENT_ID = "apartment"
ENVIRONMENT_ID_RE = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_COORDINATE_SYSTEM = "gltf-y-up-meters"
ACTIVE_ENVIRONMENT_FILENAME = ".active-environment.json"
TRACKED_ENVIRONMENT_DIRECTORY = "environments"
LOCAL_ENVIRONMENT_DIRECTORY = "environments.local"


def _fail(message: str, manifest_path: Path | None = None) -> StackError:
    location = f" ({manifest_path})" if manifest_path else ""
    return StackError(f"Invalid simulator environment manifest{location}: {message}")


def _object(parent: dict[str, object], key: str, manifest_path: Path) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise _fail(f"{key!r} must be an object", manifest_path)
    return value


def _string(parent: dict[str, object], key: str, manifest_path: Path) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{key!r} must be a non-empty string", manifest_path)
    return value.strip()


def _number(parent: dict[str, object], key: str, manifest_path: Path) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _fail(f"{key!r} must be a finite number", manifest_path)
    return float(value)


def _relative_path(parent: dict[str, object], key: str, manifest_path: Path) -> Path:
    raw = _string(parent, key, manifest_path)
    path = PurePosixPath(raw)
    if "\\" in raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail(f"{key!r} must be a contained relative path", manifest_path)
    return Path(*path.parts)


def _layer_identity(sim_repo: Path) -> bytes:
    tokens: list[str] = []
    for relative in (Path("assets/.assets-tag"), Path("viewer/public/.installed-tag")):
        try:
            parts = (sim_repo / relative).read_text(encoding="utf-8").split()
        except (OSError, UnicodeError):
            parts = []
        tokens.append(parts[0] if parts else "<missing>")
    return "\0".join(tokens).encode()


def _local_generation(raw: dict[str, object], manifest_path: Path) -> bytes:
    attribution = raw.get("attribution")
    value = attribution.get("generated_assets_sha256") if isinstance(attribution, dict) else None
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise _fail("local packs require attribution.generated_assets_sha256", manifest_path)
    return value.encode()


@dataclass(frozen=True)
class EnvironmentPack:
    manifest: dict[str, object]
    manifest_path: Path
    assets_root: Path
    viewer_public_root: Path
    fingerprint: str

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def display_name(self) -> str:
        return str(self.manifest["display_name"])

    @property
    def is_local(self) -> bool:
        return self.manifest_path.parent.name == LOCAL_ENVIRONMENT_DIRECTORY

    def _section(self, name: str) -> dict[str, object]:
        value = self.manifest[name]
        assert isinstance(value, dict)
        return value

    def _asset_path(self, section: str, field: str, root: Path) -> Path:
        return root / Path(str(self._section(section)[field]))

    @property
    def collision_path(self) -> Path:
        return self._asset_path("physics", "collision_dir", self.assets_root)

    @property
    def visual_path(self) -> Path:
        return self._asset_path("physics", "visual_dir", self.assets_root)

    @property
    def nav_map_yaml_path(self) -> Path:
        return self._asset_path("navigation", "map_yaml", self.assets_root)

    @property
    def nav_map_image_path(self) -> Path:
        return self._asset_path("navigation", "map_image", self.assets_root)

    @property
    def viewer_type(self) -> str:
        return str(self._section("viewer")["type"])

    @property
    def spawn_x(self) -> float:
        return float(self._section("spawn")["x"])

    @property
    def spawn_y(self) -> float:
        return float(self._section("spawn")["y"])

    @property
    def spawn_yaw_degrees(self) -> float:
        return float(self._section("spawn")["yaw_degrees"])

    @property
    def spawn_pose(self) -> tuple[float, float, float]:
        return self.spawn_x, self.spawn_y, self.spawn_yaw_degrees

    @property
    def active_path(self) -> Path:
        return self.assets_root / ACTIVE_ENVIRONMENT_FILENAME

    def active_descriptor(self) -> dict[str, object]:
        return {**self.manifest, "fingerprint": self.fingerprint}

    def _validate_required(self, required: list[tuple[Path, Path, bool]]) -> None:
        for path, root, is_directory in required:
            try:
                contained = path.resolve().is_relative_to(root.resolve())
            except (OSError, RuntimeError):
                contained = False
            if not contained:
                raise StackError(f"Environment {self.id!r} contains an asset path outside {root}: {path}")
            if not (path.is_dir() if is_directory else path.is_file()):
                raise StackError(f"Environment {self.id!r} is missing a required asset: {path}")

    def validate_physics_assets(self) -> None:
        self._validate_required(
            [
                (self.collision_path, self.assets_root, True),
                (self.visual_path, self.assets_root, True),
                (self.nav_map_yaml_path, self.assets_root, False),
                (self.nav_map_image_path, self.assets_root, False),
            ]
        )

    def validate_viewer_assets(self) -> None:
        viewer = self._section("viewer")
        required = [
            (self._asset_path("viewer", "collision_dir", self.viewer_public_root), self.viewer_public_root, True),
        ]
        if self.viewer_type == "glb":
            required.append((self.viewer_public_root / Path(str(viewer["model"])), self.viewer_public_root, False))
        else:
            required.extend(
                (
                    (self.viewer_public_root / Path(str(viewer["manifest"])), self.viewer_public_root, False),
                    (self.viewer_public_root / Path(str(viewer["base_dir"])), self.viewer_public_root, True),
                )
            )
        self._validate_required(required)

    def validate_assets(self) -> None:
        self.validate_physics_assets()
        self.validate_viewer_assets()


def available_environment_ids(sim_repo: Path) -> list[str]:
    ids: set[str] = set()
    for name in (TRACKED_ENVIRONMENT_DIRECTORY, LOCAL_ENVIRONMENT_DIRECTORY):
        directory = sim_repo / name
        if directory.is_dir():
            ids.update(path.stem for path in directory.glob("*.json") if ENVIRONMENT_ID_RE.fullmatch(path.stem))
    return sorted(ids)


def _manifest_path(sim_repo: Path, environment_id: str) -> Path:
    tracked = sim_repo / TRACKED_ENVIRONMENT_DIRECTORY / f"{environment_id}.json"
    return tracked if tracked.is_file() else sim_repo / LOCAL_ENVIRONMENT_DIRECTORY / f"{environment_id}.json"


def load_environment_pack(
    sim_repo: Path,
    environment_id: str,
    *,
    validate_assets: bool = False,
) -> EnvironmentPack:
    if len(environment_id) > 64 or ENVIRONMENT_ID_RE.fullmatch(environment_id) is None:
        raise StackError(f"Invalid simulator environment {environment_id!r}.")
    manifest_path = _manifest_path(sim_repo, environment_id)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        choices = ", ".join(available_environment_ids(sim_repo)) or "none"
        raise StackError(
            f"Unknown simulator environment {environment_id!r}. Available environments: {choices}."
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(str(exc), manifest_path) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise _fail("top level must be an object with schema_version 1", manifest_path)
    if _string(raw, "id", manifest_path) != environment_id:
        raise _fail("id must match the manifest filename", manifest_path)
    display_name = _string(raw, "display_name", manifest_path)
    if len(display_name) > 160 or any(ord(character) < 0x20 for character in display_name):
        raise _fail("display_name contains invalid characters", manifest_path)
    if _string(raw, "coordinate_system", manifest_path) != SUPPORTED_COORDINATE_SYSTEM:
        raise _fail(f"coordinate_system must be {SUPPORTED_COORDINATE_SYSTEM!r}", manifest_path)

    physics = _object(raw, "physics", manifest_path)
    viewer = _object(raw, "viewer", manifest_path)
    navigation = _object(raw, "navigation", manifest_path)
    spawn = _object(raw, "spawn", manifest_path)
    _relative_path(physics, "collision_dir", manifest_path)
    _relative_path(physics, "visual_dir", manifest_path)
    _relative_path(viewer, "collision_dir", manifest_path)
    viewer_type = _string(viewer, "type", manifest_path)
    if viewer_type == "glb":
        _relative_path(viewer, "model", manifest_path)
    elif viewer_type == "split-glb":
        _relative_path(viewer, "manifest", manifest_path)
        _relative_path(viewer, "base_dir", manifest_path)
    else:
        raise _fail("viewer.type must be 'glb' or 'split-glb'", manifest_path)
    _relative_path(navigation, "map_yaml", manifest_path)
    _relative_path(navigation, "map_image", manifest_path)
    _number(spawn, "x", manifest_path)
    _number(spawn, "y", manifest_path)
    _number(spawn, "yaw_degrees", manifest_path)

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    identity = (
        _local_generation(raw, manifest_path)
        if manifest_path.parent.name == LOCAL_ENVIRONMENT_DIRECTORY
        else _layer_identity(sim_repo)
    )
    pack = EnvironmentPack(
        manifest=raw,
        manifest_path=manifest_path,
        assets_root=sim_repo / "assets",
        viewer_public_root=sim_repo / "viewer" / "public",
        fingerprint=hashlib.sha256(canonical + b"\0" + identity).hexdigest(),
    )
    if validate_assets:
        pack.validate_assets()
    return pack


def read_active_descriptor(sim_repo: Path) -> dict[str, object] | None:
    try:
        value = json.loads((sim_repo / "assets" / ACTIVE_ENVIRONMENT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    if not isinstance(value.get("id"), str) or not isinstance(value.get("fingerprint"), str):
        return None
    return value


def load_active_environment(sim_repo: Path, *, validate_assets: bool = False) -> EnvironmentPack | None:
    descriptor = read_active_descriptor(sim_repo)
    if descriptor is None:
        return None
    try:
        pack = load_environment_pack(sim_repo, str(descriptor["id"]), validate_assets=validate_assets)
    except StackError:
        return None
    if descriptor.get("fingerprint") != pack.fingerprint or descriptor.get("display_name") != pack.display_name:
        return None
    return pack


def select_environment(config: dict[str, object], override: str | None = None) -> EnvironmentPack:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    pack = load_environment_pack(sim_repo, override or str(config.get("environment_id") or DEFAULT_ENVIRONMENT_ID))
    config["environment"] = pack
    config["environment_id"] = pack.id
    return pack


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def activate_environment(pack: EnvironmentPack) -> bool:
    pack.validate_assets()
    payload = pack.active_descriptor()
    previous = read_active_descriptor(pack.assets_root.parent)
    if previous == payload:
        return False
    _write_json_atomic(pack.active_path, payload)
    return True
