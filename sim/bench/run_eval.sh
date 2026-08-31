#!/usr/bin/env bash
# The full benchmark: 45 challenges, 8 maps, innate's own brain.
#
# ONE STACK RESTART PER MAP, not per challenge. The live stack serves one world
# at a time and two things have to change together: the physics world
# (VIRTUAL_MARS_ASSETS) and the nav map (AMCL/costmaps, seeded at container
# start). Changing only the first leaves the brain localised against a room
# that is not loaded.
#
# WALL-CLOCK CAPS BY CATEGORY, tighter than the challenges' own time limits.
# Summed, those limits allow 7.2 hours of episodes and most of that would be
# spent watching known failures burn their full budget. These caps bound the
# run to about four hours and roughly $5-12 at the measured $0.00274/call,
# while still leaving every category more time than any passing episode has
# ever needed. A challenge cut off by the cap is scored as a failure, and that
# is a harness decision, so the cap is recorded next to the score.
set -uo pipefail
# Resolve the repo from this script's own location, so the one command
# works from any clone. It used to be `cd "$HOME/innate-os"`, which meant
# the documented invocation only worked for a checkout at exactly that
# path, with these scripts copied into $HOME.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1

CAP_1=180
CAP_2=300
CAP_3=480

OUT="sim/bench/results/eval"
mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$OUT/run-$STAMP.log"
BUNDLES="${*:-counter pantry workshop gallery rounds household bridge blaze}"

echo "=== full benchmark, maps: $BUNDLES" | tee "$LOG"
echo "=== caps: cat1 ${CAP_1}s, cat2 ${CAP_2}s, cat3 ${CAP_3}s" | tee -a "$LOG"
rm -f workspace/gemini_usage.jsonl

