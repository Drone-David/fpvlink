#!/usr/bin/env bash
# =============================================================================
# FPVLink – 07-wifi-ap.sh
# Turn the USB WiFi adapter into a field access point.
#
# The box is reachable two ways today, and both need a cable: the LAN port via
# fpvlink.local, and the service port at 10.10.10.1. At a flying field there is
# no cable and no router, so there is no way to reach the dashboard at all.
# This adds a third way in that needs no infrastructure — the box broadcasts
# its own network and hands a phone an address.
#
# Four things have to be true for that to work, and three of them are the kind
# that fail silently months later (see docs/wifi-ap-research.md):
#
#   1. The right driver is bound. Two drivers claim this adapter and currently
#      race; the mainline one must win, so the other is blacklisted.
#   2. The interface has a stable name. The kernel names it after its MAC,
#      which pins every config to one physical stick.
#   3. The regulatory domain is set. The default world domain forbids
#      beaconing outright, so hostapd cannot start at all.
#   4. Something hands joined clients an IP. networkd already does this on the
#      service port, so it does it here too rather than adding dnsmasq.
#
# Run as root on the device:
#   sudo bash 07-wifi-ap.sh
#
# The WPA2 passphrase is prompted for, or taken from FPVLINK_AP_PASSPHRASE.
# It is written only to /etc/hostapd/fpvlink.conf (mode 0600) and is never
# stored in the repo.
#
# Overridable: FPVLINK_AP_SSID (default FPVLink), FPVLINK_AP_CHANNEL (6),
#              FPVLINK_AP_COUNTRY (US), FPVLINK_AP_PASSPHRASE
#
# MULTI-BOX: give each box its own SSID (FPVLINK_AP_SSID) so you can tell at a
# glance which unit a phone is joining. Two boxes may keep the same AP address
# (10.10.20.1) — a client is only ever on one of these networks at a time.
# The channel is the one to think about at a field with both units live: two
# APs on ch6 within a few metres will step on each other, so put the second on
# ch1 or ch11.
#
# Idempotent: safe to re-run. Re-running without FPVLINK_AP_PASSPHRASE keeps
# the passphrase already installed.
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
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
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

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WLAN_IF="wlan0"
AP_IP="10.10.20.1"
USB_ID="2357:012e"

AP_SSID="${FPVLINK_AP_SSID:-FPVLink}"
AP_CHANNEL="${FPVLINK_AP_CHANNEL:-6}"
AP_COUNTRY="${FPVLINK_AP_COUNTRY:-US}"

LINK_UNIT="10-fpvlink-wlan.link"
NET_UNIT="06-fpvlink-ap.network"
MODPROBE_CONF="fpvlink-wifi.conf"
HOSTAPD_CONF="/etc/hostapd/fpvlink.conf"
AP_SERVICE="fpvlink-ap.service"

step "FPVLink WiFi AP Setup – ${AP_SSID} on 2.4GHz ch${AP_CHANNEL} (${AP_COUNTRY})"

# -----------------------------------------------------------------------------
# 1. Preflight
# -----------------------------------------------------------------------------
step "1/7  Preflight"

if ! lsusb -d "${USB_ID}" >/dev/null 2>&1; then
    err "No adapter with USB ID ${USB_ID} found (TP-Link Archer T3U Nano)."
    info "Adapters present: $(lsusb | grep -iE 'wireless|802\.11|realtek|tp-link' || echo none)"
    err "Plug the adapter in, or edit USB_ID here and in system/network/$LINK_UNIT."
    exit 1
fi
ok "Adapter ${USB_ID} present"

# Find the wireless interface under whatever name it currently has — this
# script may be running before the rename has ever been applied.
CUR_WLAN="$(ls /sys/class/net/ | grep -E '^(wlan|wlx)' | head -1 || true)"
if [[ -z "$CUR_WLAN" ]]; then
    err "Adapter is on the USB bus but no wireless interface exists."
    err "No driver bound. Check: dmesg | grep -iE 'rtw|8812|88x2'"
    exit 1
fi

BOUND_DRIVER="$(basename "$(readlink -f "/sys/class/net/$CUR_WLAN/device/driver")" 2>/dev/null || echo unknown)"
info "Interface $CUR_WLAN, driver $BOUND_DRIVER"

