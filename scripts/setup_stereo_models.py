#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ISAAC_ROS_DNN_REPO = "https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_dnn_stereo_depth.git"
ISAAC_ROS_REQUIRED_REPOS = (
    "isaac_ros_common",
    "isaac_ros_nitros",
    "isaac_ros_dnn_inference",
    "isaac_ros_image_pipeline",
)


@dataclass(frozen=True)
class ExecutableSpec:
    package: str
    executable: str


@dataclass(frozen=True)
class ModelInstallSpec:
    name: str
    source_type: str
    enabled: bool
    git_url: str | None
    git_ref: str | None
    git_recursive: bool
    checkout_dir: Path | None
    apt_packages: tuple[str, ...]
    ros_packages: tuple[str, ...]
    colcon_packages: tuple[str, ...]
    executables: tuple[ExecutableSpec, ...]
    setup_commands: tuple[str, ...]


def _quote(path: Path) -> str:
    return shlex.quote(str(path))


def _run(command: list[str], cwd: Path, dry_run: bool) -> None:
    text = " ".join(shlex.quote(tok) for tok in command)
    print(f"[cmd] {text}")
    if dry_run:
        return
    subprocess.run(command, cwd=str(cwd), check=True)


def _run_bash(command: str, cwd: Path, dry_run: bool) -> None:
    print(f"[bash] {command}")
    if dry_run:
        return
    subprocess.run(["bash", "-lc", command], cwd=str(cwd), check=True)


def _parse_model(raw: dict[str, Any], defaults: dict[str, Any], repo_root: Path) -> ModelInstallSpec:
    name = str(raw["name"])
    enabled = bool(raw.get("enabled", True))
    source = raw.get("source", {})
    source_type = str(source.get("type", "git"))

    git_url = source.get("url")
    git_ref = str(source.get("ref", defaults.get("git_ref", "main"))) if source_type == "git" else None
    git_recursive = bool(source.get("recursive", defaults.get("git_recursive", True)))
    checkout_dir_raw = raw.get("checkout_dir")
    checkout_dir = (repo_root / checkout_dir_raw).resolve() if checkout_dir_raw else None

    apt_packages = tuple(str(pkg) for pkg in source.get("packages", [])) if source_type == "apt" else ()
    ros_packages = tuple(str(pkg) for pkg in raw.get("ros_packages", []))
    colcon_packages = tuple(str(pkg) for pkg in raw.get("colcon_packages", ros_packages))
    exec_specs = tuple(
        ExecutableSpec(package=str(item["package"]), executable=str(item["executable"]))
        for item in raw.get("executables", [])
    )
    setup_commands = tuple(str(cmd) for cmd in raw.get("setup_commands", []))

    if source_type == "git":
        if not git_url:
            raise ValueError(f"Model '{name}' is git-based but missing source.url")
        if checkout_dir is None:
            raise ValueError(f"Model '{name}' is git-based but missing checkout_dir")
    if source_type == "apt" and not apt_packages:
        raise ValueError(f"Model '{name}' is apt-based but source.packages is empty")
    if source_type not in ("git", "apt"):
        raise ValueError(f"Model '{name}' has unsupported source.type '{source_type}'")
    if not ros_packages:
        raise ValueError(f"Model '{name}' needs ros_packages for verification")
    if not exec_specs:
        raise ValueError(f"Model '{name}' needs executables for verification")

    return ModelInstallSpec(
        name=name,
        source_type=source_type,
        enabled=enabled,
        git_url=str(git_url) if git_url else None,
        git_ref=git_ref,
        git_recursive=git_recursive,
        checkout_dir=checkout_dir,
        apt_packages=apt_packages,
        ros_packages=ros_packages,
        colcon_packages=colcon_packages,
        executables=exec_specs,
        setup_commands=setup_commands,
    )


