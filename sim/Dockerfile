# syntax=docker/dockerfile:1.7
#
# Dockerfile for Mars Robot (ROS 2 Humble) with zsh + oh-my-zsh.
#
# Keep this image focused on OS/runtime dependencies. The ROS workspace source
# is bind-mounted by sim/docker-compose.dev.yml and built into persistent named
# volumes at runtime, so source edits do not require rebuilding the image.

ARG ROS_BASE_IMAGE=ros:humble-ros-base
FROM ${ROS_BASE_IMAGE} AS os-deps

ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 1. Install base packages needed before the apt dependency lists.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
        gnupg \
        lsb-release \
        ca-certificates \
        htop \
        btop

# 2. Add the third-party apt repositories (Innate packages) via the same
# script the on-device updater uses, keeping the two in sync.
COPY scripts/update/setup_repos.sh /tmp/setup_repos.sh
RUN bash /tmp/setup_repos.sh && rm /tmp/setup_repos.sh

# 3. Copy and install simulation apt dependencies (shared common + sim overlay).
COPY ros2_ws/apt-dependencies.common.txt ros2_ws/apt-dependencies.sim.txt /tmp/
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    cat /tmp/apt-dependencies.common.txt /tmp/apt-dependencies.sim.txt | \
        grep -v '^#' | grep -v '^$' | \
        xargs apt-get install -y --no-install-recommends && \
    rm /tmp/apt-dependencies.common.txt /tmp/apt-dependencies.sim.txt

# 4. Install uv for fast, cached Python package installation.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -sf /root/.local/bin/uv /usr/local/bin/uv

# 5. Install Python runtime packages with uv. CPU PyTorch wheels avoid pulling
# multi-GB CUDA/NVIDIA packages into local Apple Silicon Docker builds. The
# trailing packaging/setuptools reinstall pins them after torch resolves its deps.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system \
        websockets \
        pydantic \
        click \
        'packaging>=24' \
        'setuptools==59.6.0' \
        'numpy<2' \
        h5py \
        opencv-python-headless \
        pyyaml \
        cartesia \
        einops \
        python-dotenv \
        urdf-parser-py \
        websocket-client \
        pillow && \
    uv pip install --system \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        --index-strategy unsafe-best-match \
        'torch==2.11.0+cpu' \
        'torchvision==0.26.0+cpu' && \
    uv pip install --system \
        'packaging>=24' \
        'setuptools==59.6.0'

# 6. Install oh-my-zsh (for root, since containers typically run as root unless changed).
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 7. Make zsh the default shell for root inside the container and use a compact theme.
RUN chsh -s /usr/bin/zsh root && \
    sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 8. Pre-approve GitHub SSH host key to avoid prompts.
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null

FROM os-deps AS dev-runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# sim/docker-compose.dev.yml bind-mounts the real source tree here. Creating the
# mount points keeps the image usable even before the bind mounts are attached.
RUN mkdir -p \
        /root/innate-os/ros2_ws \
        /root/innate-os/scripts \
        /root/innate-os/config \
        /root/innate-os/workspace

# Patch root .zshrc to source ROS, the workspace, and DDS only when present.
RUN cat >> /root/.zshrc <<'EOF'

# ----- Innate OS custom environment -----
[ -f /opt/ros/humble/setup.zsh ] && source /opt/ros/humble/setup.zsh
[ -f /root/innate-os/ros2_ws/install/setup.zsh ] && source /root/innate-os/ros2_ws/install/setup.zsh
[ -f /root/innate-os/config/dds/setup_dds.zsh ] && source /root/innate-os/config/dds/setup_dds.zsh
export INNATE_OS_ROOT=/root/innate-os
export CCACHE_DIR=/root/.cache/ccache
export PATH="/root/innate-os/scripts:$PATH"  # put the `innate` developer CLI on PATH
EOF

WORKDIR /root/innate-os
CMD ["zsh", "-l"]
