#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Fast-FoundationStereo single-engine TensorRT artifacts.")
    parser.add_argument(
        "--model-repo",
        default="/home/jetson1/innate-os/ros2_ws/src/third_party/stereo_models/Fast-FoundationStereo",
        help="Path to Fast-FoundationStereo repository.",
    )
    parser.add_argument(
        "--checkpoint",
        action="append",
        dest="checkpoints",
        help="Checkpoint folder under weights/, repeat for multiple.",
    )
    parser.add_argument("--valid-iters", type=int, default=8, help="Refinement iterations used during export.")
    parser.add_argument("--max-disp", type=int, default=192, help="Max disparity for export.")
    parser.add_argument("--height", type=int, default=480, help="Engine input height, divisible by 32.")
    parser.add_argument("--width", type=int, default=640, help="Engine input width, divisible by 32.")
    parser.add_argument(
        "--trtexec",
        default="/usr/src/tensorrt/bin/trtexec",
        help="Path to trtexec binary.",
    )
    parser.add_argument(
        "--output-root",
        default="",
        help="Override engine output root directory (default: <repo>/engines).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model_repo = Path(args.model_repo).expanduser().resolve()
    make_onnx = model_repo / "scripts" / "make_single_onnx.py"
    trtexec = Path(args.trtexec).expanduser().resolve()

    if not model_repo.exists():
        raise RuntimeError(f"Model repo does not exist: {model_repo}")
    if not make_onnx.exists():
        raise RuntimeError(f"ONNX export script missing: {make_onnx}")
    if not trtexec.exists():
        raise RuntimeError(f"trtexec not found: {trtexec}")
    if args.height % 32 != 0 or args.width % 32 != 0:
        raise RuntimeError("height and width must be divisible by 32")

    checkpoints = args.checkpoints or ["23-36-37", "20-30-48"]
    output_root = Path(args.output_root).expanduser().resolve() if args.output_root else (model_repo / "engines")
    output_root.mkdir(parents=True, exist_ok=True)

    for ckpt in checkpoints:
        ckpt_dir = model_repo / "weights" / ckpt
        model_path = ckpt_dir / "model_best_bp2_serialize.pth"
        if not model_path.exists():
            raise RuntimeError(f"Missing checkpoint file: {model_path}")

        out_dir = output_root / f"{ckpt}_i{args.valid_iters}_{args.height}x{args.width}"
        out_dir.mkdir(parents=True, exist_ok=True)
        onnx_name = "fast_foundationstereo"
        onnx_path = out_dir / f"{onnx_name}.onnx"
        engine_path = out_dir / f"{onnx_name}.engine"

        run(
            [
                "python3",
                str(make_onnx),
                "--model_dir",
                str(model_path),
                "--save_path",
                str(out_dir),
                "--height",
                str(args.height),
                "--width",
                str(args.width),
                "--valid_iters",
                str(args.valid_iters),
                "--max_disp",
                str(args.max_disp),
                "--onnx_name",
                onnx_name,
            ]
        )
        run(
            [
                str(trtexec),
                f"--onnx={onnx_path}",
                f"--saveEngine={engine_path}",
                "--fp16",
                "--useCudaGraph",
            ]
        )
        print(f"Built engine: {engine_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