def _load_manifest(manifest_path: Path, repo_root: Path) -> tuple[Path, bool, tuple[str, ...], list[ModelInstallSpec]]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults", {})
    workspace_rel = str(data.get("workspace", "ros2_ws"))
    workspace_dir = (repo_root / workspace_rel).resolve()
    if not workspace_dir.exists():
        raise RuntimeError(f"Workspace path does not exist: {workspace_dir}")
    run_rosdep = bool(defaults.get("run_rosdep", True))
    global_colcon_args = tuple(str(arg) for arg in data.get("colcon_args", ["--symlink-install"]))

    models_raw = data.get("models", [])
    if not models_raw:
        raise RuntimeError(f"No models listed in manifest {manifest_path}")
    models = [_parse_model(raw, defaults=defaults, repo_root=repo_root) for raw in models_raw]
    return workspace_dir, run_rosdep, global_colcon_args, models


def _ensure_git_repo(
    target: Path,
    git_url: str,
    git_ref: str,
    git_recursive: bool,
    update_existing: bool,
    dry_run: bool,
    repo_root: Path,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not (target / ".git").exists():
            raise RuntimeError(f"Target exists but is not a git repository: {target}")
        if not update_existing:
            print(f"[skip] existing checkout kept at {target}")
            return target
        _run(["git", "fetch", "--all"], cwd=target, dry_run=dry_run)
        _run(["git", "checkout", git_ref], cwd=target, dry_run=dry_run)
        _run(["git", "pull", "--ff-only", "origin", git_ref], cwd=target, dry_run=dry_run)
        if git_recursive:
            _run(["git", "submodule", "update", "--init", "--recursive"], cwd=target, dry_run=dry_run)
        return target

    clone_cmd = ["git", "clone", "--branch", git_ref]
    if git_recursive:
        clone_cmd.append("--recursive")
    clone_cmd.extend([git_url, str(target)])
    _run(clone_cmd, cwd=repo_root, dry_run=dry_run)
    return target


def _ensure_git_checkout(model: ModelInstallSpec, update_existing: bool, dry_run: bool, repo_root: Path) -> Path:
    assert model.checkout_dir is not None
    return _ensure_git_repo(
        target=model.checkout_dir,
        git_url=model.git_url or "",
        git_ref=model.git_ref or "main",
        git_recursive=model.git_recursive,
        update_existing=update_existing,
        dry_run=dry_run,
        repo_root=repo_root,
    )


def _bootstrap_isaac_ros_dependencies(
    workspace_dir: Path,
    git_ref: str,
    update_existing: bool,
    dry_run: bool,
    repo_root: Path,
) -> list[Path]:
    base_checkout_dir = workspace_dir / "src" / "third_party" / "stereo_models"
    checkouts: list[Path] = []
    print(f"\n[info] bootstrapping Isaac ROS dependencies ({git_ref})")
    for repo in ISAAC_ROS_REQUIRED_REPOS:
        url = f"https://github.com/NVIDIA-ISAAC-ROS/{repo}.git"
        target = base_checkout_dir / repo
        print(f"[info] dependency repo: {repo}")
        checkout = _ensure_git_repo(
            target=target,
            git_url=url,
            git_ref=git_ref,
            git_recursive=True,
            update_existing=update_existing,
            dry_run=dry_run,
            repo_root=repo_root,
        )
        checkouts.append(checkout)
    return checkouts


def _install_apt_packages(packages: list[str], dry_run: bool, repo_root: Path) -> None:
    if not packages:
        return
    _run(["sudo", "apt-get", "update"], cwd=repo_root, dry_run=dry_run)
    _run(["sudo", "apt-get", "install", "-y", *packages], cwd=repo_root, dry_run=dry_run)


def _run_rosdep(workspace_dir: Path, source_paths: list[Path], dry_run: bool, isaac_rosdep_ref: str | None = None) -> None:
    if not source_paths:
        return
    unique_source_paths = list(dict.fromkeys(source_paths))
    quoted_paths = " ".join(_quote(path) for path in unique_source_paths)
    extra_rosdep_source = ""
    if isaac_rosdep_ref:
        extra_rosdep_source = (
            "if [ ! -f /etc/ros/rosdep/sources.list.d/00-nvidia-isaac.list ]; then "
            "sudo curl -fsSL -o /etc/ros/rosdep/sources.list.d/nvidia-isaac.yaml "
            f"https://raw.githubusercontent.com/NVIDIA-ISAAC-ROS/isaac-ros-cli/{isaac_rosdep_ref}/docker/rosdep/extra_rosdeps.yaml && "
            "echo 'yaml file:///etc/ros/rosdep/sources.list.d/nvidia-isaac.yaml' | "
            "sudo tee /etc/ros/rosdep/sources.list.d/00-nvidia-isaac.list >/dev/null; "
            "fi && "
        )
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        "if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then sudo rosdep init; fi && "
        f"{extra_rosdep_source}"
        "rosdep update && "
        f"rosdep install --from-paths {quoted_paths} --ignore-src -r -y"
    )
    _run_bash(cmd, cwd=workspace_dir, dry_run=dry_run)


