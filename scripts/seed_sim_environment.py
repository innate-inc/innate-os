#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Seed Nav2 with the map selected by the active simulator environment."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path


class EnvironmentSeedError(RuntimeError):
    """The active environment cannot safely seed the navigation map."""


def _required_identity(descriptor: dict[str, object]) -> tuple[str, str]:
    values: list[str] = []
    for field in ("id", "fingerprint"):
        value = descriptor.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EnvironmentSeedError(f"active environment descriptor requires a non-empty {field}")
        values.append(value)
    return values[0], values[1]


def _read_environment_identity(marker_path: Path) -> tuple[str, str] | None:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or marker.get("schema_version") != 1:
        return None
    environment_id = marker.get("id")
    fingerprint = marker.get("fingerprint")
    if not isinstance(environment_id, str) or not isinstance(fingerprint, str):
        return None
    return environment_id, fingerprint


def _write_environment_identity(marker_path: Path, identity: tuple[str, str]) -> None:
    """Replace the successful-launch marker atomically in its own directory."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "id": identity[0],
        "fingerprint": identity[1],
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marker_path.parent,
            prefix=f"{marker_path.name}.tmp-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, marker_path)
    except OSError as exc:
        raise EnvironmentSeedError(f"could not persist active environment identity {marker_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _saved_map_is_available(last_map_path: Path, maps_dir: Path) -> bool:
    try:
        saved_map = last_map_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    if not saved_map or Path(saved_map).name != saved_map:
        return False
    return (maps_dir / saved_map).is_file()


def _required_contained_file(assets_root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentSeedError(f"active environment {label} must be a non-empty relative path")

    relative = Path(value)
    if relative.is_absolute():
        raise EnvironmentSeedError(f"active environment {label} must be relative to {assets_root}")

    root = assets_root.resolve()
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise EnvironmentSeedError(f"active environment {label} escapes the simulator assets directory") from exc
    if not source.is_file():
        raise EnvironmentSeedError(f"active environment {label} not found: {source}")
    return source


def seed_sim_environment(descriptor_path: Path, assets_root: Path, data_root: Path) -> tuple[Path, Path]:
    """Copy the active pack's Nav2 files and select its YAML for startup."""
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EnvironmentSeedError(f"active environment descriptor not found: {descriptor_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentSeedError(f"could not read active environment descriptor {descriptor_path}: {exc}") from exc

    if not isinstance(descriptor, dict):
        raise EnvironmentSeedError("active environment descriptor must be a JSON object")
    identity = _required_identity(descriptor)
    navigation = descriptor.get("navigation")
    if not isinstance(navigation, dict):
        raise EnvironmentSeedError("active environment descriptor requires a navigation object")

    for field in ("map_yaml", "map_image"):
        value = navigation.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EnvironmentSeedError(f"active environment navigation.{field} must be a non-empty relative path")

    map_yaml = _required_contained_file(assets_root, navigation.get("map_yaml"), "navigation.map_yaml")
    map_image = _required_contained_file(assets_root, navigation.get("map_image"), "navigation.map_image")

    maps_dir = data_root / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    selected_yaml = maps_dir / map_yaml.name
    selected_image = maps_dir / map_image.name
    try:
        yaml_text = map_yaml.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EnvironmentSeedError(f"could not read navigation.map_yaml {map_yaml}: {exc}") from exc
    # Map YAMLs resolve `image` relative to their copied location. Repoint it
    # to the manifest-selected image basename so packs can keep any source
    # directory layout while mode_manager still discovers a top-level YAML.
    yaml_text, replacements = re.subn(
        r"(?m)^(\s*image\s*:).*$",
        lambda match: f"{match.group(1)} {json.dumps(selected_image.name)}",
        yaml_text,
        count=1,
    )
    if replacements != 1:
        raise EnvironmentSeedError(f"active environment navigation.map_yaml has no image field: {map_yaml}")
    shutil.copy2(map_image, selected_image)
    selected_yaml.write_text(yaml_text, encoding="utf-8")

    # A map recorded while this exact pack was active may be a user-generated
    # map of the same physical world, so preserve it across an ordinary ROS
    # restart. A first launch, changed pack/assets, malformed marker, or stale
    # saved-map name resets Nav2 to the manifest-selected baseline map.
    marker_path = data_root / ".last_environment"
    last_map_path = data_root / ".last_map"
    same_environment = _read_environment_identity(marker_path) == identity
    saved_map_is_available = _saved_map_is_available(last_map_path, maps_dir)
    # Upgrade compatibility: before environment packs, the simulator could
    # only be the apartment and had no .last_environment marker. Preserve its
    # selected/generated map on the first launch of the new code. A first
    # launch into any other pack must select that pack's baseline map.
    legacy_apartment = not marker_path.exists() and identity[0] == "apartment"
    if (not same_environment and not legacy_apartment) or not saved_map_is_available:
        last_map_path.write_text(f"{selected_yaml.name}\n", encoding="utf-8")
    # Write this last: the marker means both the map assets and selection were
    # successfully prepared. A crash before replace makes the next run reset
    # to the pack map instead of preserving ambiguous state.
    _write_environment_identity(marker_path, identity)
    return selected_yaml, selected_image


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-root", type=Path, help="Simulator asset directory (default: <Innate OS>/sim/assets)")
    parser.add_argument("--data-root", type=Path, help="Innate runtime data directory (default: <Innate OS>/data)")
    parser.add_argument(
        "--descriptor", type=Path, help="Active environment JSON (default: <assets>/.active-environment.json)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os_root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
    assets_root = args.assets_root or os_root / "sim" / "assets"
    data_root = args.data_root or os_root / "data"
    descriptor_path = args.descriptor or assets_root / ".active-environment.json"
    try:
        selected_yaml, _selected_image = seed_sim_environment(descriptor_path, assets_root, data_root)
    except EnvironmentSeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Seeded simulator navigation map: {selected_yaml.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
