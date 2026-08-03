#!/usr/bin/env bash
# =============================================================================
# FPVLink – fpvlink-ap-guard.sh
# Decide whether the field access point should start on this boot.
#
# The rule: the AP exists for the field, where there is no cable. On the bench
# there IS a cable, and the box is already reachable at fpvlink.local over it,
# so beaconing as well is pointless noise on a band the goggles may care about.
#
#   Ethernet has carrier  ->  bench   ->  skip the AP
#   No Ethernet carrier   ->  field   ->  start the AP
#
# Run as systemd ExecCondition=, whose exit codes are NOT the usual ones:
#
#   0        start the unit
#   1..254   skip the unit cleanly — recorded as skipped, NOT as a failure
#   255      genuine error (this script never returns it deliberately)
#
# That distinction is the whole reason this is an ExecCondition and not an
# ExecStartPre: on the bench, "do not start" is the correct outcome, and it
# should not leave a failed unit sitting in systemctl --failed forever.
#
# Can be run by hand to see what it would decide:
#   /opt/fpvlink/scripts/fpvlink-ap-guard.sh; echo "exit=$?"
# =============================================================================

set -u

WLAN_IF="${FPVLINK_AP_IF:-wlan0}"

# The service port is excluded from the "is there Ethernet" test on purpose.
# It is a point-to-point management port for a directly-cabled laptop, not an
# uplink — a laptop plugged in there does not mean the box is on the bench,
# and should not suppress the AP.
SERVICE_IF="${FPVLINK_SERVICE_IF:-enP3p49s0}"

# How long to wait for Ethernet to show carrier before concluding there is
# none. This is the one genuinely awkward number here. At boot the PHY takes
# a few seconds to negotiate, so checking instantly would see carrier=0 with a
# cable plugged in and wrongly start the AP. Too long and the AP is late to
# appear in the field, where nothing else is going to trigger it.
CARRIER_WAIT="${FPVLINK_AP_CARRIER_WAIT:-25}"

log() { echo "[ap-guard] $*"; }

# -----------------------------------------------------------------------------
# 1. The adapter has to be present at all.
#
# Skip rather than fail: a box running with the USB stick unplugged is a
# perfectly normal state, not an error worth reporting.
# -----------------------------------------------------------------------------
if [[ ! -e "/sys/class/net/$WLAN_IF" ]]; then
    log "no $WLAN_IF present (adapter unplugged?) — not starting AP"
    exit 1
fi

# -----------------------------------------------------------------------------
# 2. Collect the Ethernet ports that count as an uplink.
# -----------------------------------------------------------------------------
uplinks=()
for path in /sys/class/net/*; do
    iface="$(basename "$path")"
    case "$iface" in
        lo|wlan*|wlx*|docker*|veth*|br-*|usb*) continue ;;
    esac
    [[ "$iface" == "$SERVICE_IF" ]] && continue
    [[ -e "$path/carrier" ]] || continue
    uplinks+=("$iface")
done

if [[ ${#uplinks[@]} -eq 0 ]]; then
    log "no Ethernet uplink ports found — treating as field, starting AP"
    exit 0
fi

log "watching for carrier on: ${uplinks[*]} (up to ${CARRIER_WAIT}s)"

# -----------------------------------------------------------------------------
# 3. Wait for carrier to appear. Any uplink with a cable means bench.
#
# Note this exits the moment carrier is seen — it only pays the full wait in
# the field case, where there is nothing else competing for the time anyway.
# -----------------------------------------------------------------------------
elapsed=0
while [[ $elapsed -lt $CARRIER_WAIT ]]; do
    for iface in "${uplinks[@]}"; do
        # An interface that is administratively down always reads carrier 0.
        # Read it anyway: networkd brings these up early, and a down port is
        # genuinely not an uplink right now.
        if [[ "$(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo 0)" == "1" ]]; then
            log "$iface has carrier after ${elapsed}s — on the bench, skipping AP"
            exit 1
        fi
    done
    sleep 1
    elapsed=$((elapsed + 1))
done

log "no Ethernet carrier after ${CARRIER_WAIT}s — in the field, starting AP"
exit 0
