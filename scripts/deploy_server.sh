#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;36m'
PLAIN='\033[0m'

if [ "$(id -u)" != "0" ]; then
  echo -e "${RED}错误: 请在服务器上以 root 权限运行。${PLAIN}"
  exit 1
fi

INSTALL_DIR="${AIMILI_INSTALL_DIR:-/opt/aimilivpn}"
SERVICE_NAME="${AIMILI_SERVICE_NAME:-aimilivpn}"
MODE="${AIMILI_MODE:-${SERVICE_MODE:-gateway}}"
UI_HOST="${UI_HOST:-::}"
UI_PORT="${UI_PORT:-8787}"
LOCAL_PROXY_HOST="${LOCAL_PROXY_HOST:-127.0.0.1}"
LOCAL_PROXY_PORT="${LOCAL_PROXY_PORT:-7928}"
REPO_URL="${AIMILI_REPO_URL:-https://github.com/yigehui/aimili-vpngate.git}"
BRANCH="${AIMILI_BRANCH:-main}"
SOURCE_DIR="${AIMILI_SOURCE_DIR:-}"

POOL_SIZE="${POOL_SIZE:-50}"
POOL_PORT_BASE="${POOL_PORT_BASE:-52000}"
POOL_PUBLIC_HOST="${POOL_PUBLIC_HOST:-}"
POOL_LISTEN_HOST="${POOL_LISTEN_HOST:-0.0.0.0}"
POOL_MAX_STARTING="${POOL_MAX_STARTING:-5}"
POOL_API_RETURN_CREDENTIALS="${POOL_API_RETURN_CREDENTIALS:-true}"
POOL_API_TOKEN="${POOL_API_TOKEN:-}"
POOL_PROXY_USER="${POOL_PROXY_USER:-}"
POOL_PROXY_PASS="${POOL_PROXY_PASS:-}"

if [ "$MODE" != "gateway" ] && [ "$MODE" != "pool" ]; then
  echo -e "${RED}错误: AIMILI_MODE/SERVICE_MODE 只能是 gateway 或 pool。${PLAIN}"
  exit 1
fi

if [ "$MODE" = "pool" ] && [ -z "$POOL_PUBLIC_HOST" ]; then
  DETECTED_IP="$(curl -4 -s --max-time 4 https://api.ipify.org || curl -s --max-time 4 https://ifconfig.me || true)"
  if [ -n "$DETECTED_IP" ]; then
    POOL_PUBLIC_HOST="$DETECTED_IP"
    echo -e "${YELLOW}未设置 POOL_PUBLIC_HOST，已自动检测为: ${POOL_PUBLIC_HOST}${PLAIN}"
  else
    echo -e "${RED}错误: 池模式必须设置 POOL_PUBLIC_HOST=服务器公网IP或域名。${PLAIN}"
    exit 1
  fi
fi

detect_pkg_mgr() {
  OS_TYPE=""
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_TYPE="${ID:-}"
  fi
  case "$OS_TYPE" in
    ubuntu|debian) echo "apt" ;;
    alpine) echo "apk" ;;
    centos|rhel|rocky|almalinux|fedora|ol|amzn) echo "yum" ;;
    *) echo "unknown" ;;
  esac
}

install_deps() {
  local mgr="$1"
  echo -e "${BLUE}[1/5] 安装系统依赖...${PLAIN}"
  case "$mgr" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -q || true
      apt-get install -y openvpn curl git ca-certificates iptables iproute2 psmisc python3 rsync
      ;;
    apk)
      apk update || true
      apk add openvpn curl git ca-certificates iptables iproute2 psmisc python3 bash rsync
      ;;
    yum)
      if command -v dnf >/dev/null 2>&1; then PM=dnf; else PM=yum; fi
      $PM install -y epel-release || true
      $PM install -y openvpn curl git ca-certificates iptables iproute psmisc python3 rsync || \
        $PM install -y openvpn curl git ca-certificates iptables iproute2 psmisc python3 rsync
      ;;
    *)
      echo -e "${RED}错误: 不支持的系统，请手动安装 openvpn/curl/git/python3/iproute2/iptables。${PLAIN}"
      exit 1
      ;;
  esac
}

