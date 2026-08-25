#!/bin/bash
# Build the innate-gstreamer-opt Debian package: a self-contained GStreamer at
# /opt/gst that unlocks webrtcbin's ULPFEC (broken on the system 1.20 — see
# docs/WEBRTC_FEC_GST_UPGRADE.md). Run natively on a Jetson (arm64); the deb
# ships as a GitHub Release asset on innate-packages, referenced from its
# prebuilt-debs.txt manifest (this script prints the exact handoff commands),
# where check-prebuilt-deb.sh is the CI gate and publish.sh signs and indexes
# it. Robots pick it up through the normal update flow via
# apt-dependencies.hardware.txt, and camera_composable.launch.py activates
# /opt/gst automatically when present.
#
#   ./scripts/update/build_gst_opt.sh   # -> innate-gstreamer-opt_<v>-1jammy_arm64.deb
#   GST_VERSION=1.24.13 DEB_INC=2 ./scripts/update/build_gst_opt.sh
#
# DEB_INC bumps the Debian revision (same convention as innate-packages
# build.sh): a rebuild of the same upstream version is invisible to apt
# unless the revision changes.
set -euo pipefail

V="${GST_VERSION:-1.24.12}"
REV="${DEB_INC:-1}jammy"
WORK="${WORK_DIR:-$HOME/gst-src}"
PREFIX=/opt/gst
LIBDIR=lib/aarch64-linux-gnu
SRC_COMMIT=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse HEAD 2>/dev/null || echo unknown)

missing=$(for p in ninja-build flex bison libssl-dev libsrtp2-dev libvpx-dev \
                   libjpeg-dev python3-pip pkg-config cmake curl binutils file; do
  dpkg -s "$p" >/dev/null 2>&1 || echo "$p"
done)
[ -z "$missing" ] || sudo apt-get install -y $missing
pip3 install --user --quiet "meson>=1.1" patchelf
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$WORK" && cd "$WORK"
if [ ! -d "gstreamer-$V" ]; then
  curl -fsSL -o gst.tar.gz \
    "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/archive/$V/gstreamer-$V.tar.gz"
  tar xf gst.tar.gz
fi
cd "gstreamer-$V"

[ -f build/build.ninja ] || meson setup build --prefix="$PREFIX" \
  -Dbuildtype=release -Dgpl=disabled \
  -Dugly=disabled -Dlibav=disabled -Ddevtools=disabled -Dges=disabled \
  -Drtsp_server=disabled -Ddoc=disabled -Dexamples=disabled -Dtests=disabled \
  -Dintrospection=disabled -Dpython=disabled -Dlibnice=enabled \
  -Dgst-plugins-bad:webrtc=enabled -Dgst-plugins-bad:dtls=enabled \
  -Dgst-plugins-bad:srtp=enabled -Dgst-plugins-bad:sctp=enabled \
  -Dgst-plugins-good:vpx=enabled -Dgst-plugins-good:rtpmanager=enabled
ninja -C build

ROOT="$WORK/debroot"
rm -rf "$ROOT" && mkdir -p "$ROOT$PREFIX" "$ROOT/DEBIAN"
DESTDIR="$ROOT" meson install -C build --no-rebuild >/dev/null
GST="$ROOT$PREFIX"

# Runtime-only payload: headers, dev symlinks, tests and non-gst tools stay
# home. Each pruned plugin below is the sole consumer of a heavy external dep
# (qmlgl alone drags in Qt5); openh264/fdk-aac also carry licenses we must
# not redistribute from a public apt repo.
rm -rf "$GST/$LIBDIR/pkgconfig" "$GST/$LIBDIR/cmake" \
       "$GST/libexec/installed-tests" \
       "$GST/share/man" "$GST/share/aclocal" "$GST/share/gdb" \
       "$GST/share/installed-tests" "$GST/share/bash-completion"
find "$GST/bin" -maxdepth 1 \( -type f -o -type l \) ! -name 'gst-*' -delete
rm -f "$GST"/libexec/gstreamer-1.0/gst-hotdoc-plugins-scanner \
      "$GST"/libexec/gstreamer-1.0/gst-plugins-doc-cache-generator \
      "$GST"/libexec/gstreamer-1.0/gst-completion-helper
rm -f "$GST/$LIBDIR"/libgstcheck-1.0.so* "$GST/$LIBDIR"/liborc-test-0.4.so* \
      "$GST/$LIBDIR"/libopenh264.so* "$GST/$LIBDIR"/libfdk_aac.so* \
      "$GST/$LIBDIR"/libpangoxft-1.0.so*
