#!/usr/bin/env bash
# Build the public sim demo image. The asset and viewer tags are content-addressed,
# so they are resolved from sim/launcher/config.py exactly as `./innate-sim up` does.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${IMAGE:-ghcr.io/innate-inc/innate-os-sim-demo}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
ROS_IMAGE="${ROS_IMAGE:-ghcr.io/innate-inc/innate-os-sim-ros:main}"
# Set empty (CI does, off main) so a one-off build cannot become what visitors get.
LATEST_TAG="${LATEST_TAG-latest}"
# A docker-container builder exports nowhere by default; load unless pushing.
case " $* " in *" --push "*) ;; *) set -- --load "$@" ;; esac

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

# amd64 explicitly: a silent arm64 build on a laptop would only fail at deploy.
exec docker buildx build \
    --platform linux/amd64 \
    --build-arg "ROS_IMAGE=$ROS_IMAGE" \
    --build-arg "ASSETS_IMAGE=$ASSETS_IMAGE" \
    --build-arg "VIEWER_IMAGE=$VIEWER_IMAGE" \
    --cache-from "type=registry,ref=$IMAGE:buildcache" \
    --tag "$IMAGE:$TAG" \
    ${LATEST_TAG:+--tag "$IMAGE:$LATEST_TAG"} \
    -f sim/demo/Dockerfile \
    "$@" \
    .
