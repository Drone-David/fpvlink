#!/usr/bin/env bash
# =============================================================================
# FPVLink – 05-network.sh
# Give the box an address that never moves.
#
# Until now the only way to reach a deployed box was its DHCP lease, which
# changes. On a headless unit with no screen there is nothing to read the new
# address off of, so a lease change meant ARP-sweeping the subnet to find it.
#
# This installs two independent ways in, either of which is enough on its own:
#
#   1. 10.10.10.1 on the second NIC (enP3p49s0), static, no DHCP involved.
#      Cable a laptop straight into that port and the box is always there.
#      The box also hands the laptop 10.10.10.50-69 so nothing needs to be
#      configured by hand.
#
#   2. fpvlink.local via mDNS, on whatever network the primary port joins.
#      avahi is already running; the host was just still named orangepi5-plus.
#
# The primary LAN port is deliberately left alone — it keeps its DHCP address
# so normal LAN and internet access are unchanged.
#
# Run as root on the device:
#   sudo bash 05-network.sh
#
# MULTI-BOX: every name here must be unique per box, or two units on one
# network fight over the same mDNS name and you cannot tell which one you are
# deploying to. Override with:
#
#   FPVLINK_HOSTNAME    default 'fpvlink'     -> <name>.local
#   FPVLINK_SERVICE_IP  default '10.10.10.1'  -> the /24 is derived from this
#   FPVLINK_SERVICE_IF  default 'enP3p49s0'   -> which NIC is the service port
#
# Keep FPVLINK_SERVICE_IP at the default unless you are cabling more than one
# box's service port into the SAME switch. The service port is normally a
# point-to-point run to one laptop, so every box answering on 10.10.10.1 is a
# feature — one address to remember, whichever unit you are plugged into.
# The hostname is the one that MUST differ; it is shared-network visible.
#
#   sudo FPVLINK_HOSTNAME=fpvlink-2 bash 05-network.sh
#
# Idempotent: safe to re-run.
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

SERVICE_IF="${FPVLINK_SERVICE_IF:-enP3p49s0}"
SERVICE_IP="${FPVLINK_SERVICE_IP:-10.10.10.1}"
HOSTNAME_NEW="${FPVLINK_HOSTNAME:-fpvlink}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET_UNIT="05-fpvlink-service.network"

# The defaults as they appear in the shipped template, so the install step
# knows what to substitute out of it.
TPL_IF="enP3p49s0"
TPL_IP="10.10.10.1"