def _run_setup_commands(commands: tuple[str, ...], cwd: Path, dry_run: bool) -> None:
    for command in commands:
        _run_bash(command, cwd=cwd, dry_run=dry_run)


def _ensure_sudo_access_if_needed(dry_run: bool) -> None:
    if dry_run:
        return
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
    if check.returncode == 0:
        return
    raise RuntimeError("sudo access is required. Run 'sudo -v' in this terminal, then re-run this script.")


def _build_packages(workspace_dir: Path, colcon_packages: list[str], colcon_args: tuple[str, ...], dry_run: bool) -> None:
    if not colcon_packages:
        return
    pkg_select = " ".join(shlex.quote(pkg) for pkg in sorted(set(colcon_packages)))
    args = " ".join(shlex.quote(arg) for arg in colcon_args)
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"export ISAAC_ROS_WS={_quote(workspace_dir)} && "
        f"cd {_quote(workspace_dir)} && "
        f"colcon build {args} --packages-up-to {pkg_select}"
    )
    _run_bash(cmd, cwd=workspace_dir, dry_run=dry_run)


def _verify_ros_package(workspace_dir: Path, pkg: str, dry_run: bool) -> None:
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {_quote(workspace_dir / 'install' / 'setup.bash')} && "
        f"ros2 pkg prefix {shlex.quote(pkg)}"
    )
    _run_bash(cmd, cwd=workspace_dir, dry_run=dry_run)