install_source() {
  echo -e "${BLUE}[2/5] 部署项目代码到 ${INSTALL_DIR}...${PLAIN}"
  if [ -z "$SOURCE_DIR" ] && [ -f "$(pwd)/vpngate_manager.py" ]; then
    SOURCE_DIR="$(pwd)"
  fi

  if [ -n "$SOURCE_DIR" ] && [ -f "${SOURCE_DIR}/vpngate_manager.py" ]; then
    mkdir -p "$INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete \
        --exclude '.git' \
        --exclude '.worktrees' \
        --exclude '__pycache__' \
        --exclude 'vpngate_data' \
        "${SOURCE_DIR}/" "${INSTALL_DIR}/"
    else
      cp -a "${SOURCE_DIR}/." "$INSTALL_DIR/"
    fi
  else
    if [ -d "${INSTALL_DIR}/.git" ]; then
      git -C "$INSTALL_DIR" fetch --all || true
      git -C "$INSTALL_DIR" checkout "$BRANCH" || true
      git -C "$INSTALL_DIR" reset --hard "origin/${BRANCH}" || git -C "$INSTALL_DIR" pull origin "$BRANCH"
    else
      rm -rf "$INSTALL_DIR"
      git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || git clone "$REPO_URL" "$INSTALL_DIR"
    fi
  fi
}

write_env() {
  echo -e "${BLUE}[3/5] 写入运行配置 /etc/default/${SERVICE_NAME}...${PLAIN}"
  mkdir -p /etc/default
  cat > "/etc/default/${SERVICE_NAME}" <<EOF
SERVICE_MODE=${MODE}
UI_HOST=${UI_HOST}
UI_PORT=${UI_PORT}
LOCAL_PROXY_HOST=${LOCAL_PROXY_HOST}
LOCAL_PROXY_PORT=${LOCAL_PROXY_PORT}
POOL_SIZE=${POOL_SIZE}
POOL_PORT_BASE=${POOL_PORT_BASE}
POOL_PUBLIC_HOST=${POOL_PUBLIC_HOST}
POOL_LISTEN_HOST=${POOL_LISTEN_HOST}
POOL_MAX_STARTING=${POOL_MAX_STARTING}
POOL_API_RETURN_CREDENTIALS=${POOL_API_RETURN_CREDENTIALS}
EOF
  if [ -n "$POOL_API_TOKEN" ]; then
    printf 'POOL_API_TOKEN=%s\n' "$POOL_API_TOKEN" >> "/etc/default/${SERVICE_NAME}"
  fi
  if [ -n "$POOL_PROXY_USER" ]; then
    printf 'POOL_PROXY_USER=%s\n' "$POOL_PROXY_USER" >> "/etc/default/${SERVICE_NAME}"
  fi
  if [ -n "$POOL_PROXY_PASS" ]; then
    printf 'POOL_PROXY_PASS=%s\n' "$POOL_PROXY_PASS" >> "/etc/default/${SERVICE_NAME}"
  fi
  chmod 600 "/etc/default/${SERVICE_NAME}" || true
}

