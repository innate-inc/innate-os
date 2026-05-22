#!/usr/bin/env zsh
set -eo pipefail

ROS_WS="${INNATE_OS_ROS_WS:-$HOME/innate-os/ros2_ws}"
PREBUILT_ROOT="${INNATE_OS_PREBUILT_ROS_ROOT:-/opt/innate-os-prebuilt/ros2_ws}"

source /opt/ros/humble/setup.zsh
cd "$ROS_WS"

colcon_build() {
    colcon build --symlink-install \
        --cmake-args \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
}

current_source_hash() {
    find src -type f \
        ! -path '*/__pycache__/*' \
        ! -name '*.pyc' \
        -print0 | sort -z | xargs -0 -r sha256sum | sha256sum | awk '{print $1}'
}

seed_prebuilt_install() {
    [[ -f "$PREBUILT_ROOT/install/setup.zsh" ]] || return 1
    [[ -f "$PREBUILT_ROOT/source.sha256" ]] || return 1
    [[ -f "$PREBUILT_ROOT/install.sha256" ]] || return 1

    if find "$PREBUILT_ROOT/install" -xtype l -print -quit | grep -q .; then
        echo "Prebuilt ROS workspace install has dangling symlinks; running colcon build."
        return 1
    fi

    prebuilt_hash="$(cat "$PREBUILT_ROOT/source.sha256")"
    prebuilt_install_hash="$(cat "$PREBUILT_ROOT/install.sha256")"
    current_hash="$(current_source_hash)"
    if [[ "$current_hash" != "$prebuilt_hash" ]]; then
        echo "Local ROS source differs from the prebuilt image; running colcon build."
        return 1
    fi

    if [[ ! -f install/setup.zsh ]] ||
        [[ "$(cat install/.innate-prebuilt-source.sha256 2>/dev/null)" != "$prebuilt_hash" ]] ||
        [[ "$(cat install/.innate-prebuilt-install.sha256 2>/dev/null)" != "$prebuilt_install_hash" ]]; then
        echo "Using prebuilt ROS workspace install from image."
        mkdir -p install
        find install -mindepth 1 -maxdepth 1 -exec rm -rf {} +
        cp -a "$PREBUILT_ROOT/install/." install/
        echo "$prebuilt_hash" > install/.innate-prebuilt-source.sha256
        echo "$prebuilt_install_hash" > install/.innate-prebuilt-install.sha256
    else
        echo "Prebuilt ROS workspace install is current."
    fi

    echo "ROS workspace install is current; skipping rebuild."
    return 0
}

install_is_stale() {
    [[ ! -f install/setup.zsh ]] && return 0
    find install -xtype l -print -quit | grep -q . && return 0
    find src -type f -newer install/setup.zsh -print -quit | grep -q . && return 0
    return 1
}

if [[ "${INNATE_OS_ALWAYS_BUILD:-0}" == "1" ]]; then
    colcon_build
elif seed_prebuilt_install; then
    :
elif install_is_stale; then
    if [[ -d install ]] && find install -xtype l -print -quit | grep -q .; then
        echo "Removing unusable ROS install before rebuild."
        mkdir -p build install log
        find build install log -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    fi
    colcon_build
else
    echo "ROS workspace install is current; skipping rebuild."
fi
