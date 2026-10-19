#!/usr/bin/env bash
# Speak one instruction to the robot. Takes the text on stdin.
#
# Shelled out to ros2 rather than published over rosbridge: the rosbridge
# advertise+publish returned no error and the brain never logged a User message
# (grep "User message" == 0), while this path has worked every time. A silent
# publish is the worst possible failure here -- the agent just sits there and
# the whole sweep scores zero for a reason nothing reports.
# A per-invocation path: two live runs on one host would otherwise stage
# over each other's payload, in the container as well as here.
PAYLOAD="/tmp/say_payload.$$.py"
trap 'rm -f "$PAYLOAD"' EXIT
TEXT="$(cat)"
python3 - "$TEXT" > "$PAYLOAD" <<'PY'
import json, sys
msg = json.dumps({"text": sys.argv[1]})
print("import subprocess, json")
# check=True: a publish that fails must fail the script. Without it Docker
# succeeds, the shell sees 0, and the brain simply never hears the brief.
print(f"subprocess.run(['ros2','topic','pub','--once','/brain/chat_in','std_msgs/String',"
      f"{json.dumps(json.dumps({'data': msg}))}], timeout=40, check=True)")
PY
# os_container.py picks THIS checkout's container by the launcher's own
# naming rule; `head -1` over `innate-dev*` could pick another checkout's
# stack when two are running, and succeed against the wrong robot.
OS_CONTAINER=$(python3 "$(dirname "${BASH_SOURCE[0]}")/os_container.py") || exit 1
if ! docker cp "$PAYLOAD" "$OS_CONTAINER":"$PAYLOAD" >/dev/null 2>&1; then
  echo "could not copy the brief payload into $OS_CONTAINER" >&2
  exit 1
fi
# RMW_IMPLEMENTATION: exec shells do not inherit it, so without this the
# publish goes to a DDS graph nobody is on -- it succeeds, and the brain never
# hears the brief. Same silent no-op the rosbridge path had.
docker exec -e RMW_IMPLEMENTATION=rmw_zenoh_cpp "$OS_CONTAINER" bash -lc 'source /opt/ros/humble/setup.bash; source /root/innate-os/ros2_ws/install/setup.bash; python3 "$1"' _ "$PAYLOAD" >/dev/null
SAY_RC=$?
docker exec "$OS_CONTAINER" rm -f "$PAYLOAD" >/dev/null 2>&1 || true
exit "$SAY_RC"