for BUNDLE in $BUNDLES; do
  ASSETS="$REPO/sim/bundles/$BUNDLE"
  [ -d "$ASSETS" ] || { echo "no bundle at $ASSETS" | tee -a "$LOG"; continue; }

  echo "" | tee -a "$LOG"
  echo "########## $BUNDLE" | tee -a "$LOG"

  # Whichever backend is configured must be verified BEFORE the map is built,
  # because both failure modes look identical from the outside: a robot that
  # sits still. Which backend is in play is decided by .env, not by this
  # script's assumptions -- pick_transport and make_client both prefer the
  # Innate proxy when a service key is present, and fall back to
  # GEMINI_BASE_URL only when it is not.
  SERVICE_KEY="$(grep -E '^INNATE_SERVICE_KEY=' .env | cut -d= -f2- | tr -d '\r')"
  BASE_URL="$(grep -E '^GEMINI_BASE_URL=' .env | cut -d= -f2- | tr -d '\r')"
  if [ -n "$SERVICE_KEY" ]; then
    echo "--- backend: Innate proxy" | tee -a "$LOG"
  elif [ -n "$BASE_URL" ]; then
    echo "--- backend: GEMINI_BASE_URL shim" | tee -a "$LOG"
    if ! ps -eo args --no-headers | grep -q "[g]emini_shim.py"; then
      GEMINI_API_KEY="$(grep '^GEMINI_API_KEY=' .env | cut -d= -f2-)" \
        nohup ./sim/.venv/bin/python sim/bench/gemini_shim.py 8099 >> /tmp/gemini_shim.log 2>&1 &
      sleep 2
    fi
  else
    echo "    no grasp/vision backend configured in .env -- refusing to run" | tee -a "$LOG"
    exit 1
  fi

  # ROS entry points must keep their executable bit. `colcon --symlink-install`
  # links install/.../lib/<pkg>/<node>.py straight at the source file, and
  # `ros2 launch` reports a non-executable target as "executable not found on
  # the libexec directory" -- which reads as a missing build, not a mode. One
  # edit through a Windows path stripped it from brain_client_node.py and cost
  # a whole eval run: the stack came up 5/7, the brain never started, and every
  # map was skipped.
  STRIPPED=$(find ros2_ws/src -path '*/nodes/*.py' ! -name '__init__.py' ! -perm -u+x 2>/dev/null)
  if [ -n "$STRIPPED" ]; then
    echo "--- restoring lost executable bits:" | tee -a "$LOG"
    echo "$STRIPPED" | tee -a "$LOG"
    echo "$STRIPPED" | xargs chmod +x
  fi

  echo "--- map" | tee -a "$LOG"
  ( cd sim && VIRTUAL_MARS_ASSETS="$ASSETS" MUJOCO_GL=osmesa \
      "$(command -v uv || echo "$HOME/.local/bin/uv")" run bench/export_nav_map.py 2>&1 | tail -1 ) | tee -a "$LOG"
  if ! ./sim/.venv/bin/python sim/bench/lint_navmap.py sim/assets/map/sim_apartment.yaml \
       2>&1 | tail -2 | tee -a "$LOG" | grep -q '^OK'; then
    echo "    map failed its lint -- skipping $BUNDLE" | tee -a "$LOG"
    continue
  fi

  # The OS container used to be plain `innate-dev`; since upstream gave each
  # checkout its own stack it carries a per-checkout suffix. Discover it
  # rather than hardcoding, so this keeps working either way.
  OS_CONTAINER=""
  echo "--- restart" | tee -a "$LOG"
  timeout 300 bash "$REPO/sim/bench/innate_up.sh" down >/dev/null 2>&1
  sleep 2
  export VIRTUAL_MARS_ASSETS="$ASSETS"
  timeout 900 bash "$REPO/sim/bench/innate_up.sh" up --offline 2>&1 | grep -cE '✓' \
    | xargs echo "    checks passed:" | tee -a "$LOG"
  OS_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E '^innate-dev' | head -1)
  if [ -z "$OS_CONTAINER" ]; then
    echo "    no innate-dev* container is running -- skipping $BUNDLE" | tee -a "$LOG"
    continue
  fi
  echo "    container: $OS_CONTAINER" | tee -a "$LOG"

  # Prove the CONTAINER can reach the backend, not just this shell -- they are
  # different hops, and checking the easy one passes while the real one fails.
  if [ -n "$SERVICE_KEY" ]; then
    if ! timeout 90 docker exec "$OS_CONTAINER" bash -lc \
         "curl -sf -o /dev/null --max-time 30 https://auth-v1.svc.innate.bot/.well-known/openid-configuration"; then
      echo "    the CONTAINER cannot reach Innate auth -- skipping $BUNDLE" | tee -a "$LOG"
      continue
    fi
  elif ! timeout 60 docker exec "$OS_CONTAINER" bash -lc \
       "curl -sf -o /dev/null 'http://host.docker.internal:8099/v1beta/models?pageSize=1'"; then
    echo "    the CONTAINER cannot reach the shim -- skipping $BUNDLE" | tee -a "$LOG"
    continue
  fi

  export PYTHONPATH="$PWD/ros2_ws/src/mars_bot/mars_sim_driver"
  for CAT in 1 2 3; do
    IDS=$(./sim/.venv/bin/python - "$BUNDLE" "$CAT" <<'PY'
import sys
sys.path.insert(0, 'ros2_ws/src/mars_bot/mars_sim_driver')
from pathlib import Path
from mars_sim_driver.challenges import load_challenges
bundle, cat = sys.argv[1], int(sys.argv[2])
ch = load_challenges([Path('sim/bundles') / bundle / 'challenges'])
print(' '.join(sorted(c.id for c in ch.values() if c.category == cat)))
PY
)
    [ -z "$IDS" ] && continue
    CAP=$(eval echo \$CAP_$CAT)
    ARGS=(); for cid in $IDS; do ARGS+=(--challenge "$cid"); done
    echo "--- category $CAT (${CAP}s cap): $IDS" | tee -a "$LOG"
    # tee FIRST, filter second. Piping straight into grep discarded anything
    # that did not match -- including tracebacks. live_runner crashed on the
    # first episode of every category with a one-line TypeError, printed
    # nothing that matched, and the run carried on to the next map looking
    # healthy while the brain kept billing: three categories, zero result
    # files, $1.11 spent, and not one word anywhere saying why.
    timeout 7200 ./sim/.venv/bin/python sim/bench/live_runner.py \
      "${ARGS[@]}" --timeout "$CAP" \
      --out "$OUT/${BUNDLE}_cat${CAT}.json" 2>&1 \
      | tee -a "$LOG" | grep -E '^\[|passed|not attempted|harness'
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
      echo "!!! live_runner exited $rc on $BUNDLE cat$CAT -- full output in $LOG" | tee -a "$LOG"
    fi
    if [ ! -s "$OUT/${BUNDLE}_cat${CAT}.json" ]; then
      # A category that produced no results file produced no evidence. Say so
      # loudly rather than let an empty map read as a bad score.
      echo "!!! no results written for $BUNDLE cat$CAT" | tee -a "$LOG"
    fi
  done

  ./sim/.venv/bin/python sim/bench/spend.py 2>&1 | tail -2 | tee -a "$LOG"
done

echo "" | tee -a "$LOG"
echo "=== done. results in $OUT, log $LOG" | tee -a "$LOG"
./sim/.venv/bin/python sim/bench/spend.py 2>&1 | tail -4 | tee -a "$LOG"
