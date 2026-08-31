#!/usr/bin/env bash
# Bring the innate runtime up against the images that actually exist locally.
#
# WHY THE OVERRIDES. The launcher names each image by a content hash over the
# tracked inputs, so editing anything under sim/ renames them -- and this branch
# has a benchmark in sim/bench that is not pushed, so CI has never built an
# image under the new name. The launcher says exactly this and offers the
# escape hatch itself:
#
#   "Editing anything the image is built from renames it. Push the branch so CI
#    publishes it, or set INNATE_SIM_ASSETS_IMAGE to one that exists."
#
# These are the tags in the local store. The assets and viewer content is
# unchanged by the benchmark work; only the hash of the surrounding tree moved.
set -uo pipefail
# Resolve the repo from this script, not $HOME: run_eval.sh calls this
# one, so a hardcoded home path here made the whole live path
# home-directory-bound even after the callers were fixed.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1

export INNATE_OS_IMAGE="innate-os-sim-clean-innate:inputs-3acfd3403d107c7672ea0cefd1539c6f4eaa8714f484f0743a4b6138a040ebc3"
export INNATE_SIM_ASSETS_IMAGE="ghcr.io/innate-inc/innate-os-sim-assets:inputs-2dcc207e745947d540d9866e1028f41f6ccc6065c154c7298ce4ec468ad86f5b"
export INNATE_SIM_VIEWER_BUNDLE_IMAGE="ghcr.io/innate-inc/innate-os-sim-viewer:inputs-553758957ded0d4525107c394e69e8a70aeaf3c16376becd691b0a3a991301b4"

exec ./innate-sim "$@"
