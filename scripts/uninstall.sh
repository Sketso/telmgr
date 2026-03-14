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

echo -e "\n${BOLD}=== telmgr uninstaller ===${RESET}\n"

[[ $EUID -ne 0 ]] && echo -e "${RED}❌ Запусти от root${RESET}" && exit 1

TELEMT_DIR="${TELEMT_DIR:-/root/telemt}"

# Бэкап перед удалением
read -p "Создать бэкап перед удалением? [Y/n]: " DO_BACKUP
if [[ ! "$DO_BACKUP" =~ ^[Nn]$ ]]; then
    if command -v telmgr &>/dev/null; then
        telmgr backup
    else
        warn "telmgr не найден — бэкап пропущен"
    fi
fi

# Останавливаем бота
if systemctl is-active --quiet telmgr-bot 2>/dev/null; then
    systemctl stop telmgr-bot
    systemctl disable telmgr-bot
    rm -f /etc/systemd/system/telmgr-bot.service
    systemctl daemon-reload
    ok "Systemd сервис бота удалён"
fi

# Останавливаем Docker
if [ -f "$TELEMT_DIR/docker-compose.yml" ]; then
    docker compose -f "$TELEMT_DIR/docker-compose.yml" down
    ok "Docker контейнер остановлен"
fi

# Удаляем файлы
rm -rf "$TELEMT_DIR"
ok "Директория $TELEMT_DIR удалена"

rm -f /usr/local/bin/telmgr
rm -f /usr/local/bin/telmgr.py
ok "telmgr удалён из /usr/local/bin"

# Чистим .bashrc
sed -i '/TELEMT_HOST/d; /TELEMT_PORT/d; /TELEMT_DIR/d; /# telmgr/d' ~/.bashrc
ok ".bashrc очищен"

# Чистим cron
crontab -l 2>/dev/null | grep -v "telmgr" | crontab - 2>/dev/null || true
ok "Cron задачи удалены"

echo ""
echo -e "${BOLD}=== Готово! telmgr полностью удалён ===${RESET}"
echo ""