write_service() {
  echo -e "${BLUE}[4/5] 配置系统服务...${PLAIN}"
  if command -v systemctl >/dev/null 2>&1; then
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=AimiliVPN OpenVPN Manager with HTTP/SOCKS5 Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-/etc/default/${SERVICE_NAME}
EnvironmentFile=-${INSTALL_DIR}/.env
ExecStart=/usr/bin/python3 vpngate_manager.py
Restart=always
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
  elif command -v rc-service >/dev/null 2>&1; then
    cat > "/etc/init.d/${SERVICE_NAME}" <<EOF
#!/sbin/openrc-run
description="AimiliVPN OpenVPN Manager with HTTP/SOCKS5 Proxy"
command="/usr/bin/python3"
command_args="${INSTALL_DIR}/vpngate_manager.py"
command_background="yes"
directory="${INSTALL_DIR}"
pidfile="/run/${SERVICE_NAME}.pid"
depend() {
  need net
  after firewall
}
EOF
    chmod +x "/etc/init.d/${SERVICE_NAME}"
    rc-update add "$SERVICE_NAME" default
  else
    echo -e "${YELLOW}警告: 未检测到 systemd/OpenRC，请手动运行 ${INSTALL_DIR}/vpngate_manager.py。${PLAIN}"
  fi
}

open_firewall() {
  echo -e "${BLUE}[5/5] 尝试放行防火墙端口...${PLAIN}"
  if command -v ufw >/dev/null 2>&1; then
    ufw allow "${UI_PORT}/tcp" || true
    if [ "$MODE" = "gateway" ] && [ "$LOCAL_PROXY_HOST" != "127.0.0.1" ]; then
      ufw allow "${LOCAL_PROXY_PORT}/tcp" || true
    fi
    if [ "$MODE" = "pool" ]; then
      local end_port=$((POOL_PORT_BASE + POOL_SIZE - 1))
      ufw allow "${POOL_PORT_BASE}:${end_port}/tcp" || true
    fi
  elif command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --add-port="${UI_PORT}/tcp" --permanent || true
    if [ "$MODE" = "gateway" ] && [ "$LOCAL_PROXY_HOST" != "127.0.0.1" ]; then
      firewall-cmd --add-port="${LOCAL_PROXY_PORT}/tcp" --permanent || true
    fi
    if [ "$MODE" = "pool" ]; then
      local end_port=$((POOL_PORT_BASE + POOL_SIZE - 1))
      firewall-cmd --add-port="${POOL_PORT_BASE}-${end_port}/tcp" --permanent || true
    fi
    firewall-cmd --reload || true
  else
    echo -e "${YELLOW}未检测到 ufw/firewalld；如有云安全组，请手动放行端口。${PLAIN}"
  fi
}

start_service() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart "${SERVICE_NAME}.service"
  elif command -v rc-service >/dev/null 2>&1; then
    rc-service "$SERVICE_NAME" restart || rc-service "$SERVICE_NAME" start
  else
    nohup /usr/bin/python3 "${INSTALL_DIR}/vpngate_manager.py" >/var/log/${SERVICE_NAME}.log 2>&1 &
  fi
}

PKG_MGR="$(detect_pkg_mgr)"
install_deps "$PKG_MGR"
install_source
write_env
write_service
open_firewall
start_service

PUBLIC_IP="$(curl -4 -s --max-time 4 https://api.ipify.org || echo '服务器IP')"
SECRET_PATH="$(python3 - <<PY
import json
from pathlib import Path
p=Path("${INSTALL_DIR}")/"vpngate_data"/"ui_auth.json"
try:
    print(json.loads(p.read_text(encoding="utf-8")).get("secret_path","EJsW2EeBo9lY"))
except Exception:
    print("EJsW2EeBo9lY")
PY
)"

echo -e "${GREEN}部署完成。${PLAIN}"
echo -e "管理页面: ${BLUE}http://${PUBLIC_IP}:${UI_PORT}/${SECRET_PATH}/${PLAIN}"
if [ "$MODE" = "pool" ]; then
  END_PORT=$((POOL_PORT_BASE + POOL_SIZE - 1))
  echo -e "代理池端口: ${BLUE}${POOL_PORT_BASE}-${END_PORT}/tcp${PLAIN}"
  echo -e "API Token 文件: ${BLUE}${INSTALL_DIR}/vpngate_data/pool_secrets.json${PLAIN}"
fi
