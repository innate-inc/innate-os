#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Build and push the layered sim asset image.
#
# One buildx invocation covers both arches: the payload is static data, so there
# is no per-arch runner and no manifest stitching pass, unlike the colcon images
# in ci/build_sim_images.sh.
#
# Env:
#   IMAGE_PREFIX        ghcr.io/innate-inc
#   IMAGE_TAG           sha-<short12>
#   IMAGE_INPUTS_HASH   config.compute_assets_image_inputs_hash of the checkout
#   PUSH_MAIN_TAGS      "true" on main
#   CACHE_SCOPE         the branch name; unset or "main" writes the shared cache
#
# Deliberately NO image.revision label, and so no COMMIT_SHA input: it varies
# per commit, which changes the config blob and therefore the index digest, so
# byte-identical content would get a new digest every push.
set -euo pipefail

IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/innate-inc}"
# An unset hash would publish the literal, mutable tag "inputs-": every later
# build clobbers it, and cleanup-sim-images.yml cannot pin it against retention.
: "${IMAGE_INPUTS_HASH:?must be set (config.compute_assets_image_inputs_hash)}"
: "${IMAGE_TAG:?must be set}"
assets_image="${IMAGE_PREFIX}/innate-os-sim-assets"
inputs_tag="inputs-${IMAGE_INPUTS_HASH}"

# BUILD ONCE PER HASH, NEVER REBUILD. inputs-<hash> is a content address, so it
# has to name one payload forever -- but CoACD is OpenMP-threaded and its output
# is not bit-identical between runs even at a fixed seed, so rebuilding an
# existing tag could replace its contents with different bytes.
if python3 ci/verify_assets_image.py --exists "${assets_image}" "${inputs_tag}"; then
  echo "${assets_image}:${inputs_tag} already published; nothing to build."
  exit 0
fi

echo "=== seeding the build context ==="
# The generated geometry is not in git; without this the COPYs below would
# fail (or, worse, silently ship an empty bundle).
python3 ci/seed_asset_context.py

echo "=== building ${assets_image} ==="
# --provenance/--sbom off: buildx otherwise pushes two unknown/unknown
# attestation manifests into the index -- untagged package versions the
# retention sweep has to tolerate, and one more way a platform-resolution
# fallback can land on a manifest with no usable layers.
#
# mode=max, NOT mode=min. min exports only the final image's layers, so the
# ~17-minute CoACD stage would never be cached.
tags=(--tag "${assets_image}:${IMAGE_TAG}" --tag "${assets_image}:${inputs_tag}")
if [ "${PUSH_MAIN_TAGS:-false}" = "true" ]; then
  tags+=(--tag "${assets_image}:main")
fi

# A registry cache export REPLACES the ref's records rather than merging into
# them, so a branch that wrote `buildcache` would evict main's bakes and cost
# main's next publish the full 17 minutes. Branches read main's cache and their
# own, and write only their own (cache-<branch>, aged out by
# cleanup-sim-images.yml); main alone writes `buildcache`.
cache_from=(--cache-from "type=registry,ref=${assets_image}:buildcache")
cache_ref="buildcache"
if [ -n "${CACHE_SCOPE:-}" ] && [ "${CACHE_SCOPE}" != "main" ]; then
  cache_ref="cache-$(printf '%s' "${CACHE_SCOPE}" | tr -c 'A-Za-z0-9_.-' '-' | cut -c1-100)"
  cache_from+=(--cache-from "type=registry,ref=${assets_image}:${cache_ref}")
fi

DOCKER_BUILDKIT=1 docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file sim/Dockerfile.assets \
  --build-arg "SIM_RESOURCE_LOG=${SIM_RESOURCE_LOG:-0}" \
  --provenance=false --sbom=false \
  "${cache_from[@]}" \
  --cache-to "type=registry,ref=${assets_image}:${cache_ref},mode=max" \
  --label "org.opencontainers.image.source=https://github.com/innate-inc/innate-os" \
  "${tags[@]}" \
  --push \
  .

echo "=== verifying the pushed layers ==="
# The launcher takes the geometry by position out of the manifest
# (config.ASSETS_IMAGE_LAYERS) and the Dockerfile cannot import that constant,
# so reordering the final stage's COPYs must fail the publish rather than send
# every notebook user the viewer layer.
python3 ci/verify_assets_image.py "${assets_image}" "${inputs_tag}"
