#!/bin/bash
set -e

RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
BOLD="\033[1m"
RESET="\033[0m"

ok()   { echo -e "${GREEN}✅ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠️  $1${RESET}"; }
info() { echo -e "${CYAN}$1${RESET}"; }
err()  { echo -e "${RED}❌ $1${RESET}"; exit 1; }

echo -e "\n${BOLD}=== telmgr uninstaller ===${RESET}\n"

[[ $EUID -ne 0 ]] && err "Запусти от root"

# Читаем TELEMT_DIR из .env если есть
ENV_FILE="${HOME}/telemt/.env"
if [ -f "$ENV_FILE" ]; then
    TELEMT_DIR=$(grep "^TELEMT_DIR=" "$ENV_FILE" | cut -d'=' -f2)
fi
TELEMT_DIR="${TELEMT_DIR:-$HOME/telemt}"

# === Бэкап ===
read -p "Создать бэкап перед удалением? [y/N]: " DO_BACKUP
if [[ "$DO_BACKUP" =~ ^[Yy]$ ]]; then
    if command -v telmgr &>/dev/null; then
        telmgr backup
    else
        warn "telmgr не найден — бэкап пропущен"
    fi
fi

# === Останавливаем systemd бота (старые установки) ===
if systemctl is-active --quiet telmgr-bot 2>/dev/null; then
    systemctl stop telmgr-bot
    systemctl disable telmgr-bot
    rm -f /etc/systemd/system/telmgr-bot.service
    systemctl daemon-reload
    ok "Systemd сервис бота удалён"
fi

# === Останавливаем Docker (прокси + бот) ===
if [ -f "$TELEMT_DIR/docker-compose.yml" ]; then
    docker compose -f "$TELEMT_DIR/docker-compose.yml" down
    ok "Docker контейнеры остановлены"
fi

# === Удаляем файлы ===
rm -rf "$TELEMT_DIR"
ok "Директория $TELEMT_DIR удалена"

rm -f /usr/local/bin/telmgr
rm -f /usr/local/bin/telmgr.py
ok "telmgr удалён из /usr/local/bin"

# === Чистим cron ===
crontab -l 2>/dev/null | grep -v "telmgr" | crontab - 2>/dev/null || true
ok "Cron задачи удалены"

echo ""
echo -e "${BOLD}=== Готово! telmgr полностью удалён ===${RESET}"
echo ""