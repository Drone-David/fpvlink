#!/usr/bin/env bash
# =============================================================================
# FPVLink – 04-service.sh
# Deploy the FPVLink application to /opt/fpvlink and install it as a
# managed systemd service running under a dedicated 'fpvlink' system user.
#
# Run as root after 03-gstreamer.sh:
#   sudo bash 04-service.sh
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

step "FPVLink Service Installation – Orange Pi 5 Plus"
info "Script started at $(date '+%Y-%m-%d %H:%M:%S %Z')"

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
FPVLINK_USER="fpvlink"
FPVLINK_GROUP="fpvlink"
INSTALL_DIR="/opt/fpvlink"
SERVICE_FILE="/etc/systemd/system/fpvlink.service"

# Candidate source directories (checked in order)
SOURCE_CANDIDATES=(
    "/home/pi/fpvlink"
    "$HOME/fpvlink"
    "$(dirname "$(realpath "$0")")/.."   # parent of the setup/ directory
    "/root/fpvlink"
)

# Additional groups the service user should belong to
EXTRA_GROUPS=(video audio plugdev dialout)

# -----------------------------------------------------------------------------
# 1. Create 'fpvlink' system user
# -----------------------------------------------------------------------------
step "1/6  Creating system user '$FPVLINK_USER'"

if id "$FPVLINK_USER" &>/dev/null; then
    info "User '$FPVLINK_USER' already exists – skipping creation"
else
    useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "FPVLink service account" \
        "$FPVLINK_USER"
    ok "Created system user: $FPVLINK_USER"
fi

# Add to required groups
for grp in "${EXTRA_GROUPS[@]}"; do
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" "$FPVLINK_USER"
        ok "Added $FPVLINK_USER to group: $grp"
    else
        warn "Group '$grp' does not exist – skipping (install relevant drivers first)"
    fi
done

# -----------------------------------------------------------------------------
# 2. Locate source directory
# -----------------------------------------------------------------------------
step "2/6  Locating FPVLink source"

SOURCE_DIR=""
for candidate in "${SOURCE_CANDIDATES[@]}"; do
    if [[ -d "$candidate" ]]; then
        SOURCE_DIR="$(realpath "$candidate")"
        ok "Found source directory: $SOURCE_DIR"
        break
    fi
done

if [[ -z "$SOURCE_DIR" ]]; then
    err "Could not find the FPVLink source directory.  Checked:"
    for c in "${SOURCE_CANDIDATES[@]}"; do
        err "  $c"
    done
    err "Clone the repository first, e.g.:"
    err "  git clone https://github.com/YOUR_ORG/fpvlink.git ~/fpvlink"
    exit 1
fi

# Sanity-check that it looks like the right project
if [[ ! -f "$SOURCE_DIR/package.json" ]] && \
   [[ ! -f "$SOURCE_DIR/requirements.txt" ]] && \
   [[ ! -f "$SOURCE_DIR/system/fpvlink.service" ]]; then
    warn "Source directory '$SOURCE_DIR' may not be the FPVLink project."
    warn "Expected to find at least one of: package.json, requirements.txt,"
    warn "system/fpvlink.service.  Proceeding anyway – check paths if service fails."
fi

# -----------------------------------------------------------------------------
# 3. Copy / sync project to /opt/fpvlink
# -----------------------------------------------------------------------------
step "3/6  Deploying to $INSTALL_DIR"

mkdir -p "$INSTALL_DIR"

# Use rsync if available for clean incremental copies; fall back to cp.
if command -v rsync &>/dev/null; then
    rsync -a --delete \
        --exclude='.git' \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        "${SOURCE_DIR}/" "${INSTALL_DIR}/"
    ok "rsync complete: $SOURCE_DIR → $INSTALL_DIR"
else
    cp -a "${SOURCE_DIR}/." "${INSTALL_DIR}/"
    ok "cp complete: $SOURCE_DIR → $INSTALL_DIR"
