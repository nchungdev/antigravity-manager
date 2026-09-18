#!/usr/bin/env bash
# ==============================================================================
# Antigravity (AGY) Manager - 1-Line Universal Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/nchungdev/antigravity-manager/main/install.sh | bash
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

REPO="nchungdev/antigravity-manager"
DEFAULT_PORT=8585
CURRENT_USER=$(id -un)
USER_HOME="${HOME}"

echo -e "${BLUE}============================================================${NC}"
echo -e "${GREEN}       🌌 Antigravity (AGY) Manager Universal Installer      ${NC}"
echo -e "${BLUE}============================================================${NC}"

# Detect Local IP
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}') || LOCAL_IP="127.0.0.1"

echo -e "Target User: ${GREEN}${CURRENT_USER}${NC} (${USER_HOME})"
echo -e "Detected IP: ${GREEN}${LOCAL_IP}${NC}"
echo ""

echo "Select Installation Method:"
echo "  1) Docker (Recommended - Pulls pre-built image from GHCR)"
echo "  2) Standalone Binary (No Docker, runs as Systemd User Service)"
echo "  3) Native Python Script (Runs with host python3)"
read -p "Enter choice [1-3] (default: 1): " INSTALL_TYPE
INSTALL_TYPE=${INSTALL_TYPE:-1}

# ------------------------------------------------------------------------------
# Option 1: Docker (Prebuilt GHCR Image)
# ------------------------------------------------------------------------------
if [ "$INSTALL_TYPE" = "1" ]; then
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Error: Docker is not installed on this system.${NC}"
        echo "Please install Docker first, or choose Option 2 for Standalone Binary."
        exit 1
    fi

    COMPOSE_DIR="${USER_HOME}/antigravity-manager"
    mkdir -p "${COMPOSE_DIR}"
    cd "${COMPOSE_DIR}"

    echo -e "${BLUE}Writing docker-compose.yml...${NC}"
    cat <<EOF > docker-compose.yml
services:
  agy-manager:
    image: ghcr.io/${REPO}:latest
    container_name: agy-manager
    restart: unless-stopped
    privileged: true
    pid: host
    ports:
      - "${DEFAULT_PORT}:8585"
    volumes:
      - "${USER_HOME}:${USER_HOME}"
      - /etc/ssl/certs:/etc/ssl/certs:ro
    environment:
      - PORT=${DEFAULT_PORT}
      - HOST_USER=${CURRENT_USER}
      - USER_HOME=${USER_HOME}
      - SYSTEMD_SERVICE=antigravity-cli-daemon.service
      - TZ=Asia/Ho_Chi_Minh
EOF

    echo -e "${BLUE}Pulling and starting container...${NC}"
    if docker compose version &> /dev/null; then
        docker compose pull && docker compose up -d
    elif command -v docker-compose &> /dev/null; then
        docker-compose pull && docker-compose up -d
    else
        docker run -d \
          --name agy-manager \
          --restart unless-stopped \
          --privileged \
          --pid host \
          -p ${DEFAULT_PORT}:8585 \
          -v "${USER_HOME}:${USER_HOME}" \
          -v /etc/ssl/certs:/etc/ssl/certs:ro \
          -e PORT=${DEFAULT_PORT} \
          -e HOST_USER=${CURRENT_USER} \
          -e USER_HOME=${USER_HOME} \
          ghcr.io/${REPO}:latest
    fi

    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}🎉 Installation Completed Successfully!${NC}"
    echo -e "Web UI is live at: ${BLUE}http://${LOCAL_IP}:${DEFAULT_PORT}${NC}"
    echo -e "Docker Compose Directory: ${COMPOSE_DIR}"
    echo -e "${GREEN}============================================================${NC}"

