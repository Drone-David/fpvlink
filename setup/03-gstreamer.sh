#!/usr/bin/env bash
# =============================================================================
# FPVLink – 03-gstreamer.sh
# Install and validate Rockchip MPP (rkmpp) hardware-accelerated GStreamer
# plugins on Orange Pi 5 Plus (RK3588) running Armbian Bookworm.
#
# Covers:
#   • mppvideodec  – H.265 / H.264 hardware decode via Rockchip MPP
#   • mppvideoenc  – H.264 hardware encode via Rockchip MPP
#   • srtsink      – SRT streaming output
#   • rtmpsink     – RTMP streaming output
#
# Run as root after 01-system.sh and a reboot from 02-usb-otg.sh:
#   sudo bash 03-gstreamer.sh
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# Colour helpers
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
err()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
step() { echo -e "\n${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; \
         echo -e "${BOLD}${YELLOW}  $*${NC}"; \
         echo -e "${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; }

# Result tracking (pass=1, fail=0)
declare -A RESULTS

gst_check() {
    local plugin="$1"
    local label="${2:-$1}"
    if gst-inspect-1.0 "$plugin" &>/dev/null; then
        RESULTS["$label"]="PASS"
        ok "gst-inspect-1.0 $plugin  →  found"
    else
        RESULTS["$label"]="FAIL"
        warn "gst-inspect-1.0 $plugin  →  NOT FOUND"
    fi
}

# -----------------------------------------------------------------------------
# Root check
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root.  Try: sudo bash $0"
    exit 1
fi

step "FPVLink GStreamer / Rockchip MPP Setup – Orange Pi 5 Plus"
info "Script started at $(date '+%Y-%m-%d %H:%M:%S %Z')"

export DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
# 1. Quick initial probe: is mppvideodec already available?
# -----------------------------------------------------------------------------
step "1/6  Checking for existing Rockchip MPP GStreamer plugin"

if gst-inspect-1.0 mppvideodec &>/dev/null; then
    ok "mppvideodec already available – skipping installation"
    RKMPP_PRESENT=true
else
    warn "mppvideodec not found – will attempt installation"
    RKMPP_PRESENT=false
fi

# -----------------------------------------------------------------------------
# 2. Attempt apt install from Armbian / BSP repos
# -----------------------------------------------------------------------------
if [[ "$RKMPP_PRESENT" == "false" ]]; then
    step "2/6  Attempting apt install of Rockchip BSP GStreamer packages"

    # Armbian ships BSP packages for RK3588 boards; the package name varies
    # across releases.  We try the most common names in order.
    BSP_PACKAGES=(
        gstreamer1.0-rockchip1          # Armbian 24.x BSP name
        gstreamer1.0-rockchip           # older naming
        rockchip-multimedia-config      # pulls in librockchip_mpp + gst plugin
    )

    apt-get update -y 2>/dev/null || true

    INSTALLED_APT=false
    for pkg in "${BSP_PACKAGES[@]}"; do
        info "Trying: apt-get install -y $pkg"
        if apt-get install -y --no-install-recommends "$pkg" 2>/dev/null; then
            ok "Installed $pkg via apt"
            INSTALLED_APT=true
            break
        else
            warn "$pkg not available in current apt sources"
        fi
    done

    # ----- Add Armbian unstable / vendor BSP apt source if packages not found --
    if [[ "$INSTALLED_APT" == "false" ]]; then
        warn "BSP package not found in default sources."
        info "Adding Armbian vendor/BSP source and retrying …"

        ARMBIAN_VENDOR_LIST="/etc/apt/sources.list.d/armbian-vendor-bsp.list"

        if [[ ! -f "$ARMBIAN_VENDOR_LIST" ]]; then
            OS_CODENAME=$(lsb_release -cs)
            cat > "$ARMBIAN_VENDOR_LIST" << EOF
# Armbian BSP / vendor packages (RK3588 hardware codecs)
# This source provides rockchip-multimedia-config and the gst-rkmpp plugin.
deb [signed-by=/usr/share/keyrings/armbian.gpg] http://apt.armbian.com $OS_CODENAME main ${OS_CODENAME}-utils ${OS_CODENAME}-desktop
EOF
            # Fetch the Armbian GPG key if it doesn't already exist
            if [[ ! -f /usr/share/keyrings/armbian.gpg ]]; then
                if curl -fsSL "https://apt.armbian.com/armbian.key" | \
                       gpg --dearmor --yes -o /usr/share/keyrings/armbian.gpg 2>/dev/null; then
                    ok "Armbian GPG key imported"
                else
                    warn "Could not fetch Armbian GPG key – apt source may not work without it"
                    rm -f "$ARMBIAN_VENDOR_LIST"
                fi
            else
                ok "Armbian GPG key already exists"
            fi
        fi

        apt-get update -y 2>/dev/null || true

        for pkg in "${BSP_PACKAGES[@]}"; do
            if apt-get install -y --no-install-recommends "$pkg" 2>/dev/null; then
                ok "Installed $pkg from Armbian vendor repo"
                INSTALLED_APT=true
                break
            fi
        done
    fi

    # ----- Build from source as last resort -------------------------------------
    if [[ "$INSTALLED_APT" == "false" ]]; then
        warn "apt install failed for all BSP packages."
        step "2b/6  Building gst-rkmpp from source (github.com/rockchip-linux/gst-rkmpp)"

        # Build dependencies
        BUILD_DEPS=(
            librockchip-mpp-dev
            librockchip-mpp1
            libgstreamer1.0-dev
            libgstreamer-plugins-base1.0-dev
            meson
            ninja-build
            pkg-config
        )
        info "Installing build dependencies …"
        apt-get install -y --no-install-recommends "${BUILD_DEPS[@]}" || {
            err "Could not install build dependencies.  librockchip-mpp-dev may not be"
            err "available without the Armbian BSP repo.  Please check:"
            err "  https://github.com/armbian/build or your OPi5+ vendor image notes."
            err "Continuing with validation – mppvideodec will likely FAIL."
        }

        BUILD_DIR="/tmp/gst-rkmpp-build"
        rm -rf "$BUILD_DIR"
        mkdir -p "$BUILD_DIR"

        info "Cloning gst-rkmpp …"
        if git clone --depth=1 \
               "https://github.com/rockchip-linux/gst-rkmpp.git" \
               "$BUILD_DIR" 2>&1 | tail -5; then
            (
                cd "$BUILD_DIR"
                info "Configuring with meson …"
                meson setup build --prefix=/usr --libdir=lib/aarch64-linux-gnu
                info "Building …"
                ninja -C build
                info "Installing …"
                ninja -C build install
                ok "gst-rkmpp installed from source"
            )
        else
            err "git clone failed – network may not be available or repo moved."
            err "Manual build instructions:"
            err "  git clone https://github.com/rockchip-linux/gst-rkmpp.git"
            err "  cd gst-rkmpp && meson setup build --prefix=/usr && ninja -C build install"
        fi

        rm -rf "$BUILD_DIR"
    fi

    # Refresh plugin cache
    info "Refreshing GStreamer plugin registry …"
    gst-inspect-1.0 --print-all &>/dev/null || true
    ok "Plugin registry refreshed"
fi

# -----------------------------------------------------------------------------
# 3. SRT support check / fix
# -----------------------------------------------------------------------------
step "3/6  Checking SRT (Secure Reliable Transport) support"

if gst-inspect-1.0 srtsink &>/dev/null; then
    ok "srtsink found – SRT support present"
else
    warn "srtsink not found.  Attempting to install libsrt and rebuild gst-plugins-bad …"

    apt-get install -y --no-install-recommends \
        libsrt-dev \
        libsrt1.5-gnutls \
        2>/dev/null || apt-get install -y --no-install-recommends libsrt-dev 2>/dev/null || true

    # If gstreamer was built without SRT we need to rebuild gst-plugins-bad.
    # For most Armbian installs the packaged version already includes SRT;
    # if not, print instructions rather than silently failing a long build.
    if ! gst-inspect-1.0 srtsink &>/dev/null; then
        warn "srtsink still not found after installing libsrt-dev."
        warn "The packaged gst-plugins-bad was built without SRT support."
        warn "To add SRT support, rebuild gst-plugins-bad:"
        warn ""
        warn "  apt-get source gstreamer1.0-plugins-bad"
        warn "  cd gstreamer1.0-plugins-bad-*"
        warn "  apt-get build-dep gstreamer1.0-plugins-bad"
        warn "  dpkg-buildpackage -b -uc -us"
        warn "  dpkg -i ../gstreamer1.0-plugins-bad_*.deb"
        warn ""
        warn "Alternatively, use rtmpsink or udpsink for streaming output."
    fi
fi

# -----------------------------------------------------------------------------
# 4. RTMP support check
# -----------------------------------------------------------------------------
step "4/6  Checking RTMP support"

if gst-inspect-1.0 rtmpsink &>/dev/null; then
    ok "rtmpsink found"
else
    warn "rtmpsink not found – attempting gstreamer1.0-plugins-ugly reinstall"
    apt-get install -y --no-install-recommends gstreamer1.0-plugins-ugly 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# 4b. Build the fpvlut3d HDMI 3D-LUT element
# -----------------------------------------------------------------------------
step "4b/6  Building fpvlut3d (HDMI 3D LUT) GStreamer element"

# The .cube 3D-LUT colour grade for HDMI has no stock element on this target, so
# FPVLink ships its own tiny native one (capture/fpvlut3d.c). Needs the -base and
# -video dev headers; a plain gcc build (no meson) then produces the .so that
# capture/pipeline.py loads via GST_PLUGIN_PATH.
apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    2>/dev/null || warn "Could not install gst dev headers — fpvlut3d build may fail"

if bash "$(dirname "$0")/build-lut-plugin.sh"; then
    RESULTS["HDMI 3D LUT (fpvlut3d)"]="PASS"
else
    RESULTS["HDMI 3D LUT (fpvlut3d)"]="FAIL"
    warn "fpvlut3d build failed — the HDMI LUT toggle will safely no-op until fixed."
fi

# -----------------------------------------------------------------------------
# 5. Run full validation suite
# -----------------------------------------------------------------------------
step "5/6  Running GStreamer plugin validation"

gst_check "mppvideodec"  "H.265 decode (mppvideodec)"
gst_check "mppvideoenc"  "H.264 encode (mppvideoenc)"
gst_check "srtsink"      "SRT output (srtsink)"
gst_check "rtmpsink"     "RTMP output (rtmpsink)"

# Bonus checks – nice to have
gst_check "v4l2src"      "V4L2 camera input (v4l2src)"
gst_check "udpsink"      "UDP output (udpsink) [fallback]"
gst_check "rtspclientsink" "RTSP client sink"
gst_check "h264parse"    "H.264 parser (h264parse)"
gst_check "h265parse"    "H.265 parser (h265parse)"

# -----------------------------------------------------------------------------
# 6. Print summary table
# -----------------------------------------------------------------------------
step "6/6  Validation summary"

PAD=40
echo ""
echo -e "  ${BOLD}Plugin / Feature                        Result${NC}"
echo -e "  ──────────────────────────────────────  ──────"

PASS_COUNT=0
FAIL_COUNT=0

for label in "${!RESULTS[@]}"; do
    result="${RESULTS[$label]}"
    # Pad label to PAD chars
    printf "  %-${PAD}s" "$label"
    if [[ "$result" == "PASS" ]]; then
        echo -e "${GREEN}PASS ✓${NC}"
        (( PASS_COUNT++ ))
    else
        echo -e "${RED}FAIL ✗${NC}"
        (( FAIL_COUNT++ ))
    fi
done | sort   # sort for consistent ordering

echo ""
echo -e "  Total: ${GREEN}${PASS_COUNT} passed${NC}  /  ${RED}${FAIL_COUNT} failed${NC}"

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
echo ""
if [[ $FAIL_COUNT -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║   FPVLink GStreamer Setup Complete – All checks passed ✓ ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${YELLOW}║   FPVLink GStreamer Setup Complete – some checks FAILED  ║${NC}"
    echo -e "${BOLD}${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${YELLOW}Review the FAIL entries above and consult:${NC}"
    echo -e "    • https://github.com/rockchip-linux/gst-rkmpp"
    echo -e "    • https://github.com/rockchip-linux/mpp"
    echo -e "    • Armbian forum: https://forum.armbian.com"
fi

echo ""
echo -e "  ${BOLD}Next step:${NC}  Install the FPVLink systemd service:"
echo -e "    ${CYAN}sudo bash 04-service.sh${NC}"
echo ""