# Reject a bad hostname here rather than letting hostnamectl take it and
# leaving the box with a name mDNS will not publish. RFC 1123: letters,
# digits and hyphens, no leading or trailing hyphen, 63 chars max.
if ! [[ "$HOSTNAME_NEW" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
    err "Invalid hostname '$HOSTNAME_NEW'."
    err "Use letters, digits and hyphens only (no leading/trailing hyphen), e.g. fpvlink-2"
    exit 1
fi

if ! [[ "$SERVICE_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    err "Invalid FPVLINK_SERVICE_IP '$SERVICE_IP' — expected a bare IPv4 address."
    exit 1
fi

step "FPVLink Network Setup – fixed service address + mDNS name"

info "Hostname:     $HOSTNAME_NEW  (-> $HOSTNAME_NEW.local)"
info "Service port: $SERVICE_IF at $SERVICE_IP"

# -----------------------------------------------------------------------------
# 1. Sanity check: the service port must exist, and must NOT be the port we
#    are currently reachable on. Pinning the live port would drop this session
#    and leave the box unreachable.
# -----------------------------------------------------------------------------
step "1/4  Checking interfaces"

if ! ip link show "$SERVICE_IF" >/dev/null 2>&1; then
    err "Interface $SERVICE_IF does not exist on this box."
    info "Interfaces present: $(ip -o link show | awk -F': ' '{print $2}' | tr '\n' ' ')"
    err "Re-run with the right port, e.g. FPVLINK_SERVICE_IF=eth1 sudo -E bash $0"
    exit 1
fi
ok "Service port $SERVICE_IF present"

PRIMARY_IF="$(ip -4 route show default | awk '{print $5; exit}')"
if [[ -n "$PRIMARY_IF" ]]; then
    info "Primary (default-route) port is $PRIMARY_IF — will not be touched"
    if [[ "$PRIMARY_IF" == "$SERVICE_IF" ]]; then
        err "$SERVICE_IF currently carries the default route."
        err "Pinning it would cut this box off the LAN. Move the LAN cable to the"
        err "other port first, or pick a different SERVICE_IF."
        exit 1
    fi
else
    warn "No default route found — cannot confirm which port is the LAN port."
fi

# -----------------------------------------------------------------------------
# 2. Install the networkd unit for the service port
# -----------------------------------------------------------------------------
step "2/4  Pinning $SERVICE_IF to $SERVICE_IP"

if [[ ! -f "$SRC_DIR/system/network/$NET_UNIT" ]]; then
    err "Missing $SRC_DIR/system/network/$NET_UNIT"
    exit 1
fi

# The repo file is a template carrying the defaults. Substitute the interface
# and address in on the way to /etc so a second box can differ without the two
# copies drifting apart — there is exactly one file to maintain.
sed -e "s/^Name=${TPL_IF}\$/Name=${SERVICE_IF}/" \
    -e "s|^Address=${TPL_IP}/24\$|Address=${SERVICE_IP}/24|" \
    "$SRC_DIR/system/network/$NET_UNIT" > "/etc/systemd/network/$NET_UNIT"
chmod 0644 "/etc/systemd/network/$NET_UNIT"

# Substitution is not optional — if the template ever stops matching these
# patterns the box would silently come up on the wrong address.
if ! grep -q "^Name=${SERVICE_IF}\$" "/etc/systemd/network/$NET_UNIT" \
   || ! grep -q "^Address=${SERVICE_IP}/24\$" "/etc/systemd/network/$NET_UNIT"; then
    err "Failed to apply $SERVICE_IF / $SERVICE_IP to $NET_UNIT."
    err "The template no longer matches 'Name=${TPL_IF}' / 'Address=${TPL_IP}/24'."
    exit 1
fi
ok "Installed /etc/systemd/network/$NET_UNIT ($SERVICE_IF -> $SERVICE_IP)"

# 'networkctl reload' picks up new .network files without restarting
# systemd-networkd. A full restart would re-run DHCP on the primary port and
# can drop an SSH session; reload + a targeted reconfigure does not.
networkctl reload
networkctl reconfigure "$SERVICE_IF" >/dev/null
ok "Applied without disturbing $PRIMARY_IF"

# -----------------------------------------------------------------------------
# 3. Hostname -> fpvlink, so mDNS publishes fpvlink.local
# -----------------------------------------------------------------------------
step "3/4  Setting hostname to $HOSTNAME_NEW (for $HOSTNAME_NEW.local)"

HOSTNAME_OLD="$(hostnamectl --static)"
if [[ "$HOSTNAME_OLD" == "$HOSTNAME_NEW" ]]; then
    ok "Hostname already $HOSTNAME_NEW"
else
    hostnamectl set-hostname "$HOSTNAME_NEW"
    # Keep /etc/hosts in step, or sudo warns about an unresolvable host on
    # every invocation.
    if grep -qE "^127\.0\.1\.1\s" /etc/hosts; then
        sed -i -E "s/^(127\.0\.1\.1\s+).*/\1$HOSTNAME_NEW/" /etc/hosts
    else
        echo -e "127.0.1.1\t$HOSTNAME_NEW" >> /etc/hosts
    fi
    sed -i -E "s/\b$HOSTNAME_OLD\b/$HOSTNAME_NEW/g" /etc/hosts
    ok "Hostname $HOSTNAME_OLD -> $HOSTNAME_NEW (/etc/hosts updated)"
fi

# Publish IPv4 only.
#
# This is not cosmetic — it is load-bearing. The web server binds 0.0.0.0:8080,
# which is IPv4-only; there is no IPv6 listener. avahi by default also publishes
# AAAA records for the host, and browsers prefer IPv6 (RFC 6724). The result is
# that http://fpvlink.local:8080 resolves to an IPv6 address nothing is
# listening on: the page itself may still limp in over an IPv4 fallback, but
# the dashboard's WebSocket does not, so every live value on it goes blank.
# That was a real, observed failure the first time this name went live.
#
# If the web server is ever made to listen on :: (dual-stack), this can be
# reverted. Until then, only advertise an address that actually answers.
AVAHI_CONF=/etc/avahi/avahi-daemon.conf
if [[ -f "$AVAHI_CONF" ]]; then
    [[ -f "$AVAHI_CONF.bak-fpvlink" ]] || cp "$AVAHI_CONF" "$AVAHI_CONF.bak-fpvlink"
    sed -i "s/^use-ipv6=yes/use-ipv6=no/" "$AVAHI_CONF"
    grep -q "^publish-aaaa-on-ipv4=" "$AVAHI_CONF" \
        || sed -i "/^\[publish\]/a publish-aaaa-on-ipv4=no" "$AVAHI_CONF"
    ok "avahi set to advertise IPv4 only (backup: $AVAHI_CONF.bak-fpvlink)"
fi

if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
    systemctl restart avahi-daemon
    ok "avahi-daemon restarted — publishing $HOSTNAME_NEW.local"
else
    systemctl enable --now avahi-daemon
    ok "avahi-daemon enabled and started"
fi

# -----------------------------------------------------------------------------
# 4. Verify
# -----------------------------------------------------------------------------
step "4/4  Verifying"

FAILED=0

if ip -4 addr show "$SERVICE_IF" | grep -q "inet $SERVICE_IP/24"; then
    ok "$SERVICE_IF holds $SERVICE_IP (present even with no cable attached)"
else
    err "$SERVICE_IF does not hold $SERVICE_IP"
    ip -4 addr show "$SERVICE_IF" || true
    FAILED=1
fi

# Confirm OUR file is the one networkd chose for this port. This is the part
# that could silently regress: netplan's wildcard "e*" file also matches, and
# if it ever sorted ahead of ours the port would quietly go back to DHCP.
if networkctl status "$SERVICE_IF" 2>/dev/null | grep -q "Network File: /etc/systemd/network/$NET_UNIT"; then
    ok "networkd is using $NET_UNIT for $SERVICE_IF (won over netplan's wildcard)"
else
    err "$SERVICE_IF is NOT using $NET_UNIT — netplan's wildcard likely claimed it."
    networkctl status "$SERVICE_IF" 2>/dev/null | grep -i "network file" || true
    FAILED=1
fi

# The DHCP server only binds once the link has carrier, so with no cable in
# the port there is nothing to observe yet. Report that honestly rather than
# claiming a pass we did not measure.
if ss -ulnp 2>/dev/null | grep -q "%$SERVICE_IF:67"; then
    ok "DHCP server bound on $SERVICE_IF:67"
elif ip link show "$SERVICE_IF" | grep -q "NO-CARRIER"; then
    info "DHCP server configured; it binds when a cable is plugged in (no carrier now)."
    info "With a laptop attached, confirm with: ss -ulnp | grep %$SERVICE_IF:67"
else
    warn "Link has carrier but no DHCP server on :67 — check: networkctl status $SERVICE_IF"
fi

if [[ -n "$PRIMARY_IF" ]] && ip -4 addr show "$PRIMARY_IF" | grep -q "inet "; then
    ok "Primary port $PRIMARY_IF still up: $(ip -4 -o addr show "$PRIMARY_IF" | awk '{print $4}' | tr '\n' ' ')"
else
    err "Primary port lost its address — investigate before rebooting."
    FAILED=1
fi

if [[ "$(hostnamectl --static)" == "$HOSTNAME_NEW" ]]; then
    ok "Hostname is $HOSTNAME_NEW"
else
    err "Hostname did not change"
    FAILED=1
fi

# The dashboard breaks if the name resolves to an IPv6 address, because
# nothing listens there — so treat a published AAAA as a failure, not a nit.
if grep -q "^use-ipv6=no" "$AVAHI_CONF" 2>/dev/null; then
    ok "avahi publishing IPv4 only (no AAAA for $HOSTNAME_NEW.local)"
else
    err "avahi may publish AAAA records — the dashboard WebSocket will fail."
    err "Check use-ipv6 in $AVAHI_CONF."
    FAILED=1
fi

if ss -tln | grep -qE "^tcp.*\*:8080|:::8080"; then
    info "Web server now has an IPv6 listener — the avahi IPv4-only pin can be reverted."
fi

if [[ $FAILED -ne 0 ]]; then
    err "Network setup finished with errors — see above."
    exit 1
fi

step "Done — the box is now reachable at:"
echo -e "  ${BOLD}http://$HOSTNAME_NEW.local:8080${NC}   on any network the LAN port joins"
echo -e "  ${BOLD}ssh root@$HOSTNAME_NEW.local${NC}"
echo -e "  ${BOLD}http://$SERVICE_IP:8080${NC}        laptop cabled into the $SERVICE_IF port"
echo -e "  ${BOLD}ssh root@$SERVICE_IP${NC}"
echo
warn "Do not plug the LAN into the $SERVICE_IF port — the DHCP server there"
warn "would start handing out addresses on your real network."
