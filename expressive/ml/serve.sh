#!/usr/bin/env bash
# (Re)start the MARS planner service on the 5090 in the background: ./serve.sh [stop]
# Layout: $W/repo/expressive (this code), $W/envs/serve (vLLM), $W/runs (models), $W/logs/server.log.
# PLANNERS=speech (default, what :8000 runs since Mon 2026-10-05) serves the speech-trained bundles, which plan spoken
# sentences and still every emotion prompt; PLANNERS=talk is the rollback; MEDIUM / LOW override either set.
# A PORT other than 8000 gets its own pid and log file, so a test server never stops the live one.
set -euo pipefail
W=${W:-/media/jetson1/nvme/theo/expressive}
PORT=${PORT:-8000}
NAME=server$([[ $PORT == 8000 ]] || echo "-$PORT")
PIDFILE=$W/logs/$NAME.pid

# The server runs in its own session so stop can take down vLLM's engine-core children with it.
if [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill -- -"$(cat "$PIDFILE")"
  while pgrep -g "$(cat "$PIDFILE")" >/dev/null; do sleep 1; done
fi
rm -f "$PIDFILE"
[[ ${1:-} == stop ]] && exit 0

case ${PLANNERS:-speech} in
  speech) medium=runs/planner-4b-speech/served low=runs/planner-08b-speech/served ;;
  talk) medium=runs/planner-4b-talk/served low=runs/planner-08b-talk/merged ;;
  *) echo "PLANNERS must be talk or speech" >&2; exit 1 ;;
esac

export HF_HOME=/media/jetson1/nvme/theo/hf PYTHONPATH=$W/repo/expressive PYTHONUNBUFFERED=1
# FlashInfer's sampler JIT-compiles with nvcc, which this box does not have; vLLM's own sampler is as fast here.
export VLLM_USE_FLASHINFER_SAMPLER=0
# Default flags: FP8 planner weights + 3 MTP draft tokens (4B: 1.04 s -> 0.67 s per prompt, probes unchanged);
# SERVE_FLAGS= (set but empty) serves exact bf16 weights.
cd "$W"
setsid nohup envs/serve/bin/python -m ml.server \
  --bundle medium="${MEDIUM:-$medium}" \
  --bundle low="${LOW:-$low}" \
  --generator "${GENERATOR:-runs/generator/generator.pt}" \
  ${SERVE_FLAGS---fp8 --spec-tokens 3} --port "$PORT" > "logs/$NAME.log" 2>&1 &
echo $! > "$PIDFILE"
echo "starting (pid $(cat "$PIDFILE")); tail -f $W/logs/$NAME.log; ready when curl -s localhost:$PORT/health answers"