if [[ "$BOUND_DRIVER" != "rtw_8822bu" ]]; then
    warn "Expected the mainline rtw_8822bu driver, found '$BOUND_DRIVER'."
    warn "hostapd's nl80211 config assumes mac80211. Step 2 blacklists the"
    warn "vendor blob; a reboot after this run should correct the binding."
fi

if ! command -v hostapd >/dev/null 2>&1; then
    info "Installing hostapd…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y hostapd >/dev/null
fi
ok "hostapd $(hostapd -v 2>&1 | head -1 | awk '{print $2}' || echo present)"

# The distro unit is masked and stays that way: it would run a *different*
# config file, and two hostapds fighting over one radio is a confusing failure.
if ! systemctl is-enabled hostapd 2>/dev/null | grep -q masked; then
    systemctl stop hostapd 2>/dev/null || true
    systemctl mask hostapd >/dev/null 2>&1 || true
fi
ok "Distro hostapd.service masked (we run our own unit against our own config)"

# -----------------------------------------------------------------------------
# 2. Settle the driver race
# -----------------------------------------------------------------------------
step "2/7  Pinning the driver (blacklisting the vendor blob)"

install -m 0644 "$SRC_DIR/system/modprobe/$MODPROBE_CONF" "/etc/modprobe.d/$MODPROBE_CONF"
ok "Installed /etc/modprobe.d/$MODPROBE_CONF"

if lsmod | grep -q '^88x2bu'; then
    # Only unloadable while nothing is bound to it. If rtw88 won the race it
    # sits at refcount 0 and this succeeds; if it is actually in use, leave it
    # alone and let the reboot sort it out rather than yanking the interface.
    if rmmod 88x2bu 2>/dev/null; then
        ok "Unloaded the idle 88x2bu blob"
    else
        warn "88x2bu is loaded and in use — takes effect after a reboot"
    fi
fi

# -----------------------------------------------------------------------------
# 3. Stable interface name
# -----------------------------------------------------------------------------
step "3/7  Pinning the interface name to $WLAN_IF"

install -m 0644 "$SRC_DIR/system/network/$LINK_UNIT" "/etc/systemd/network/$LINK_UNIT"
udevadm control --reload
ok "Installed /etc/systemd/network/$LINK_UNIT"

if [[ "$CUR_WLAN" != "$WLAN_IF" ]]; then
    # Renaming only works while the link is down; it is down at this point in
    # a fresh install, but not if a previous run left the AP up.
    systemctl stop "$AP_SERVICE" 2>/dev/null || true
    ip link set "$CUR_WLAN" down 2>/dev/null || true
    if ip link set "$CUR_WLAN" name "$WLAN_IF" 2>/dev/null; then
        ok "Renamed $CUR_WLAN -> $WLAN_IF (and permanently, via the .link file)"
    else
        warn "Live rename failed — the .link file will apply it on next boot."
    fi
else
    ok "Already named $WLAN_IF"
fi

# -----------------------------------------------------------------------------
# 4. Address + DHCP server
# -----------------------------------------------------------------------------
step "4/7  Address $AP_IP, DHCP server and DNS on $WLAN_IF"

install -m 0644 "$SRC_DIR/system/network/$NET_UNIT" "/etc/systemd/network/$NET_UNIT"
ok "Installed /etc/systemd/network/$NET_UNIT"

# The DHCP config advertises 10.10.20.1 as the resolver, so something has to
# actually answer there. Without this drop-in, resolved binds 127.0.0.53 only,
# the advertised resolver is a black hole, and phones abandon the interface
# entirely — the exact failure this whole arrangement exists to avoid.
mkdir -p /etc/systemd/resolved.conf.d
install -m 0644 "$SRC_DIR/system/resolved-fpvlink-ap.conf" \
    "/etc/systemd/resolved.conf.d/fpvlink-ap.conf"
systemctl restart systemd-resolved
ok "systemd-resolved now answers DNS on $AP_IP"

# reload + targeted reconfigure, never a networkd restart — a restart re-runs
# DHCP on the primary LAN port and can drop this SSH session.
networkctl reload
networkctl reconfigure "$WLAN_IF" >/dev/null 2>&1 || true
ok "Applied without disturbing the LAN port"

# -----------------------------------------------------------------------------
# 5. hostapd config (this is the only file holding the passphrase)
# -----------------------------------------------------------------------------
step "5/7  Writing $HOSTAPD_CONF"