def _verify_executable(workspace_dir: Path, spec: ExecutableSpec, dry_run: bool) -> None:
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {_quote(workspace_dir / 'install' / 'setup.bash')} && "
        f"ros2 pkg executables {shlex.quote(spec.package)}"
    )
    print(f"[verify] {spec.package}/{spec.executable}")
    if dry_run:
        print(f"[bash] {cmd}")
        return
    result = subprocess.run(["bash", "-lc", cmd], cwd=str(workspace_dir), capture_output=True, text=True, check=True)
    pairs = [line.strip().split() for line in result.stdout.splitlines() if line.strip()]
    found = any(len(parts) == 2 and parts[0] == spec.package and parts[1] == spec.executable for parts in pairs)
    if not found:
        raise RuntimeError(
            f"Executable '{spec.executable}' not found under package '{spec.package}'.\n"
            f"Available entries:\n{result.stdout}"
        )


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    parser = argparse.ArgumentParser(description="Download/build/verify stereo model ROS packages and executables.")
    parser.add_argument(
        "--manifest",
        default=str(repo_root / "ros2_ws" / "src" / "mars_bot" / "mars_cam" / "config" / "stereo_model_sources.example.yaml"),
        help="YAML manifest listing model repositories and executable checks.",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        help="Optional model names to install (default: all enabled models in manifest).",
    )
    parser.add_argument("--skip-rosdep", action="store_true", help="Skip rosdep install.")
    parser.add_argument("--skip-build", action="store_true", help="Skip colcon build.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--no-update-existing", action="store_true", help="Do not pull/fetch existing git checkouts.")
    parser.add_argument("--allow-apt", action="store_true", help="Allow apt-based model installation entries.")
    parser.add_argument("--accept-eula", action="store_true", help="Accept Isaac ROS model EULAs for automated asset install.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.exists():
        raise RuntimeError(f"Manifest not found: {manifest_path}")

    workspace_dir, run_rosdep_default, colcon_args, manifest_models = _load_manifest(manifest_path, repo_root=repo_root)
    selected_names = set(args.models or [])
    selected_models = [
        model
        for model in manifest_models
        if model.enabled and (not selected_names or model.name in selected_names)
    ]
    if not selected_models:
        raise RuntimeError("No models selected. Enable models in manifest or pass --models.")

    print(f"[info] repo root: {repo_root}")
    print(f"[info] workspace: {workspace_dir}")
    print(f"[info] manifest : {manifest_path}")
    print(f"[info] models   : {', '.join(model.name for model in selected_models)}")

    apt_packages: list[str] = []
    source_paths: list[Path] = []
    all_colcon_packages: list[str] = []
    isaac_rosdep_ref: str | None = None

    for model in selected_models:
        print(f"\n=== {model.name} ===")
        if model.source_type == "apt":
            if not args.allow_apt:
                raise RuntimeError(f"{model.name} is apt-based. Re-run with --allow-apt.")
            apt_packages.extend(model.apt_packages)
        elif model.source_type == "git":
            checkout = _ensure_git_checkout(
                model=model,
                update_existing=not args.no_update_existing,
                dry_run=args.dry_run,
                repo_root=repo_root,
            )
            source_paths.append(checkout)
            _run_setup_commands(model.setup_commands, cwd=checkout, dry_run=args.dry_run)
        all_colcon_packages.extend(model.colcon_packages)

    isaac_models = [
        model for model in selected_models if model.git_url and model.git_url.rstrip("/") == ISAAC_ROS_DNN_REPO.rstrip("/")
    ]
    if isaac_models:
        isaac_refs = {model.git_ref or "release-4.5" for model in isaac_models}
        if len(isaac_refs) > 1:
            raise RuntimeError(f"Inconsistent Isaac ROS refs in manifest: {', '.join(sorted(isaac_refs))}")
        isaac_rosdep_ref = next(iter(isaac_refs))
        if args.accept_eula:
            os.environ["ISAAC_ROS_ACCEPT_EULA"] = "1"
        source_paths.extend(
            _bootstrap_isaac_ros_dependencies(
                workspace_dir=workspace_dir,
                git_ref=isaac_rosdep_ref,
                update_existing=not args.no_update_existing,
                dry_run=args.dry_run,
                repo_root=repo_root,
            )
        )

    if apt_packages:
        _ensure_sudo_access_if_needed(dry_run=args.dry_run)
        deduped = sorted(set(apt_packages))
        print(f"\n[info] apt packages: {' '.join(deduped)}")
        _install_apt_packages(deduped, dry_run=args.dry_run, repo_root=repo_root)

    if not args.skip_rosdep and run_rosdep_default:
        _ensure_sudo_access_if_needed(dry_run=args.dry_run)
        _run_rosdep(
            workspace_dir=workspace_dir,
            source_paths=source_paths,
            dry_run=args.dry_run,
            isaac_rosdep_ref=isaac_rosdep_ref,
        )

    if isaac_models and not args.skip_build and not args.dry_run:
        accepted = os.environ.get("ISAAC_ROS_ACCEPT_EULA", "").strip().lower() in {"1", "true", "yes"}
        if not accepted:
            raise RuntimeError(
                "Isaac model assets require EULA acceptance. Re-run with --accept-eula "
                "or set ISAAC_ROS_ACCEPT_EULA=1."
            )

    if not args.skip_build:
        _build_packages(
            workspace_dir=workspace_dir,
            colcon_packages=all_colcon_packages,
            colcon_args=colcon_args,
            dry_run=args.dry_run,
        )

    print("\n[info] verifying ROS packages and executables")
    for model in selected_models:
        for pkg in model.ros_packages:
            _verify_ros_package(workspace_dir=workspace_dir, pkg=pkg, dry_run=args.dry_run)
        for spec in model.executables:
            _verify_executable(workspace_dir=workspace_dir, spec=spec, dry_run=args.dry_run)

    print("\n[done] model installation checks passed")
    print("[next] use these in benchmark config:")
    for model in selected_models:
        first = model.executables[0]
        print(f"  - {model.name}: model_package: {first.package}, model_executable: {first.executable}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