for p in openh264 fdkaac qmlgl ximagesrc ximagesink rfbsrc aom dc1394 de265 \
         openni2 openexr openjpeg theora uvch264 curl colormanagement webp; do
  rm -f "$GST/$LIBDIR/gstreamer-1.0/libgst$p.so"
done
find "$GST/$LIBDIR" -maxdepth 1 -type l -name 'lib*.so' -delete
rm -rf "$GST/$LIBDIR/cairo"  # only cairo's LD_PRELOAD trace shims live here
# Subprojects nest more include/ dirs under lib (graphene does).
find "$GST" -type d -name include -prune -exec rm -rf {} +
find "$GST" -depth -type d -empty -delete

# The 1.24 CLI must not share a registry cache with the system 1.20: both
# default to $XDG_CACHE_HOME/gstreamer-1.0/registry.aarch64.bin, and the
# magic-version mismatch makes each run discard the other's cache (a full
# plugin rescan on every alternating start). Real binaries live in libexec;
# bin/ keeps wrappers pinning a cache file of their own.
mkdir -p "$GST/libexec/gst-opt"
for tool in "$GST"/bin/gst-*; do
  name=${tool##*/}
  mv "$tool" "$GST/libexec/gst-opt/$name"
  { echo '#!/bin/sh'
    echo 'export GST_REGISTRY="${GST_REGISTRY:-${XDG_CACHE_HOME:-$HOME/.cache}/gstreamer-1.0/registry-opt.aarch64.bin}"'
    echo "exec $PREFIX/libexec/gst-opt/$name \"\$@\""
  } > "$tool"
  chmod 755 "$tool"
done

# $ORIGIN-relative RUNPATH (not LD_LIBRARY_PATH) is what makes /opt/gst
# self-contained next to the system GStreamer: without it, gst-inspect-1.0
# run by hand binds the system 1.20 core and rejects every 1.24 plugin.
find "$ROOT" -type f | while read -r f; do
  readelf -h "$f" >/dev/null 2>&1 || continue
  strip --strip-unneeded "$f"
  # grep without -q: an early exit would SIGPIPE readelf and, under pipefail,
  # silently skip ELFs that do have NEEDED entries.
  readelf -d "$f" 2>/dev/null | grep NEEDED >/dev/null || continue
  rel=$(realpath --relative-to="$(dirname "$f")" "$GST/$LIBDIR")
  patchelf --set-rpath "\$ORIGIN/$rel" "$f"
done

# The gate that matters: every WebRTC element present, and NVIDIA's hardware
# plugins (built against the system gst) loading under this core.
export LD_LIBRARY_PATH="$GST/$LIBDIR"
export GST_PLUGIN_PATH="$GST/$LIBDIR/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0"
export GST_REGISTRY="$WORK/verify.registry"
rm -f "$GST_REGISTRY"
# The real ELF, not the bin/ wrapper: wrappers exec the installed $PREFIX
# path, which doesn't exist in the staging tree.
B="$GST/libexec/gst-opt/gst-inspect-1.0"
for el in webrtcbin vp8enc rtpvp8pay srtpenc dtlssrtpenc rtpulpfecenc rtpredenc \
          nicesink nvv4l2decoder nvvidconv nvjpegenc; do
  "$B" "$el" > /dev/null || { echo "VERIFY FAILED: $el"; exit 1; }
done

# Depends comes from dpkg-shlibdeps, never a bare package-ownership closure:
# symbols files carry per-symbol version floors (the payload imports g_memdup2,
# GLib 2.68 — an unversioned dep installs cleanly on focal/L4T r35 and every
# plugin dlopen then dies at runtime with no apt error). Bundled sonames map
# to this package via shlibs.local and are filtered back out of the field.
SHLIB="$WORK/shlibdeps"
rm -rf "$SHLIB" && mkdir -p "$SHLIB/debian"
printf 'Source: innate-gstreamer-opt\nMaintainer: Innate Inc <ops@innate.bot>\n\nPackage: innate-gstreamer-opt\nArchitecture: arm64\nDescription: shlibdeps stub\n' \
  > "$SHLIB/debian/control"
find "$GST/$LIBDIR" -maxdepth 1 -type f -name 'lib*.so*' -exec readelf -d {} \; 2>/dev/null \
  | awk '/SONAME/ {gsub(/[][]/, "", $NF); print $NF}' | sort -u \
  | sed -E 's/^(.+)\.so\.(.+)$/\1 \2 innate-gstreamer-opt/' > "$SHLIB/debian/shlibs.local"
# `if`, not `&&`: a trailing non-ELF (the bin/ wrappers) would otherwise end
# the substitution non-zero and errexit kills the build with no message.
ELFS=$(find "$ROOT" -type f ! -path "$ROOT/DEBIAN/*" \
  | while read -r f; do
      if readelf -h "$f" >/dev/null 2>&1; then echo "$f"; fi
    done)
DEPENDS=$(cd "$SHLIB" && dpkg-shlibdeps -O -l"$GST/$LIBDIR" $(printf -- '-e%s ' $ELFS) \
    2>"$WORK/shlibdeps.log" \
  | sed 's/^shlibs:Depends=//' | tr ',' '\n' | sed 's/^ *//;s/ *$//' \
  | grep -v '^innate-gstreamer-opt$' | sort -u | paste -sd, - | sed 's/,/, /g') \
  || { tail -5 "$WORK/shlibdeps.log"; echo "DEPENDS FAILED: dpkg-shlibdeps"; exit 1; }
case "$DEPENDS" in *"libc6 (>="*) ;; *)
  echo "DEPENDS FAILED: unversioned libc6 (symbols files missing?)"; exit 1 ;; esac
