# telmgr

Менеджер пользователей для [Telemt MTProxy](https://github.com/telemt/telemt) — быстрого MTProto прокси для Telegram на Rust.

Форк [An0nX/telemt-docker](https://github.com/An0nX/telemt-docker) с добавленным CLI для управления пользователями.

> 🤖 Проект создавался совместно с AI (Claude, Anthropic).

---

## Быстрый старт

### 1. Генерируем секрет и создаём конфиг

```bash
mkdir -p ~/telemt && cd ~/telemt
openssl rand -hex 16
```

Создай `telemt.toml` (см. [пример конфига](telemt.toml.example)).

### 2. Запускаем прокси

```bash
docker compose up -d
docker compose logs -f
```

### 3. Устанавливаем telmgr

```bash
git clone https://github.com/Sketso/telmgr.git
cp telmgr/telmgr /usr/local/bin/telmgr
chmod +x /usr/local/bin/telmgr
```

### 4. Открываем порт

```bash
sudo ufw allow 2053/tcp comment "Telemt MTProxy"
```

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

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEMT_DIR` | `/root/telemt` | Путь к директории с конфигом |
| `TELEMT_HOST` | `your.domain.com` | Публичный хост прокси |
| `TELEMT_PORT` | `2053` | Публичный порт прокси |

---

## Требования

- Ubuntu 22.04 / 24.04
- Docker + Docker Compose
- Python 3.10+
- UFW