fi

# Set ownership
chown -R "${FPVLINK_USER}:${FPVLINK_GROUP}" "$INSTALL_DIR"
ok "Ownership set: $FPVLINK_USER:$FPVLINK_GROUP on $INSTALL_DIR"

# -----------------------------------------------------------------------------
# 4. Install Node.js and Python dependencies
# -----------------------------------------------------------------------------
step "4/6  Installing application dependencies"

# ── Node.js (npm install) ────────────────────────────────────────────────────
if [[ -f "${INSTALL_DIR}/package.json" ]]; then
    info "Running npm install in $INSTALL_DIR …"
    # Run as fpvlink user to avoid root-owned node_modules
    sudo -u "$FPVLINK_USER" bash -c "cd '${INSTALL_DIR}' && npm install --omit=dev 2>&1"
    ok "npm install complete"
else
    warn "No package.json found in $INSTALL_DIR – skipping npm install"
fi

# ── Python (pip3 install -r requirements.txt) ────────────────────────────────
if [[ -f "${INSTALL_DIR}/requirements.txt" ]]; then
    info "Installing Python requirements from requirements.txt …"
    pip3 install \
        --break-system-packages \
        --no-cache-dir \
        -r "${INSTALL_DIR}/requirements.txt"
    ok "Python requirements installed"
else
    warn "No requirements.txt found in $INSTALL_DIR – skipping pip install"
fi

# Ensure correct ownership after installs
chown -R "${FPVLINK_USER}:${FPVLINK_GROUP}" "$INSTALL_DIR"

# -----------------------------------------------------------------------------
# 5. Install systemd service unit
# -----------------------------------------------------------------------------
step "5/6  Installing systemd service → $SERVICE_FILE"

TEMPLATE_FILE="${INSTALL_DIR}/system/fpvlink.service"

if [[ -f "$TEMPLATE_FILE" ]]; then
    info "Using service template from $TEMPLATE_FILE"
    cp "$TEMPLATE_FILE" "$SERVICE_FILE"
    ok "Service file copied from template"
else
    warn "Template $TEMPLATE_FILE not found – generating a default service unit"

    cat > "$SERVICE_FILE" << EOF
# /etc/systemd/system/fpvlink.service
# Auto-generated by 04-service.sh on $(date '+%Y-%m-%d %H:%M:%S %Z')
# Edit to suit your application entry-point and environment.

[Unit]
Description=FPVLink – OTG FPV streaming service for DJI Goggles 2
Documentation=https://github.com/YOUR_ORG/fpvlink
After=network.target local-fs.target sys-subsystem-net-devices-usb0.device
Wants=network.target

[Service]
Type=simple
User=${FPVLINK_USER}
Group=${FPVLINK_GROUP}
WorkingDirectory=${INSTALL_DIR}

# ---------- environment --------------------------------------------------------
Environment=NODE_ENV=production
Environment=FPVLINK_INSTALL_DIR=${INSTALL_DIR}
# Uncomment and adjust for SRT / RTMP target:
# Environment=FPVLINK_STREAM_URL=srt://your-server:1935
# Environment=GST_DEBUG=2

# ---------- resource limits ---------------------------------------------------
# Allow real-time scheduling for low-latency video
AmbientCapabilities=CAP_SYS_NICE CAP_NET_BIND_SERVICE
LimitRTPRIO=99
LimitNICE=-20
MemoryMax=1G

# ---------- command -----------------------------------------------------------
# Adjust ExecStart to match your actual entry point:
#   Node.js app:   ExecStart=/usr/bin/node ${INSTALL_DIR}/src/index.js
#   Python app:    ExecStart=/usr/bin/python3 ${INSTALL_DIR}/main.py
ExecStart=/usr/bin/node ${INSTALL_DIR}/src/index.js

# ---------- restart policy ----------------------------------------------------
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=60
StartLimitBurst=5

