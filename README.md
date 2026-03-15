# telmgr

Инструмент управления пользователями для [Telemt](https://github.com/telemt/telemt) — MTProto прокси-сервера на Rust.

Форк [An0nX/telemt-docker](https://github.com/An0nX/telemt-docker) с добавленным CLI и Telegram ботом для управления пользователями.

> 🤖 Проект создавался совместно с AI (Claude, Anthropic) в учебных целях.

---

## ⚠️ Отказ от ответственности

Этот проект создан исключительно в **образовательных целях** для изучения DevOps практик, Docker, Python и разработки CLI инструментов.

Использование MTProto прокси может быть ограничено или запрещено законодательством вашей страны. Вы несёте полную ответственность за соблюдение применимых местных законов и нормативных актов. Авторы проекта не несут ответственности за любое использование данного программного обеспечения.

---

## Установка
```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/scripts/install.sh)
```

Скрипт установит Docker (если нет), создаст конфиг, запустит сервис и установит `telmgr`. Опционально настроит Telegram бота в Docker.

> UFW не устанавливается автоматически — если он есть, порт откроется сам. Если нет — открой вручную.

---

## Удаление
```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/scripts/uninstall.sh)
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
telmgr user limit <name> <days>    # установить лимит (0 — снять и включить)
telmgr user link <name>            # показать ссылку для подключения
telmgr user import <name>          # импортировать существующего юзера из конфига
telmgr user expire [days]          # юзеры с истекающим сроком (default: 7 дней)
```

### Админы
```bash
telmgr admin list                  # список админов
telmgr admin add <telegram_id>     # добавить админа
telmgr admin delete <telegram_id>  # удалить админа
```

### Прокси
```bash
telmgr status                      # статус сервиса и статистика
telmgr logs [lines]                # логи контейнера (default: 50)
telmgr update                      # обновить Docker образ
telmgr backup                      # создать бэкап
telmgr restore <file>              # восстановить из бэкапа
```

> При восстановлении на новом сервере домен должен совпадать с оригинальным.

---

## Telegram бот

Устанавливается опционально через `install.sh`. Для создания бота — [@BotFather](https://t.me/BotFather), для получения своего Telegram ID — [@userinfobot](https://t.me/userinfobot).

Запускается в Docker вместе с сервисом. Управление:
```bash
docker compose -f ~/telemt/docker-compose.yml logs -f telmgr-bot
docker compose -f ~/telemt/docker-compose.yml restart telmgr-bot
```

---

## Конфигурация

Все настройки хранятся в `~/telemt/.env`:

| Переменная | Обязательная | Описание |
|---|---|---|
| `TELEMT_HOST` | ✅ | Публичный домен или IP сервера |
| `TELEMT_PORT` | — | Публичный порт (default: `2053`) |
| `TELEMT_DIR` | — | Путь к директории с конфигом (default: `~/telemt`) |
| `BOT_TOKEN` | — | Токен Telegram бота от @BotFather |
| `SUPER_ADMIN_ID` | — | Telegram ID суперадмина |

---

## Требования

- Ubuntu 22.04 / 24.04
- Docker + Docker Compose
- Python 3.10+
- UFW (опционально)
- Права root (рекомендуется)
