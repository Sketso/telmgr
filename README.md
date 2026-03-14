# telmgr

Менеджер пользователей для [Telemt MTProxy](https://github.com/telemt/telemt) — быстрого MTProto прокси для Telegram на Rust.

Форк [An0nX/telemt-docker](https://github.com/An0nX/telemt-docker) с добавленным CLI для управления пользователями.

> 🤖 Проект создавался совместно с AI (Claude, Anthropic).

---

## Установка
```bash
git clone https://github.com/Sketso/telmgr.git
cd telmgr
bash scripts/install.sh
```

Скрипт установит Docker (если нет), создаст конфиг, запустит прокси и установит `telmgr`.

> UFW не устанавливается автоматически — если он есть, порт откроется сам. Если нет — открой вручную.

---

## telmgr — управление пользователями
```bash
telmgr user list                   # список пользователей
telmgr user add <name> [days]      # добавить (days=0 — бессрочно)
telmgr user delete <name>          # удалить
telmgr user disable <name>         # отключить
telmgr user enable <name>          # включить
telmgr user limit <name> <days>    # установить лимит (0 — снять)
telmgr user link <name>            # показать ссылку для подключения
telmgr user import <name>          # импортировать существующего юзера из конфига
```

При добавлении с лимитом — автоматически создаётся cron на отключение пользователя.

### Переменные окружения

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
