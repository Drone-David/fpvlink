#!/usr/bin/env bash
# =============================================================================
# FPVLink – collect-diagnostics.sh
#
# Bundles logs and system state into a single tarball a field user can grab
# via the dashboard's "Download Diagnostics" button (web/server.js's
# GET /api/diagnostics) and hand to support — no internet or SSH required to
# generate it, only local LAN access to the dashboard.
#
# Deliberately resilient: no `set -e`. A device broken enough to need this
# bundle is exactly the device where some command here might fail (DRM node
# missing, gst-inspect not on PATH, etc.) — one failed step must not blank
# out everything else. Each collector redirects its own stderr into the output
# file so a failure shows up there instead of vanishing.
#
# Output contract: every progress message goes to STDERR. The single line on
# STDOUT is the final tarball path — that's what callers (web/server.js) parse.
#
# Usage: collect-diagnostics.sh [output-path.tar.gz]
# =============================================================================

set -uo pipefail

OUT_PATH="${1:-/tmp/fpvlink-diagnostics-$(date +%Y%m%d-%H%M%S).tar.gz}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d /tmp/fpvlink-diag.XXXXXX)"

info() { echo "[collect-diagnostics] $*" >&2; }

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

info "Working directory: $WORK_DIR"

# ── System info ──────────────────────────────────────────────────────────────
{
    echo "=== Generated ==="
    date -u +"%Y-%m-%dT%H:%M:%SZ"
    echo
    echo "=== fpvlink repo commit ==="
    git -C "$ROOT_DIR" rev-parse HEAD 2>&1 || echo "(not a git checkout or git unavailable)"
    echo
    echo "=== uname ==="
    uname -a 2>&1
    echo
    echo "=== Armbian release ==="
    cat /etc/armbian-release 2>&1 || echo "(not present)"
    echo
    echo "=== uptime ==="
    uptime 2>&1
    echo
    echo "=== memory ==="
    free -h 2>&1
    echo
    echo "=== disk ==="
    df -h / 2>&1
    echo
    echo "=== SoC temperature ==="
    cat /sys/class/thermal/thermal_zone0/temp 2>&1 || echo "(not present)"
} > "$WORK_DIR/system-info.txt" 2>&1
info "Collected system-info.txt"

# ── Service status ───────────────────────────────────────────────────────────
{
    echo "=== systemctl status: fpvlink.service ==="
    systemctl status fpvlink.service --no-pager -l 2>&1
    echo
    echo "=== systemctl status: fpvlink-pipeline.service ==="
    systemctl status fpvlink-pipeline.service --no-pager -l 2>&1
    echo
    echo "=== systemctl show (restart counts / timestamps) ==="
    systemctl show fpvlink.service fpvlink-pipeline.service \
        -p NRestarts -p ActiveState -p SubState \
        -p ActiveEnterTimestamp -p ExecMainStartTimestamp 2>&1
} > "$WORK_DIR/service-status.txt" 2>&1
info "Collected service-status.txt"

# ── DRM/display plane state (see fpvlink-video-path-facts: plane 194 must be
#    live on CRTC 89 for HDMI out to actually be reaching the panel) ─────────
if command -v modetest &>/dev/null; then
    modetest -M rockchip -p > "$WORK_DIR/drm-planes.txt" 2>&1
    info "Collected drm-planes.txt"
else
    echo "(modetest not on PATH)" > "$WORK_DIR/drm-planes.txt"
fi

# ── Journal — bounded to 7 days so the bundle can't grow unbounded even once
#    journald retention is long. Needs the fpvlink user in systemd-journal
#    group (setup/04-service.sh); if missing, journalctl fails loudly here
#    rather than silently, so the gap itself is visible in the bundle. ───────
journalctl -u fpvlink.service -u fpvlink-pipeline.service \
    --no-pager --since "7 days ago" \
    > "$WORK_DIR/journal.log" 2>&1
info "Collected journal.log ($(wc -l < "$WORK_DIR/journal.log" 2>/dev/null || echo 0) lines)"

# ── Config — with secrets redacted. This travels outside the device (emailed
#    to support), so stream keys / session secrets must never be in it even
#    though the dashboard's own /api/config already returns them unredacted
#    on the local LAN (existing, unrelated trust boundary — not one to widen
#    by putting the same values in a bundle meant to leave the device). ─────
CONFIG_PATH="${FPVLINK_CONFIG:-$ROOT_DIR/system/config.json}"
if [[ -f "$CONFIG_PATH" ]]; then
    # Recursive by design, not a hardcoded path list: config.json has both a
    # flat legacy schema (rtmp_key) and a nested one (outputs.rtmp.stream_key,
    # web.session_secret) — a fixed allowlist of paths silently misses whatever
    # it wasn't updated for. Any key at any depth whose name contains
    # key/secret/password/token gets redacted, so a future field is covered
    # without this script needing to know its exact location.
    python3 -c "
import json, sys

CONFIG_PATH = sys.argv[1]
REDACT_MARKERS = ('key', 'secret', 'password', 'token')

def redact(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                redact(v)
            elif v and any(m in k.lower() for m in REDACT_MARKERS):
                obj[k] = '<redacted>'
    elif isinstance(obj, list):
        for item in obj:
            redact(item)

with open(CONFIG_PATH) as f:
    cfg = json.load(f)
redact(cfg)
json.dump(cfg, sys.stdout, indent=2)
" "$CONFIG_PATH" > "$WORK_DIR/config.json" 2>"$WORK_DIR/config-redact-error.txt"
    if [[ ! -s "$WORK_DIR/config.json" ]]; then
        echo "(config.json redaction failed — see config-redact-error.txt)" > "$WORK_DIR/config.json"
    else
        rm -f "$WORK_DIR/config-redact-error.txt"
    fi
    info "Collected config.json (secrets redacted)"
else
    echo "(config.json not found at $CONFIG_PATH)" > "$WORK_DIR/config.json"
fi

# Note what's intentionally excluded, so support knows to ask rather than
# assume the bundle is exhaustive.
cat > "$WORK_DIR/README.txt" <<EOF
FPVLink diagnostics bundle
Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

Contents:
  system-info.txt   uname, uptime, memory, disk, SoC temp
  service-status.txt  systemctl status/show for both fpvlink services
  drm-planes.txt    modetest output (HDMI display plane state)
  journal.log       journalctl for fpvlink.service + fpvlink-pipeline.service, last 7 days
  config.json       system/config.json with stream keys/secrets redacted

Deliberately NOT included:
  system/fpvlink.env  may hold real stream keys / session secret
EOF

# ── Package ───────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$OUT_PATH")"
if tar -czf "$OUT_PATH" -C "$WORK_DIR" . ; then
    info "Wrote $OUT_PATH ($(du -h "$OUT_PATH" | cut -f1))"
    echo "$OUT_PATH"
    exit 0
else
    info "tar failed"
    exit 1
fi
