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

# === Root? ===
[[ $EUID -ne 0 ]] && err "Запусти скрипт от root: sudo bash install.sh"

# === UFW ===
if command -v ufw &>/dev/null; then
    ok "UFW найден"
else
    warn "UFW не установлен — порт нужно открыть вручную после установки"
fi

# === Docker ===
if command -v docker &>/dev/null; then
    ok "Docker уже установлен"
else
    info "Устанавливаем Docker..."
    curl -fsSL https://get.docker.com | sh
    ok "Docker установлен"
fi

# === Python3 ===
if command -v python3 &>/dev/null; then
    ok "Python3 найден"
else
    err "Python3 не найден — установи его вручную: apt install python3"
fi

# === Запрашиваем параметры ===
echo ""
read -p "Введи публичный домен или IP сервера: " TELEMT_HOST
TELEMT_HOST=$(echo "$TELEMT_HOST" | tr -cd '[:alnum:].-')
[[ -z "$TELEMT_HOST" ]] && err "Домен не может быть пустым"

read -p "Введи порт прокси [2053]: " TELEMT_PORT
TELEMT_PORT=$(echo "${TELEMT_PORT:-2053}" | tr -cd '[:digit:]')

read -p "Введи имя первого пользователя [myproxy]: " FIRST_USER
FIRST_USER=${FIRST_USER:-myproxy}

# === Telegram Bot ===
echo ""
read -p "Установить Telegram бота для управления? [y/N]: " INSTALL_BOT
INSTALL_BOT_ENABLED=false
if [[ "$INSTALL_BOT" =~ ^[Yy]$ ]]; then
    INSTALL_BOT_ENABLED=true
    echo ""
    info "Для создания бота напиши @BotFather в Telegram -> /newbot"
    info "Для получения своего Telegram ID напиши @userinfobot"
    echo ""
    read -p "Введи BOT_TOKEN от @BotFather: " BOT_TOKEN
    [[ -z "$BOT_TOKEN" ]] && err "BOT_TOKEN не может быть пустым"
    read -p "Введи свой Telegram ID (суперадмин): " SUPER_ADMIN_ID
    [[ -z "$SUPER_ADMIN_ID" ]] && err "SUPER_ADMIN_ID не может быть пустым"
fi

# === Директория ===
TELEMT_DIR="${TELEMT_DIR:-$HOME/telemt}"
mkdir -p "$TELEMT_DIR"
ok "Директория $TELEMT_DIR создана"

# === Генерируем секрет ===
SECRET=$(openssl rand -hex 16)

# === .env ===
cat > "$TELEMT_DIR/.env" << EOF
TELEMT_HOST=$TELEMT_HOST
TELEMT_PORT=$TELEMT_PORT
TELEMT_DIR=$TELEMT_DIR
EOF
if $INSTALL_BOT_ENABLED; then
    cat >> "$TELEMT_DIR/.env" << EOF
BOT_TOKEN=$BOT_TOKEN
SUPER_ADMIN_ID=$SUPER_ADMIN_ID
EOF
fi
ok ".env создан"

# === telemt.toml ===
cat > "$TELEMT_DIR/telemt.toml" << EOF
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
ok "telemt.toml создан"

# === docker-compose.yml ===
if $INSTALL_BOT_ENABLED; then
    # Копируем бота
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp "$SCRIPT_DIR/../bot/bot.py" "$TELEMT_DIR/bot.py"
    ok "bot.py скопирован в $TELEMT_DIR"

    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
services:
  telemt:
    image: whn0thacked/telemt-docker:latest
    container_name: telemt
    restart: unless-stopped
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

  telmgr-bot:
    image: python:3.11-slim
    container_name: telmgr-bot
    restart: unless-stopped
    working_dir: /app
    volumes:
      - ./bot.py:/app/bot.py:ro
      - ./.env:/app/.env:ro
      - ./.telmgr-meta.json:/app/data/.telmgr-meta.json
      - ./.telmgr-admins.json:/app/data/.telmgr-admins.json
      - /usr/local/bin/telmgr:/usr/local/bin/telmgr.py:ro
    environment:
      - TELEMT_DIR=/app/data
    env_file:
      - .env
    command: >
      sh -c "pip install aiogram python-dotenv --quiet &&
             python3 bot.py"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
else
    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
services:
  telemt:
    image: whn0thacked/telemt-docker:latest
    container_name: telemt
    restart: unless-stopped
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
fi
ok "docker-compose.yml создан"

# === Устанавливаем telmgr ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/../telmgr" /usr/local/bin/telmgr
chmod +x /usr/local/bin/telmgr
cp /usr/local/bin/telmgr /usr/local/bin/telmgr.py
pip3 install python-dotenv --break-system-packages -q
ok "telmgr установлен в /usr/local/bin"

# === Метаданные первого юзера ===
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
ok "Метаданные созданы"

# === admins.json ===
if $INSTALL_BOT_ENABLED; then
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
    ok ".telmgr-admins.json создан"
fi

# === UFW ===
if command -v ufw &>/dev/null; then
    ufw allow "$TELEMT_PORT/tcp" comment "Telemt MTProxy"
    ok "Порт $TELEMT_PORT открыт в UFW"
fi

# === Запускаем Docker ===
cd "$TELEMT_DIR"
docker compose up -d
ok "Telemt запущен"

if $INSTALL_BOT_ENABLED; then
    info "Ожидаем запуска бота..."
    sleep 15
    BOT_STATUS=$(docker inspect --format='{{.State.Status}}' telmgr-bot 2>/dev/null)
    BOT_LOGS=$(docker logs telmgr-bot 2>&1 | tail -5)
    if echo "$BOT_LOGS" | grep -q "Бот запущен"; then
        ok "Бот запущен в Docker"
    elif [ "$BOT_STATUS" = "running" ]; then
        ok "Бот запущен в Docker"
        info "Логи: docker compose logs -f telmgr-bot"
    else
        warn "Бот не запустился! Проверь логи:"
        echo "$BOT_LOGS"
        warn "Логи: docker compose logs telmgr-bot"
    fi
fi

# === Итог ===
DOMAIN_HEX=$(echo -n "$TELEMT_HOST" | xxd -p)
LINK="tg://proxy?server=${TELEMT_HOST}&port=${TELEMT_PORT}&secret=ee${SECRET}${DOMAIN_HEX}"

echo ""
echo -e "${BOLD}=== Готово! ===${RESET}"
echo -e "Пользователь: ${CYAN}$FIRST_USER${RESET}"
echo -e "Ссылка:       ${CYAN}$LINK${RESET}"
echo ""
echo -e "Управление: ${BOLD}telmgr --help${RESET}"
echo ""