# ---------- security hardening ------------------------------------------------
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/data /tmp

# ---------- logging -----------------------------------------------------------
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fpvlink

[Install]
WantedBy=multi-user.target
EOF
    ok "Default service unit written to $SERVICE_FILE"
fi

# Fix permissions on service file
chmod 644 "$SERVICE_FILE"
info "Service file:"
echo ""
cat "$SERVICE_FILE"
echo ""

# -----------------------------------------------------------------------------
# 5b. Install the always-on video pipeline unit (fpvlink-pipeline.service)
#
# This is a SEPARATE service from fpvlink.service (the Node dashboard). It runs
# capture/pipeline.py — the always-on GStreamer graph that drives HDMI out. The
# dashboard alone produces no video, so a fresh flash must install this too or
# the device boots to a black screen.
# -----------------------------------------------------------------------------
PIPELINE_SERVICE_FILE="/etc/systemd/system/fpvlink-pipeline.service"
PIPELINE_TEMPLATE_FILE="${INSTALL_DIR}/system/fpvlink-pipeline.service"

if [[ -f "$PIPELINE_TEMPLATE_FILE" ]]; then
    step "5b/6  Installing pipeline unit → $PIPELINE_SERVICE_FILE"
    cp "$PIPELINE_TEMPLATE_FILE" "$PIPELINE_SERVICE_FILE"
    chmod 644 "$PIPELINE_SERVICE_FILE"
    ok "Pipeline service file copied from template"
else
    warn "Pipeline template $PIPELINE_TEMPLATE_FILE not found – HDMI output will not start on boot"
fi

# -----------------------------------------------------------------------------
# 6. Enable and start the services
# -----------------------------------------------------------------------------
step "6/6  Enabling and starting services"

systemctl daemon-reload
ok "systemd daemon reloaded"

systemctl enable fpvlink.service
ok "fpvlink.service enabled (will start on boot)"

systemctl start fpvlink.service
ok "fpvlink.service started"

if [[ -f "$PIPELINE_SERVICE_FILE" ]]; then
    systemctl enable fpvlink-pipeline.service
    ok "fpvlink-pipeline.service enabled (will start on boot)"
    systemctl start fpvlink-pipeline.service
    ok "fpvlink-pipeline.service started"
fi

# Give the service a moment to stabilise
sleep 2

echo ""
info "Service status:"
echo ""
systemctl status fpvlink.service --no-pager --lines=20 || true
if [[ -f "$PIPELINE_SERVICE_FILE" ]]; then
    echo ""
    systemctl status fpvlink-pipeline.service --no-pager --lines=20 || true
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
echo ""
if systemctl is-active --quiet fpvlink.service; then
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║     FPVLink Service Installed and Running ✓              ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${YELLOW}║  FPVLink Service Installed – but not yet active          ║${NC}"
    echo -e "${BOLD}${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
echo -e "    ${CYAN}systemctl status fpvlink fpvlink-pipeline${NC}   – service status"
echo -e "    ${CYAN}journalctl -u fpvlink-pipeline -f${NC}          – live pipeline (video) log tail"
echo -e "    ${CYAN}journalctl -u fpvlink -f${NC}                   – live dashboard log tail"
echo -e "    ${CYAN}systemctl restart fpvlink-pipeline fpvlink${NC} – restart both"
echo -e "    ${CYAN}systemctl disable fpvlink fpvlink-pipeline${NC} – disable at boot"
echo ""
echo -e "  Service user:    ${CYAN}$FPVLINK_USER${NC}"
echo -e "  Install dir:     ${CYAN}$INSTALL_DIR${NC}"
echo -e "  Service unit:    ${CYAN}$SERVICE_FILE${NC}"
echo ""
echo -e "  ${BOLD}${GREEN}FPVLink setup is complete.  Happy flying! 🚁${NC}"
echo ""
