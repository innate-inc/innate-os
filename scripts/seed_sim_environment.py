#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Seed Nav2 from the active simulator environment descriptor."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path


class EnvironmentSeedError(RuntimeError):
    pass


def _contained_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise EnvironmentSeedError(f"active environment {label} must be a relative path")
    root = root.resolve()
    source = (root / value).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise EnvironmentSeedError(f"active environment {label} is unavailable: {source}")
    return source


def _marker_identity(path: Path) -> tuple[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    environment_id, fingerprint = value.get("id"), value.get("fingerprint")
    return (environment_id, fingerprint) if isinstance(environment_id, str) and isinstance(fingerprint, str) else None


def _write_marker(path: Path, identity: tuple[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps({"schema_version": 1, "id": identity[0], "fingerprint": identity[1]}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seed_sim_environment(descriptor_path: Path, assets_root: Path, data_root: Path) -> tuple[Path, Path]:
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EnvironmentSeedError(f"could not read active environment descriptor {descriptor_path}: {exc}") from exc
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
        raise EnvironmentSeedError("active environment descriptor must use schema_version 1")
    environment_id, fingerprint = descriptor.get("id"), descriptor.get("fingerprint")
    if not isinstance(environment_id, str) or not environment_id or not isinstance(fingerprint, str) or not fingerprint:
        raise EnvironmentSeedError("active environment descriptor requires id and fingerprint")
    navigation = descriptor.get("navigation")
    if not isinstance(navigation, dict):
        raise EnvironmentSeedError("active environment descriptor requires navigation")

    map_yaml = _contained_file(assets_root, navigation.get("map_yaml"), "navigation.map_yaml")
    map_image = _contained_file(assets_root, navigation.get("map_image"), "navigation.map_image")
    maps = data_root / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    selected_yaml, selected_image = maps / map_yaml.name, maps / map_image.name
    try:
        yaml_text = map_yaml.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EnvironmentSeedError(f"could not read navigation map {map_yaml}: {exc}") from exc
    yaml_text, count = re.subn(
        r"(?m)^(\s*image\s*:).*$",
        lambda match: f"{match.group(1)} {json.dumps(selected_image.name)}",
        yaml_text,
        count=1,
    )
    if count != 1:
        raise EnvironmentSeedError(f"navigation map has no image field: {map_yaml}")
    shutil.copy2(map_image, selected_image)
    selected_yaml.write_text(yaml_text, encoding="utf-8")

    identity = environment_id, fingerprint
    environment_marker = data_root / ".last_environment"
    last_map = data_root / ".last_map"
    try:
        saved = last_map.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        saved = ""
    saved_exists = bool(saved) and Path(saved).name == saved and (maps / saved).is_file()
    legacy_apartment = not environment_marker.exists() and environment_id == "apartment"
    if (_marker_identity(environment_marker) != identity and not legacy_apartment) or not saved_exists:
        last_map.write_text(f"{selected_yaml.name}\n", encoding="utf-8")
    _write_marker(environment_marker, identity)
    return selected_yaml, selected_image


def main() -> int:
    os_root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
    assets = os_root / "sim" / "assets"
    try:
        selected, _image = seed_sim_environment(assets / ".active-environment.json", assets, os_root / "data")
    except EnvironmentSeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Seeded simulator navigation map: {selected.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
