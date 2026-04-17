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

# === Проверка существующей установки ===
if command -v telmgr &>/dev/null; then
    echo -e "${YELLOW}telmgr уже установлен.${RESET}"
    read -p "Обновить (без изменения конфига)? [Y/n]: " DO_UPGRADE
    DO_UPGRADE=${DO_UPGRADE:-Y}
    if [[ "$DO_UPGRADE" =~ ^[Yy]$ ]]; then
        info "Обновляем telmgr..."
        curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/telmgr -o /usr/local/bin/telmgr
        chmod +x /usr/local/bin/telmgr
        cp /usr/local/bin/telmgr /usr/local/bin/telmgr.py
        ok "telmgr обновлён"

        ENV_FILE="${HOME}/telemt/.env"
        TELEMT_DIR_UPGRADE="${HOME}/telemt"
        if [ -f "$ENV_FILE" ]; then
            TELEMT_DIR_UPGRADE=$(grep "^TELEMT_DIR=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 || echo "$HOME/telemt")
        fi

        BOT_PY="$TELEMT_DIR_UPGRADE/bot.py"
        if [ -f "$BOT_PY" ]; then
            info "Обновляем bot.py..."
            curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/bot/bot.py -o "$BOT_PY"
            ok "bot.py обновлён"
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q telmgr-bot; then
                docker compose -f "$TELEMT_DIR_UPGRADE/docker-compose.yml" restart telmgr-bot
                ok "telmgr-bot перезапущен"
            fi
        fi
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q telmgr-api; then
            docker compose -f "$TELEMT_DIR_UPGRADE/docker-compose.yml" restart telmgr-api
            ok "telmgr-api перезапущен"
        fi
        echo ""
        echo -e "${BOLD}=== Обновление завершено ===${RESET}"
        exit 0
    fi
fi

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
    info "Устанавливаем Python3..."
    apt-get install -y python3 -q
    ok "Python3 установлен"
fi

# === pip3 ===
if ! command -v pip3 &>/dev/null; then
    info "Устанавливаем python3-pip..."
    apt-get install -y python3-pip -q
    ok "python3-pip установлен"
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
BOT_SLAVE=false
if [[ "$INSTALL_BOT" =~ ^[Yy]$ ]]; then
    INSTALL_BOT_ENABLED=true
    echo ""
    read -p "Режим: [M]aster (новый бот) или [s]lave (подключить к существующему)? [M/s]: " BOT_MODE
    BOT_MODE=${BOT_MODE:-M}
    if [[ "$BOT_MODE" =~ ^[Ss]$ ]]; then
        BOT_SLAVE=true
        TELMGR_API_KEY=$(openssl rand -hex 16)
        read -p "Порт API сервера [8765]: " TELMGR_API_PORT
        TELMGR_API_PORT=${TELMGR_API_PORT:-8765}
    else
        echo ""
        info "Для создания бота напиши @BotFather в Telegram -> /newbot"
        info "Для получения своего Telegram ID напиши @userinfobot"
        echo ""
        read -p "Название этого сервера [Local]: " SERVER_NAME
        SERVER_NAME=${SERVER_NAME:-Local}
        read -p "Введи BOT_TOKEN от @BotFather: " BOT_TOKEN
        [[ -z "$BOT_TOKEN" ]] && err "BOT_TOKEN не может быть пустым"
        read -p "Введи свой Telegram ID (суперадмин): " SUPER_ADMIN_ID
        [[ -z "$SUPER_ADMIN_ID" ]] && err "SUPER_ADMIN_ID не может быть пустым"
    fi
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
TELEMT_SERVICE=$(cat << EOF
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
)

if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
    # Master: telemt + telmgr-bot
    curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/bot/bot.py -o "$TELEMT_DIR/bot.py"
    ok "bot.py скопирован в $TELEMT_DIR"

    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
$TELEMT_SERVICE

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
      - ./.telmgr-servers.json:/app/data/.telmgr-servers.json
      - ./telemt.toml:/app/data/telemt.toml
      - /usr/local/bin/telmgr:/usr/local/bin/telmgr.py:ro
    environment:
      - TELEMT_DIR=/app/data
    env_file:
      - .env
    command: >
      sh -c "pip install aiogram python-dotenv apscheduler --quiet &&
             python3 bot.py"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
elif $BOT_SLAVE; then
    # Slave: telemt + telmgr-api
    cat > "$TELEMT_DIR/docker-compose.yml" << EOF
$TELEMT_SERVICE

  telmgr-api:
    image: python:3.11-slim
    container_name: telmgr-api
    restart: unless-stopped
    working_dir: /app
    volumes:
      - ./.env:/app/.env:ro
      - ./.telmgr-meta.json:/app/data/.telmgr-meta.json
      - ./telemt.toml:/app/data/telemt.toml
      - /usr/local/bin/telmgr:/usr/local/bin/telmgr.py:ro
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
ok "docker-compose.yml создан"

# === Устанавливаем telmgr ===
curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/telmgr -o /usr/local/bin/telmgr
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
    ok ".telmgr-admins.json создан"

    # Реестр серверов для master
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
    ok ".telmgr-servers.json создан"
fi

# === UFW ===
if command -v ufw &>/dev/null; then
    ufw allow "$TELEMT_PORT/tcp" comment "Telemt MTProxy"
    ok "Порт $TELEMT_PORT открыт в UFW"
    if $BOT_SLAVE; then
        ufw allow "$TELMGR_API_PORT/tcp" comment "telmgr API"
        ok "Порт $TELMGR_API_PORT открыт в UFW"
    fi
fi

# === Запускаем Docker ===
cd "$TELEMT_DIR"
docker compose up -d
ok "Telemt запущен"

if $INSTALL_BOT_ENABLED && ! $BOT_SLAVE; then
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
if $BOT_SLAVE; then
    echo -e "${BOLD}=== Slave сервер: регистрация ===${RESET}"
    echo -e "API порт:  ${CYAN}$TELMGR_API_PORT${RESET}"
    echo -e "API ключ:  ${CYAN}$TELMGR_API_KEY${RESET}"
    echo ""
    echo -e "На Master сервере выполни:"
    echo -e "  ${BOLD}telmgr server add \"Название\" http://$TELEMT_HOST:$TELMGR_API_PORT $TELMGR_API_KEY${RESET}"
    echo ""
fi
echo -e "Управление: ${BOLD}telmgr --help${RESET}"
echo ""
