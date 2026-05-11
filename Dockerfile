# syntax=docker/dockerfile:1.7
#
# Dockerfile for Maurice Robot (ROS 2 Humble) with zsh + oh-my-zsh.
#
# Keep this image focused on OS/runtime dependencies. The ROS workspace source
# is bind-mounted by docker-compose.dev.yml and built into persistent named
# volumes at runtime, so source edits do not require rebuilding the image.

ARG ROS_BASE_IMAGE=ros:humble-ros-base
FROM ${ROS_BASE_IMAGE} AS os-deps

# Build argument to choose between simulation and hardware dependency sets.
# Usage: docker build --build-arg MODE=simulation .
#        docker build --build-arg MODE=hardware .
ARG MODE=simulation

ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 1. Install base packages needed before apt-dependencies.txt.
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

# 2. Add Innate packages repository (for ros-humble-innate-rws, etc.).
RUN curl -fsSL https://innate-inc.github.io/innate-packages/pubkey.gpg | \
        gpg --dearmor -o /usr/share/keyrings/innate-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/innate-archive-keyring.gpg] https://innate-inc.github.io/innate-packages/ $(lsb_release -cs) main" | \
        tee /etc/apt/sources.list.d/innate.list > /dev/null

# 3. Copy and install common apt dependencies.
COPY ros2_ws/apt-dependencies.common.txt /tmp/apt-dependencies.common.txt
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    grep -v '^#' /tmp/apt-dependencies.common.txt | grep -v '^$' | xargs apt-get install -y --no-install-recommends && \
    rm /tmp/apt-dependencies.common.txt

# 4. Install hardware-specific dependencies only in hardware mode.
COPY ros2_ws/apt-dependencies.hardware.txt /tmp/apt-dependencies.hardware.txt
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ "$MODE" = "hardware" ]; then \
        apt-get update && \
        grep -v '^#' /tmp/apt-dependencies.hardware.txt | grep -v '^$' | xargs apt-get install -y --no-install-recommends; \
    fi && \
    rm /tmp/apt-dependencies.hardware.txt

# 5. Install uv for fast, cached Python package installation.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -sf /root/.local/bin/uv /usr/local/bin/uv

# 6. Install Python runtime packages with uv.
# Simulation images use CPU PyTorch wheels to avoid pulling multi-GB CUDA/NVIDIA
# packages into local Apple Silicon Docker builds. Hardware images keep using
# the normal torch packages so Jetson-specific indexes/tooling can provide CUDA.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system \
        websockets \
        pydantic \
        'numpy<2' \
        'opencv-python<4.10' \
        h5py \
        cartesia \
        einops \
        python-dotenv \
        websocket-client && \
    if [ "$MODE" = "simulation" ]; then \
        uv pip install --system \
            --index-url https://download.pytorch.org/whl/cpu \
            --extra-index-url https://pypi.org/simple \
            --index-strategy unsafe-best-match \
            'torch==2.11.0+cpu' \
            'torchvision==0.26.0+cpu'; \
    else \
        uv pip install --system \
            --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126 \
            --index-strategy unsafe-best-match \
            torch \
            torchvision; \
    fi

# 7. Install oh-my-zsh (for root, since containers typically run as root unless changed).
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 8. Make zsh the default shell for root inside the container and use a compact theme.
RUN chsh -s /usr/bin/zsh root && \
    sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 9. Pre-approve GitHub SSH host key to avoid prompts.
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null

FROM os-deps AS dev-runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# docker-compose.dev.yml bind-mounts the real source tree here. Creating the
# mount points keeps the image usable even before the bind mounts are attached.
RUN mkdir -p \
        /root/innate-os/ros2_ws \
        /root/innate-os/dds \
        /root/innate-os/scripts \
        /root/innate-os/config \
        /root/innate-os/agents \
        /root/innate-os/skills

# Patch root .zshrc to source ROS, the workspace, and DDS only when present.
RUN cat >> /root/.zshrc <<'EOF'

# ----- Innate OS custom environment -----
[ -f /opt/ros/humble/setup.zsh ] && source /opt/ros/humble/setup.zsh
[ -f /root/innate-os/ros2_ws/install/setup.zsh ] && source /root/innate-os/ros2_ws/install/setup.zsh
[ -f /root/innate-os/dds/setup_dds.zsh ] && source /root/innate-os/dds/setup_dds.zsh
export INNATE_OS_ROOT=/root/innate-os
export CCACHE_DIR=/root/.cache/ccache
EOF

WORKDIR /root/innate-os
CMD ["zsh", "-l"]