AP_PASSPHRASE="${FPVLINK_AP_PASSPHRASE:-}"

if [[ -z "$AP_PASSPHRASE" ]] && [[ -f "$HOSTAPD_CONF" ]]; then
    EXISTING="$(grep -E '^wpa_passphrase=' "$HOSTAPD_CONF" 2>/dev/null | cut -d= -f2- || true)"
    if [[ -n "$EXISTING" && "$EXISTING" != "__SET_BY_SETUP_SCRIPT__" ]]; then
        AP_PASSPHRASE="$EXISTING"
        info "Keeping the passphrase already installed (re-run with FPVLINK_AP_PASSPHRASE to change it)"
    fi
fi

while [[ -z "$AP_PASSPHRASE" ]]; do
    echo
    read -r -s -p "  WPA2 passphrase for '$AP_SSID' (8-63 chars): " AP_PASSPHRASE
    echo
    read -r -s -p "  Repeat: " AP_PASSPHRASE_CONFIRM
    echo
    if [[ "$AP_PASSPHRASE" != "$AP_PASSPHRASE_CONFIRM" ]]; then
        warn "Passphrases do not match."
        AP_PASSPHRASE=""
    elif [[ ${#AP_PASSPHRASE} -lt 8 || ${#AP_PASSPHRASE} -gt 63 ]]; then
        warn "WPA2 requires 8-63 characters."
        AP_PASSPHRASE=""
    fi
done

mkdir -p /etc/hostapd
# Write via the template so the explanatory comments travel with it, then fill
# in the values. umask first: the file holds the passphrase and must never be
# world-readable, not even for the instant between create and chmod.
OLD_UMASK="$(umask)"
umask 077
sed -e "s/^ssid=.*/ssid=${AP_SSID}/" \
    -e "s/^channel=.*/channel=${AP_CHANNEL}/" \
    -e "s/^country_code=.*/country_code=${AP_COUNTRY}/" \
    -e "s|^wpa_passphrase=.*|wpa_passphrase=${AP_PASSPHRASE}|" \
    "$SRC_DIR/system/hostapd-fpvlink.conf" > "$HOSTAPD_CONF"
umask "$OLD_UMASK"
chmod 0600 "$HOSTAPD_CONF"
ok "Wrote $HOSTAPD_CONF (mode 0600, passphrase not echoed and not in the repo)"

# -----------------------------------------------------------------------------
# 6. The unit
# -----------------------------------------------------------------------------
step "6/7  Installing $AP_SERVICE"

chmod 0755 "$SRC_DIR/scripts/fpvlink-ap-guard.sh"
install -m 0644 "$SRC_DIR/system/$AP_SERVICE" "/etc/systemd/system/$AP_SERVICE"
systemctl daemon-reload
systemctl enable "$AP_SERVICE" >/dev/null 2>&1
ok "Installed and enabled $AP_SERVICE"
info "It starts only when no Ethernet cable has carrier — see scripts/fpvlink-ap-guard.sh"

# -----------------------------------------------------------------------------
# 7. Verify
# -----------------------------------------------------------------------------
step "7/7  Verifying"

FAILED=0

if [[ -e "/sys/class/net/$WLAN_IF" ]]; then
    ok "$WLAN_IF exists"
else
    err "$WLAN_IF does not exist — the rename did not apply. Reboot and re-check."
    FAILED=1
fi

# The part that could silently regress: confirm OUR .network file is the one
# networkd chose, exactly as 05-network.sh does for the service port.
if networkctl status "$WLAN_IF" 2>/dev/null | grep -q "Network File: /etc/systemd/network/$NET_UNIT"; then
    ok "networkd is using $NET_UNIT for $WLAN_IF"
else
    err "$WLAN_IF is NOT using $NET_UNIT — another .network file claimed it."
    networkctl status "$WLAN_IF" 2>/dev/null | grep -i "network file" || true
    FAILED=1
fi

if ip -4 addr show "$WLAN_IF" 2>/dev/null | grep -q "inet $AP_IP/24"; then
    ok "$WLAN_IF holds $AP_IP"
else
    err "$WLAN_IF does not hold $AP_IP"
    FAILED=1
fi

# A resolver that is advertised but never answers is worse than none — it is
# the difference between a phone that loads the dashboard and one that shows
# no WiFi icon and sends not a single packet. Prove it answers.
if ss -tulnp 2>/dev/null | grep -q "$AP_IP:53"; then
    ok "systemd-resolved is listening on $AP_IP:53"
else
    err "Nothing is listening on $AP_IP:53 — phones will abandon this network."
    err "Check /etc/systemd/resolved.conf.d/fpvlink-ap.conf and systemd-resolved."
    FAILED=1
fi

if grep -q "^EmitRouter=yes" "/etc/systemd/network/$NET_UNIT" \
   && grep -q "^DNS=$AP_IP" "/etc/systemd/network/$NET_UNIT"; then
    ok "DHCP advertises a default route and a resolver (required by phones)"
else
    err "$NET_UNIT is not advertising both a router and DNS=$AP_IP."
    err "Phones will associate and then refuse to use the interface."
    FAILED=1
fi

if [[ "$(stat -c '%a' "$HOSTAPD_CONF")" == "600" ]]; then
    ok "$HOSTAPD_CONF is mode 0600"
else
    err "$HOSTAPD_CONF is mode $(stat -c '%a' "$HOSTAPD_CONF") — it holds the passphrase"
    FAILED=1
fi

# Prove hostapd actually accepts this config and the radio beacons, rather
# than assuming. Only meaningful when the guard would let it run; on the
# bench (cable in) starting it would be wrong, so test in the foreground
# briefly instead of via the unit.
#
# But not if an AP is already up. Two hostapds cannot both own one radio —
# the second dies with "nl80211: kernel reports: Match already configured"
# (and on 2.11, a segfault). A re-run while the AP is serving clients would
# then report a failure that is purely an artefact of the test itself, which
# is worse than not testing: it would send you looking for a fault that is
# not there. A running hostapd is stronger evidence than anything this check
# could produce anyway.
if pgrep -x hostapd >/dev/null 2>&1; then
    if iw dev "$WLAN_IF" info 2>/dev/null | grep -q "type AP"; then
        ok "hostapd already running and $WLAN_IF is in AP mode — skipped the test-start"
    else
        warn "hostapd is running but $WLAN_IF is not in AP mode — check it by hand"
    fi
else
    info "Test-starting hostapd for 6s to confirm the radio beacons…"
    AP_LOG="$(mktemp)"
    timeout 6 hostapd "$HOSTAPD_CONF" > "$AP_LOG" 2>&1 || true
    if grep -q "AP-ENABLED" "$AP_LOG"; then
        ok "hostapd reached AP-ENABLED on ch${AP_CHANNEL} — the radio beacons"
    else
        err "hostapd did not reach AP-ENABLED. Last lines:"
        tail -8 "$AP_LOG" >&2
        FAILED=1
    fi
    rm -f "$AP_LOG"
fi

if systemctl is-enabled "$AP_SERVICE" >/dev/null 2>&1; then
    ok "$AP_SERVICE enabled at boot"
else
    err "$AP_SERVICE is not enabled"
    FAILED=1
fi

if [[ $FAILED -ne 0 ]]; then
    err "WiFi AP setup finished with errors — see above."
    exit 1
fi

step "Done"
echo -e "  SSID:      ${BOLD}${AP_SSID}${NC}  (2.4GHz, channel ${AP_CHANNEL}, WPA2)"
echo -e "  Dashboard: ${BOLD}http://${AP_IP}:8080${NC}  or  ${BOLD}http://$(hostnamectl --static).local:8080${NC}"
echo -e "  Clients get ${BOLD}${AP_IP%.*}.50-69${NC}, plus a default route and a resolver."
echo -e "  The box does not forward or NAT, so there is no internet through it — the"
echo -e "  route and DNS exist because phones refuse to use an interface without them."
echo
info "The AP starts automatically only when no Ethernet cable has carrier."
info "On the bench with a cable in, it stays off by design. To force it up:"
echo -e "    ${BOLD}systemctl start $AP_SERVICE${NC}   (bypasses nothing — the guard still runs)"
echo -e "    ${BOLD}FPVLINK_AP_CARRIER_WAIT=0 hostapd $HOSTAPD_CONF${NC}   (foreground, ad hoc)"
echo
warn "5GHz is deliberately unused — that band belongs to the DJI video link."
