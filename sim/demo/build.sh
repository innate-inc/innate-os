#!/usr/bin/env bash
# Build the public sim demo image.
#
# The asset and viewer images are content-addressed (inputs-<hash>), so the tags
# are computed, never typed: this asks sim/launcher/config.py for the same refs
# `./innate-sim up` would install from, which is what keeps the demo image and a
# dev checkout showing the same apartment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${IMAGE:-ghcr.io/innate-inc/innate-os-sim-demo}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
ROS_IMAGE="${ROS_IMAGE:-ghcr.io/innate-inc/innate-os-sim-ros:main}"

resolve() {
    python3 - "$1" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "sim" / "launcher"))
import config

root = Path.cwd()
print(getattr(config, f"resolve_{sys.argv[1]}_image")(root))
PY
}

ASSETS_IMAGE="${ASSETS_IMAGE:-$(resolve assets)}"
VIEWER_IMAGE="${VIEWER_IMAGE:-$(resolve viewer)}"

echo "ros    : $ROS_IMAGE"
echo "assets : $ASSETS_IMAGE"
echo "viewer : $VIEWER_IMAGE"
echo "output : $IMAGE:$TAG"

# linux/amd64 explicitly: the demo runs on x86 cloud instances, and a silent
# arm64 build on an Apple Silicon laptop would only fail at deploy time.
exec docker buildx build \
    --platform linux/amd64 \
    --build-arg "ROS_IMAGE=$ROS_IMAGE" \
    --build-arg "ASSETS_IMAGE=$ASSETS_IMAGE" \
    --build-arg "VIEWER_IMAGE=$VIEWER_IMAGE" \
    --cache-from "type=registry,ref=$IMAGE:buildcache" \
    --tag "$IMAGE:$TAG" \
    --tag "$IMAGE:latest" \
    -f sim/demo/Dockerfile \
    "$@" \
    .
