#!/bin/bash
# The on-robot VLM behind BRAIN_BACKEND=local: llama-server holding Qwen3.5-2B
# resident on the Jetson's GPU. Launched by brain_client.launch.py when that
# backend is chosen; scripts/install_local_llm.sh builds the binary and fetches
# the weights. Every knob is an .env override with a working default.
set -euo pipefail

INNATE_OS_ROOT="${INNATE_OS_ROOT:-$HOME/innate-os}"
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
LOCAL_LLM_MODEL_DIR="${LOCAL_LLM_MODEL_DIR:-$INNATE_OS_ROOT/data/models/qwen3.5-2b}"
LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-qwen3.5-2b}"
LOCAL_LLM_PORT="${LOCAL_LLM_PORT:-8080}"
LOCAL_LLM_CTX="${LOCAL_LLM_CTX:-8192}"
LOCAL_LLM_IMAGE_MAX_TOKENS="${LOCAL_LLM_IMAGE_MAX_TOKENS:-512}"

weights=$(ls "$LOCAL_LLM_MODEL_DIR"/*.gguf 2>/dev/null | grep -v mmproj | head -1 || true)
mmproj=$(ls "$LOCAL_LLM_MODEL_DIR"/mmproj-*.gguf 2>/dev/null | head -1 || true)
if [ ! -x "$LLAMA_SERVER_BIN" ] || [ -z "$weights" ] || [ -z "$mmproj" ]; then
    echo "local_llm_server: missing llama-server ($LLAMA_SERVER_BIN) or weights in $LOCAL_LLM_MODEL_DIR" >&2
    echo "local_llm_server: run scripts/install_local_llm.sh first" >&2
    exit 1
fi

# Sampling is Qwen's recommendation for the 3.5 small models in non-thinking
# mode, minus temperature, which the brain pins per request (brain/local_llm.py);
# -np 1 keeps a single KV slot (one turn at a time by construction) and
# q8 KV halves the cache. The prompt cache is what makes consecutive turns
# cheap: the context is built so each request extends the previous one.
exec "$LLAMA_SERVER_BIN" \
    --model "$weights" \
    --mmproj "$mmproj" \
    --alias "$LOCAL_LLM_MODEL" \
    --host 127.0.0.1 --port "$LOCAL_LLM_PORT" \
    --n-gpu-layers 99 \
    --ctx-size "$LOCAL_LLM_CTX" \
    --parallel 1 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --image-max-tokens "$LOCAL_LLM_IMAGE_MAX_TOKENS" \
    --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5 \
    --jinja \
    --log-prefix --log-colors off
