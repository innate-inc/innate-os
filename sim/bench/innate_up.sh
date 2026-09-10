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
# These are the tags in the local store. The assets content is unchanged by
# the benchmark work; only the hash of the surrounding tree moved. The viewer
# bundle is deliberately NOT pinned: sim/viewer now draws primitive-authored
# rooms from the roster frame (src/rooms.ts), so a published bundle from
# before that change would show every benchmark world as an empty box. The
# launcher builds the bundle from this tree on its own when no published one
# describes it.
set -uo pipefail
# Resolve the repo from this script, not $HOME: run_eval.sh calls this
# one, so a hardcoded home path here made the whole live path
# home-directory-bound even after the callers were fixed.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1



# Use a locally-built image ONLY if it is really there, and only if the
# caller has not chosen one. On any other machine these tags do not
# exist, and pinning them turns "no such image" into what looks like a
# launcher bug; unset, the launcher resolves images as it always does.
pin_if_present() {
  local var="$1" tag="$2"
  [ -n "${!var:-}" ] && return 0
  if docker image inspect "$tag" >/dev/null 2>&1; then
    export "$var=$tag"
  fi
}

pin_if_present INNATE_OS_IMAGE "innate-os-sim-clean-innate:inputs-3acfd3403d107c7672ea0cefd1539c6f4eaa8714f484f0743a4b6138a040ebc3"
# The assets image is no longer pinned here: the layer that pin named predates
# the worlds upstream added (backrooms, intersection), and the merged launcher
# refuses to install a partial geometry store from it. The launcher reuses an
# installed store whose geometry inputs are unchanged; to seed one, set
# INNATE_SIM_ASSETS_IMAGE to upstream's published image for the merged base
# (FINDINGS.md, "Bringing the live stack up from this fork").

exec ./innate-sim "$@"
