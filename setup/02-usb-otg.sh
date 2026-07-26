#!/usr/bin/env bash
# =============================================================================
# FPVLink – 02-usb-otg.sh
# Configure the USB-C OTG port on Orange Pi 5 Plus (RK3588) for
# peripheral / device mode so that DJI Goggles 2 (acting as USB host)
# can enumerate the board as a UVC / composite USB gadget.
#
# Run as root AFTER 01-system.sh:
#   sudo bash 02-usb-otg.sh
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

# -----------------------------------------------------------------------------
# Root check
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root.  Try: sudo bash $0"
    exit 1
fi

step "FPVLink USB-OTG Configuration – Orange Pi 5 Plus / RK3588"
info "Script started at $(date '+%Y-%m-%d %H:%M:%S %Z')"

ARMBIAN_ENV="/boot/armbianEnv.txt"
UDEV_RULES="/etc/udev/rules.d/99-fpvlink-usb.rules"

# -----------------------------------------------------------------------------
# 1. Auto-detect the USB OTG DT node
# -----------------------------------------------------------------------------
step "1/5  Detecting USB OTG controller DT node"

# RK3588 has two DWC3 USB controllers:
#   fc000000.usb  – USB2 OTG (USB-C port on OPi5+)
#   fcd00000.usb  – USB3 OTG (USB-C port on OPi5+, superspeed)
# The peripheral-mode UDC will typically appear as one of these.

KNOWN_NODES=(
    "fc000000.usb"    # RK3588 USB2 OTG (most common for gadget mode)
    "fcd00000.usb"    # RK3588 USB3 OTG
    "fe900000.usb"    # alternate node name seen on some BSP kernels
    "fd000000.usb"
)

DETECTED_NODE=""

# First, check debugfs entries (requires debugfs mounted)
if mountpoint -q /sys/kernel/debug 2>/dev/null; then
    for node in "${KNOWN_NODES[@]}"; do
        if [[ -d "/sys/kernel/debug/usb/${node}" ]]; then
            DETECTED_NODE="$node"
            ok "Found DT node in debugfs: $DETECTED_NODE"
            break
        fi
    done
fi

# Fall back: look for DWC3 in /sys/bus/platform/drivers/dwc3/
if [[ -z "$DETECTED_NODE" ]]; then
    for node in "${KNOWN_NODES[@]}"; do
        if [[ -e "/sys/bus/platform/drivers/dwc3/${node}" ]] || \
           [[ -e "/sys/bus/platform/devices/${node}" ]]; then
            DETECTED_NODE="$node"
            ok "Found DT node in platform bus: $DETECTED_NODE"
            break
        fi
    done
fi

# Last resort: use the first known node and warn
if [[ -z "$DETECTED_NODE" ]]; then
    DETECTED_NODE="${KNOWN_NODES[0]}"
    warn "Could not auto-detect DT node.  Defaulting to: $DETECTED_NODE"
    warn "Check /sys/bus/platform/devices/ or /sys/kernel/debug/usb/ manually."
fi

info "OTG DT node: $DETECTED_NODE"

# -----------------------------------------------------------------------------
# 2. Configure armbianEnv.txt for peripheral (device) mode
# -----------------------------------------------------------------------------
step "2/5  Configuring /boot/armbianEnv.txt"

if [[ ! -f "$ARMBIAN_ENV" ]]; then
    err "$ARMBIAN_ENV not found. Is this an Armbian system?"
    exit 1
fi

info "Current $ARMBIAN_ENV:"
cat "$ARMBIAN_ENV"
echo ""

# The Armbian / U-Boot mechanism to force dr_mode=peripheral for RK3588:
#   overlays= line can add an OTG overlay, OR
#   extraargs= passes kernel cmdline params.
#
# We prefer extraargs= because it works without a board-specific overlay.

EXTRA_ARG="dr_mode=peripheral"

if grep -q "extraargs=" "$ARMBIAN_ENV"; then
    # Append to existing extraargs line if not already there
    if grep -q "dr_mode=" "$ARMBIAN_ENV"; then
        info "dr_mode already set in extraargs – skipping"
    else
        # Use sed to append in-place
        sed -i "s/^extraargs=\(.*\)/extraargs=\1 ${EXTRA_ARG}/" "$ARMBIAN_ENV"
        ok "Appended '${EXTRA_ARG}' to existing extraargs line"
    fi
else
    # No extraargs line; add one
    echo "extraargs=${EXTRA_ARG}" >> "$ARMBIAN_ENV"
    ok "Added 'extraargs=${EXTRA_ARG}' to $ARMBIAN_ENV"
fi

# Also set the overlays for USB OTG peripheral if supported by the DTB
# Some Armbian builds for OPi5+ ship a dedicated overlay:
OVERLAY_NAME="rk3588-otg-peripheral"
if grep -q "^overlays=" "$ARMBIAN_ENV"; then
    if ! grep -q "$OVERLAY_NAME" "$ARMBIAN_ENV"; then
        sed -i "s/^overlays=\(.*\)/overlays=\1 ${OVERLAY_NAME}/" "$ARMBIAN_ENV"
        info "Added overlay '$OVERLAY_NAME' to overlays line (harmless if overlay file absent)"
    fi
