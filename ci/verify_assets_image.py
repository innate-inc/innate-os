#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Two checks against the published asset image.
#
#   --exists <image> <tag>   exit 0 if the tag is already published, so
#                            ci/build_assets_image.sh can skip the build.
#
#   <image> <tag>            assert the pushed image's layer LAYOUT.
#
# The layout matters because sim/launcher/runtime.py takes the geometry as
# layer 0: a manifest names no layer, so a reordered COPY would silently hand
# every notebook user the viewer instead.
#
# Usage: python3 ci/verify_assets_image.py [--exists] <image> <tag>
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sim" / "launcher"))
import oci  # noqa: E402  (needs sim/launcher on the path first)
from config import ASSETS_IMAGE_LAYERS as ORDER  # noqa: E402

# ORDER must match the COPY order of the final stage in sim/Dockerfile.assets;
# config.py owns the one copy the launcher also reads from.


def subtree_of(repo: str, digest: str, token: str) -> str:
    """The single top-level directory a layer blob contains.

    Also enforces the one-disjoint-subtree-per-layer rule: two top-level names,
    or any whiteout, means a COPY modified an earlier layer and the blob is no
    longer independently extractable -- which is what `innate-sim assets` does.
    """
    # A temp file, not BytesIO: only the names are wanted, so there is no
    # reason to hold a whole ~85 MB compressed blob resident.
    with tempfile.TemporaryFile() as buf:
        oci.fetch_layer(repo, digest, buf, token, label=f"layer {digest[:19]}")
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            names = []
            for member in tar:
                oci.validate_layer_member(member)
                if ".cabinet" in Path(member.name).parts:
                    raise oci.OciError(f"host-local cabinet cache in asset layer: {member.name}")
                names.append(member.name)
    whiteouts = [n for n in names if ".wh." in n]
    if whiteouts:
        sys.exit(f"layer {digest[:19]} has whiteouts ({whiteouts[:3]}): it is not standalone-extractable")
    tops = {name.split("/")[0] for name in names if name and not name.startswith(".")}
    if len(tops) != 1:
        sys.exit(f"layer {digest[:19]} holds {sorted(tops)}, expected exactly one subtree")
    return tops.pop()


def main() -> None:
    args = sys.argv[1:]
    exists_only = "--exists" in args
    args = [a for a in args if a != "--exists"]
    if len(args) != 2:
        sys.exit("usage: verify_assets_image.py [--exists] <image> <tag>")
    image, tag = args
    repo = oci.repo_path(image)

    try:
        token = oci.anon_token(repo)
        manifest = oci.fetch_manifest(repo, tag, token)
    except oci.OciError as exc:
        if exists_only:
            # 404 is the clean answer: the tag is not there, build it. 401/403
            # are answers too -- GHCR will not say whether a package it won't
            # show you exists, and a brand-new package reads the same as a
            # private one, so the first publish has to be allowed through.
            #
            # Anything else (5xx, 429, or status None for a timeout or reset)
            # means the registry never answered. Treating that as "absent"
            # re-pushes a content address over a payload CoACD may not
            # reproduce, so it warns -- but still proceeds, since refusing would
            # strand every push behind a blip.
            if exc.status not in (401, 403, 404):
                print(
                    f"::warning::could not confirm whether {repo}:{tag} is published ({exc}). "
                    "Treating it as absent and building; if it did exist, this re-pushes a "
                    "content address over a payload CoACD may not reproduce byte for byte.",
                    file=sys.stderr,
                )
            return sys.exit(1)
        sys.exit(
            f"::error::{repo}:{tag} is not anonymously pullable ({exc}).\n"
            "GHCR creates new packages PRIVATE, which breaks more than a pull: sim/launcher/oci.py\n"
            "fetches layers anonymously so `innate-sim assets` needs no Docker, and compose mounts\n"
            "the viewer straight off the image. Make it public:\n"
            "  github.com/orgs/innate-inc/packages -> innate-os-sim-assets\n"
            "  -> Package settings -> Change visibility -> Public\n"
            "and link it to innate-os under 'Manage Actions access'."
        )
    if exists_only:
        return sys.exit(0)

    digests = [layer["digest"] for layer in manifest["layers"]]
    if len(digests) != len(ORDER):
        sys.exit(f"expected {len(ORDER)} layers {list(ORDER)}, got {len(digests)}; sim/Dockerfile.assets has drifted")
    for position, (digest, expected) in enumerate(zip(digests, ORDER, strict=True)):
        found = subtree_of(repo, digest, token)
        if found != expected:
            sys.exit(
                f"layer {position} holds {found!r}, expected {expected!r}.\n"
                "sim/launcher/runtime.py reads the geometry by position, so this would hand "
                "every user the wrong subtree."
            )
        print(f"  layer {position}: {expected} ({digest[:19]})")
    print(f"{image}:{tag} layout OK")


if __name__ == "__main__":
    main()
