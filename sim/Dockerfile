# syntax=docker/dockerfile:1.7
#
# Dockerfile for Mars Robot (ROS 2 Humble) with zsh + oh-my-zsh.
#
# Keep this image focused on OS/runtime dependencies. The ROS workspace source
# is bind-mounted by sim/docker-compose.dev.yml and built into persistent named
# volumes at runtime, so source edits do not require rebuilding the image.

# The index digest, not the tag: upstream rebuilds this weekly, and a moved base
# makes every layer above it different -- two consecutive publishes of
# innate-os-sim-ros shared one layer out of 26. ci/build_sim_images.sh pins the
# ROS image's own base the same way. Bump deliberately to take base updates.
ARG ROS_BASE_IMAGE=ros:humble-ros-base@sha256:75dd3aba34a3838dadbb31a9f7bef769bdfa8713e6cec686fc868db2981b0987
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
        aiohttp \
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
        watchdog \
        onnxruntime \
        'inspireface==1.2.3.post5' \
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

# InspireFace downloads its recognition pack on first session creation. Do
# that while building so simulator identity never depends on runtime internet,
# and fail the image build if the native face backend cannot initialize on the
# target architecture.
RUN python3 - <<'PY'
from pathlib import Path

import inspireface as isf

parameter = isf.SessionCustomParameter(enable_recognition=True)
session = isf.InspireFaceSession(
    param=parameter,
    detect_mode=isf.HF_DETECT_MODE_ALWAYS_DETECT,
    max_detect_num=3,
)
session.set_detection_confidence_threshold(0.4)

# inspireface 1.2.3.post5 downloads through ModelScope into its snapshot
# hierarchy, but its fast cache check looks directly under ``ms/``. Expose the
# downloaded pack at that expected path so later, offline processes reuse it.
cache_root = Path.home() / ".inspireface" / "ms"
cached_model = cache_root / "Pikachu"
if not cached_model.exists():
    downloaded_models = list((cache_root / "models").glob("**/Pikachu"))
    if len(downloaded_models) != 1:
        raise RuntimeError(
            f"Expected one downloaded InspireFace model, found {downloaded_models}"
        )
    cached_model.symlink_to(downloaded_models[0])
print("InspireFace recognition backend and model pack initialized")
PY

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

# Tab completions for the `innate` CLI — the container's answer to what
# scripts/update/post_update.sh does on a physical robot, but deliberately NOT
# to the same path: post_update writes scripts/build/completions, and here
# scripts/ is a bind mount carrying the host user's ownership, which compinit's
# security audit rejects for an fpath entry root doesn't own (and generating
# there would drop root-owned files into the developer's checkout). So the file
# lives under /root instead. The bind mount also means scripts/innate does not
# exist at image build time, so this runs on login, not in a RUN layer. It lands
# after oh-my-zsh's compinit, hence the second compinit to pick up the new fpath.
fpath=("/root/.zsh/completions" $fpath)
# Generation costs ~60ms, so just regenerate every shell instead of tracking
# whether scripts/innate has outrun the cached file. Written through a temp
# file and renamed into place because the sim's tmux session opens nine windows
# at once and every one of them sources this: a plain `>` would let them
# truncate the file out from under each other's compinit. Capped at 3s (50x the
# nominal cost) and stderr to a log -- this is in the login path of every shell
# now, so a wedged generator must not be able to wedge the shell with it.
if [ -f /root/innate-os/scripts/innate ]; then
    mkdir -p /root/.zsh/completions
    innate_comp_tmp=$(mktemp /root/.zsh/completions/innate-completions.XXXXXX)
    if timeout 3 /root/innate-os/scripts/innate completions \
           > "$innate_comp_tmp" 2>/root/.zsh/innate-completions.log \
       && [ -s "$innate_comp_tmp" ]; then
        mv "$innate_comp_tmp" /root/.zsh/completions/_innate
    else
        rm -f "$innate_comp_tmp"
    fi
    unset innate_comp_tmp
fi
autoload -Uz compinit && compinit
EOF

WORKDIR /root/innate-os
CMD ["zsh", "-l"]
