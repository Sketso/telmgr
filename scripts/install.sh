#!/bin/bash
set -e

RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
BOLD="\033[1m"
RESET="\033[0m"

ok()   { echo -e "${GREEN}✅ $1${RESET}"; }
err()  { echo -e "${RED}❌ $1${RESET}"; exit 1; }
warn() { echo -e "${YELLOW}⚠️  $1${RESET}"; }
info() { echo -e "${CYAN}$1${RESET}"; }

echo -e "\n${BOLD}=== telmgr installer ===${RESET}\n"

TELMGR_BRANCH="${TELMGR_BRANCH:-master}"

# === Root? ===
[[ $EUID -ne 0 ]] && err "Run as root: sudo bash install.sh"

# === apt helper: refresh package lists once, then install ===
APT_UPDATED=false
export DEBIAN_FRONTEND=noninteractive   # не зависать на интерактивных промптах apt (needrestart/grub)
apt_install() {
    if ! $APT_UPDATED; then
        info "Updating package lists..."
        apt-get update -q || warn "apt-get update failed — trying to install anyway"
        APT_UPDATED=true
    fi
    apt-get install -y -q "$@"
}

# === Check existing installation ===
if command -v telmgr &>/dev/null; then
    echo -e "${YELLOW}telmgr is already installed.${RESET}"
    read -p "Upgrade (config unchanged)? [Y/n]: " DO_UPGRADE
    DO_UPGRADE=${DO_UPGRADE:-Y}
    if [[ "$DO_UPGRADE" =~ ^[Yy]$ ]]; then
        info "Updating telmgr..."
        curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/${TELMGR_BRANCH}/telmgr -o /usr/local/bin/telmgr
        chmod +x /usr/local/bin/telmgr
        cp /usr/local/bin/telmgr /usr/local/bin/telmgr.py
        ok "telmgr updated"

        ENV_FILE="${HOME}/telemt/.env"
        TELEMT_DIR_UPGRADE="${HOME}/telemt"
        if [ -f "$ENV_FILE" ]; then
            TELEMT_DIR_UPGRADE=$(grep "^TELEMT_DIR=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 || echo "$HOME/telemt")
        fi

        BOT_PY="$TELEMT_DIR_UPGRADE/bot.py"
        if [ -f "$BOT_PY" ]; then
            info "Updating bot.py..."
            curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/${TELMGR_BRANCH}/bot/bot.py -o "$BOT_PY"
            ok "bot.py updated"
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q telmgr-bot; then
                docker compose -f "$TELEMT_DIR_UPGRADE/docker-compose.yml" restart telmgr-bot
                ok "telmgr-bot restarted"
            fi
        fi
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q telmgr-api; then
            docker compose -f "$TELEMT_DIR_UPGRADE/docker-compose.yml" restart telmgr-api
            ok "telmgr-api restarted"
        fi
        echo ""
        echo -e "${BOLD}=== Upgrade complete ===${RESET}"
        exit 0
    fi
fi

# === curl (used to install Docker and fetch telmgr/bot.py) ===
if ! command -v curl &>/dev/null; then
    info "Installing curl..."
    apt_install curl
    ok "curl installed"
fi

# === Docker ===
if command -v docker &>/dev/null; then
    ok "Docker already installed"
else
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    ok "Docker installed"
fi

# === Docker Compose v2 plugin ===
if ! docker compose version &>/dev/null; then
    warn "Docker Compose plugin not found — installing..."
    apt_install docker-compose-plugin || err "Could not install docker-compose-plugin. Install Docker Compose v2 and re-run."
    ok "Docker Compose plugin installed"
fi

# === Python3 ===
if command -v python3 &>/dev/null; then
    ok "Python3 found"
else
    info "Installing Python3..."
    apt_install python3
    ok "Python3 installed"
fi

# === pip3 ===
if ! command -v pip3 &>/dev/null; then
    info "Installing python3-pip..."
    apt_install python3-pip
    ok "python3-pip installed"
fi

# === openssl (secrets + node TLS cert) ===
if ! command -v openssl &>/dev/null; then
    info "Installing openssl..."
    apt_install openssl
    ok "openssl installed"
fi

# === dnsutils ===
if ! command -v dig &>/dev/null; then
    info "Installing dnsutils..."
    apt_install dnsutils
    ok "dnsutils installed"
fi

# === Engine selection ===
echo ""
echo "Select proxy engine:"
echo "  [1] telemt-docker (default, recommended) — stable, low memory (~12 MB), hot-reload on user changes"
echo "  [2] mtproto.zig — advanced DPI bypass, recommended for Apple devices (~1 MB)"
echo "      Note: adding/removing users restarts the container (~1-2 sec downtime each time)"
read -p "Enter choice [1]: " ENGINE_CHOICE
ENGINE_CHOICE=${ENGINE_CHOICE:-1}

if [[ "$ENGINE_CHOICE" == "2" ]]; then
    PROXY_ENGINE=mtproto_zig
    PROXY_CONTAINER=mtproto-zig
    PROXY_IMAGE="ghcr.io/sleep3r/mtproto.zig:latest"
    DEFAULT_PORT=8443
    warn "Note: port 443 gives best DPI bypass results, but may already be in use on your server."
    info "First Docker build may take 3-5 minutes (compiling Zig)."
else
    PROXY_ENGINE=telemt
    PROXY_CONTAINER=telemt
    PROXY_IMAGE="whn0thacked/telemt-docker:latest"
    DEFAULT_PORT=2053
fi

# === Parameters ===
echo ""
read -p "Enter public domain or server IP: " TELEMT_HOST
TELEMT_HOST=$(echo "$TELEMT_HOST" | tr -cd '[:alnum:].-')
[[ -z "$TELEMT_HOST" ]] && err "Domain cannot be empty"

# === DNS check ===
if [[ ! "$TELEMT_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    info "Checking DNS for $TELEMT_HOST..."
    SERVER_IP=$(curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null \
             || curl -s --connect-timeout 5 https://ifconfig.me 2>/dev/null \
             || hostname -I | awk '{print $1}')
    DOMAIN_IP=$(dig +short "$TELEMT_HOST" A 2>/dev/null | grep -E '^[0-9.]+$' | tail -1)
    if [[ -z "$DOMAIN_IP" ]]; then
        warn "Domain $TELEMT_HOST has no A record yet."
        warn "Point it to $SERVER_IP before clients can connect."
        read -p "Continue anyway? [y/N]: " DNS_CONT
        [[ ! "$DNS_CONT" =~ ^[Yy]$ ]] && err "Aborted. Update DNS and re-run the script."
    elif [[ "$DOMAIN_IP" == "$SERVER_IP" ]]; then
        ok "DNS OK: $TELEMT_HOST → $SERVER_IP"
    else
        warn "DNS mismatch: $TELEMT_HOST → $DOMAIN_IP, but this server is $SERVER_IP"
        warn "Update DNS to point $TELEMT_HOST to $SERVER_IP."
        read -p "Continue anyway? [y/N]: " DNS_CONT
        [[ ! "$DNS_CONT" =~ ^[Yy]$ ]] && err "Aborted. Update DNS and re-run the script."
    fi
fi

read -p "Enter proxy port [$DEFAULT_PORT]: " TELEMT_PORT
TELEMT_PORT=$(echo "${TELEMT_PORT:-$DEFAULT_PORT}" | tr -cd '[:digit:]')

read -p "Enter first username [myproxy]: " FIRST_USER
FIRST_USER=${FIRST_USER:-myproxy}

# === Telegram Bot ===
echo ""
read -p "Install Telegram bot for management? [y/N]: " INSTALL_BOT
INSTALL_BOT_ENABLED=false
BOT_SLAVE=false
if [[ "$INSTALL_BOT" =~ ^[Yy]$ ]]; then
    INSTALL_BOT_ENABLED=true
    echo ""
    read -p "Mode: [M]aster (new bot) or [s]lave (connect to existing)? [M/s]: " BOT_MODE
    BOT_MODE=${BOT_MODE:-M}
    if [[ "$BOT_MODE" =~ ^[Ss]$ ]]; then
        BOT_SLAVE=true
        TELMGR_API_KEY=$(openssl rand -hex 16)
        read -p "API server port [8765]: " TELMGR_API_PORT
        TELMGR_API_PORT=${TELMGR_API_PORT:-8765}
    else
        echo ""
        info "To create a bot: message @BotFather in Telegram -> /newbot"
        info "To get your Telegram ID: message @userinfobot"
        echo ""
        read -p "Name of this server [Local]: " SERVER_NAME
        SERVER_NAME=${SERVER_NAME:-Local}
        read -p "Enter BOT_TOKEN from @BotFather: " BOT_TOKEN
        [[ -z "$BOT_TOKEN" ]] && err "BOT_TOKEN cannot be empty"
        read -p "Enter your Telegram ID (superadmin): " SUPER_ADMIN_ID
        [[ -z "$SUPER_ADMIN_ID" ]] && err "SUPER_ADMIN_ID cannot be empty"
    fi
fi

# === Directory ===
TELEMT_DIR="${TELEMT_DIR:-$HOME/telemt}"
mkdir -p "$TELEMT_DIR"
ok "Directory $TELEMT_DIR created"

# === Generate secret ===
SECRET=$(openssl rand -hex 16)

# === .env ===
cat > "$TELEMT_DIR/.env" << EOF
TELEMT_HOST=$TELEMT_HOST
TELEMT_PORT=$TELEMT_PORT
TELEMT_DIR=$TELEMT_DIR
PROXY_ENGINE=$PROXY_ENGINE
EOF
if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
    cat >> "$TELEMT_DIR/.env" << EOF
BOT_TOKEN=$BOT_TOKEN
SUPER_ADMIN_ID=$SUPER_ADMIN_ID
EOF
fi
if $BOT_SLAVE; then
    cat >> "$TELEMT_DIR/.env" << EOF
TELMGR_API_PORT=$TELMGR_API_PORT
TELMGR_API_KEY=$TELMGR_API_KEY
EOF
fi
ok ".env created"

# === Proxy config & service definition ===
if [[ "$PROXY_ENGINE" == "mtproto_zig" ]]; then
    PROXY_CONFIG="$TELEMT_DIR/mtproto-zig.toml"
    cat > "$PROXY_CONFIG" << EOF
[general]
use_middle_proxy = true

[server]
port = $TELEMT_PORT
bind_addr = "0.0.0.0"

[censorship]
tls_domain = "$TELEMT_HOST"
fast_mode = true
drs = true

[monitor]
enabled = false

[metrics]
enabled = false

[upstream]
mode = "auto"

[access.users]
$FIRST_USER = "$SECRET"
EOF
    ok "mtproto-zig.toml created"

    TELEMT_SERVICE=$(cat << EOF
services:
  mtproto-zig:
    image: ghcr.io/sleep3r/mtproto.zig:latest
    container_name: mtproto-zig
    restart: always
    volumes:
      - ./mtproto-zig.toml:/etc/mtproto-proxy/config.toml:ro
    ports:
      - "$TELEMT_PORT:$TELEMT_PORT/tcp"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
)
else
    PROXY_CONFIG="$TELEMT_DIR/telemt.toml"
    cat > "$PROXY_CONFIG" << EOF
show_link = ["$FIRST_USER"]

[general]
prefer_ipv6 = false
fast_mode = true
use_middle_proxy = false

[general.modes]
classic = false
secure = false
tls = true

[server]
port = $TELEMT_PORT
listen_addr_ipv4 = "0.0.0.0"
listen_addr_ipv6 = "::"

[censorship]
tls_domain = "$TELEMT_HOST"
mask = true
mask_port = 443
fake_cert_len = 2048

[access]
replay_check_len = 65536
ignore_time_skew = false

[access.users]
$FIRST_USER = "$SECRET"

[[upstreams]]
type = "direct"
enabled = true
weight = 10
EOF
    ok "telemt.toml created"

    TELEMT_SERVICE=$(cat << EOF
services:
  telemt:
    image: whn0thacked/telemt-docker:latest
    container_name: telemt
    restart: always
    environment:
      RUST_LOG: "info"
    volumes:
      - ./telemt.toml:/etc/telemt.toml:ro
    ports:
      - "$TELEMT_PORT:$TELEMT_PORT/tcp"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE
    read_only: true
    tmpfs:
      - /tmp:rw,nosuid,nodev,noexec,size=16m
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
)
fi

if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
    # Master: telemt + telmgr-bot
    curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/${TELMGR_BRANCH}/bot/bot.py -o "$TELEMT_DIR/bot.py"
    ok "bot.py copied to $TELEMT_DIR"

    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
$TELEMT_SERVICE

  telmgr-bot:
    image: python:3.11-slim
    container_name: telmgr-bot
    restart: always
    working_dir: /app
    volumes:
      - ./bot.py:/app/bot.py:ro
      - ./.env:/app/.env:ro
      - ./.telmgr-meta.json:/app/data/.telmgr-meta.json
      - ./.telmgr-admins.json:/app/data/.telmgr-admins.json
      - ./.telmgr-servers.json:/app/data/.telmgr-servers.json
      - $PROXY_CONFIG:/app/data/$(basename "$PROXY_CONFIG")
      - /usr/local/bin/telmgr:/usr/local/bin/telmgr.py:ro
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TELEMT_DIR=/app/data
    env_file:
      - .env
    command: >
      sh -c "pip install aiogram==3.26.0 python-dotenv apscheduler --quiet &&
             python3 bot.py"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
elif $BOT_SLAVE; then
    # Self-signed TLS cert for the node API (encrypts master<->node, pinned by fingerprint)
    CERT_FP=""
    if [[ "$TELEMT_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        SAN="subjectAltName=IP:$TELEMT_HOST"
    else
        SAN="subjectAltName=DNS:$TELEMT_HOST"
    fi
    CERT_VOL=""
    if openssl req -x509 -newkey rsa:2048 -nodes \
         -keyout "$TELEMT_DIR/.telmgr-api.key" -out "$TELEMT_DIR/.telmgr-api.crt" \
         -days 3650 -subj "/CN=telmgr-api" -addext "$SAN" >/dev/null 2>&1; then
        chmod 644 "$TELEMT_DIR/.telmgr-api.key"
        CERT_FP=$(openssl x509 -in "$TELEMT_DIR/.telmgr-api.crt" -noout -fingerprint -sha256 \
                  | sed 's/.*=//; s/://g' | tr 'A-Z' 'a-z')
        CERT_VOL=$'      - ./.telmgr-api.crt:/app/data/.telmgr-api.crt:ro\n      - ./.telmgr-api.key:/app/data/.telmgr-api.key:ro'
        ok "TLS cert for node API generated"
    else
        warn "Could not generate TLS cert — node API will run over plain HTTP"
    fi

    # Slave: telemt + telmgr-api
    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
$TELEMT_SERVICE

  telmgr-api:
    image: python:3.11-slim
    container_name: telmgr-api
    restart: always
    working_dir: /app
    volumes:
      - ./.env:/app/.env:ro
      - ./.telmgr-meta.json:/app/data/.telmgr-meta.json
      - $PROXY_CONFIG:/app/data/$(basename "$PROXY_CONFIG")
$CERT_VOL
      - /usr/local/bin/telmgr:/usr/local/bin/telmgr.py:ro
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TELEMT_DIR=/app/data
    env_file:
      - .env
    ports:
      - "$TELMGR_API_PORT:$TELMGR_API_PORT/tcp"
    command: >
      sh -c "pip install python-dotenv --quiet &&
             python3 /usr/local/bin/telmgr.py serve"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
else
    # No bot
    echo "$TELEMT_SERVICE" > "$TELEMT_DIR/docker-compose.yml"
fi
ok "docker-compose.yml created"

# === Install telmgr ===
curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/${TELMGR_BRANCH}/telmgr -o /usr/local/bin/telmgr
chmod +x /usr/local/bin/telmgr
cp /usr/local/bin/telmgr /usr/local/bin/telmgr.py
# python-dotenv удобен, но не обязателен: у telmgr есть встроенный парсер .env,
# поэтому установку делаем нефатальной и совместимой со старым pip.
pip3 install python-dotenv --break-system-packages -q 2>/dev/null \
    || pip3 install python-dotenv -q 2>/dev/null \
    || warn "python-dotenv не установлен — telmgr будет читать .env встроенным парсером"
ok "telmgr installed to /usr/local/bin"

# === First user metadata ===
cat > "$TELEMT_DIR/.telmgr-meta.json" << EOF
{
  "$FIRST_USER": {
    "secret": "$SECRET",
    "created": "$(date +%Y-%m-%d)",
    "expires": null,
    "disabled": false
  }
}
EOF
ok "User metadata created"

# === admins.json ===
if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
    cat > "$TELEMT_DIR/.telmgr-admins.json" << EOF
{
  "admins": {
    "$SUPER_ADMIN_ID": {
      "username": null,
      "full_name": "superadmin",
      "is_super": true
    }
  },
  "pending": {}
}
EOF
    ok ".telmgr-admins.json created"

    # Server registry for master
    cat > "$TELEMT_DIR/.telmgr-servers.json" << EOF
{
  "servers": {
    "local": {
      "name": "$SERVER_NAME",
      "url": "local",
      "api_key": null,
      "host": "$TELEMT_HOST",
      "port": "$TELEMT_PORT"
    }
  }
}
EOF
    ok ".telmgr-servers.json created"
fi

# === Start Docker ===
cd "$TELEMT_DIR"
docker compose up -d
ok "Telemt started"

if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
    info "Waiting for bot to start..."
    sleep 15
    BOT_STATUS=$(docker inspect --format='{{.State.Status}}' telmgr-bot 2>/dev/null)
    BOT_LOGS=$(docker logs telmgr-bot 2>&1 | tail -5)
    if echo "$BOT_LOGS" | grep -q "Bot started"; then
        ok "Bot is running in Docker"
    elif [ "$BOT_STATUS" = "running" ]; then
        ok "Bot is running in Docker"
        info "Logs: docker compose logs -f telmgr-bot"
    else
        warn "Bot failed to start! Check logs:"
        echo "$BOT_LOGS"
        warn "Logs: docker compose logs telmgr-bot"
    fi
fi

# === UFW ===
echo ""
read -p "Set up UFW firewall (recommended)? [Y/n]: " SETUP_UFW
SETUP_UFW=${SETUP_UFW:-Y}
if [[ "$SETUP_UFW" =~ ^[Yy]$ ]]; then
    if ! command -v ufw &>/dev/null; then
        info "Installing UFW..."
        apt_install ufw
        ok "UFW installed"
    fi
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow ssh comment "SSH"
    ufw allow "$TELEMT_PORT/tcp" comment "MTProxy"
    if $BOT_SLAVE; then
        ufw allow "$TELMGR_API_PORT/tcp" comment "telmgr API"
    fi
    ufw --force enable
    ok "UFW enabled"
    ufw status numbered
fi

# === fail2ban ===
echo ""
read -p "Install fail2ban — SSH brute-force protection (recommended)? [Y/n]: " SETUP_F2B
SETUP_F2B=${SETUP_F2B:-Y}
if [[ "$SETUP_F2B" =~ ^[Yy]$ ]]; then
    if ! command -v fail2ban-client &>/dev/null; then
        info "Installing fail2ban..."
        apt_install fail2ban
        ok "fail2ban installed"
    fi
    cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
EOF
    systemctl enable fail2ban --quiet
    systemctl restart fail2ban
    ok "fail2ban configured (SSH: 5 failed attempts → 1h ban)"
fi

# === Summary ===
DOMAIN_HEX=$(python3 -c "import sys; print(sys.argv[1].encode().hex())" "$TELEMT_HOST")
LINK="tg://proxy?server=${TELEMT_HOST}&port=${TELEMT_PORT}&secret=ee${SECRET}${DOMAIN_HEX}"

echo ""
echo -e "${BOLD}=== Done! ===${RESET}"
echo -e "User:  ${CYAN}$FIRST_USER${RESET}"
echo -e "Link:  ${CYAN}$LINK${RESET}"
echo ""
if $BOT_SLAVE; then
    if [[ -n "$CERT_FP" ]]; then
        SCHEME="https"; TOKEN="${TELMGR_API_KEY}:${CERT_FP}"
    else
        SCHEME="http"; TOKEN="$TELMGR_API_KEY"
    fi
    echo -e "${BOLD}=== Slave server: registration ===${RESET}"
    echo -e "API port:  ${CYAN}$TELMGR_API_PORT${RESET}"
    if [[ -n "$CERT_FP" ]]; then
        echo -e "Security:  ${GREEN}HTTPS + cert pinning${RESET}"
    fi
    echo ""
    echo -e "On the master server run:"
    echo -e "  ${BOLD}telmgr server add \"Name\" $SCHEME://$TELEMT_HOST:$TELMGR_API_PORT $TOKEN${RESET}"
    echo ""
    warn "Open port $TELMGR_API_PORT/tcp in the firewall if needed (ideally only from the master's IP)."
    if [[ -z "$CERT_FP" ]]; then
        warn "API is plain HTTP — key and user secrets travel unencrypted. Restrict the port to the master's IP."
    fi
    echo ""
fi
echo -e "Manage: ${BOLD}telmgr --help${RESET}"
echo ""
