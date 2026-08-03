#!/usr/bin/env bash
# =============================================================================
# FPVLink – 06-filesystem.sh
# Make the SD card survive an unclean shutdown.
#
# Written after a real incident (2026-08-03): the dashboard was completely dead
# — no tab switching, no WebSocket, no preview. The cause was
# web/js/monitor.js being 19707 bytes of pure NUL. app.js imports it, and one
# unparseable module kills the whole ES module graph, so a single zeroed file
# took out every interactive part of the UI at once.
#
# The file was not corrupted by anything FPVLink did. Armbian ships this image
# with two settings that together make silent zero-fill the EXPECTED outcome of
# a power loss:
#
#   * The ext4 superblock carries "Default mount options: journal_data_writeback".
#     That is why the root filesystem mounted in writeback mode even though
#     /etc/fstab only said "defaults". In writeback mode ext4 will commit the
#     metadata for a file (its new length) without ordering the data blocks
#     ahead of it — so a crash can leave a full-length file full of zeros.
#   * /etc/fstab had commit=120, stretching the window in which that can happen
#     to two minutes.
#
# On a box that is powered off by pulling the plug — which is exactly how a
# piece of field video gear gets shut down — that combination loses files.
#
# This script switches the filesystem to data=ordered (the ext4 default, where
# data blocks are flushed before the metadata that references them) and drops
# commit=120 back to the 5s default.
#
# Cost: slightly more write traffic to the card. That is the correct trade —
# the previous setting was trading data integrity for card wear.
#
# Run as root on the device:
#   sudo bash 06-filesystem.sh
#
# A REBOOT IS REQUIRED. ext4 cannot change data mode on a remount, so this
# takes effect only on the next boot.
#
# Idempotent: safe to re-run.
# =============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
err()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
info() { echo -e "${CYAN}[ INFO ]${NC} $*"; }
step() { echo -e "\n${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; \
         echo -e "${BOLD}${YELLOW}  $*${NC}"; \
         echo -e "${BOLD}${YELLOW}──────────────────────────────────────────${NC}"; }

if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root.  Try: sudo bash $0"
    exit 1
fi

step "FPVLink Filesystem Durability – data=ordered, default commit interval"

ROOT_DEV="$(findmnt -no SOURCE /)"
if [[ -z "$ROOT_DEV" ]]; then
    err "Could not determine the root device."
    exit 1
fi
info "Root device: $ROOT_DEV"

# -----------------------------------------------------------------------------
# 1. Clear journal_data_writeback from the SUPERBLOCK
#
# This is deliberately done in the superblock rather than by adding data=ordered
# to /etc/fstab. The kernel performs the initial root mount using the superblock
# defaults; systemd-remount-fs then remounts using the fstab options. ext4
# REFUSES to change data mode on a remount, so an fstab that disagrees with the
# superblock makes that remount fail and can leave / read-only at boot.
# Fixing the superblock means the very first mount is already correct and fstab
# never has to argue with it.
# -----------------------------------------------------------------------------
step "1/3  Clearing journal_data_writeback from the superblock"

if tune2fs -l "$ROOT_DEV" | grep -q "journal_data_writeback"; then
    tune2fs -o ^journal_data_writeback "$ROOT_DEV"
    ok "Cleared — filesystem will mount data=ordered from next boot"
else
    ok "Already clear (no journal_data_writeback in superblock)"
fi
info "Default mount options now: $(tune2fs -l "$ROOT_DEV" | sed -n 's/^Default mount options:[[:space:]]*//p')"

# -----------------------------------------------------------------------------
# 2. Drop commit=120 from /etc/fstab
# -----------------------------------------------------------------------------
step "2/3  Removing commit=120 from /etc/fstab"

if grep -q "commit=120" /etc/fstab; then
    [[ -f /etc/fstab.bak-fpvlink ]] || cp /etc/fstab /etc/fstab.bak-fpvlink
    # Remove the option in whichever position it appears, without disturbing
    # the rest of the option list.
    sed -i -E 's/,commit=120//; s/commit=120,//; s/(\s)commit=120(\s)/\1defaults\2/' /etc/fstab
    ok "Removed (backup: /etc/fstab.bak-fpvlink) — commit interval back to 5s"
else
    ok "No commit=120 in /etc/fstab"
fi

# A malformed fstab means the box does not boot. Never leave without checking.
if findmnt --verify >/dev/null 2>&1; then
    ok "fstab verifies clean"
else
    err "fstab FAILED verification — restoring backup and aborting."
    [[ -f /etc/fstab.bak-fpvlink ]] && cp /etc/fstab.bak-fpvlink /etc/fstab
    findmnt --verify 2>&1 | tail -10
    exit 1
fi
systemctl daemon-reload

# -----------------------------------------------------------------------------
# 3. Report
# -----------------------------------------------------------------------------
step "3/3  Status"

CURRENT_MODE="$(dmesg 2>/dev/null | grep -o "mounted filesystem with [a-z]* data mode" | tail -1)"
info "This boot mounted with: ${CURRENT_MODE:-unknown}"
info "Current options: $(findmnt -no OPTIONS /)"

if [[ "$CURRENT_MODE" == *writeback* ]]; then
    warn "Still running in writeback mode — the change applies on the NEXT BOOT."
    warn "Reboot when convenient, then confirm with:"
    warn "  dmesg | grep 'EXT4-fs (.*): mounted filesystem'"
    warn "It must say 'ordered data mode'."
else
    ok "Running in ordered data mode."
fi

echo
info "This fixes FUTURE power losses. It does not repair files already zeroed by"
info "a past one — scan for those with:"
# Printed with a quoted heredoc so no shell or echo escape processing can mangle
# it; the reader must be able to paste this verbatim. (The device has no 'file'
# or 'xxd', so python3 is the reliable way to inspect bytes here.)
cat <<'SCAN'
    python3 - <<'EOF'
    import os
    for root, dirs, files in os.walk("/opt/fpvlink"):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", "scratch")]
        for fn in files:
            p = os.path.join(root, fn)
            try:
                if os.path.getsize(p) and not any(open(p, "rb").read()):
                    print("ZEROED:", p)
            except OSError:
                pass
    EOF
SCAN
