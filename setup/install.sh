#!/usr/bin/env bash
# =============================================================================
# FPVLink – install.sh
# Runs the whole setup in order, so you do not have to drive seven scripts
# by hand.
#
#   sudo ./setup/install.sh      # run this, reboot when told, run it again
#
# It is a driver, not a replacement: every step below is one of the numbered
# scripts in this directory, run exactly as it would be run by hand. Anything
# it cannot do safely (the reboot) it stops and asks for.
#
# WHY TWO RUNS AND NOT ONE
#   02-usb-otg.sh and 06-filesystem.sh both need a reboot to take effect, so
#   they are front-loaded into the first run and share a single reboot between
#   them (by hand it is two). Your SSH session dies on that reboot regardless,
#   so re-running this command afterwards costs nothing over reconnecting, and
#   it beats a boot-time service that fails where you cannot see it.
#
# PASSWORDS
#   This script never handles them. 04-service.sh and 07-wifi-ap.sh prompt for
#   their own, exactly as they do when run by hand, so nothing secret is
#   written to the state file. For an unattended run, set FPVLINK_PASSWORD and
#   FPVLINK_AP_PASSPHRASE and they are inherited by those scripts untouched:
#
#     sudo FPVLINK_PASSWORD='...' FPVLINK_HOSTNAME=fpvlink ./setup/install.sh
#
# RESUMING
#   Completed steps are recorded in /var/lib/fpvlink/install-state. Re-running
#   skips them, so if a step fails you fix what it complained about and run the
#   same command again — it picks up where it stopped. --reset starts over.
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Colour helpers (matching the numbered scripts)
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
err()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }
step() { echo -e "\n${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; \
         echo -e "${BOLD}${YELLOW}  $*${NC}"; \
         echo -e "${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="/var/lib/fpvlink"
STATE_FILE="${STATE_DIR}/install-state"

# The TP-Link Archer T3U Nano, as identified by 07-wifi-ap.sh.
AP_USB_ID="2357:012e"

# Steps that must happen before the reboot. 06 only needs tune2fs, so it does
# not depend on 01 and is safe to pull forward here — which is what lets both
# reboot-requiring steps share one reboot.
PHASE1_STEPS=("01-system.sh" "02-usb-otg.sh" "06-filesystem.sh")
PHASE2_STEPS=("03-gstreamer.sh" "04-service.sh" "05-network.sh")

WITH_AP="auto"   # auto | yes | no
ASSUME_YES="no"

# -----------------------------------------------------------------------------
# Arguments
# -----------------------------------------------------------------------------
usage() {
    cat <<EOF
FPVLink installer — runs the numbered setup scripts in order.

  sudo ./setup/install.sh            Run the next unfinished stage
  sudo ./setup/install.sh --status   Show what is done and what is left
  sudo ./setup/install.sh --reset    Forget progress and start from the top

Options:
  --with-ap      Always run 07-wifi-ap.sh (field WiFi access point)
  --skip-ap      Never run it
                 Default: run it only if the adapter (${AP_USB_ID}) is plugged in
  --yes          Do not ask before rebooting between stages
  -h, --help     This text

Environment passed through to the scripts that read it:
  FPVLINK_PASSWORD, FPVLINK_HOSTNAME, FPVLINK_AP_SSID,
  FPVLINK_AP_CHANNEL, FPVLINK_AP_PASSPHRASE
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)   SHOW_STATUS="yes"; shift ;;
        --reset)    DO_RESET="yes"; shift ;;
        --with-ap)  WITH_AP="yes"; shift ;;
        --skip-ap)  WITH_AP="no"; shift ;;
        --yes|-y)   ASSUME_YES="yes"; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          err "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
done

# -----------------------------------------------------------------------------
# Root check
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root.  Try: sudo $0"
    exit 1
fi

# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------
mkdir -p "$STATE_DIR"
touch "$STATE_FILE"
chmod 600 "$STATE_FILE"

is_done()   { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
mark_done() { is_done "$1" || echo "$1" >> "$STATE_FILE"; }

# When stage 1 finished, as epoch seconds.
phase1_at() { sed -n 's/^phase1_at=//p' "$STATE_FILE" 2>/dev/null | tail -1; }

# When this kernel booted, as epoch seconds.
boot_at() {
    if [[ -r /proc/stat ]] && awk '/^btime/{print $2; found=1} END{exit !found}' /proc/stat 2>/dev/null; then
        return 0
    fi
    # Fallback for anything without /proc/stat
    date -d "$(uptime -s)" +%s 2>/dev/null || echo 0
}

# Has the box rebooted since stage 1 finished? Asking the kernel beats
# trusting a flag we wrote ourselves: answering "no" to the reboot prompt and
# re-running must not let stage 2 start, because 03-gstreamer.sh needs the USB
# device mode that only comes up on a fresh boot.
has_rebooted() {
    local p b
    p="$(phase1_at)"
    [[ -n "$p" ]] || return 1
    b="$(boot_at)"
    [[ -n "$b" && "$b" -gt "$p" ]]
}

if [[ "${DO_RESET:-no}" == "yes" ]]; then
    : > "$STATE_FILE"
    ok "Progress cleared. The next run starts from 01-system.sh."
    info "Nothing already installed on this box was undone — the scripts are"
    info "safe to re-run, so this only makes the installer walk them again."
    exit 0
fi

# Decide about the access point once, so --status and the run agree.
ap_wanted() {
    case "$WITH_AP" in
        yes) return 0 ;;
        no)  return 1 ;;
        *)   lsusb -d "$AP_USB_ID" >/dev/null 2>&1 ;;
    esac
}

