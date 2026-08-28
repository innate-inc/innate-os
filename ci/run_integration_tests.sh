#!/usr/bin/env bash
# Build innate-os and run its test suite inside the test image.
#
# Single source of truth for "the tests", invoked by both:
#   - ci/docker-compose.test.yml         (local / self-hosted)
#   - ci/cloudbuild.integration-test.yaml (Cloud Build)
#
# The image entrypoint has already sourced ROS 2 and set the Zenoh/RMW env.
# We disable Zenoh shared-memory transport below (see the export block): the SHM
# provider rmw_zenoh allocates at startup cannot be created in sandboxed CI such
# as Cloud Build's gVisor, which blocks POSIX shm regardless of /dev/shm size.
# These are correctness tests (orchestration logic, not transport), so loopback
# is equivalent; the robot keeps SHM in production via the image entrypoint.
set -e

ROUTER_PID=""
cleanup() {
  if [ -n "${ROUTER_PID}" ]; then
    kill "${ROUTER_PID}" >/dev/null 2>&1 || true
    wait "${ROUTER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run_ros_integration_tests() {
  # Launch-based tests: these actually start nodes (e.g. brain_client_node.py),
  # so a node whose installed executable is missing or non-runnable fails here.
  # --no-tests=error: a regex matching nothing must fail, not silently pass —
  # that's how CI kept green while the suites it named had been deleted.
  ros2 run rmw_zenoh_cpp rmw_zenohd &
  ROUTER_PID=$!
  sleep 2
  colcon test --packages-select brain_client \
    --return-code-on-test-failure \
    --ctest-args -R "test_node_boot" --no-tests=error \
    --event-handlers console_direct+
  colcon test-result --verbose
  kill "${ROUTER_PID}" >/dev/null 2>&1 || true
  wait "${ROUTER_PID}" >/dev/null 2>&1 || true
  ROUTER_PID=""
}

echo "=== /dev/shm ==="
df -h /dev/shm || true

# Disable Zenoh shared memory for the test run. The image entrypoint enables it
# (ZENOH_*_CONFIG_OVERRIDE), so override all three to false here. Without this,
# rmw_zenoh tries to create a POSIX SHM provider at rclpy.init, which fails under
# Cloud Build's gVisor sandbox ("Failed to create POSIX SHM provider").
export ZENOH_SESSION_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"
export ZENOH_ROUTER_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"
export ZENOH_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"

# The test image already built the workspace at image-build time
# (ci/Dockerfile.test does an incremental colcon build), so just source it.
cd /root/innate-os/ros2_ws
source install/setup.bash

# The launch tests below only start brain_client nodes, so they cannot catch a
# lost exec bit in the mars_* / manipulation packages. Guard every package here:
# a --symlink-install build symlinks the installed node straight at its source,
# so a script tracked 0644 fails to launch ("executable not found on the libexec
# directory"); a copy install masks it with 0755 copies. Read each CMakeLists'
# install(PROGRAMS <files...> DESTINATION ...) list and require the source +x.
echo "=== exec-bit guard: install(PROGRAMS) node scripts ==="
missing="$(
  find src -name CMakeLists.txt | while read -r cmake; do
    awk -v dir="$(dirname "$cmake")" '
      { gsub(/[()]/, " & ") }                       # tokenize parens
      { for (i = 1; i <= NF; i++) {
          if ($i == "PROGRAMS") { prog = 1; continue }
          if ($i == ")")        { prog = 0; continue }
          if (prog && $i ~ /^(DESTINATION|RENAME|PERMISSIONS|CONFIGURATIONS|COMPONENT|OPTIONAL|EXCLUDE_FROM_ALL|TYPE)$/) { prog = 0; continue }
          if (prog) print dir "/" $i
      } }
    ' "$cmake"
  done | while read -r f; do [ -x "$f" ] || echo "$f"; done
)"
if [ -n "${missing}" ]; then
  echo "install(PROGRAMS) node scripts missing the exec bit (chmod +x them):" >&2
  echo "${missing}" >&2
  exit 1
fi

echo "=== unit tests (fast, no ROS) ==="
# The whole brain_client suite by directory, so new test files are picked up
# without editing this script. test_node_boot.launch.py is a launch_testing
# harness (run via colcon test below), not a pytest suite — skip it here.
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  src/brain/brain_client/test \
  --ignore=src/brain/brain_client/test/test_node_boot.launch.py \
  src/brain/manipulation/test/test_config_validation.py

echo "=== unit tests: webapp front door (aiohttp, no ROS) ==="
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q ../webapp/proxy/test

# Run the launch tests under both install modes. The copy build baked into the
# image (ci/Dockerfile.test) masks missing exec bits by installing 0755 copies;
# the --symlink-install build (what the local sim launcher uses) symlinks the
# install executable straight at the source, so a brain_client node that lost
# its exec bit fails to launch here. Running both catches bugs specific to
# either mode. (Exec bits outside brain_client are covered by the static guard
# above, since these tests only start brain_client nodes.)
echo "=== integration tests: regular (copy) install ==="
run_ros_integration_tests

echo "=== integration tests: --symlink-install ==="
# colcon --symlink-install installs Python entry points via `setup.py develop
# --editable`, which setuptools >= 80 removed (this image ships 82; the local sim
# image is older, which is why symlink builds work there but not here). Pin an
# older setuptools for this build pass only — it's build-time tooling, so already
# installed packages and Pass 1 are unaffected.
#
# This downgrades setuptools globally in the container, but it's the last thing
# the script does: this pass is the final step and nothing runs in this layer
# afterward, so the global mutation has no consumer to leak into.
#
# TRACKING: this is the same "colcon needs `setup.py develop`" constraint that is
# pinned independently in three other places, none cross-referenced:
#   sim/Dockerfile              setuptools==59.6.0 (re-pinned after torch resolves)
#   sim/requirements.ubuntu.txt setuptools==81.0.0
#   sim/requirements.macos.txt  setuptools==80.9.0
# The real owner of the *working* constraint is torch, not colcon: sim/Dockerfile
# pins 59.6.0 to re-pin packaging/setuptools after torch resolves its deps, and
# that just happens to be < 80. A torch bump that needs setuptools >= 80 would
# raise that pin and silently break `colcon build --symlink-install`
# (scripts/validate_sim_ros_install.zsh) — and this pass would not catch it, since
# the two derive the constraint independently. The durable fix is upstream:
# colcon-ros adopting PEP 660 editable installs (`pip install -e`) instead of the
# removed `setup.py develop`, after which none of these pins stay load-bearing.
python3 -m pip install --quiet 'setuptools<80'
rm -rf build install log
build_workspace --symlink-install
source install/setup.bash
run_ros_integration_tests
