# The SimSession bundle the operator webapp loads, as its own tiny image.
#
# ~1 MB, one layer, addressed by inputs-<content hash of the sim/viewer tree as
# it sits on disk> (config.compute_viewer_inputs_hash / resolve_viewer_image).
# Separate from sim/Dockerfile.assets because this moves per push while the
# geometry's tag stays put for months: one content address for both made every
# edit to sim/viewer/src rename the ~156 MB asset image. A miss is repaired by
# building this same Dockerfile locally (runtime._build_viewer_image_locally),
# so nobody needs Node.js on the host.
#
# THIS FILE LIVES INSIDE sim/viewer ON PURPOSE: the tag is a content hash of
# that directory, so a Dockerfile one level up could change what gets built
# without moving the address. The build CONTEXT is still the repo root (hence
# the sim/viewer/ prefixes on every COPY); only the file's location matters.
#
# One layer, one subtree, no whiteouts: sim/launcher/oci.py resolves this image
# straight over anonymous HTTPS to check it was published.
#
# Pinned to a digest, not just a version tag: publish-viewer-bundle.yml
# deliberately republishes inputs-<hash> for an unchanged tree, which is only
# idempotent while the base is fixed. Under a moving 22.21-slim that repush --
# or a local fallback build months later -- puts different bytes behind an
# address clients have already cached, which is the one thing it must not do.
# Bumping this line moves the hash and so republishes, which is the point.
FROM --platform=$BUILDPLATFORM node:22.21-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c AS bundle
WORKDIR /viewer
COPY sim/viewer/package.json sim/viewer/package-lock.json ./
RUN npm ci
COPY sim/viewer/tsconfig.json sim/viewer/vite.lib.config.ts ./
COPY sim/viewer/src ./src
COPY sim/viewer/config ./config
RUN npm run build:lib

FROM scratch
COPY --from=bundle /viewer/dist-lib /bundle
