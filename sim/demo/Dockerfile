# syntax=docker/dockerfile:1.7
#
# The public sim demo image: one self-contained container that IS a full sim
# session. No bind mounts, no host process, no launcher -- start it, route :80,
# throw it away. That is what makes it schedulable on a per-session platform
# (Fly Machines, Cloudflare Containers, a slot on your own box).
#
# It differs from the dev sim (sim/docker-compose.dev.yml) in three ways:
#   1. the MuJoCo world server runs IN here (sim_driver.launch.py's documented
#      VIRTUAL_MARS_REMOTE escape hatch) -- the host-side rule buys native GL,
#      which a headless cloud instance does not have to begin with;
#   2. every source tree and asset is baked in, not mounted;
#   3. nothing that writes is exposed: no .env, no ssh/git credentials, no
#      settings POST, no /restart.
#
# Build with sim/demo/build.sh -- it resolves the asset/viewer image tags the
# same way the launcher does, so this never drifts from what `./innate-sim up`
# would install.

ARG ROS_IMAGE=ghcr.io/innate-inc/innate-os-sim-ros:main
ARG ASSETS_IMAGE=ghcr.io/innate-inc/innate-os-sim-assets:main
ARG VIEWER_IMAGE=ghcr.io/innate-inc/innate-os-sim-viewer:main

FROM ${ASSETS_IMAGE} AS assets
FROM ${VIEWER_IMAGE} AS viewer

FROM ${ROS_IMAGE} AS demo

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The rendering libraries sim/README.md tells a Linux host to install. They are
# absent from the dev image because the dev sim renders on the host; hosting the
# world in here moves that prerequisite in here too. `import mujoco` builds the
# MUJOCO_GL context at import time, so a missing libOSMesa fails the build, not
# the first render. libegl1/libgl1/libopengl0 are for the MUJOCO_GL=egl path on
# a GPU instance.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libosmesa6 \
        libegl1 \
        libgl1 \
        libopengl0

# The dev image deliberately ships no MuJoCo -- its driver node is a pure RPC
# client and the world runs on the host. The demo hosts the world itself, so it
# needs the physics. numpy/pillow/websockets are already in the base; the pin
# keeps mujoco on the base image's numpy<2.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system 'mujoco>=3.2,<4'

WORKDIR /root/innate-os

# Source. Ordered least- to most-churning so an edit to workspace/ or webapp/
# does not reinstall the ROS workspace above it.
COPY ros2_ws /root/innate-os/ros2_ws
COPY scripts /root/innate-os/scripts
COPY config /root/innate-os/config
COPY workspace /root/innate-os/workspace
COPY webapp /root/innate-os/webapp
COPY sim/props /root/innate-os/sim/props
COPY sim/demo /root/innate-os/sim/demo

# Generated geometry + the viewer bundle, straight off the images the launcher
# installs from on a dev machine (assets layer `work` -> sim/assets, layer
# `viewer` -> the viewer's public assets).
COPY --from=assets /work /root/innate-os/sim/assets
COPY --from=assets /viewer /root/innate-os/sim/viewer/public
COPY --from=viewer /bundle /root/innate-os/sim/viewer/dist-lib

# data/ is runtime state (maps, robot_info.json), gitignored and absent from the
# build context; the dev sim gets it as a bind mount.
RUN mkdir -p /root/innate-os/data/maps

# Reuse the install tree sim/Dockerfile.ros-prebuilt baked into the base -- but
# only when it was built from THIS source. The base tracks main; a demo built
# off a branch would otherwise ship main's binaries against the branch's source
# and fail in ways that look like anything but a stale build. Same source digest
# the prebuilt stage writes, so the two are comparable by construction.
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

# LAST: the cache key hashes every asset file's mtime+size, so any COPY after
# this silently invalidates the .mjb and puts a multi-minute compile back on the
# session-start path.
RUN source /opt/ros/humble/setup.bash && \
    source /root/innate-os/ros2_ws/install/setup.bash && \
    python3 /root/innate-os/sim/demo/prewarm_model.py

EXPOSE 80
ENTRYPOINT ["/root/innate-os/sim/demo/entrypoint.sh"]