case "$DEPENDS" in *"libglib2.0-0 (>="*) ;; *)
  echo "DEPENDS FAILED: unversioned libglib2.0-0"; exit 1 ;; esac

mkdir -p "$ROOT/usr/share/doc/innate-gstreamer-opt"
cat > "$ROOT/usr/share/doc/innate-gstreamer-opt/copyright" << EOF
innate-gstreamer-opt repackages GStreamer $V, built unmodified from
https://gitlab.freedesktop.org/gstreamer/gstreamer/-/archive/$V/
by innate-os scripts/update/build_gst_opt.sh (GPL plugins disabled).

GStreamer libraries and plugins: LGPL-2.1-or-later; bundled meson
subproject libraries (libnice, libsoup, orc, cairo, pango, harfbuzz,
FLAC, opus, ogg, vorbis, json-glib, libpsl, fribidi, pixman, libdv):
their respective upstream licenses (LGPL/MPL/BSD/MIT). Full texts ship
in each project's source archive at the URL above.
EOF

cat > "$ROOT/DEBIAN/control" << EOF
Package: innate-gstreamer-opt
Version: $V-$REV
Section: libs
Priority: optional
Architecture: arm64
Depends: $DEPENDS
Installed-Size: $(du -ks "$GST" | cut -f1)
Maintainer: Innate Inc <ops@innate.bot>
Description: Parallel GStreamer $V runtime at /opt/gst for WebRTC FEC
 Self-contained GStreamer (core/base/good/bad + libnice) under /opt/gst,
 runtime files only, RUNPATH-pinned to its own libraries. The camera
 stack opts in via camera_composable.launch.py when this directory
 exists; without it robots run the system GStreamer unchanged.
 Ships working webrtcbin ULPFEC (broken on system 1.20).
 Built from innate-os commit $SRC_COMMIT.
EOF
(cd "$ROOT" && find opt usr -type f -print0 | LC_ALL=C sort -z \
  | xargs -0 md5sum > DEBIAN/md5sums)

# dpkg applies packaged directory modes to live system dirs (/opt included),
# so a 0775 from the build umask would chmod every robot's /opt.
chmod -R g-w "$ROOT"
find "$ROOT" -type d -exec chmod 755 {} +

# -Zxz, not the dpkg default zstd: debsigs parses only gz/xz members and
# silently signs nothing on a zstd deb.
DEB="$WORK/innate-gstreamer-opt_${V}-${REV}_arm64.deb"
dpkg-deb -Zxz --root-owner-group --build "$ROOT" "$DEB"
SHA=$(sha256sum "$DEB" | cut -d' ' -f1)
TAG="innate-gstreamer-opt-$V-$REV"
cat << EOF
OK: $DEB

To ship it (from an innate-packages checkout; validate first with
./check-prebuilt-deb.sh $DEB):

  gh release create $TAG $DEB \\
    --title "innate-gstreamer-opt $V-$REV" \\
    --notes "Built from innate-os commit $SRC_COMMIT. sha256: $SHA"

then replace the innate-gstreamer-opt line in prebuilt-debs.txt with:

  $TAG $(basename "$DEB") $SHA
EOF
