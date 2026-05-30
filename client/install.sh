#!/bin/bash
##############################################################################
# Internet Income Manager — Client Installer
#
# Auto-installs Docker + Python + IIM client on Debian/Ubuntu VPS.
# Connects to preset control server (mainsite.vinaproxy.net:18881).
#
# Usage:
#   sudo ./install.sh
##############################################################################
set -e

CONTROL_URL="${1:-http://mainsite.vinaproxy.net:18881}"
LOG=/tmp/iim-install.log
: > "$LOG"

RED="\033[0;31m"; GREEN="\033[0;32m"; YELLOW="\033[0;33m"
CYAN="\033[0;36m"; DIM="\033[2m"; BOLD="\033[1m"; NC="\033[0m"

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}[!]${NC} Must run as root. Try: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOTAL=8

# ---------- Spinner ----------
SPINNER_PID=""
spinner_start() {
    local label="$1" step="$2"
    (
        local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        local i=0
        while :; do
            local c="${frames:$((i % ${#frames})):1}"
            printf "\r${CYAN}%s${NC} ${DIM}[%d/%d]${NC} %s..." "$c" "$step" "$TOTAL" "$label"
            i=$((i + 1))
            sleep 0.08
        done
    ) &
    SPINNER_PID=$!
    disown 2>/dev/null || true
}
spinner_stop() {
    local label="$1" step="$2" status="${3:-ok}"
    if [ -n "$SPINNER_PID" ]; then
        kill "$SPINNER_PID" 2>/dev/null || true
        wait "$SPINNER_PID" 2>/dev/null || true
        SPINNER_PID=""
    fi
    if [ "$status" = "ok" ]; then
        printf "\r${GREEN}✓${NC} ${DIM}[%d/%d]${NC} %s\033[K\n" "$step" "$TOTAL" "$label"
    else
        printf "\r${RED}✗${NC} ${DIM}[%d/%d]${NC} %s\033[K\n" "$step" "$TOTAL" "$label"
    fi
}

run_step() {
    local step="$1" label="$2"; shift 2
    spinner_start "$label" "$step"
    if "$@" >>"$LOG" 2>&1; then
        spinner_stop "$label" "$step" ok
    else
        spinner_stop "$label" "$step" fail
        echo
        echo -e "${RED}Installation failed at step $step.${NC}"
        echo -e "${DIM}See log: $LOG${NC}"
        echo
        tail -20 "$LOG"
        exit 1
    fi
}

trap 'if [ -n "$SPINNER_PID" ]; then kill "$SPINNER_PID" 2>/dev/null; fi' EXIT

# ---------- Banner ----------
clear 2>/dev/null || true
echo -e "${CYAN}"
cat <<'BANNER'
╔══════════════════════════════════════════════════════╗
║                                                      ║
║      💰  Internet Income Manager Client  💰          ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"
echo -e "  ${DIM}Host    :${NC} ${BOLD}$(hostname)${NC} ($(hostname -I 2>/dev/null | awk '{print $1}'))"
echo -e "  ${DIM}Log file:${NC} $LOG"
echo

# ---------- Steps ----------
step_deps() {
    command -v apt-get >/dev/null 2>&1 || return 1
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip curl iptables ca-certificates
}

step_docker() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
}

step_pull_images() {
    docker pull ghcr.io/tun2proxy/tun2proxy:v0.7.19 &
    docker pull curlimages/curl:latest &
    wait
}

step_copy_files() {
    mkdir -p /opt/internet-income-manager/data/logs
    mkdir -p /opt/internet-income-manager/templates
    mkdir -p /opt/internet-income-manager/static
    cp "$SCRIPT_DIR/app.py" /opt/internet-income-manager/
    cp "$SCRIPT_DIR/templates/index.html" /opt/internet-income-manager/templates/
}

step_venv() {
    python3 -m venv /opt/internet-income-manager/venv
    /opt/internet-income-manager/venv/bin/pip install -q --upgrade pip
    /opt/internet-income-manager/venv/bin/pip install -q flask
}

step_config() {
    if [ ! -f /opt/internet-income-manager/data/config.json ]; then
        cat > /opt/internet-income-manager/data/config.json <<EOF
{
  "device_name": "$(hostname)",
  "server_url": "$CONTROL_URL",
  "license_key": "",
  "client_id": "",
  "hidden_apps": []
}
EOF
    fi
}

step_service() {
    cp "$SCRIPT_DIR/ii-manager.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable ii-manager
    systemctl restart ii-manager
    sleep 2
    systemctl is-active --quiet ii-manager
}

step_cleanup() {
    # Remove the bootstrap scratch dir we may have been spawned from
    rm -rf /tmp/iim-bootstrap.* 2>/dev/null || true
    # Remove a downloaded tarball / extracted dist folder if user fetched manually under common paths
    rm -f /tmp/iim-client.tar.gz /tmp/iim-client-*.tar.gz 2>/dev/null || true
    rm -rf /tmp/iim-client-dist 2>/dev/null || true
    # If we were extracted into /root or a home, also try to drop the dist folder
    # (only when it lives next to where this script runs — never elsewhere)
    if [ -d "$SCRIPT_DIR" ] && [ "$(basename "$SCRIPT_DIR")" = "iim-client-dist" ]; then
        # Defer self-cleanup so the script can finish reading itself
        (sleep 1 && rm -rf "$SCRIPT_DIR" 2>/dev/null) &
        disown 2>/dev/null || true
    fi
    return 0
}

run_step 1 "Checking system"            step_deps
run_step 2 "Installing Docker"          step_docker
run_step 3 "Pulling Docker images"      step_pull_images
run_step 4 "Copying client files"       step_copy_files
run_step 5 "Setting up Python venv"     step_venv
run_step 6 "Writing configuration"      step_config
run_step 7 "Starting service"           step_service
run_step 8 "Cleaning up"                step_cleanup

# ---------- Done ----------
PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              ✅  Installation complete                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo
echo -e "  ${BOLD}Web UI:${NC}  ${CYAN}http://$PUBLIC_IP:18880${NC}"
echo
echo -e "  ${DIM}systemctl status   ii-manager${NC}"
echo -e "  ${DIM}systemctl restart  ii-manager${NC}"
echo -e "  ${DIM}journalctl -u      ii-manager -f${NC}"
echo