fi

echo ""
info "Updated $ARMBIAN_ENV:"
cat "$ARMBIAN_ENV"

# -----------------------------------------------------------------------------
# 3. Create udev rules
# -----------------------------------------------------------------------------
step "3/5  Installing udev rules → $UDEV_RULES"

# Ensure the fpvlink user (created by 04-service.sh) can access USB devices
# and the UDC without requiring root at runtime.

cat > "$UDEV_RULES" << 'EOF'
# FPVLink USB access rules
# Applied by fpvlink group / user so the service doesn't need root.

# ── USB raw device access ─────────────────────────────────────────────────────
# Grant the 'plugdev' group read/write access to all USB devices.
SUBSYSTEM=="usb", MODE="0664", GROUP="plugdev"

# ── DJI device access (Goggles 2 VID:PID) ────────────────────────────────────
# DJI Goggles 2 USB host VID:PID – adjust if needed.
SUBSYSTEM=="usb", ATTRS{idVendor}=="2ca3", MODE="0666", GROUP="plugdev", TAG+="uaccess"

# ── UDC (USB Device Controller) access ───────────────────────────────────────
# Allow fpvlink group to write to the UDC sysfs node (gadget bind/unbind).
SUBSYSTEM=="udc",    MODE="0664", GROUP="plugdev"

# ── configfs gadget sysfs access ─────────────────────────────────────────────
SUBSYSTEM=="usb_gadget", MODE="0664", GROUP="plugdev"

# ── usbmon (packet capture / debugging) ──────────────────────────────────────
SUBSYSTEM=="usbmon", MODE="0640", GROUP="plugdev"

# ── Video4Linux (camera input) ────────────────────────────────────────────────
SUBSYSTEM=="video4linux", MODE="0664", GROUP="video"
EOF

ok "udev rules written to $UDEV_RULES"
udevadm control --reload-rules && ok "udev rules reloaded"

# -----------------------------------------------------------------------------
# 4. Mount configfs (best-effort; will be auto-mounted at boot via fstab)
# -----------------------------------------------------------------------------
step "4/5  Ensuring configfs is mounted"

CONFIGFS_MOUNT="/sys/kernel/config"

if mountpoint -q "$CONFIGFS_MOUNT" 2>/dev/null; then
    ok "configfs already mounted at $CONFIGFS_MOUNT"
else
    info "Mounting configfs at $CONFIGFS_MOUNT …"
    mount -t configfs none "$CONFIGFS_MOUNT" && ok "configfs mounted" \
        || warn "Could not mount configfs now – will be available after reboot with libcomposite loaded"
fi

# Persist configfs mount in /etc/fstab if not already present
if ! grep -q "configfs" /etc/fstab; then
    echo "none  /sys/kernel/config  configfs  defaults  0  0" >> /etc/fstab
    ok "Added configfs to /etc/fstab (will auto-mount at boot)"
else
    info "configfs already in /etc/fstab"
fi

# -----------------------------------------------------------------------------
# 5. Report UDC(s) currently visible in sysfs
# -----------------------------------------------------------------------------
step "5/5  Checking for UDC entries"

UDC_DIR="/sys/class/udc"

if [[ -d "$UDC_DIR" ]] && [[ -n "$(ls -A "$UDC_DIR" 2>/dev/null)" ]]; then
    ok "UDC device(s) found:"
    for udc in "$UDC_DIR"/*; do
        udc_name="$(basename "$udc")"
        echo -e "    ${GREEN}✓${NC}  $udc_name"
    done
else
    warn "No UDC entries found under $UDC_DIR right now."
    warn "This is normal before rebooting – the UDC will appear after reboot"
    warn "once libcomposite + dwc3 modules are loaded in peripheral mode."
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║      FPVLink USB-OTG Configuration Complete ✓            ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}${RED}⚠  REBOOT REQUIRED  ⚠${NC}"
echo ""
echo -e "  After rebooting you should see:"
echo -e "    • ${CYAN}/sys/class/udc/${DETECTED_NODE}${NC} (or similar) – UDC in peripheral mode"
echo -e "    • configfs mounted at ${CYAN}/sys/kernel/config${NC}"
echo -e "    • libcomposite, dwc3, usbmon all loaded (check with: ${CYAN}lsmod${NC})"
echo ""
echo -e "  Plug the USB-C cable from OPi5+ into the ${BOLD}DJI Goggles 2 USB-C port${NC}."
echo -e "  The goggles act as USB HOST – the board will enumerate as a device."
echo ""
echo -e "  ${BOLD}Next step (after reboot):${NC}  Run the GStreamer validation script:"
echo -e "    ${CYAN}sudo bash 03-gstreamer.sh${NC}"
echo ""
echo -e "  ${YELLOW}Rebooting in 10 seconds … press Ctrl-C to cancel.${NC}"
sleep 10
reboot
