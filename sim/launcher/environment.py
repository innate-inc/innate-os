# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Named, launch-time environment packs for the Innate simulator.

An environment pack is deliberately static for one simulator run. Its tracked
manifest binds the four consumers that must agree about the world: MuJoCo,
the browser viewer, the robot spawn, and Nav2. Generated geometry remains in
the pinned sim asset bundle; the manifest only names files inside it.
"""

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
SUPPORTED_COORDINATE_SYSTEM = "gltf-y-up-meters"
ACTIVE_ENVIRONMENT_FILENAME = ".active-environment.json"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


@dataclass(frozen=True)
class EnvironmentPack:
    id: str
    display_name: str
    coordinate_system: str
    fingerprint: str
    manifest_path: Path
    assets_root: Path
    viewer_public_root: Path
    collision_dir: Path
    visual_dir: Path
    viewer_type: str
    viewer_model: Path | None
    viewer_manifest: Path | None
    viewer_base_dir: Path | None
    viewer_collision_dir: Path
    nav_map_yaml: Path
    nav_map_image: Path
    spawn_x: float
    spawn_y: float
    spawn_yaw_degrees: float

    @property
    def collision_path(self) -> Path:
        return self.assets_root / self.collision_dir

    @property
    def visual_path(self) -> Path:
        return self.assets_root / self.visual_dir

    @property
    def viewer_model_path(self) -> Path | None:
        return self.viewer_public_root / self.viewer_model if self.viewer_model is not None else None

    @property
    def viewer_manifest_path(self) -> Path | None:
        return self.viewer_public_root / self.viewer_manifest if self.viewer_manifest is not None else None

    @property
    def viewer_base_path(self) -> Path | None:
        return self.viewer_public_root / self.viewer_base_dir if self.viewer_base_dir is not None else None

    @property
    def viewer_collision_path(self) -> Path:
        return self.viewer_public_root / self.viewer_collision_dir

    @property
    def nav_map_yaml_path(self) -> Path:
        return self.assets_root / self.nav_map_yaml

    @property
    def nav_map_image_path(self) -> Path:
        return self.assets_root / self.nav_map_image

    @property
    def spawn_pose(self) -> tuple[float, float, float]:
        return self.spawn_x, self.spawn_y, self.spawn_yaw_degrees

    @property
    def active_path(self) -> Path:
        return self.assets_root / ACTIVE_ENVIRONMENT_FILENAME

    def active_descriptor(self) -> dict[str, object]:
        """Portable runtime descriptor consumed through existing bind mounts."""
        viewer: dict[str, object] = {
            "type": self.viewer_type,
            "collision_dir": self.viewer_collision_dir.as_posix(),
        }
        if self.viewer_model is not None:
            viewer["model"] = self.viewer_model.as_posix()
        if self.viewer_manifest is not None:
            viewer["manifest"] = self.viewer_manifest.as_posix()
        if self.viewer_base_dir is not None:
            viewer["base_dir"] = self.viewer_base_dir.as_posix()
        return {
            "schema_version": 1,
            "id": self.id,
            "display_name": self.display_name,
            "coordinate_system": self.coordinate_system,
            "fingerprint": self.fingerprint,
            "physics": {
                "collision_dir": self.collision_dir.as_posix(),
                "visual_dir": self.visual_dir.as_posix(),
            },
            "viewer": viewer,
            "navigation": {
                "map_yaml": self.nav_map_yaml.as_posix(),
                "map_image": self.nav_map_image.as_posix(),
            },
            "spawn": {
                "x": self.spawn_x,
                "y": self.spawn_y,
                "yaw_degrees": self.spawn_yaw_degrees,
            },
        }

    def validate_assets(self) -> None:
        required: list[tuple[Path, Path, str, bool]] = [
            (self.collision_path, self.assets_root, "MuJoCo collision directory", True),
            (self.visual_path, self.assets_root, "MuJoCo visual directory", True),
            (self.viewer_collision_path, self.viewer_public_root, "viewer collision directory", True),
            (self.nav_map_yaml_path, self.assets_root, "Nav2 map YAML", False),
            (self.nav_map_image_path, self.assets_root, "Nav2 map image", False),
        ]
        if self.viewer_type == "glb":
            assert self.viewer_model_path is not None
            required.append((self.viewer_model_path, self.viewer_public_root, "viewer model", False))
        else:
            assert self.viewer_manifest_path is not None
            assert self.viewer_base_path is not None
            required.extend(
                (
                    (self.viewer_manifest_path, self.viewer_public_root, "viewer room manifest", False),
                    (self.viewer_base_path, self.viewer_public_root, "viewer room directory", True),
                )
            )
        escaping = [f"{label}: {path}" for path, root, label, _is_dir in required if not _path_is_within(path, root)]
        if escaping:
            details = "\n".join(f"  - {item}" for item in escaping)
            raise StackError(f"Environment {self.id!r} contains asset paths that escape their roots:\n{details}")
        missing = [
            f"{label}: {path}"
            for path, _root, label, is_dir in required
            if not (path.is_dir() if is_dir else path.is_file())
        ]
        if missing:
            details = "\n".join(f"  - {item}" for item in missing)
            raise StackError(
                f"Environment {self.id!r} is installed incompletely. Missing:\n{details}\n"
                "Refresh the pinned asset image with `./innate-sim up`, or publish an image containing the pack."
            )


def available_environment_ids(sim_repo: Path) -> list[str]:
    directory = sim_repo / "environments"
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json") if ENVIRONMENT_ID_RE.fullmatch(path.stem))


def _fail(message: str, *, manifest_path: Path | None = None) -> StackError:
    suffix = f" ({manifest_path})" if manifest_path else ""
    return StackError(f"Invalid simulator environment manifest{suffix}: {message}")


def _object(parent: dict[str, object], key: str, manifest_path: Path) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise _fail(f"{key!r} must be an object", manifest_path=manifest_path)
    return value


def _string(parent: dict[str, object], key: str, manifest_path: Path) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{key!r} must be a non-empty string", manifest_path=manifest_path)
    return value.strip()


def _number(parent: dict[str, object], key: str, manifest_path: Path) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _fail(f"{key!r} must be a finite number", manifest_path=manifest_path)
    return float(value)


def _relative_path(parent: dict[str, object], key: str, manifest_path: Path) -> Path:
    raw = _string(parent, key, manifest_path)
    if "\\" in raw:
        raise _fail(f"{key!r} must use forward slashes", manifest_path=manifest_path)
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail(f"{key!r} must be a contained relative path", manifest_path=manifest_path)
    return Path(*path.parts)


def _installed_asset_identity(sim_repo: Path) -> bytes:
    """Identity of the two installed OCI layers used by an active pack.

    The current asset pipeline records each installed layer's digest and image
    ref in a marker beside the files it describes. Hashing the digest token
    from both markers keeps a manifest fingerprint tied to the actual physics
    and viewer bytes on disk, without depending on image-ref spelling or the
    retired whole-bundle archive publisher.
    """
    parts: list[bytes] = []
    for relative in (Path("assets/.assets-tag"), Path("viewer/public/.installed-tag")):
        parts.append(relative.as_posix().encode("utf-8"))
        try:
            marker = (sim_repo / relative).read_text(encoding="utf-8").split()
            parts.append(marker[0].encode("utf-8") if marker else b"<missing>")
        except (OSError, UnicodeError):
            parts.append(b"<missing>")
    return b"\0".join(parts)


def load_environment_pack(
    sim_repo: Path,
    environment_id: str,
    *,
    validate_assets: bool = False,
) -> EnvironmentPack:
    if not ENVIRONMENT_ID_RE.fullmatch(environment_id):
        raise StackError(
            f"Invalid simulator environment {environment_id!r}. Use a lowercase name containing letters, numbers, or hyphens."
        )
    manifest_path = sim_repo / "environments" / f"{environment_id}.json"
    if not manifest_path.is_file():
        available = available_environment_ids(sim_repo)
        choices = ", ".join(available) if available else "none"
        raise StackError(f"Unknown simulator environment {environment_id!r}. Available environments: {choices}.")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(str(exc), manifest_path=manifest_path) from exc
    if not isinstance(raw, dict):
        raise _fail("top level must be an object", manifest_path=manifest_path)
    if raw.get("schema_version") != 1:
        raise _fail("schema_version must be 1", manifest_path=manifest_path)
    manifest_id = _string(raw, "id", manifest_path)
    if manifest_id != environment_id:
        raise _fail(f"id {manifest_id!r} does not match filename {environment_id!r}", manifest_path=manifest_path)
    coordinate_system = _string(raw, "coordinate_system", manifest_path)
    if coordinate_system != SUPPORTED_COORDINATE_SYSTEM:
        raise _fail(
            f"coordinate_system must be {SUPPORTED_COORDINATE_SYSTEM!r}",
            manifest_path=manifest_path,
        )

    physics = _object(raw, "physics", manifest_path)
    viewer = _object(raw, "viewer", manifest_path)
    navigation = _object(raw, "navigation", manifest_path)
    spawn = _object(raw, "spawn", manifest_path)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fingerprint = hashlib.sha256(canonical + b"\0" + _installed_asset_identity(sim_repo)).hexdigest()

    viewer_type = _string(viewer, "type", manifest_path)
    if viewer_type == "glb":
        viewer_model = _relative_path(viewer, "model", manifest_path)
        viewer_manifest = None
        viewer_base_dir = None
    elif viewer_type == "split-glb":
        viewer_model = None
        viewer_manifest = _relative_path(viewer, "manifest", manifest_path)
        viewer_base_dir = _relative_path(viewer, "base_dir", manifest_path)
    else:
        raise _fail("viewer.type must be 'glb' or 'split-glb'", manifest_path=manifest_path)

    pack = EnvironmentPack(
        id=manifest_id,
        display_name=_string(raw, "display_name", manifest_path),
        coordinate_system=coordinate_system,
        fingerprint=fingerprint,
        manifest_path=manifest_path,
        assets_root=sim_repo / "assets",
        viewer_public_root=sim_repo / "viewer" / "public",
        collision_dir=_relative_path(physics, "collision_dir", manifest_path),
        visual_dir=_relative_path(physics, "visual_dir", manifest_path),
        viewer_type=viewer_type,
        viewer_model=viewer_model,
        viewer_manifest=viewer_manifest,
        viewer_base_dir=viewer_base_dir,
        viewer_collision_dir=_relative_path(viewer, "collision_dir", manifest_path),
        nav_map_yaml=_relative_path(navigation, "map_yaml", manifest_path),
        nav_map_image=_relative_path(navigation, "map_image", manifest_path),
        spawn_x=_number(spawn, "x", manifest_path),
        spawn_y=_number(spawn, "y", manifest_path),
        spawn_yaw_degrees=_number(spawn, "yaw_degrees", manifest_path),
    )
    if validate_assets:
        pack.validate_assets()
    return pack


def select_environment(config: dict[str, object], override: str | None = None) -> EnvironmentPack:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    configured = str(config.get("environment_id") or DEFAULT_ENVIRONMENT_ID)
    pack = load_environment_pack(sim_repo, override or configured)
    config["environment"] = pack
    config["environment_id"] = pack.id
    return pack


def activate_environment(pack: EnvironmentPack) -> bool:
    """Atomically publish the selected descriptor. Returns whether it changed."""
    pack.validate_assets()
    payload = (json.dumps(pack.active_descriptor(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        previous = pack.active_path.read_bytes()
    except OSError:
        previous = b""
    if previous == payload:
        return False
    pack.active_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = pack.active_path.with_name(f"{pack.active_path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, pack.active_path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