if [[ "${SHOW_STATUS:-no}" == "yes" ]]; then
    step "FPVLink setup status"
    for s in "${PHASE1_STEPS[@]}" "${PHASE2_STEPS[@]}"; do
        if is_done "$s"; then ok "$s"; else info "$s — not yet run"; fi
    done
    if ap_wanted; then
        if is_done "07-wifi-ap.sh"; then ok "07-wifi-ap.sh"; else info "07-wifi-ap.sh — not yet run"; fi
    else
        info "07-wifi-ap.sh — skipped (no ${AP_USB_ID} adapter present)"
    fi
    if ! is_done "phase1"; then
        info "reboot between stages — not reached yet"
    elif has_rebooted; then
        ok "reboot between stages"
    else
        warn "reboot between stages — STILL NEEDED before stage 2 can run"
    fi
    exit 0
fi

# -----------------------------------------------------------------------------
# Running a step
# -----------------------------------------------------------------------------
run_step() {
    local script="$1"
    local path="${SCRIPT_DIR}/${script}"

    if is_done "$script"; then
        info "${script} already done — skipping"
        return 0
    fi
    if [[ ! -f "$path" ]]; then
        err "Missing ${path}. Are you running this from inside the project?"
        exit 1
    fi

    step "Running ${script}"
    # Children inherit this environment, so FPVLINK_* set on the sudo line
    # reaches them, and 04/07 prompt on this terminal when it is not.
    if bash "$path"; then
        mark_done "$script"
        ok "${script} finished"
    else
        local rc=$?
        err "${script} failed (exit ${rc})."
        echo
        info "Nothing after this point has run. Fix what it reported above, then"
        info "run the same command again — finished steps are skipped, so it"
        info "resumes at ${script}."
        exit "$rc"
    fi
}

# -----------------------------------------------------------------------------
# Stage 1 — everything that needs the reboot
# -----------------------------------------------------------------------------
if ! is_done "phase1"; then
    step "FPVLink setup · stage 1 of 2"
    info "Installing packages, putting the USB-C port into device mode, and"
    info "protecting the SD card. Roughly 10 minutes, mostly apt."
    echo
    info "These three all need a reboot to take effect, so they are done"
    info "together and share one reboot."

    for s in "${PHASE1_STEPS[@]}"; do
        run_step "$s"
    done

    echo "phase1_at=$(date +%s)" >> "$STATE_FILE"
    mark_done "phase1"

    step "Stage 1 done — reboot required"
    echo -e "  ${BOLD}A reboot is needed now.${NC} The USB-C device mode and the SD-card"
    echo -e "  filesystem change cannot take effect without one."
    echo
    echo -e "  Afterwards, reconnect and run the ${BOLD}same command again${NC}:"
    echo
    echo -e "      ${BOLD}sudo ./setup/install.sh${NC}"
    echo
    echo -e "  It will pick up at stage 2. Your SSH session will drop on reboot —"
    echo -e "  that is expected. Wait a minute, then reconnect."
    echo

    if [[ "$ASSUME_YES" == "yes" ]]; then
        info "Rebooting now (--yes)."
        sleep 3
        reboot
        exit 0
    fi

    REPLY=""
    read -r -p "  Reboot now? [y/N] " REPLY || REPLY="n"
    case "$REPLY" in
        [yY]|[yY][eE][sS])
            info "Rebooting."
            sleep 2
            reboot
            ;;
        *)
            info "Not rebooting. Run 'sudo reboot' when you are ready, then"
            info "run this script again."
            ;;
    esac
    exit 0
fi

# -----------------------------------------------------------------------------
# Stage 2 — everything after the reboot
# -----------------------------------------------------------------------------
if ! has_rebooted; then
    step "Reboot still needed"
    err "Stage 1 is done, but this box has not rebooted since."
    echo
    info "03-gstreamer.sh needs the USB device mode from 02-usb-otg.sh, which"
    info "only comes up on a fresh boot, so stage 2 will not start yet."
    echo
    echo -e "      ${BOLD}sudo reboot${NC}"
    echo
    info "Then reconnect and run this script again."
    exit 1
fi

step "FPVLink setup · stage 2 of 2"
info "Hardware codecs, the FPVLink service, and the network name."
info "03-gstreamer.sh builds the LUT plugin and takes a while."
echo
info "04-service.sh will ask you for a dashboard password."

for s in "${PHASE2_STEPS[@]}"; do
    run_step "$s"
done

# -----------------------------------------------------------------------------
# Optional: field access point
# -----------------------------------------------------------------------------
if ap_wanted; then
    info "TP-Link Archer T3U Nano detected — setting up the field access point."
    info "07-wifi-ap.sh will ask you for a WiFi passphrase."
    run_step "07-wifi-ap.sh"
    AP_RAN="yes"
else
    info "No ${AP_USB_ID} adapter present — skipping 07-wifi-ap.sh."
    info "Plug the adapter in and run 'sudo ./setup/07-wifi-ap.sh' to add it later."
    AP_RAN="no"
fi

mark_done "phase2"

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
HOSTNAME_SET="${FPVLINK_HOSTNAME:-fpvlink}"

step "FPVLink setup complete"
echo -e "  Dashboard:  ${BOLD}http://${HOSTNAME_SET}.local:8080${NC}"
echo -e "              (or the box's IP, if .local does not resolve for you)"
echo
echo -e "  The pipeline is always on: connect the goggles, power them up, and"
echo -e "  video appears on HDMI. There is no start button."
echo
if [[ "$AP_RAN" == "yes" ]]; then
    echo -e "  The access point needs a reboot to bind its driver cleanly:"
    echo -e "      ${BOLD}sudo reboot${NC}"
    echo
fi
info "Check anything with:  sudo ./setup/install.sh --status"
info "Individual scripts stay re-runnable on their own if you need to redo one."
