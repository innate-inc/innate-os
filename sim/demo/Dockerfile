# syntax=docker/dockerfile:1.7
# The public sim demo: one container that is a whole session, world server
# included (VIRTUAL_MARS_REMOTE). Build with sim/demo/build.sh; see README.md.

ARG ROS_IMAGE=ghcr.io/innate-inc/innate-os-sim-ros:main
ARG ASSETS_IMAGE=ghcr.io/innate-inc/innate-os-sim-assets:main
ARG VIEWER_IMAGE=ghcr.io/innate-inc/innate-os-sim-viewer:main

FROM ${ASSETS_IMAGE} AS assets
FROM ${VIEWER_IMAGE} AS viewer

FROM ${ROS_IMAGE} AS demo

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The world renders in here, not on a host. `import mujoco` opens the GL
# context at import time, so a missing libOSMesa fails the build, not a session.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libosmesa6 \
        libegl1 \
        libgl1 \
        libopengl0

# The pin keeps mujoco on the base image's numpy<2.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system 'mujoco>=3.2,<4'

WORKDIR /root/innate-os

# Least- to most-churning, so a webapp edit does not rebuild the workspace.
COPY ros2_ws /root/innate-os/ros2_ws
COPY scripts /root/innate-os/scripts
COPY config /root/innate-os/config
COPY workspace /root/innate-os/workspace
COPY webapp /root/innate-os/webapp
COPY sim/props /root/innate-os/sim/props
COPY sim/challenges /root/innate-os/sim/challenges
COPY sim/demo /root/innate-os/sim/demo

# Generated geometry and the viewer bundle, from the images the launcher installs.
COPY --from=assets /work /root/innate-os/sim/assets
COPY --from=assets /viewer /root/innate-os/sim/viewer/public
COPY --from=viewer /bundle /root/innate-os/sim/viewer/dist-lib

# Runtime state the dev sim bind-mounts; gitignored, so absent from the context.
RUN mkdir -p /root/innate-os/data/maps

# Replay episodes are fetched by post_update.sh on hardware and by the dev
# launcher; the demo runs neither, and a skill without its episode is hidden.
RUN python3 - <<'PY'
import json, urllib.request
from pathlib import Path

workspace = Path("/root/innate-os/workspace")
for meta_file in sorted(workspace.glob("*_skills/*/metadata.json")):
    downloads = json.loads(meta_file.read_text()).get("downloads") or {}
    for name, url in downloads.items():
        dest = meta_file.parent / name
        if dest.exists():
            continue
        print(f"fetching {dest.relative_to(workspace)}", flush=True)
        with urllib.request.urlopen(url, timeout=300) as response:
            dest.write_bytes(response.read())
PY

# Reuse the base's prebuilt install only if it was built from this exact source:
# the base tracks main, and main's binaries against a branch's source fail in
# ways that look like anything but a stale build.
RUN --mount=type=cache,target=/root/.cache/ccache,sharing=locked \
    set -e; \
    prebuilt=/opt/innate-os-prebuilt/ros2_ws; \
    cd /root/innate-os/ros2_ws; \
    here=$(find src -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
           | sort -z | xargs -0 -r sha256sum | sha256sum | awk '{print $1}'); \
    there=$(cat "$prebuilt/source.sha256" 2>/dev/null || echo none); \
    if [ "$here" = "$there" ]; then \
        echo "prebuilt workspace matches this source ($here) -- reusing"; \
        cp -a "$prebuilt/install" /root/innate-os/ros2_ws/install; \
    else \
        echo "prebuilt workspace is for different source ($there != $here) -- rebuilding"; \
        source /opt/ros/humble/setup.bash; \
        export CCACHE_DIR=/root/.cache/ccache; \
        colcon build --cmake-args \
            -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache; \
    fi

ENV INNATE_OS_ROOT=/root/innate-os \
    VIRTUAL_MARS_ASSETS=/root/innate-os/sim/assets \
    VIRTUAL_MARS_REMOTE=127.0.0.1:8799 \
    INNATE_WORLD_STATE_PORT=8800 \
    INNATE_ZENOH_DISABLE_SHM=1 \
    SKIP_UPDATE_CHECK=1 \
    MUJOCO_GL=osmesa \
    INNATE_SIM_RENDER_SCALE=2 \
    INNATE_WEBAPP_READONLY=1 \
    INNATE_DEMO_LEASE_SECONDS=600

# Last: the cache key hashes asset mtimes, so any COPY after this silently
# puts a multi-minute compile back on the session-start path.
RUN source /opt/ros/humble/setup.bash && \
    source /root/innate-os/ros2_ws/install/setup.bash && \
    python3 /root/innate-os/sim/demo/prewarm_model.py

EXPOSE 80
ENTRYPOINT ["/root/innate-os/sim/demo/entrypoint.sh"]
