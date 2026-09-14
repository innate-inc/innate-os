#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FAST_FOUNDATION_REPO = "https://github.com/NVlabs/Fast-FoundationStereo.git"
FAST_FOUNDATION_REF = "master"
FAST_FOUNDATION_CHECKOUT = REPO_ROOT / "ros2_ws" / "src" / "third_party" / "stereo_models" / "Fast-FoundationStereo"
FAST_FOUNDATION_VENV = REPO_ROOT / ".venvs" / "fast_foundation_stereo"
PYTORCH_INDEX_CU124 = "https://download.pytorch.org/whl/cu124"


def _run(cmd: list[str], cwd: Path, dry_run: bool) -> None:
    text = " ".join(shlex.quote(part) for part in cmd)
    print(f"[cmd] {text}")
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _run_bash(command: str, cwd: Path, dry_run: bool) -> None:
    print(f"[bash] {command}")
    if dry_run:
        return
    subprocess.run(["bash", "-lc", command], cwd=str(cwd), check=True)


def _try_run(cmd: list[str], cwd: Path) -> bool:
    text = " ".join(shlex.quote(part) for part in cmd)
    print(f"[cmd] {text}")
    result = subprocess.run(cmd, cwd=str(cwd))
    return result.returncode == 0


def _ensure_git_checkout(target: Path, url: str, ref: str, update_existing: bool, dry_run: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not (target / ".git").exists():
            raise RuntimeError(f"Checkout path exists but is not a git repo: {target}")
        if not update_existing:
            print(f"[skip] keeping existing checkout: {target}")
            return
        _run(["git", "fetch", "--all"], cwd=target, dry_run=dry_run)
        _run(["git", "checkout", ref], cwd=target, dry_run=dry_run)
        _run(["git", "pull", "--ff-only", "origin", ref], cwd=target, dry_run=dry_run)
        _run(["git", "submodule", "update", "--init", "--recursive"], cwd=target, dry_run=dry_run)
        return
    _run(["git", "clone", "--branch", ref, "--recursive", url, str(target)], cwd=REPO_ROOT, dry_run=dry_run)


def _run_lightweight_route(args: argparse.Namespace) -> None:
    checkout_dir = Path(args.checkout_dir).expanduser().resolve() if args.checkout_dir else FAST_FOUNDATION_CHECKOUT
    venv_dir = Path(args.venv_dir).expanduser().resolve() if args.venv_dir else FAST_FOUNDATION_VENV

    print(f"[route] lightweight_fast_foundation checkout={checkout_dir}")
    _ensure_git_checkout(
        target=checkout_dir,
        url=FAST_FOUNDATION_REPO,
        ref=FAST_FOUNDATION_REF,
        update_existing=not args.no_update_existing,
        dry_run=args.dry_run,
    )

    if args.skip_venv:
        print("[skip] lightweight python venv setup skipped (--skip-venv).")
        print("[next] create an environment manually when ready, then install model deps.")
        return

    try:
        _run([args.python, "-m", "venv", str(venv_dir)], cwd=REPO_ROOT, dry_run=args.dry_run)
    except subprocess.CalledProcessError:
        print("[warn] python venv creation failed (python3-venv is likely missing).")
        print("[next] run: sudo apt-get install -y python3.10-venv")
        print("[next] then re-run this script, or use --skip-venv.")
        return
    pip = venv_dir / "bin" / "pip"
    python = venv_dir / "bin" / "python"

    _run([str(pip), "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=REPO_ROOT, dry_run=args.dry_run)
    if args.install_python_deps:
        preferred = [
            str(pip),
            "install",
            "torch==2.6.0",
            "torchvision==0.21.0",
            "xformers",
            "--index-url",
            PYTORCH_INDEX_CU124,
        ]
        fallback = [
            str(pip),
            "install",
            "torch==2.5.1",
            "torchvision==0.20.1",
            "--index-url",
            PYTORCH_INDEX_CU124,
        ]
        if args.dry_run:
            _run(preferred, cwd=REPO_ROOT, dry_run=True)
            _run(fallback, cwd=REPO_ROOT, dry_run=True)
        else:
            print("[info] installing Fast-FoundationStereo runtime deps")
            if _try_run(preferred, cwd=REPO_ROOT):
                print("[info] installed preferred deps (torch 2.6.0 / torchvision 0.21.0 / xformers)")
            else:
                print("[warn] preferred deps unavailable for this platform; using fallback torch/vision without xformers")
                if not _try_run(fallback, cwd=REPO_ROOT):
                    raise RuntimeError(
                        "Could not install fallback torch/torchvision wheels from cu124 index. "
                        "Pass custom wheel/index settings for your Jetson image."
                    )
        req = checkout_dir / "requirements.txt"
        if req.exists():
            _run([str(pip), "install", "-r", str(req)], cwd=checkout_dir, dry_run=args.dry_run)
    else:
        print("[next] lightweight deps not installed yet; run with --install-python-deps when ready.")

    print("[next] lightweight route ready.")
    print(f"  source {venv_dir}/bin/activate")
    print(f"  python {checkout_dir}/scripts/run_demo.py --help")
    print("  # or wire this env + repo into your ROS wrapper launch")
    _run_bash(f"test -x {shlex.quote(str(python))}", cwd=REPO_ROOT, dry_run=args.dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Setup stereo benchmarking dependencies (lightweight route only).")
    parser.add_argument(
        "--route",
        default="lightweight_fast_foundation",
        choices=("lightweight_fast_foundation",),
        help="Setup route to execute.",
    )
    parser.add_argument("--checkout-dir", help="Override Fast-FoundationStereo checkout path.")
    parser.add_argument("--venv-dir", help="Override lightweight venv path.")
    parser.add_argument("--python", default="python3", help="Python executable used to create lightweight venv.")
    parser.add_argument("--skip-venv", action="store_true", help="Skip venv creation in lightweight route.")
    parser.add_argument(
        "--install-python-deps",
        action="store_true",
        help="Install torch/xformers and requirements for lightweight route.",
    )
    parser.add_argument("--no-update-existing", action="store_true", help="Do not update existing git checkouts.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _run_lightweight_route(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
