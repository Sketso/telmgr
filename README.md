# telmgr

**English** | [Русский](README.ru.md)

CLI and Telegram bot for managing users of an MTProto proxy server.

Supports two proxy engines: **Telemt** (Rust, stable) and **mtproto.zig** (Zig, advanced DPI bypass). Provides user management, backups, monitoring, and multi-server support through a single bot.

> 🤖 Built collaboratively with AI (Claude, Anthropic) for educational purposes.

---

## ⚠️ Disclaimer

This project was created for **educational purposes** to learn DevOps practices, Docker, Python, and CLI tool development.

The use of MTProto proxies may be restricted or prohibited by the laws of your country. You are solely responsible for complying with applicable local laws and regulations. The authors are not liable for any use of this software.

---

## Installation
```bash
bash <(curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/scripts/install.sh)
```

The script handles everything step by step:
- Installs Docker, Python, dependencies
- Lets you choose proxy engine (Telemt or mtproto.zig)
- Checks that the domain's DNS points to this server
- Creates config, starts the proxy in Docker
- Optionally sets up a Telegram bot
- Sets up UFW firewall and fail2ban (SSH brute-force protection)

Re-running the script on an already installed server will offer an **upgrade** (telmgr + bot.py, config unchanged). Alternatively, use `telmgr update`.

---

## Uninstall
```bash
telmgr uninstall
# or directly:
bash <(curl -Ls https://raw.githubusercontent.com/Sketso/telmgr/master/scripts/uninstall.sh)
```

The script will offer to create a backup before removal.

---

## telmgr CLI

### Users
```bash
telmgr user list                   # list users
telmgr user stats                  # stats: active / disabled / overdue
telmgr user add <name> [days]      # add user (days=0 — no expiry)
telmgr user delete <name>          # delete user
telmgr user disable <name>         # disable user
telmgr user enable <name>          # enable user
telmgr user limit <name> <days>    # set expiry limit (0 — remove and enable)
telmgr user link <name>            # show connection link
telmgr user import <name>          # import existing user from config
telmgr user expire [days]          # list users expiring soon (default: 7 days)
```

### Admins
```bash
telmgr admin list                  # list admins
telmgr admin add <telegram_id>     # add admin
telmgr admin delete <telegram_id>  # remove admin
```

### Servers (multi-server)
```bash
telmgr server list                              # list connected servers
telmgr server add "<name>" <url> <key>          # add remote server
telmgr server rename <id|name> "<new name>"     # rename server
telmgr server test <id|name>                    # check server availability
telmgr server remove <id|name>                  # remove from registry
```

### Bot
```bash
telmgr bot setup                   # set up Telegram bot (master) or API server (slave)
telmgr bot restart                 # restart Telegram bot
telmgr bot logs [lines]            # Telegram bot logs (default: 50)
```

### Proxy & maintenance
```bash
telmgr status                      # proxy, bot, and user status
telmgr logs [lines]                # proxy container logs (default: 50)
telmgr restart                     # restart proxy
telmgr update                      # update telmgr and bot.py from GitHub
telmgr coreupdate                  # update proxy Docker image
telmgr backup                      # create local backup (tar.gz)
telmgr backup auto enable <interval>  # enable auto-backup (e.g. 3h, 7d, 2w, 1m)
telmgr backup auto disable            # disable auto-backup
telmgr backup auto                    # show auto-backup status
telmgr restore <file>              # restore from backup
telmgr uninstall                   # fully remove telmgr
```

> When restoring on a new server, the domain and port must match the originals — otherwise user links will stop working.

---

## Telegram Bot

Installed optionally via `install.sh`. Supports two modes:

- **Master** — full-featured bot, manages the local server and can connect remote ones
- **Slave** — runs an HTTP API on the server, which the master bot connects to

To create a bot — [@BotFather](https://t.me/BotFather). To get your Telegram ID — [@userinfobot](https://t.me/userinfobot).

If the bot was not configured during installation, it can be added later:
```bash
telmgr bot setup
```

### Permissions

- **Superadmin** — full access to all users and servers
- **Admin** — manages only their own users; attempting to modify another admin's user is blocked
- User names are globally unique per server

### Bot commands

| Command | Description |
|---|---|
| `/start` | Main menu |
| `/backup` | Collect and send backups from all servers to Telegram |
| `/backup_auto` | Configure automatic backup schedule |

`/backup_auto` syntax: `/backup_auto on <interval>` or `/backup_auto off`

Interval format: `Nh` (hours), `Nd` (days), `Nw` (weeks), `Nm` (months). Examples: `3h`, `7d`, `2w`, `1m`.

Backups are sent to the superadmin as `.tar.gz` files containing `telemt.toml`, `.env`, and metadata. Restore with `telmgr restore <file>` after copying the file to the server.

### Multiple servers

The master bot can manage multiple servers. On each additional server:
```bash
telmgr update
telmgr bot setup   # choose slave → get the API key and registration command
```

On the master, register the new server:
```bash
telmgr server add "Name" http://<IP>:8765 <API_KEY>
```

Once added, a server selector button appears in the bot. The admin list is shared across all servers. When `/backup` is triggered, the master bot collects backups from all registered servers automatically.

---

## Configuration

All settings are stored in `~/telemt/.env`:

| Variable | Required | Description |
|---|---|---|
| `TELEMT_HOST` | ✅ | Public domain or IP of the server |
| `TELEMT_PORT` | — | Public port (default: `2053`) |
| `TELEMT_DIR` | — | Path to config directory (default: `~/telemt`) |
| `PROXY_ENGINE` | — | `telemt` (default) or `mtproto_zig` |
| `BOT_TOKEN` | — | Telegram bot token (master only) |
| `SUPER_ADMIN_ID` | — | Superadmin Telegram ID (master only) |
| `TELMGR_API_PORT` | — | HTTP API port (slave only, default: `8765`) |
| `TELMGR_API_KEY` | — | HTTP API authorization key (slave only) |

---

## Requirements

- Ubuntu 22.04 / 24.04
- Root access
- Docker, Python, UFW, fail2ban — installed automatically by the script