# ------------------------------------------------------------------------------
# Option 2: Standalone Binary (Downloaded from GitHub Releases)
# ------------------------------------------------------------------------------
elif [ "$INSTALL_TYPE" = "2" ]; then
    echo -e "${BLUE}Detecting latest release from GitHub...${NC}"
    LATEST_JSON=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null || echo "")
    
    BIN_URL=""
    if [ -n "$LATEST_JSON" ]; then
        BIN_URL=$(echo "$LATEST_JSON" | grep -o 'https://[^"]*antigravity-manager-linux-amd64.tar.gz' | head -n 1 || true)
    fi

    if [ -z "$BIN_URL" ]; then
        # Fallback to direct latest release URL
        BIN_URL="https://github.com/${REPO}/releases/latest/download/antigravity-manager-linux-amd64.tar.gz"
    fi

    echo -e "Downloading binary package from: ${BLUE}${BIN_URL}${NC}..."
    TMP_DIR=$(mktemp -d)
    if ! curl -fsSL -o "${TMP_DIR}/package.tar.gz" "${BIN_URL}"; then
        echo -e "${YELLOW}Release binary not yet available, falling back to Native Python installation...${NC}"
        INSTALL_TYPE="3"
    else
        tar -xzf "${TMP_DIR}/package.tar.gz" -C "${TMP_DIR}"
        BIN_DEST="${USER_HOME}/.local/bin"
        mkdir -p "${BIN_DEST}"
        mv "${TMP_DIR}/antigravity-manager" "${BIN_DEST}/antigravity-manager"
        chmod +x "${BIN_DEST}/antigravity-manager"
        rm -rf "${TMP_DIR}"

        echo -e "${BLUE}Configuring systemd user service...${NC}"
        SVC_DIR="${USER_HOME}/.config/systemd/user"
        mkdir -p "${SVC_DIR}"
        cat <<EOF > "${SVC_DIR}/antigravity-manager.service"
[Unit]
Description=Antigravity Manager Web Dashboard
After=network.target

[Service]
Type=simple
Environment=PORT=${DEFAULT_PORT}
Environment=HOST_USER=${CURRENT_USER}
Environment=USER_HOME=${USER_HOME}
ExecStart=${BIN_DEST}/antigravity-manager
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable --now antigravity-manager.service
        if command -v loginctl &> /dev/null; then
            loginctl enable-linger "${CURRENT_USER}" || true
        fi

        echo ""
        echo -e "${GREEN}============================================================${NC}"
        echo -e "${GREEN}🎉 Standalone Binary Installed & Running!${NC}"
        echo -e "Web UI is live at: ${BLUE}http://${LOCAL_IP}:${DEFAULT_PORT}${NC}"
        echo -e "Service Status:   ${BLUE}systemctl --user status antigravity-manager${NC}"
        echo -e "${GREEN}============================================================${NC}"
        exit 0
    fi
fi

# ------------------------------------------------------------------------------
# Option 3: Native Python Script
# ------------------------------------------------------------------------------
if [ "$INSTALL_TYPE" = "3" ]; then
    echo -e "${BLUE}Installing native python service...${NC}"
    APP_DIR="${USER_HOME}/.antigravity-manager"
    mkdir -p "${APP_DIR}"
    
    echo -e "Downloading server.py..."
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/main/server.py" -o "${APP_DIR}/server.py"
    chmod +x "${APP_DIR}/server.py"

    SVC_DIR="${USER_HOME}/.config/systemd/user"
    mkdir -p "${SVC_DIR}"
    cat <<EOF > "${SVC_DIR}/antigravity-manager.service"
[Unit]
Description=Antigravity Manager Web Dashboard
After=network.target

[Service]
Type=simple
Environment=PORT=${DEFAULT_PORT}
Environment=HOST_USER=${CURRENT_USER}
Environment=USER_HOME=${USER_HOME}
ExecStart=/usr/bin/python3 -u ${APP_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now antigravity-manager.service
    if command -v loginctl &> /dev/null; then
        loginctl enable-linger "${CURRENT_USER}" || true
    fi

    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}🎉 Python Service Installed & Running!${NC}"
    echo -e "Web UI is live at: ${BLUE}http://${LOCAL_IP}:${DEFAULT_PORT}${NC}"
    echo -e "Service Status:   ${BLUE}systemctl --user status antigravity-manager${NC}"
    echo -e "${GREEN}============================================================${NC}"
fi
