# telmgr

Менеджер пользователей для [Telemt MTProxy](https://github.com/telemt/telemt) — быстрого MTProto прокси для Telegram на Rust.

Форк [An0nX/telemt-docker](https://github.com/An0nX/telemt-docker) с добавленным CLI и Telegram ботом для управления пользователями.

> 🤖 Проект создавался совместно с AI (Claude, Anthropic).

---

## Установка
```bash
git clone https://github.com/Sketso/telmgr.git
cd telmgr
source scripts/install.sh
```

Скрипт установит Docker (если нет), создаст конфиг, запустит прокси, установит `telmgr` и опционально настроит Telegram бота как systemd сервис.

> UFW не устанавливается автоматически — если он есть, порт откроется сам. Если нет — открой вручную.

---

## Удаление
```bash
bash scripts/uninstall.sh
```

Скрипт предложит создать бэкап перед удалением.

---

## telmgr CLI

### Пользователи
```bash
telmgr user list                   # список пользователей
telmgr user add <name> [days]      # добавить (days=0 — бессрочно)
telmgr user delete <name>          # удалить
telmgr user disable <name>         # отключить
telmgr user enable <name>          # включить
telmgr user limit <name> <days>    # установить лимит (0 — снять)
telmgr user link <name>            # показать ссылку для подключения
telmgr user import <name>          # импортировать существующего юзера из конфига
telmgr user expire [days]          # юзеры с истекающим сроком (default: 7 дней)
```

При добавлении с лимитом — автоматически создаётся cron на отключение пользователя.

### Админы
```bash
telmgr admin list                  # список админов
telmgr admin add <telegram_id>     # добавить админа
telmgr admin delete <telegram_id>  # удалить админа
```

### Прокси
```bash
telmgr status                      # статус контейнера и статистика
telmgr logs [lines]                # логи контейнера (default: 50)
telmgr update                      # обновить Docker образ
telmgr backup                      # создать бэкап
telmgr restore <file>              # восстановить из бэкапа
```

> При восстановлении на новом сервере домен должен совпадать с оригинальным — иначе ссылки пользователей перестанут работать.

---

## Telegram бот

Устанавливается опционально через `install.sh`. Для создания бота — [@BotFather](https://t.me/BotFather), для получения своего Telegram ID — [@userinfobot](https://t.me/userinfobot).

Запускается как systemd сервис `telmgr-bot`. Управление:
```bash
systemctl status telmgr-bot
systemctl restart telmgr-bot
journalctl -u telmgr-bot -f
```

---

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `TELEMT_HOST` | ✅ | Публичный домен или IP сервера |
| `TELEMT_DIR` | — | Путь к директории с конфигом (default: `/root/telemt`) |
| `TELEMT_PORT` | — | Публичный порт прокси (default: `2053`) |

---

## Требования

- Ubuntu 22.04 / 24.04
- Docker + Docker Compose
- Python 3.10+
- UFW (опционально)
- Права root или sudo (рекомендуется запускать от root)
