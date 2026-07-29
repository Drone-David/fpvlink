#!/usr/bin/env bash
# =============================================================================
# FPVLink – build-lut-plugin.sh
# Compile the fpvlut3d GStreamer element (capture/fpvlut3d.c) into a loadable
# plugin next to the live pipeline. No meson/ninja needed — a single gcc call
# against pkg-config flags. capture/pipeline.py adds its own directory to
# GST_PLUGIN_PATH, so the .so is discovered from there with no system install.
#
#   sudo bash setup/build-lut-plugin.sh   # (sudo only if writing to /opt)
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAPTURE_DIR="$(cd "$SCRIPT_DIR/../capture" && pwd)"
SRC="$CAPTURE_DIR/fpvlut3d.c"
OUT="$CAPTURE_DIR/libgstfpvlut3d.so"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
err()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }

for dep in gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0; do
  if ! pkg-config --exists "$dep"; then
    err "missing dev package for $dep"
    err "install: apt-get install -y libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev"
    exit 1
  fi
done

# -fopenmp splits rows across cores so 1080p60 stays real-time; if libgomp is
# unavailable the element still works (pragmas ignored), just single-threaded.
OMP_FLAG="-fopenmp"
if ! echo 'int main(){return 0;}' | gcc -fopenmp -x c - -o /dev/null 2>/dev/null; then
  info "OpenMP unavailable — building single-threaded"
  OMP_FLAG=""
fi

info "Compiling $SRC → $OUT"
gcc -O3 -Wall -fPIC -shared $OMP_FLAG \
    -o "$OUT" "$SRC" \
    $(pkg-config --cflags gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0) \
    $(pkg-config --libs gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0) \
    -lm

ok "Built $OUT"

# Verify GStreamer can load and register the element from the capture dir.
if GST_PLUGIN_PATH="$CAPTURE_DIR" gst-inspect-1.0 fpvlut3d >/dev/null 2>&1; then
  ok "gst-inspect-1.0 fpvlut3d → registered"
else
  err "Element built but not loadable — check gst-inspect-1.0 fpvlut3d output:"
  GST_PLUGIN_PATH="$CAPTURE_DIR" gst-inspect-1.0 fpvlut3d || true
  exit 1
fi
