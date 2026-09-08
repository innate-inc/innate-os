#!/bin/bash
# Build llama.cpp for the Jetson's GPU and fetch the Qwen3.5-2B weights the
# on-robot brain uses (BRAIN_BACKEND=local). Idempotent; no sudo. ~20 min on
# an Orin Nano for the build, ~2 GB download.
set -euo pipefail

INNATE_OS_ROOT="${INNATE_OS_ROOT:-$HOME/innate-os}"
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-$HOME/llama.cpp}"
LOCAL_LLM_MODEL_DIR="${LOCAL_LLM_MODEL_DIR:-$INNATE_OS_ROOT/data/models/qwen3.5-2b}"
HF_REPO="${LOCAL_LLM_HF_REPO:-unsloth/Qwen3.5-2B-GGUF}"
# Unsloth's dynamic 4-bit: 1.3 GB, the best quality/size point that leaves the
# rest of the 8 GB to the robot. The F16 projector is the vision encoder.
HF_INCLUDE=("*UD-Q4_K_XL*" "*mmproj-F16*")
# nvcc jobs take ~1 GB each; cap them while the robot stack is up.
BUILD_JOBS="${LOCAL_LLM_BUILD_JOBS:-$(nproc)}"

if [ ! -d "$LLAMA_CPP_DIR/.git" ]; then
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_CPP_DIR"
fi
if [ ! -x "$LLAMA_CPP_DIR/build/bin/llama-server" ]; then
    # sm_87 is the Orin's compute capability; NATIVE tunes the CPU side to this core.
    cmake -S "$LLAMA_CPP_DIR" -B "$LLAMA_CPP_DIR/build" \
        -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DGGML_NATIVE=ON \
        -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
    cmake --build "$LLAMA_CPP_DIR/build" --config Release -j"$BUILD_JOBS" \
        --target llama-server llama-mtmd-cli llama-bench
fi

if ! command -v hf >/dev/null 2>&1; then
    pip3 install --user -q -U huggingface_hub
    export PATH="$HOME/.local/bin:$PATH"
fi
mkdir -p "$LOCAL_LLM_MODEL_DIR"
includes=()
for pattern in "${HF_INCLUDE[@]}"; do includes+=(--include "$pattern"); done
hf download "$HF_REPO" "${includes[@]}" --local-dir "$LOCAL_LLM_MODEL_DIR"

echo "local llm ready: $(ls "$LOCAL_LLM_MODEL_DIR"/*.gguf | xargs -n1 basename | tr '\n' ' ')"
echo "set BRAIN_BACKEND=local in $INNATE_OS_ROOT/.env and run: innate restart"
