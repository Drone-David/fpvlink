#!/usr/bin/env bash
# =============================================================================
# FPVLink – 01-system.sh
# System bootstrap for Armbian Bookworm (arm64) on Orange Pi 5 Plus (RK3588)
#
# Run as root on a freshly flashed system:
#   sudo bash 01-system.sh
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
NC='\033[0m' # No Colour

ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
err()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }
step() { echo -e "\n${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; \
         echo -e "${BOLD}${YELLOW}  $*${NC}"; \
         echo -e "${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; }

# -----------------------------------------------------------------------------
# Root check
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root.  Try: sudo bash $0"
    exit 1
fi

step "FPVLink System Setup – Orange Pi 5 Plus / RK3588"
info "Armbian Bookworm (arm64) assumed."
info "Script started at $(date '+%Y-%m-%d %H:%M:%S %Z')"

# -----------------------------------------------------------------------------
# 1. Update & upgrade
# -----------------------------------------------------------------------------
step "1/5  Updating package lists and upgrading system"

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
ok "apt-get update complete"

apt-get upgrade -y \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold"
ok "System packages upgraded"

# -----------------------------------------------------------------------------
# 2. Install required packages
# -----------------------------------------------------------------------------
step "2/5  Installing required packages"

PACKAGES=(
    # Python runtime & tools
    python3
    python3-pip
    python3-venv
    python3-usb

    # USB / libusb
    libusb-1.0-0-dev

    # GStreamer core + plugin collections
    gstreamer1.0-tools
    gstreamer1.0-plugins-base
    gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad
    gstreamer1.0-plugins-ugly
    gstreamer1.0-python3-plugin-loader
    python3-gst-1.0

    # Node.js / npm  (for the web UI / control plane)
    nodejs
    npm

    # Media / video tools
    ffmpeg
    v4l-utils

    # USB inspection & tracing
    usbutils

    # General build & utilities
    curl
    git
    build-essential
)

info "Installing: ${PACKAGES[*]}"
apt-get install -y --no-install-recommends "${PACKAGES[@]}"
ok "All packages installed successfully"

# -----------------------------------------------------------------------------
# 3. Enable kernel modules at boot
# -----------------------------------------------------------------------------
step "3/5  Enabling kernel modules"

MODULES_FILE="/etc/modules"
MODULES_TO_ADD=(libcomposite dwc3 usbmon)

for mod in "${MODULES_TO_ADD[@]}"; do
    if grep -qx "$mod" "$MODULES_FILE" 2>/dev/null; then
        info "Module '$mod' already in $MODULES_FILE – skipping"
    else
        echo "$mod" >> "$MODULES_FILE"
        ok "Added '$mod' to $MODULES_FILE"
    fi

    # Load immediately (best-effort – not fatal if hardware not present yet)
    if modprobe "$mod" 2>/dev/null; then
        ok "modprobe $mod – loaded"
    else
        info "modprobe $mod – not loaded now (will load at boot, or hardware may not be present yet)"
    fi
done

# -----------------------------------------------------------------------------
# 4. Install Python packages via pip
# -----------------------------------------------------------------------------
step "4/5  Installing Python packages via pip"

# On Armbian Bookworm, pip may be externally managed; use --break-system-packages
# or a venv.  We use --break-system-packages here for simplicity in a dedicated
# embedded device context where the OS Python IS the app Python.
PY_PACKAGES=(pyusb construct)

info "Installing Python packages: ${PY_PACKAGES[*]}"
pip3 install --break-system-packages --no-cache-dir "${PY_PACKAGES[@]}"
ok "Python packages installed: ${PY_PACKAGES[*]}"

# -----------------------------------------------------------------------------
# 5. Persist the journal (needed for field troubleshooting)
#
# Default Storage=volatile wipes all logs on every reboot, including the
# always-on pipeline's own cold-boot warm-restart. A field device with no
# internet relies on "Download Diagnostics" in the dashboard to explain what
# went wrong after the fact — that's useless if the journal never survives a
# reboot. See system/journald-fpvlink.conf for the full rationale.
# -----------------------------------------------------------------------------
step "5/5  Enabling persistent journal storage"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOURNALD_TEMPLATE="${SCRIPT_DIR}/../system/journald-fpvlink.conf"
JOURNALD_DROPIN_DIR="/etc/systemd/journald.conf.d"
JOURNALD_DROPIN="${JOURNALD_DROPIN_DIR}/fpvlink.conf"

if [[ -f "$JOURNALD_TEMPLATE" ]]; then
    mkdir -p "$JOURNALD_DROPIN_DIR"
    cp "$JOURNALD_TEMPLATE" "$JOURNALD_DROPIN"
    ok "Installed $JOURNALD_DROPIN"

    mkdir -p /var/log/journal
    systemd-tmpfiles --create --prefix /var/log/journal || true
    chown root:systemd-journal /var/log/journal
    chmod 2755 /var/log/journal
    ok "Created persistent journal directory /var/log/journal"

    systemctl restart systemd-journald
    ok "systemd-journald restarted with persistent storage"

    # The restart alone doesn't reliably create /var/log/journal/<machine-id>/
    # on every system (observed on Armbian: it stayed empty until forced) —
    # --flush explicitly migrates volatile (/run) entries into persistent
    # storage and makes journald start writing there going forward.
    journalctl --flush
    ok "Flushed journal into persistent storage"
else
    warn "Template $JOURNALD_TEMPLATE not found – skipping persistent journal setup"
fi
info "Note: the fpvlink user isn't created yet at this stage (04-service.sh does"
info "that) — it's added to the systemd-journal group there so it can read logs."

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║        FPVLink – System Setup Complete ✓                 ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Next step:${NC}  Run the USB-OTG configuration script:"
echo ""
echo -e "    ${CYAN}sudo bash 02-usb-otg.sh${NC}"
echo ""
echo -e "  ${YELLOW}Note:${NC} A reboot will be required after 02-usb-otg.sh."
echo ""
