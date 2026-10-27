#!/bin/sh
# colcon build with both parallelism caps, sampling memory and load throughout.
# Run from ros2_ws with ROS sourced; extra args pass through to colcon.
#
# colcon injects make -j$(nproc) per package unless MAKEFLAGS carries a -j.
set -u

MAKE_JOBS=3
PACKAGE_WORKERS=$(( $(nproc) < 3 ? $(nproc) : 3 ))
SAMPLE_INTERVAL=5

sample_resources() {
    while :; do
        awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} /^MemFree:/{f=$2}
             END {if (a == "") a = f; printf "SAMPLE used_MiB=%d total_MiB=%d", (t-a)/1024, t/1024}' \
            /proc/meminfo
        printf ' load1=%s runnable=%s' \
            "$(cut -d' ' -f1 /proc/loadavg)" "$(cut -d' ' -f4 /proc/loadavg)"
        printf ' compilers=%s\n' \
            "$(grep -lxE 'cc1plus|cc1|lto1|ld' /proc/[0-9]*/comm 2>/dev/null | wc -l)"
        sleep "$SAMPLE_INTERVAL"
    done
}

sample_resources &
sampler=$!

MAKEFLAGS="-j${MAKE_JOBS} -l$(nproc)" \
    colcon build --parallel-workers "$PACKAGE_WORKERS" "$@" --cmake-args \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
rc=$?

kill "$sampler" 2>/dev/null || true
echo  # the kill lands mid-printf, so close the truncated sample line
exit "$rc"
