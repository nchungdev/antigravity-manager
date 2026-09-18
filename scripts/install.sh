#!/usr/bin/env bash
# ==============================================================================
# Antigravity (AGY) Manager - 1-Click Installer & Service Setup
# Supports: Debian, Ubuntu, OpenMediaVault, Proxmox, Arch, Fedora
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}=====================================================${NC}"
echo -e "${GREEN}       Antigravity (AGY) Manager Installer            ${NC}"
echo -e "${BLUE}=====================================================${NC}"

CURRENT_USER=$(id -un)
USER_HOME=$(eval echo "~${CURRENT_USER}")

echo -e "Detected User: ${GREEN}${CURRENT_USER}${NC}"
echo -e "Home Directory: ${GREEN}${USER_HOME}${NC}"

# Detect Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python 3 is not installed. Installing via apt...${NC}"
    if [ "$EUID" -ne 0 ]; then
        sudo apt-get update && sudo apt-get install -y python3 python3-pip
    else
        apt-get update && apt-get install -y python3 python3-pip
    fi
fi

# Ask installation mode
echo ""
echo "Select Deployment Mode:"
echo "  1) Docker Compose (Recommended for OMV, NAS, Containers)"
echo "  2) Native Systemd Service (Direct host execution)"
read -p "Enter choice [1-2] (default: 1): " DEPLOY_CHOICE
DEPLOY_CHOICE=${DEPLOY_CHOICE:-1}

if [ "$DEPLOY_CHOICE" = "1" ]; then
    echo -e "${BLUE}Deploying via Docker Compose...${NC}"
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Docker is not installed. Please install Docker first.${NC}"
        exit 1
    fi

    # Create .env if not present
    if [ ! -f .env ]; then
        cp .env.example .env
        sed -i "s|HOST_USER=chungnh|HOST_USER=${CURRENT_USER}|g" .env
        sed -i "s|USER_HOME=/home/chungnh|USER_HOME=${USER_HOME}|g" .env
    fi

    if docker compose version &> /dev/null; then
        docker compose up -d
    elif command -v docker-compose &> /dev/null; then
        docker-compose up -d
    else
        echo -e "${RED}Neither 'docker compose' nor 'docker-compose' found.${NC}"
        exit 1
    fi

    echo -e "${GREEN}✅ Docker container started successfully!${NC}"
    echo -e "Access Web UI at: ${BLUE}http://localhost:8585${NC} or ${BLUE}http://<YOUR-SERVER-IP>:8585${NC}"

else
    echo -e "${BLUE}Installing as Native Systemd User Service...${NC}"
    SERVICE_DIR="${USER_HOME}/.config/systemd/user"
    mkdir -p "${SERVICE_DIR}"

    INSTALL_DIR="${USER_HOME}/.agy-manager"
    mkdir -p "${INSTALL_DIR}"
    cp server.py "${INSTALL_DIR}/server.py"

    cat <<EOF > "${SERVICE_DIR}/agy-manager.service"
[Unit]
Description=Antigravity Manager Web Dashboard
After=network.target

[Service]
Type=simple
Environment=PORT=8585
Environment=HOST_USER=${CURRENT_USER}
Environment=USER_HOME=${USER_HOME}
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now agy-manager.service

    # Enable linger for lingering without active ssh session
    if command -v loginctl &> /dev/null; then
        loginctl enable-linger "${CURRENT_USER}" || true
    fi

    echo -e "${GREEN}✅ Native service agy-manager.service is active and enabled!${NC}"
    echo -e "Access Web UI at: ${BLUE}http://localhost:8585${NC} or ${BLUE}http://<YOUR-SERVER-IP>:8585${NC}"
fi
