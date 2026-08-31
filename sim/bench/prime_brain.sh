#!/usr/bin/env bash
# Put the brain in a state where it can actually act. Five gates, each of which
# fails closed and silently:
#   1. the brain boots INACTIVE                     -> set_brain_active
#   2. no skills are enabled for the directive      -> set_active_skills
#   3. chat_in needs a JSON envelope                -> {"text": ...}
#   4. the camera feed gates every turn             -> sim_driver must publish
#   5. starting a challenge does not instruct       -> live_runner speaks the brief
# Without 2, the model is offered exactly one tool -- `wait` -- and correctly
# sits still, which looks identical to a broken agent.
#
# THE ROSTER IS NOT "EVERY SKILL THAT EXISTS". A skill that cannot work here is
# not neutral to offer: the model spends a turn calling it, gets a failure in
# under a second, and answers wait({}). Measured on the cafe map, that is the
# whole of the observed "robot gives up" behaviour.
#
# Exactly which skill needs which credential, checked against the source rather
# than assumed:
#
#   pick_any_object       Needs a VISION backend to find the object and verify
#                         the grasp -- not specifically an Innate key, despite
#                         what its error message says. innate.gemini.make_client
#                         returns a ProxyClient when INNATE_SERVICE_KEY is set,
#                         else a _DirectClient when GEMINI_BASE_URL is set, and
#                         only None when NEITHER is; execute() fails on None.
#                         19 of the 38 challenges need a pick, 12 of the 14 in
#                         category 2, so with neither set live_runner reports
#                         those BLOCKED rather than scoring them 0.
#   navigate_with_vision  needs UniNavid, also behind INNATE_SERVICE_KEY, but
#                         Innate's advice is to disregard UniNavid for now. Held
#                         out even when the key is present; BENCH_ALLOW_UNINAVID=1
#                         puts it back when there is a version worth testing.
#   search_memory         does NOT need the Innate key. With no proxy the memory
#                         tier talks straight to Google with GEMINI_API_KEY, and
#                         the failures seen in the last run were dropped sockets
#                         (RemoteProtocolError, never an HTTP status), not a
#                         refused credential. It is held out because memory earns
#                         nothing here and its spend is unmetered -- so it follows
#                         BRAIN_DISABLE_MEMORY, not the key.
#
# Nothing here is a physical property of the robot; it is which of its
# capabilities are actually wired up. The roster is printed per run so the
# scores always say which one produced them.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1

RESET=0
[ "${1:-}" = "--reset" ] && RESET=1

# THE ROBOT'S CONFIG, NOT THIS SHELL'S. The brain runs in a container fed by
# the repo .env; this script runs on the host, where those variables are
# normally unset. Reading the shell answers a different question and gets it
# wrong silently: with BRAIN_DISABLE_MEMORY=1 in .env and unset here, the
# roster enabled search_memory anyway, and the first live episode of the run
# spent a turn on "Let me check my memory..." followed by a failure. Same
# mistake the capability gate made -- see sim/bench/capabilities.runtime_env.
env_value() {
  # Two statements on purpose: `local key="$1" from_shell="${!key:-}"` expands
  # ${!key} before `key` is assigned, and bash answers "invalid indirect
  # expansion" on stderr, returns empty, and carries on -- so the gate silently
  # read every value as unset and enabled everything.
  local key="$1"
  local from_shell="${!key:-}"
  if [ -n "$from_shell" ]; then echo "$from_shell"; return; fi
  grep -E "^${key}=" "$REPO/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r'
}

SERVICE_KEY="$(env_value INNATE_SERVICE_KEY)"
BASE_URL="$(env_value GEMINI_BASE_URL)"
NO_MEMORY="$(env_value BRAIN_DISABLE_MEMORY)"
ALLOW_UNINAVID="$(env_value BENCH_ALLOW_UNINAVID)"

# Each optional skill gated on the thing it actually needs.
EXTRA_SKILLS=""
if [ -n "$SERVICE_KEY" ] || [ -n "$BASE_URL" ]; then
  EXTRA_SKILLS="\"innate-os/pick_any_object\","
fi
case "$(echo "$NO_MEMORY" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) ;;                                    # memory off: skill withheld
  *) EXTRA_SKILLS="$EXTRA_SKILLS\"innate-os/search_memory\"," ;;
esac
if [ -n "$ALLOW_UNINAVID" ] && [ -n "$SERVICE_KEY" ]; then
  EXTRA_SKILLS="$EXTRA_SKILLS\"innate-os/navigate_with_vision\","
fi
echo "roster gate: pick=$([ -n "$SERVICE_KEY$BASE_URL" ] && echo yes || echo no)" \
     "memory=$([ -n "$NO_MEMORY" ] && echo off || echo on)"

cat > /tmp/prime.py <<PY
import json, subprocess, time

RESET = $RESET

def run(args, t=45):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception as e:
        return f"(failed: {e})"

if RESET:
    # Clear the conversation, then re-activate: reset_brain leaves it inactive.
    print("reset:", run(["ros2", "service", "call", "/brain/reset_brain",
                         "brain_messages/srv/ResetBrain", "{}"])[-100:])
    time.sleep(2)

print("activate:", run(["ros2", "service", "call", "/brain/set_brain_active",
                        "std_srvs/srv/SetBool", "{data: true}"])[-120:])
time.sleep(2)

SKILLS = [
    $EXTRA_SKILLS
    "innate-os/navigate_to_position",
    "innate-os/move_straight", "innate-os/turn_in_place",
    "innate-os/open_gripper", "innate-os/close_gripper",
    "innate-os/arm_rest_position", "innate-os/head_emotion",
]
payload = json.dumps({"agent_id": "empty_directive", "skills": SKILLS})
run(["ros2", "topic", "pub", "--once", "/brain/set_active_skills",
     "std_msgs/String", json.dumps({"data": payload})])
print("skills enabled:", len(SKILLS), "->", ", ".join(s.split("/")[-1] for s in SKILLS))
PY
# Upstream gives each checkout its own stack, so the container carries a
# per-checkout suffix. Discover it rather than hardcoding `innate-dev`.
OS_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E '^innate-dev' | head -1)
if [ -z "$OS_CONTAINER" ]; then
  echo "no innate-dev* container is running" >&2
  exit 1
fi
docker cp /tmp/prime.py "$OS_CONTAINER":/tmp/prime.py >/dev/null
# RMW_IMPLEMENTATION is essential: exec shells do not inherit it, and every
# ros2 service call then times out against the Zenoh graph -- which reads as
# 'brain refused to activate' and leaves the model with only the wait tool.
docker exec -e RMW_IMPLEMENTATION=rmw_zenoh_cpp "$OS_CONTAINER" bash -lc 'source /opt/ros/humble/setup.bash; source /root/innate-os/ros2_ws/install/setup.bash; python3 /tmp/prime.py' 2>&1 | tail -4

echo
echo "--- brain log ---"
docker exec "$OS_CONTAINER" bash -lc 'tail -6 /tmp/brain.log' 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | sed -E 's/.*\]: //'
