#!/usr/bin/env python3

import asyncio
import os
import sys
import re
import json
import html
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

load_dotenv(os.path.join(os.path.expanduser('~'), 'telemt', '.env'))

BOT_TOKEN = os.getenv('BOT_TOKEN')
SUPER_ADMIN_ID = int(os.getenv('SUPER_ADMIN_ID'))
TELEMT_DIR = os.getenv('TELEMT_DIR', os.path.join(os.path.expanduser('~'), 'telemt'))
ADMINS_PATH = os.path.join(TELEMT_DIR, '.telmgr-admins.json')
SERVERS_PATH = os.path.join(TELEMT_DIR, '.telmgr-servers.json')

# Импортируем функции из telmgr
import importlib.util
spec = importlib.util.spec_from_file_location("telmgr", "/usr/local/bin/telmgr.py")
assert spec is not None, "telmgr не найден в /usr/local/bin/telmgr.py"
telmgr = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(telmgr)

# === Server clients ===

class LocalServerClient:
    def __init__(self, info: dict):
        self.name = info.get("name", "Local")
        self.host = info.get("host", "")
        self.port = info.get("port", "")

    def _wrap(self, fn, *args):
        import io, contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn(*args)
            return None
        except SystemExit as e:
            raise ValueError(buf.getvalue().strip() or str(e))
        except Exception as e:
            raise ValueError(str(e))

    async def get_users(self) -> dict:
        loop = asyncio.get_event_loop()
        def _sync():
            content = telmgr.read_toml()
            toml_users = telmgr.get_users_from_toml(content)
            meta = telmgr.load_meta()
            result = {}
            for name, tdata in toml_users.items():
                m = meta.get(name, {})
                result[name] = {**m, "disabled": tdata["disabled"], "secret": tdata["secret"]}
            return result
        return await loop.run_in_executor(None, _sync)

    async def add_user(self, name: str, days: int, admin_id: int, admin_name: str, admin_username: str) -> dict:
        loop = asyncio.get_event_loop()
        def _sync():
            with telmgr.config_lock():
                content = telmgr.read_toml()
                users = telmgr.get_users_from_toml(content)
                if name in users:
                    raise ValueError(f"Юзер '{name}' уже существует")
                secret = telmgr.generate_secret()
                lines = content.splitlines()
                result_lines, inserted = [], False
                for line in lines:
                    result_lines.append(line)
                    if line.strip() == telmgr.USER_SECTION_HEADER and not inserted:
                        result_lines.append(f'{name} = "{telmgr.render_user_value(secret)}"')
                        inserted = True
                if not inserted:
                    raise ValueError(f"Секция {telmgr.USER_SECTION_HEADER} не найдена")
                telmgr.write_toml("\n".join(result_lines))
                expires = None
                if days > 0:
                    expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
                meta = telmgr.load_meta()
                meta[name] = {
                    "secret": secret,
                    "created": datetime.now().strftime("%Y-%m-%d"),
                    "expires": expires,
                    "disabled": False,
                    "admin_id": admin_id,
                    "admin_name": admin_name,
                    "admin_username": admin_username,
                }
                telmgr.save_meta(meta)
                if expires:
                    telmgr.add_cron(name, expires)
                telmgr.reload_proxy()
                return {"link": telmgr.build_link(secret), "expires": expires}
        return await loop.run_in_executor(None, _sync)

    async def delete_user(self, name: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._wrap(telmgr.cmd_delete, name))

    async def enable_user(self, name: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._wrap(telmgr.cmd_enable, name))

    async def disable_user(self, name: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._wrap(telmgr.cmd_disable, name))

    async def set_limit(self, name: str, days: int):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._wrap(telmgr.cmd_limit, name, days))

    async def get_link(self, name: str) -> str:
        loop = asyncio.get_event_loop()
        def _sync():
            content = telmgr.read_toml()
            users = telmgr.get_users_from_toml(content)
            if name not in users:
                raise ValueError(f"Юзер '{name}' не найден")
            return telmgr.build_link(users[name]["secret"])
        return await loop.run_in_executor(None, _sync)

    async def get_status(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, telmgr.local_status)

    async def get_backup(self) -> tuple:
        """Создать бэкап локально и вернуть (bytes, filename)."""
        import io, contextlib
        loop = asyncio.get_event_loop()
        def _sync():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                path = telmgr.cmd_backup()
            if not path or not os.path.exists(path):
                raise ValueError("Бэкап не создан")
            with open(path, "rb") as f:
                return f.read(), os.path.basename(path)
        return await loop.run_in_executor(None, _sync)


class RemoteServerClient:
    def __init__(self, info: dict):
        self.name = info.get("name", "Remote")
        self.host = info.get("host", "")
        self.port = info.get("port", "")
        self._base = info["url"].rstrip("/")
        self._key = info["api_key"]
        self._fp = info.get("cert_fp")

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        # pinned HTTPS (или legacy http) — единая реализация в telmgr
        try:
            return telmgr._api_request(self._base, self._key, method, path, body=body, cert_fp=self._fp)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Сервер недоступен: {e}")

    async def _req(self, method, path, body=None):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._request, method, path, body)

    async def get_users(self) -> dict:
        return await self._req("GET", "/users")

    async def add_user(self, name: str, days: int, admin_id: int, admin_name: str, admin_username: str) -> dict:
        return await self._req("POST", "/users", {
            "name": name, "days": days,
            "admin_id": admin_id, "admin_name": admin_name, "admin_username": admin_username
        })

    async def delete_user(self, name: str):
        await self._req("DELETE", f"/users/{name}")

    async def enable_user(self, name: str):
        await self._req("POST", f"/users/{name}/enable")

    async def disable_user(self, name: str):
        await self._req("POST", f"/users/{name}/disable")

    async def set_limit(self, name: str, days: int):
        await self._req("POST", f"/users/{name}/limit", {"days": days})

    async def get_link(self, name: str) -> str:
        data = await self._req("GET", f"/users/{name}/link")
        return data["link"]

    async def get_status(self) -> dict:
        return await self._req("GET", "/status")

    async def get_backup(self) -> tuple:
        import base64
        data = await self._req("POST", "/backup")
        return base64.b64decode(data["data"]), data["filename"]


# === Server context ===

def load_servers_config() -> dict:
    if not Path(SERVERS_PATH).exists():
        return {"servers": {"local": {
            "name": "Local", "url": "local", "api_key": None,
            "host": telmgr.PUBLIC_HOST or "", "port": telmgr.PUBLIC_PORT
        }}}
    with open(SERVERS_PATH) as f:
        return json.load(f)

def save_servers_config(data: dict):
    with open(SERVERS_PATH, "w") as f:
        json.dump(data, f, indent=2)

def has_multiple_servers() -> bool:
    cfg = load_servers_config()
    return len(cfg.get("servers", {})) > 1

_user_server_ctx: dict = {}
_user_list_ctx: dict = {}  # user_id -> (scope, page): откуда открыта карточка, для «К списку»

def get_user_server_id(user_id: int) -> str:
    if user_id in _user_server_ctx:
        return _user_server_ctx[user_id]
    cfg = load_servers_config()
    servers = cfg.get("servers", {})
    return "local" if "local" in servers else next(iter(servers), "local")

def set_user_server(user_id: int, server_id: str):
    _user_server_ctx[user_id] = server_id

def get_client(user_id: int):
    sid = get_user_server_id(user_id)
    cfg = load_servers_config()
    info_data = cfg["servers"].get(sid, cfg["servers"].get("local", {}))
    if info_data.get("url") == "local":
        return LocalServerClient(info_data)
    return RemoteServerClient(info_data)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# === Scheduler ===
def _disable_job_id(server_id: str, name: str) -> str:
    return "disable_" + server_id + "_" + name

def client_for_server(server_id: str):
    """Возвращает клиента для конкретного сервера (а не для контекста юзера)."""
    cfg = load_servers_config()
    info_data = cfg.get("servers", {}).get(server_id)
    if info_data is None:
        return None
    if info_data.get("url") == "local":
        return LocalServerClient(info_data)
    return RemoteServerClient(info_data)

async def disable_user_job(name: str, admin_id: int, server_id: str = "local"):
    try:
        client = client_for_server(server_id)
        if client is None:
            raise ValueError("сервер '" + server_id + "' не найден в реестре")
        await client.disable_user(name)
        try:
            await bot.send_message(admin_id, "⏰ Лимит истёк — юзер <b>" + name + "</b> отключён", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        try:
            await bot.send_message(SUPER_ADMIN_ID, "❌ Ошибка при автоотключении юзера " + name + ": " + str(e))
        except Exception:
            pass

def schedule_user_disable(name: str, expires: str, admin_id: int, server_id: str = "local"):
    dt = datetime.strptime(expires, "%Y-%m-%d").replace(hour=12, minute=0)
    if dt > datetime.now():
        job_id = _disable_job_id(server_id, name)
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(
            disable_user_job,
            trigger=DateTrigger(run_date=dt),
            args=[name, admin_id, server_id],
            id=job_id
        )

async def load_scheduled_jobs() -> list:
    """Сканирует все серверы (а не только локальный) и планирует авто-отключения.
    Возвращает список (name, admin_id, server_id) уже просроченных юзеров."""
    cfg = load_servers_config()
    overdue = []
    for sid, info_data in cfg.get("servers", {}).items():
        try:
            client = LocalServerClient(info_data) if info_data.get("url") == "local" else RemoteServerClient(info_data)
            users = await client.get_users()
        except Exception:
            # недоступный сервер пропускаем — переразберёмся при следующем рестарте/SIGHUP
            continue
        for name, data in users.items():
            if data.get('expires') and not data.get('disabled'):
                admin_id = data.get('admin_id') or SUPER_ADMIN_ID
                dt = datetime.strptime(data['expires'], "%Y-%m-%d").replace(hour=12, minute=0)
                if dt <= datetime.now():
                    overdue.append((name, admin_id, sid))
                else:
                    schedule_user_disable(name, data['expires'], admin_id, sid)
    return overdue

# === Admins storage ===

def load_admins() -> dict:
    if not Path(ADMINS_PATH).exists():
        return {"admins": {}, "pending": {}}
    with open(ADMINS_PATH) as f:
        return json.load(f)

def save_admins(data: dict):
    with open(ADMINS_PATH, 'w') as f:
        json.dump(data, f, indent=2)

def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    admins = load_admins()
    return str(user_id) in admins.get('admins', {})

def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID

def add_admin(user_id: int):
    data = load_admins()
    uid = str(user_id)
    pending = data.get('pending', {})
    if uid in pending:
        data['admins'][uid] = pending.pop(uid)
    else:
        data['admins'][uid] = {"username": None, "full_name": None}
    data['pending'] = pending
    save_admins(data)

def remove_admin(user_id: int):
    data = load_admins()
    uid = str(user_id)
    data.get('admins', {}).pop(uid, None)
    save_admins(data)

def add_pending(user_id: int, username: str, full_name: str):
    data = load_admins()
    uid = str(user_id)
    if uid not in data.get('admins', {}) and uid != str(SUPER_ADMIN_ID):
        data.setdefault('pending', {})[uid] = {
            "username": username,
            "full_name": full_name,
            "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        save_admins(data)

def remove_pending(user_id) -> bool:
    """Тихо убирает запрос из pending. True, если что-то удалили."""
    data = load_admins()
    removed = data.get('pending', {}).pop(str(user_id), None) is not None
    if removed:
        save_admins(data)
    return removed

def is_banned(user_id) -> bool:
    return str(user_id) in load_admins().get('banned', {})

def add_banned(user_id, username=None, full_name=None):
    """Бан: в чёрный список + убрать из pending. Заявки от него больше не доходят."""
    data = load_admins()
    data.setdefault('banned', {})[str(user_id)] = {
        "username": username,
        "full_name": full_name,
        "banned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    data.get('pending', {}).pop(str(user_id), None)
    save_admins(data)

def remove_banned(user_id) -> bool:
    data = load_admins()
    removed = data.get('banned', {}).pop(str(user_id), None) is not None
    if removed:
        save_admins(data)
    return removed

# --- Приглашение админа по username ---
# Bot API не умеет резолвить @username -> id, поэтому храним приглашённые
# username'ы и выдаём админа автоматически, когда человек впервые откроет бота.
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{4,32}$')

def _norm_username(s: str) -> str:
    return (s or "").strip().lstrip('@').lower()

def grant_admin(user_id, username=None, full_name=None):
    """Прямая выдача админа (с подписью), убирает из pending/invited/banned."""
    data = load_admins()
    data.setdefault('admins', {})[str(user_id)] = {"username": username, "full_name": full_name}
    data.get('pending', {}).pop(str(user_id), None)
    if username:
        data.get('invited', {}).pop(username.lower(), None)
    data.get('banned', {}).pop(str(user_id), None)
    save_admins(data)

def add_invited(username: str, by_id=None) -> str:
    """Добавляет username в приглашённые. Возвращает нормализованный username."""
    u = _norm_username(username)
    data = load_admins()
    data.setdefault('invited', {})[u] = {
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "by": by_id,
    }
    save_admins(data)
    return u

def remove_invited(username: str) -> bool:
    data = load_admins()
    removed = data.get('invited', {}).pop(_norm_username(username), None) is not None
    if removed:
        save_admins(data)
    return removed

def pop_invited_for(username: str) -> bool:
    """Если username приглашён — убрать из invited и вернуть True."""
    if not username:
        return False
    data = load_admins()
    if username.lower() in data.get('invited', {}):
        data['invited'].pop(username.lower(), None)
        save_admins(data)
        return True
    return False


# === FSM States ===

class AddUser(StatesGroup):
    waiting_name = State()
    waiting_days = State()   # своё число дней при добавлении

class CardLimit(StatesGroup):
    waiting_days = State()   # своё число дней для лимита из карточки

class AddServer(StatesGroup):
    waiting_name = State()
    waiting_url  = State()
    waiting_key  = State()

class InviteAdmin(StatesGroup):
    waiting_username = State()


# === Keyboards ===

def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = []
    if has_multiple_servers():
        sid = get_user_server_id(user_id)
        cfg = load_servers_config()
        server_name = cfg["servers"].get(sid, {}).get("name", "?")
        buttons.append([InlineKeyboardButton(text=f"🖥 {server_name} ▼", callback_data="select_server")])
    buttons += [
        [InlineKeyboardButton(text="➕ Добавить юзера", callback_data="add_user")],
        [
            InlineKeyboardButton(text="👥 Мои юзеры", callback_data="my_users"),
            InlineKeyboardButton(text="⏰ Истекают", callback_data="expiring_users"),
        ],
    ]
    if is_super_admin(user_id):
        buttons.append([InlineKeyboardButton(text="👑 Все юзеры", callback_data="all_users")])
        buttons.append([
            InlineKeyboardButton(text="📊 Статус", callback_data="status"),
            InlineKeyboardButton(text="📦 Бэкапы", callback_data="backups"),
        ])
        buttons.append([
            InlineKeyboardButton(text="➕ Добавить админа", callback_data="add_admin"),
            InlineKeyboardButton(text="➖ Удалить админа", callback_data="remove_admin"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Меню")]],
        resize_keyboard=True,
        persistent=True
    )

def admin_requests_kb() -> InlineKeyboardMarkup:
    """Экран «Добавить админа»: pending-запросы (одобрить/отклонить/бан) + вход в ЧС."""
    data = load_admins()
    pending = data.get('pending', {})
    banned = data.get('banned', {})
    buttons = []
    for uid, info in pending.items():
        username = info.get('username')
        full_name = info.get('full_name') or uid
        label = "@" + username if username else full_name
        when = info.get('requested_at', '')
        buttons.append([InlineKeyboardButton(
            text="✅ " + label + (" (" + when + ")" if when else ""),
            callback_data="approve_admin_" + uid
        )])
        buttons.append([
            InlineKeyboardButton(text="🚫 Отклонить", callback_data="reject_pending_" + uid),
            InlineKeyboardButton(text="⛔ Бан", callback_data="ban_pending_" + uid),
        ])
    invited = data.get('invited', {})
    buttons.append([InlineKeyboardButton(text="✍️ Пригласить по @username", callback_data="invite_username")])
    if invited:
        buttons.append([InlineKeyboardButton(
            text=f"✉️ Приглашённые ({len(invited)})", callback_data="invitelist")])
    if banned:
        buttons.append([InlineKeyboardButton(
            text=f"⛔ Чёрный список ({len(banned)})", callback_data="banlist")])
    buttons.append([InlineKeyboardButton(text="🏠 Меню", callback_data="umenu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def invitelist_kb() -> InlineKeyboardMarkup:
    invited = load_admins().get('invited', {})
    rows = []
    for uname in invited:
        rows.append([InlineKeyboardButton(text="🗑 @" + uname, callback_data="uninvite_" + uname)])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="add_admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def banlist_kb() -> InlineKeyboardMarkup:
    data = load_admins()
    banned = data.get('banned', {})
    rows = []
    for uid, info in banned.items():
        username = info.get('username')
        full_name = info.get('full_name') or uid
        label = "@" + username if username else full_name
        rows.append([InlineKeyboardButton(text="♻️ Разбанить " + label, callback_data="unban_" + uid)])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="add_admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admins_keyboard() -> InlineKeyboardMarkup:
    data = load_admins()
    admins = data.get('admins', {})
    if not admins:
        return None
    buttons = []
    for uid, info in admins.items():
        if int(uid) == SUPER_ADMIN_ID:
            continue
        username = info.get('username')
        full_name = info.get('full_name') or uid
        label = "@" + username if username else full_name
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data="revoke_admin_" + uid
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def owns_user(user_id: int, users: dict, name: str) -> bool:
    if is_super_admin(user_id):
        return True
    return str(users.get(name, {}).get('admin_id')) == str(user_id)

def format_users(users: dict) -> str:
    if not users:
        return "Юзеров нет"
    lines = []
    for name, data in users.items():
        status = "🔴" if data.get('disabled') else "🟢"
        expires = data.get('expires') or "∞"
        lines.append(status + " <b>" + esc(name) + "</b> — до " + esc(expires))
    return "\n".join(lines)


# === UI helpers (clickable user list + card) ===

PAGE_SIZE = 8  # юзеров на страницу списка

def esc(s) -> str:
    """Экранируем HTML — имена/username из Telegram могут содержать < > &."""
    return html.escape(str(s), quote=False)

CANCEL_ROW = [InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel")]

async def _edit_or_send(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Редактируем текущее сообщение (плавная навигация), иначе шлём новое."""
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

def _user_button_label(name: str, data: dict, with_owner: bool = False) -> str:
    st = "🔴" if data.get('disabled') else "🟢"
    exp = data.get('expires') or "∞"
    label = f"{st} {name} · {exp}"
    if with_owner:
        owner = data.get('admin_username')
        owner = ("@" + owner) if owner else (data.get('admin_name') or "CLI")
        label += f" · {owner}"
    return label

def users_list_kb(users: dict, scope: str, page: int = 0, with_owner: bool = False) -> InlineKeyboardMarkup:
    names = sorted(users.keys())
    pages = max(1, (len(names) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = []
    for name in names[start:start + PAGE_SIZE]:
        rows.append([InlineKeyboardButton(
            text=_user_button_label(name, users[name], with_owner),
            callback_data="uc:" + name
        )])
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀", callback_data=f"page:{scope}:{page - 1}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton(text="▶", callback_data=f"page:{scope}:{page + 1}"),
        ])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="umenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _list_title(scope: str, total: int) -> str:
    base = {"my": "👥 <b>Твои юзеры</b>", "all": "👑 <b>Все юзеры</b>",
            "exp": "⏰ <b>Истекают (≤5 дней)</b>"}.get(scope, "Юзеры")
    return f"{base} ({total}) — выбери для управления:"

async def _scope_users(cb: CallbackQuery, scope: str):
    """Возвращает (users_dict, with_owner) для нужного среза или (None, None) при ошибке/отказе."""
    client = get_client(cb.from_user.id)
    try:
        allu = await client.get_users()
    except Exception as e:
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)
        return None, None
    is_super = is_super_admin(cb.from_user.id)
    if scope == "all":
        if not is_super:
            await cb.answer("⛔ Нет доступа", show_alert=True)
            return None, None
        return allu, True
    if scope == "exp":
        soon = {}
        for name, data in allu.items():
            if str(data.get('admin_id')) != str(cb.from_user.id) and not is_super:
                continue
            if data.get('expires'):
                exp = datetime.strptime(data['expires'], "%Y-%m-%d")
                if (exp - datetime.now()).days <= 5:
                    soon[name] = data
        return soon, is_super
    my = {k: v for k, v in allu.items() if str(v.get('admin_id')) == str(cb.from_user.id)}
    return my, False

async def _render_user_list(cb: CallbackQuery, scope: str, page: int = 0):
    users, with_owner = await _scope_users(cb, scope)
    if users is None:
        return
    # запоминаем, из какого среза/страницы открыта карточка — для «К списку»
    pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
    _user_list_ctx[cb.from_user.id] = (scope, max(0, min(page, pages - 1)))
    if not users:
        empty = {"my": "👥 У тебя пока нет юзеров.", "all": "👑 Юзеров нет.",
                 "exp": "✅ Нет юзеров с истекающим сроком в ближайшие 5 дней."}.get(scope, "Пусто.")
        await _edit_or_send(cb, empty, main_keyboard(cb.from_user.id))
        return
    await _edit_or_send(cb, _list_title(scope, len(users)),
                        users_list_kb(users, scope, page, with_owner))

def user_card_text(name: str, data: dict, link: str = None) -> str:
    status = "🔴 отключён" if data.get('disabled') else "🟢 активен"
    created = data.get('created') or "—"
    expires = data.get('expires') or "∞"
    owner = data.get('admin_username')
    owner = ("@" + owner) if owner else (data.get('admin_name') or data.get('admin_id') or "CLI")
    text = (
        f"👤 <b>{esc(name)}</b>\n"
        f"Статус: {status}\n"
        f"Создан: {esc(created)}\n"
        f"Истекает: {esc(expires)}\n"
        f"Владелец: {esc(owner)}"
    )
    if link:
        # <code> делает ссылку тап-копируемой прямо в карточке
        text += f"\n\n🔗 <code>{esc(link)}</code>\n<i>(нажми на ссылку, чтобы скопировать)</i>"
    else:
        text += "\n\n🔗 <i>ссылка недоступна</i>"
    return text

def user_card_kb(name: str, data: dict) -> InlineKeyboardMarkup:
    toggle = ("▶️ Включить", "utog:" + name) if data.get('disabled') else ("⏸ Отключить", "utog:" + name)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle[0], callback_data=toggle[1])],
        [InlineKeyboardButton(text="⏱ Лимит", callback_data="ulim:" + name)],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="udel:" + name)],
        [
            InlineKeyboardButton(text="⬅️ К списку", callback_data="ulist"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="umenu"),
        ],
    ])

def days_presets_kb(prefix: str, name: str) -> InlineKeyboardMarkup:
    """prefix: 'addset' (добавление) | 'uset' (лимит из карточки).
    callback: <prefix>:<name>:<days>; своё число — <prefix>c:<name>."""
    presets = [("7 дн.", "7"), ("30 дн.", "30"), ("90 дн.", "90"), ("∞", "0")]
    row = [InlineKeyboardButton(text=t, callback_data=f"{prefix}:{name}:{d}") for t, d in presets]
    back_cb = ("uc:" + name) if prefix == "uset" else "cancel"
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="✏️ Своё число", callback_data=f"{prefix}c:{name}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
    ])

async def _load_owned(cb: CallbackQuery, name: str):
    """Возвращает (client, users) если юзер существует и принадлежит админу, иначе (None, None)."""
    client = get_client(cb.from_user.id)
    try:
        users = await client.get_users()
    except Exception as e:
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)
        return None, None
    if name not in users:
        await cb.answer("Юзер не найден (возможно, удалён)", show_alert=True)
        return None, None
    if not owns_user(cb.from_user.id, users, name):
        await cb.answer("⛔ Это юзер другого админа", show_alert=True)
        return None, None
    return client, users

async def _show_card(cb: CallbackQuery, name: str, users: dict, client=None):
    link = None
    try:
        c = client or get_client(cb.from_user.id)
        link = await c.get_link(name)
    except Exception:
        pass
    await _edit_or_send(cb, user_card_text(name, users[name], link), user_card_kb(name, users[name]))


# === Handlers ===

async def _try_grant_invited(user) -> bool:
    """Если username юзера в приглашённых — выдать админа и уведомить суперадмина. True, если выдали."""
    uname = getattr(user, 'username', None)
    if uname and pop_invited_for(uname):
        grant_admin(user.id, uname, getattr(user, 'full_name', None))
        try:
            await bot.send_message(
                SUPER_ADMIN_ID,
                "✅ Приглашённый @" + esc(uname) + " (ID: " + str(user.id) + ") активировал доступ админа.")
        except Exception:
            pass
        return True
    return False

async def _welcome_admin(message: Message, invited: bool = False):
    greet = "👋 Тебе выдан доступ админа!" if invited else "👋 Привет!"
    await message.answer(greet, reply_markup=menu_keyboard())
    await message.answer("Управление Telemt MTProxy:", reply_markup=main_keyboard(message.from_user.id))

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not is_admin(user_id):
        if is_banned(user_id):
            await message.answer("⛔ Нет доступа.")
            return
        if await _try_grant_invited(message.from_user):
            await _welcome_admin(message, invited=True)
            return
        add_pending(user_id, message.from_user.username, message.from_user.full_name)
        await message.answer("⛔ Нет доступа. Запрос отправлен администратору.")
        username = message.from_user.username
        name = message.from_user.full_name
        label = "@" + username if username else name
        approve_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Дать доступ", callback_data="approve_admin_" + str(user_id))],
            [InlineKeyboardButton(text="🚫 Отклонить", callback_data="reject_pending_" + str(user_id)),
             InlineKeyboardButton(text="⛔ Бан", callback_data="ban_pending_" + str(user_id))],
        ])
        await bot.send_message(
            SUPER_ADMIN_ID,
            "🔔 Новый запрос доступа:\n" + label + " (ID: " + str(user_id) + ")",
            reply_markup=approve_kb
        )
        return
    await message.answer("👋 Привет!", reply_markup=menu_keyboard())
    await message.answer("Управление Telemt MTProxy:", reply_markup=main_keyboard(user_id))

@dp.message(F.text == "📋 Меню")
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        if is_banned(message.from_user.id):
            await message.answer("⛔ Нет доступа.")
            return
        if await _try_grant_invited(message.from_user):
            await _welcome_admin(message, invited=True)
            return
        add_pending(message.from_user.id, message.from_user.username, message.from_user.full_name)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Запросить доступ", callback_data="request_access")]
        ])
        await message.answer("⛔ Нет доступа.", reply_markup=kb)
        return
    await message.answer("Управление Telemt MTProxy:", reply_markup=main_keyboard(message.from_user.id))

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))

@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _edit_or_send(cb, "Управление Telemt MTProxy:", main_keyboard(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data == "umenu")
async def cb_umenu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _edit_or_send(cb, "Управление Telemt MTProxy:", main_keyboard(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data == "request_access")
async def cb_request_access(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    add_pending(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    await cb.message.answer("✅ Запрос отправлен администратору.")
    username = cb.from_user.username
    name = cb.from_user.full_name
    label = "@" + username if username else name
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Дать доступ", callback_data="approve_admin_" + str(cb.from_user.id))],
        [InlineKeyboardButton(text="🚫 Отклонить", callback_data="reject_pending_" + str(cb.from_user.id)),
         InlineKeyboardButton(text="⛔ Бан", callback_data="ban_pending_" + str(cb.from_user.id))],
    ])
    await bot.send_message(
        SUPER_ADMIN_ID,
        "🔔 Повторный запрос доступа:\n" + label + " (ID: " + str(cb.from_user.id) + ")",
        reply_markup=approve_kb
    )
    await cb.answer()
# === User management ===

async def _notify_if_corrupt(e):
    msg = str(e)
    if "откат" in msg.lower() or "невалидный" in msg.lower():
        try:
            cfg_name = os.path.basename(telmgr.TOML_PATH)
            await bot.send_message(SUPER_ADMIN_ID, f"🚨 Конфиг {cfg_name} повреждён и откатан!\nОшибка: " + msg)
        except Exception:
            pass

# --- Добавление юзера ---
@dp.callback_query(F.data == "add_user")
async def cb_add_user(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddUser.waiting_name)
    await _edit_or_send(cb, "Введи имя нового юзера (латиница, цифры, _):",
                        InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]))
    await cb.answer()

@dp.message(AddUser.waiting_name)
async def add_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', name):
        await message.answer("❌ Только латинские буквы, цифры и _ (кириллица не поддерживается)")
        return
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
    except Exception as e:
        await message.answer("❌ Ошибка: " + str(e))
        await state.clear()
        return
    if name in users:
        await message.answer("❌ Юзер '" + esc(name) + "' уже существует, введи другое имя")
        return
    await state.clear()
    await message.answer("Срок для <b>" + esc(name) + "</b>:", parse_mode="HTML",
                         reply_markup=days_presets_kb("addset", name))

async def _do_add_user(actor, name: str, days: int) -> str:
    """Создаёт юзера (actor — Message или CallbackQuery). Возвращает текст-результат."""
    server_id = get_user_server_id(actor.from_user.id)
    client = get_client(actor.from_user.id)
    result = await client.add_user(name, days, actor.from_user.id,
                                   actor.from_user.full_name, actor.from_user.username)
    link = result["link"]
    expires = result.get("expires")
    if expires:
        schedule_user_disable(name, expires, actor.from_user.id, server_id)
    text = "✅ Юзер <b>" + esc(name) + "</b> добавлен\n"
    if expires:
        text += "📅 Истекает: " + expires + "\n"
    text += "🔗 <code>" + esc(link) + "</code>"
    return text

@dp.callback_query(F.data.startswith("addset:"))
async def cb_add_set_days(cb: CallbackQuery):
    _, name, days = cb.data.split(":")
    try:
        text = await _do_add_user(cb, name, int(days))
        await _edit_or_send(cb, text, main_keyboard(cb.from_user.id))
        await cb.answer("Готово")
    except Exception as e:
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)

@dp.callback_query(F.data.startswith("addsetc:"))
async def cb_add_set_custom(cb: CallbackQuery, state: FSMContext):
    name = cb.data.split(":", 1)[1]
    await state.set_state(AddUser.waiting_days)
    await state.update_data(name=name)
    await _edit_or_send(cb, "Введи число дней для <b>" + esc(name) + "</b> (0 = бессрочно):",
                        InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]))
    await cb.answer()

@dp.message(AddUser.waiting_days)
async def add_user_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число")
        return
    if days < 0:
        await message.answer("❌ Число дней не может быть отрицательным")
        return
    data = await state.get_data()
    name = data['name']
    await state.clear()
    try:
        text = await _do_add_user(message, name, days)
        await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    except Exception as e:
        await message.answer("❌ Ошибка: " + str(e))

# --- Списки юзеров (кликабельные) ---
@dp.callback_query(F.data == "my_users")
async def cb_my_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_user_list(cb, "my", 0)
    await cb.answer()

@dp.callback_query(F.data == "all_users")
async def cb_all_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_user_list(cb, "all", 0)
    await cb.answer()

@dp.callback_query(F.data == "expiring_users")
async def cb_expiring_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_user_list(cb, "exp", 0)
    await cb.answer()

@dp.callback_query(F.data.startswith("page:"))
async def cb_page(cb: CallbackQuery):
    _, scope, page = cb.data.split(":")
    await _render_user_list(cb, scope, int(page))
    await cb.answer()

@dp.callback_query(F.data == "ulist")
async def cb_back_to_list(cb: CallbackQuery):
    scope, page = _user_list_ctx.get(cb.from_user.id, ("my", 0))
    await _render_user_list(cb, scope, page)
    await cb.answer()

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

# --- Карточка юзера ---
@dp.callback_query(F.data.startswith("uc:"))
async def cb_user_card(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    client, users = await _load_owned(cb, name)
    if not client:
        return
    await _show_card(cb, name, users, client)
    await cb.answer()

@dp.callback_query(F.data.startswith("utog:"))
async def cb_user_toggle(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    client, users = await _load_owned(cb, name)
    if not client:
        return
    try:
        if users[name]['disabled']:
            await client.enable_user(name)
        else:
            await client.disable_user(name)
        users = await client.get_users()
        await _show_card(cb, name, users, client)
        await cb.answer("Готово")
    except Exception as e:
        await _notify_if_corrupt(e)
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)

@dp.callback_query(F.data.startswith("udelyes:"))
async def cb_user_delete(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    client, users = await _load_owned(cb, name)
    if not client:
        return
    try:
        await client.delete_user(name)
        await cb.answer("Удалён")
    except Exception as e:
        await _notify_if_corrupt(e)
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)
        return
    scope, page = _user_list_ctx.get(cb.from_user.id, ("my", 0))
    await _render_user_list(cb, scope, page)

@dp.callback_query(F.data.startswith("udel:"))
async def cb_user_delete_confirm(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    client, users = await _load_owned(cb, name)
    if not client:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data="udelyes:" + name),
         InlineKeyboardButton(text="⬅️ Отмена", callback_data="uc:" + name)],
    ])
    await _edit_or_send(cb, "Удалить юзера <b>" + esc(name) + "</b>? Действие необратимо.", kb)
    await cb.answer()

# --- Лимит из карточки ---
async def _apply_limit(actor, name: str, days: int):
    server_id = get_user_server_id(actor.from_user.id)
    client = get_client(actor.from_user.id)
    await client.set_limit(name, days)
    job_id = _disable_job_id(server_id, name)
    if days == 0:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    else:
        expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        schedule_user_disable(name, expires, actor.from_user.id, server_id)

@dp.callback_query(F.data.startswith("usetc:"))
async def cb_user_set_limit_custom(cb: CallbackQuery, state: FSMContext):
    name = cb.data.split(":", 1)[1]
    await state.set_state(CardLimit.waiting_days)
    await state.update_data(name=name)
    await _edit_or_send(cb, "Введи число дней для <b>" + esc(name) + "</b> (0 = снять лимит):",
                        InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]))
    await cb.answer()

@dp.callback_query(F.data.startswith("uset:"))
async def cb_user_set_limit(cb: CallbackQuery):
    _, name, days = cb.data.split(":")
    client, users = await _load_owned(cb, name)
    if not client:
        return
    try:
        await _apply_limit(cb, name, int(days))
        users = await client.get_users()
        await _show_card(cb, name, users, client)
        await cb.answer("Лимит обновлён")
    except Exception as e:
        await _notify_if_corrupt(e)
        await cb.answer("Ошибка: " + str(e)[:180], show_alert=True)

@dp.callback_query(F.data.startswith("ulim:"))
async def cb_user_limit(cb: CallbackQuery):
    name = cb.data.split(":", 1)[1]
    client, users = await _load_owned(cb, name)
    if not client:
        return
    await _edit_or_send(cb, "Лимит для <b>" + esc(name) + "</b> (0 = снять):",
                        days_presets_kb("uset", name))
    await cb.answer()

@dp.message(CardLimit.waiting_days)
async def card_limit_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число")
        return
    if days < 0:
        await message.answer("❌ Число дней не может быть отрицательным")
        return
    data = await state.get_data()
    name = data['name']
    await state.clear()
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
        if name not in users:
            await message.answer("❌ Юзер не найден")
            return
        if not owns_user(message.from_user.id, users, name):
            await message.answer("⛔ Это юзер другого админа")
            return
        await _apply_limit(message, name, days)
        if days == 0:
            await message.answer("✅ Лимит для <b>" + esc(name) + "</b> снят", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
        else:
            expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            await message.answer("✅ Лимит для <b>" + esc(name) + "</b>: до " + expires, parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
    except Exception as e:
        await _notify_if_corrupt(e)
        await message.answer("❌ Ошибка: " + str(e))


# === Admin management ===

def _requests_text() -> str:
    data = load_admins()
    n_pending = len(data.get('pending', {}))
    if n_pending:
        return f"Запросы доступа ({n_pending}) — одобрить / отклонить / забанить:"
    return "Новых запросов нет."

@dp.callback_query(F.data == "add_admin")
async def cb_add_admin(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await _edit_or_send(cb, _requests_text(), admin_requests_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("approve_admin_"))
async def cb_approve_admin(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = int(cb.data.replace("approve_admin_", ""))
    add_admin(uid)

    data = load_admins()
    info = data.get('admins', {}).get(str(uid), {})
    username = info.get('username')
    name = info.get('full_name') or str(uid)
    label = "@" + username if username else name

    await cb.message.answer("✅ Доступ выдан: " + label, reply_markup=main_keyboard(cb.from_user.id))

    # Уведомляем нового админа
    try:
        await bot.send_message(uid, "✅ Тебе выдан доступ к боту! Напиши /start")
    except Exception:
        pass

    await cb.answer()

@dp.callback_query(F.data.startswith("reject_pending_"))
async def cb_reject_pending(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = cb.data.replace("reject_pending_", "")
    remove_pending(uid)  # тихо, без уведомления заявителю
    await _edit_or_send(cb, "Запрос отклонён.\n\n" + _requests_text(), admin_requests_kb())
    await cb.answer("Отклонён")

@dp.callback_query(F.data.startswith("ban_pending_"))
async def cb_ban_pending(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = cb.data.replace("ban_pending_", "")
    info = load_admins().get('pending', {}).get(uid, {})
    add_banned(uid, info.get('username'), info.get('full_name'))  # + убирает из pending
    await _edit_or_send(cb, "⛔ Забанен — заявки от него больше не будут доходить.\n\n" + _requests_text(),
                        admin_requests_kb())
    await cb.answer("Забанен")

@dp.callback_query(F.data == "banlist")
async def cb_banlist(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    banned = load_admins().get('banned', {})
    if not banned:
        await _edit_or_send(cb, _requests_text(), admin_requests_kb())
        await cb.answer("Чёрный список пуст")
        return
    await _edit_or_send(cb, f"⛔ <b>Чёрный список</b> ({len(banned)}):", banlist_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("unban_"))
async def cb_unban(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = cb.data.replace("unban_", "")
    remove_banned(uid)
    banned = load_admins().get('banned', {})
    if banned:
        await _edit_or_send(cb, f"♻️ Разбанен.\n\n⛔ <b>Чёрный список</b> ({len(banned)}):", banlist_kb())
    else:
        await _edit_or_send(cb, "♻️ Разбанен. Чёрный список пуст.\n\n" + _requests_text(), admin_requests_kb())
    await cb.answer("Разбанен")

# --- Приглашение админа по username ---
@dp.callback_query(F.data == "invite_username")
async def cb_invite_username(cb: CallbackQuery, state: FSMContext):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(InviteAdmin.waiting_username)
    await _edit_or_send(
        cb,
        "Введи username будущего админа — можно <code>@username</code> или просто <code>username</code>.\n\n"
        "Доступ выдастся автоматически, как только он откроет бота.",
        InlineKeyboardMarkup(inline_keyboard=[CANCEL_ROW]),
    )
    await cb.answer()

@dp.message(InviteAdmin.waiting_username)
async def invite_username_input(message: Message, state: FSMContext):
    uname = _norm_username(message.text)
    if not USERNAME_RE.match(uname):
        await message.answer("❌ Похоже на некорректный username. Допустимо 4–32 символа: латиница, цифры, _ (с @ или без).")
        return
    await state.clear()
    # уже админ?
    data = load_admins()
    for uid, info in data.get('admins', {}).items():
        if (info.get('username') or "").lower() == uname:
            await message.answer("ℹ️ @" + esc(uname) + " уже админ.",
                                 reply_markup=main_keyboard(message.from_user.id))
            return
    add_invited(uname, message.from_user.id)
    await message.answer(
        "✅ @" + esc(uname) + " приглашён.\nКак только он напишет боту /start — получит доступ админа автоматически.",
        parse_mode="HTML",
        reply_markup=main_keyboard(message.from_user.id),
    )

@dp.callback_query(F.data == "invitelist")
async def cb_invitelist(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    invited = load_admins().get('invited', {})
    if not invited:
        await _edit_or_send(cb, _requests_text(), admin_requests_kb())
        await cb.answer("Список пуст")
        return
    await _edit_or_send(cb, f"✉️ <b>Приглашённые</b> ({len(invited)}) — ждут первого входа:", invitelist_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("uninvite_"))
async def cb_uninvite(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uname = cb.data.replace("uninvite_", "")
    remove_invited(uname)
    invited = load_admins().get('invited', {})
    if invited:
        await _edit_or_send(cb, f"Убран.\n\n✉️ <b>Приглашённые</b> ({len(invited)}):", invitelist_kb())
    else:
        await _edit_or_send(cb, "Убран. Приглашённых больше нет.\n\n" + _requests_text(), admin_requests_kb())
    await cb.answer("Убран")

@dp.callback_query(F.data == "remove_admin")
async def cb_remove_admin(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    kb = admins_keyboard()
    if kb is None:
        await cb.message.answer("Нет админов для удаления")
    else:
        await cb.message.answer("Выбери кого удалить:", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("revoke_admin_"))
async def cb_revoke_admin(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = cb.data.replace("revoke_admin_", "")
    data = load_admins()
    info = data.get('admins', {}).get(uid, {})
    username = info.get('username')
    name = info.get('full_name') or uid
    label = "@" + username if username else name

    # Считаем юзеров этого админа
    meta = telmgr.load_meta()
    admin_users = [k for k, v in meta.items() if str(v.get('admin_id')) == uid]
    count = len(admin_users)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить юзеров (" + str(count) + ")", callback_data="revoke_with_users_" + uid),
            InlineKeyboardButton(text="👤 Оставить юзеров", callback_data="revoke_keep_users_" + uid),
        ]
    ])
    await cb.message.answer(
        "Удалить админа <b>" + esc(label) + "</b>?\n\nУ него " + str(count) + " юзеров.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("revoke_with_users_"))
async def cb_revoke_with_users(cb: CallbackQuery):
    uid = cb.data.replace("revoke_with_users_", "")
    client = get_client(cb.from_user.id)
    try:
        all_users = await client.get_users()
        admin_users = [k for k, v in all_users.items() if str(v.get('admin_id')) == uid]
    except Exception:
        admin_users = []
    for name in admin_users:
        try:
            await client.delete_user(name)
        except Exception:
            pass
    remove_admin(int(uid))
    try:
        await bot.send_message(int(uid), "⛔ Твой доступ к боту отозван.")
    except Exception:
        pass
    await cb.message.answer(
        "✅ Админ удалён, юзеры удалены: " + str(len(admin_users)),
        reply_markup=main_keyboard(cb.from_user.id)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("revoke_keep_users_"))
async def cb_revoke_keep_users(cb: CallbackQuery):
    uid = cb.data.replace("revoke_keep_users_", "")
    data = load_admins()
    info = data.get('admins', {}).get(uid, {})
    username = info.get('username')
    name = info.get('full_name') or uid
    label = "@" + username if username else name
    remove_admin(int(uid))
    try:
        await bot.send_message(int(uid), "⛔ Твой доступ к боту отозван.")
    except Exception:
        pass
    await cb.message.answer(
        "✅ Доступ отозван: " + label + ". Юзеры сохранены.",
        reply_markup=main_keyboard(cb.from_user.id)
    )
    await cb.answer()

# === Server selector ===

@dp.callback_query(F.data == "select_server")
async def cb_select_server(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    cfg = load_servers_config()
    current_id = get_user_server_id(cb.from_user.id)
    buttons = []
    for sid, sinfo in cfg["servers"].items():
        checkmark = "✅ " if sid == current_id else ""
        buttons.append([InlineKeyboardButton(
            text=checkmark + sinfo["name"],
            callback_data="switch_server_" + sid
        )])
    if is_super_admin(cb.from_user.id):
        buttons.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_server")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await cb.message.answer("Выбери сервер:", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("switch_server_"))
async def cb_switch_server(cb: CallbackQuery):
    sid = cb.data.replace("switch_server_", "")
    cfg = load_servers_config()
    if sid not in cfg["servers"]:
        await cb.answer("❌ Сервер не найден", show_alert=True)
        return
    set_user_server(cb.from_user.id, sid)
    name = cfg["servers"][sid]["name"]
    await cb.message.answer(
        "✅ Переключено на: <b>" + esc(name) + "</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(cb.from_user.id)
    )
    await cb.answer()

@dp.callback_query(F.data == "add_server")
async def cb_add_server(cb: CallbackQuery, state: FSMContext):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.answer("Введи название нового сервера:")
    await state.set_state(AddServer.waiting_name)
    await cb.answer()

@dp.message(AddServer.waiting_name)
async def add_server_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введи URL API сервера (например: http://1.2.3.4:8765):")
    await state.set_state(AddServer.waiting_url)

@dp.message(AddServer.waiting_url)
async def add_server_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        await message.answer("❌ URL должен начинаться с http:// или https://")
        return
    await state.update_data(url=url)
    await message.answer("Введи токен ноды (строку после URL из вывода установки — вида <code>ключ:отпечаток</code>):", parse_mode="HTML")
    await state.set_state(AddServer.waiting_key)

@dp.message(AddServer.waiting_key)
async def add_server_key(message: Message, state: FSMContext):
    import secrets as _secrets
    data = await state.get_data()
    name = data["name"]
    url = data["url"]
    token = message.text.strip()
    api_key, cert_fp = telmgr._parse_api_token(token)
    test_client = RemoteServerClient({"name": name, "url": url, "api_key": api_key, "cert_fp": cert_fp, "host": "", "port": ""})
    try:
        status = await test_client._req("GET", "/status")
        host = status.get("host", "")
        port = str(status.get("port", ""))
    except ValueError as e:
        await message.answer("❌ Не могу подключиться: " + str(e) + "\nПроверь URL и токен.")
        await state.clear()
        return
    cfg = load_servers_config()
    sid = _secrets.token_hex(4)
    cfg["servers"][sid] = {"name": name, "url": url, "api_key": api_key, "cert_fp": cert_fp, "host": host, "port": port}
    save_servers_config(cfg)
    tls_note = "\n🔒 TLS-отпечаток подтверждён" if cert_fp else "\n⚠️ plain HTTP без шифрования"
    await message.answer(
        "✅ Сервер <b>" + esc(name) + "</b> добавлен!\nХост: " + esc(host) + ":" + esc(port) + tls_note,
        parse_mode="HTML",
        reply_markup=main_keyboard(message.from_user.id)
    )
    await state.clear()


BACKUP_JOB_ID = "telmgr_backup_auto"

def _safe_filename(name: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '-', name).strip('-').lower()
    return safe

def _server_slug(info_data: dict, sid: str) -> str:
    """Латинский slug для имени файла. Приоритет: имя → первая часть host → sid."""
    name_slug = _safe_filename(info_data.get("name", ""))
    if name_slug:
        return name_slug
    host = info_data.get("host", "")
    if host:
        host_slug = _safe_filename(host.split(".")[0])
        if host_slug:
            return host_slug
    return _safe_filename(sid) or "server"

async def run_backup_all():
    from aiogram.types import BufferedInputFile
    cfg = load_servers_config()
    date = datetime.now().strftime("%Y-%m-%d")
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    for sid, info_data in cfg.get("servers", {}).items():
        srv_name = info_data.get("name") or sid
        try:
            client = LocalServerClient(info_data) if info_data.get("url") == "local" else RemoteServerClient(info_data)
            data, _orig_name = await client.get_backup()
            filename = f"telmgr-{_server_slug(info_data, sid)}-{date}.tar.gz"
            await bot.send_document(
                SUPER_ADMIN_ID,
                BufferedInputFile(data, filename=filename),
                caption=f"📦 {srv_name} — {when}",
            )
        except Exception as e:
            try:
                await bot.send_message(SUPER_ADMIN_ID, f"❌ Бэкап {srv_name} не удался: {e}")
            except Exception:
                pass

def apply_backup_schedule():
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    s = telmgr._load_backup_schedule()
    try:
        scheduler.remove_job(BACKUP_JOB_ID)
    except Exception:
        pass
    if not s.get("enabled"):
        return
    parsed = telmgr._parse_interval(s.get("interval", "1d"))
    if not parsed:
        return
    n, unit = parsed
    if unit == 'h':
        trigger = CronTrigger(hour=f'0/{n}', minute=0)
    elif unit == 'd':
        trigger = CronTrigger(day=f'1/{n}', hour=3, minute=0)
    elif unit == 'w':
        trigger = IntervalTrigger(weeks=n)
    else:  # 'm'
        trigger = CronTrigger(month=f'1/{n}', day=1, hour=3, minute=0)
    scheduler.add_job(run_backup_all, trigger, id=BACKUP_JOB_ID, replace_existing=True)

@dp.message(Command("backup"))
async def backup_cmd_handler(message: Message):
    if not is_super_admin(message.from_user.id):
        return
    await message.answer("📦 Собираю бэкапы со всех серверов...")
    await run_backup_all()

_BACKUP_AUTO_HELP = (
    "Формат интервала: <code>N{h|d|w|m}</code>\n"
    "  <b>h</b> — часы (1–23), напр. <code>3h</code>\n"
    "  <b>d</b> — дни (1–31), напр. <code>7d</code>\n"
    "  <b>w</b> — недели (1–52), напр. <code>2w</code>\n"
    "  <b>m</b> — месяцы (1–12), напр. <code>1m</code>"
)

@dp.message(Command("backup_auto"))
async def backup_auto_cmd_handler(message: Message):
    if not is_super_admin(message.from_user.id):
        return
    args = (message.text or "").split()[1:]
    s = telmgr._load_backup_schedule()
    interval = s.get("interval", "1d")
    if not args:
        if s.get("enabled"):
            await message.answer(
                f"🔄 Авто-бэкап: <b>включён</b>, {telmgr._format_interval(interval)}\n\n"
                "<code>/backup_auto off</code> — отключить\n"
                "<code>/backup_auto on N{h|d|w|m}</code> — изменить\n\n"
                + _BACKUP_AUTO_HELP,
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "⏸ Авто-бэкап: <b>отключён</b>\n\n"
                "<code>/backup_auto on N{h|d|w|m}</code> — включить (по умолчанию 1d)\n\n"
                + _BACKUP_AUTO_HELP,
                parse_mode="HTML",
            )
        return
    if args[0] == "off":
        telmgr._save_backup_schedule({"enabled": False, "interval": interval})
        apply_backup_schedule()
        await message.answer("⏸ Авто-бэкап отключён")
    elif args[0] == "on":
        new_interval = args[1] if len(args) > 1 else "1d"
        if not telmgr._parse_interval(new_interval):
            await message.answer(f"❌ Неверный интервал: <code>{new_interval}</code>\n\n" + _BACKUP_AUTO_HELP, parse_mode="HTML")
            return
        telmgr._save_backup_schedule({"enabled": True, "interval": new_interval})
        apply_backup_schedule()
        await message.answer(f"🔄 Авто-бэкап включён: {telmgr._format_interval(new_interval)}")
    else:
        await message.answer("Использование: <code>/backup_auto [on N{h|d|w|m} | off]</code>", parse_mode="HTML")


# === Меню: Статус и Бэкапы (кнопками) ===

def _fmt_uptime(secs) -> str:
    total = int(secs or 0)
    h, rem = divmod(total, 3600)
    m = rem // 60
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}д {h}ч {m}м"
    return f"{h}ч {m}м"

@dp.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery, state: FSMContext):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer("Собираю статус…")
    cfg = load_servers_config()
    lines = ["📊 <b>Статус</b>\n"]
    for sid, info_data in cfg.get("servers", {}).items():
        srv = info_data.get("name") or sid
        try:
            client = LocalServerClient(info_data) if info_data.get("url") == "local" else RemoteServerClient(info_data)
            st = await client.get_status()
            emoji = "🟢" if st.get("proxy_status") == "running" else "🔴"
            lines.append(
                f"{emoji} <b>{esc(srv)}</b> — {esc(st.get('proxy_status', '?'))}\n"
                f"   {esc(st.get('host', ''))}:{esc(st.get('port', ''))} · аптайм {_fmt_uptime(st.get('uptime_seconds'))}\n"
                f"   юзеры: {st.get('users_active', 0)} 🟢 / {st.get('users_disabled', 0)} 🔴"
            )
        except Exception as e:
            lines.append(f"⚠️ <b>{esc(srv)}</b> — недоступен: {esc(str(e)[:80])}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="status"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="umenu"),
    ]])
    await _edit_or_send(cb, "\n".join(lines), kb)

def backups_text() -> str:
    s = telmgr._load_backup_schedule()
    if s.get("enabled"):
        body = "Авто-бэкап: <b>включён</b>, " + telmgr._format_interval(s.get("interval", "1d"))
    else:
        body = "Авто-бэкап: <b>отключён</b>"
    return "📦 <b>Бэкапы</b>\n\n" + body + "\nФайлы приходят суперадмину в этот чат."

def backups_kb() -> InlineKeyboardMarkup:
    s = telmgr._load_backup_schedule()
    rows = [[InlineKeyboardButton(text="📦 Сделать бэкап сейчас", callback_data="bkrun")]]
    if s.get("enabled"):
        rows.append([InlineKeyboardButton(text="⏸ Выключить авто-бэкап", callback_data="bkauto_off")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Включить авто-бэкап", callback_data="bkauto_on")])
    rows.append([
        InlineKeyboardButton(text="каждый день", callback_data="bkint:1d"),
        InlineKeyboardButton(text="раз в неделю", callback_data="bkint:7d"),
        InlineKeyboardButton(text="раз в месяц", callback_data="bkint:30d"),
    ])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="umenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "backups")
async def cb_backups(cb: CallbackQuery, state: FSMContext):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.clear()
    await _edit_or_send(cb, backups_text(), backups_kb())
    await cb.answer()

@dp.callback_query(F.data == "bkrun")
async def cb_backup_run(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.answer("Собираю бэкапы со всех серверов…")
    await run_backup_all()
    await cb.message.answer("✅ Готово.", reply_markup=main_keyboard(cb.from_user.id))

@dp.callback_query(F.data == "bkauto_on")
async def cb_bkauto_on(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    s = telmgr._load_backup_schedule()
    telmgr._save_backup_schedule({"enabled": True, "interval": s.get("interval", "1d")})
    apply_backup_schedule()
    await _edit_or_send(cb, backups_text(), backups_kb())
    await cb.answer("Авто-бэкап включён")

@dp.callback_query(F.data == "bkauto_off")
async def cb_bkauto_off(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    s = telmgr._load_backup_schedule()
    telmgr._save_backup_schedule({"enabled": False, "interval": s.get("interval", "1d")})
    apply_backup_schedule()
    await _edit_or_send(cb, backups_text(), backups_kb())
    await cb.answer("Авто-бэкап отключён")

@dp.callback_query(F.data.startswith("bkint:"))
async def cb_bkint(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    iv = cb.data.split(":", 1)[1]
    if not telmgr._parse_interval(iv):
        await cb.answer("Неверный интервал", show_alert=True)
        return
    telmgr._save_backup_schedule({"enabled": True, "interval": iv})
    apply_backup_schedule()
    await _edit_or_send(cb, backups_text(), backups_kb())
    await cb.answer("Авто-бэкап: " + telmgr._format_interval(iv))


async def setup_bot_commands():
    """Команды в меню по областям: всем — только /start; суперадмину в личке —
    + /backup и /backup_auto (чтобы люди без доступа их не видели)."""
    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    start_only = [BotCommand(command="start", description="Запустить бота")]
    super_cmds = start_only + [
        BotCommand(command="backup", description="Создать бэкап всех серверов сейчас"),
        BotCommand(command="backup_auto", description="Расписание авто-бэкапов"),
    ]
    try:
        await bot.set_my_commands(start_only, scope=BotCommandScopeDefault())
        await bot.set_my_commands(super_cmds, scope=BotCommandScopeChat(chat_id=SUPER_ADMIN_ID))
    except Exception as e:
        print(f"set_my_commands failed: {e}")

async def main():
    import signal as _signal
    overdue = await load_scheduled_jobs()
    scheduler.start()
    for name, admin_id, server_id in overdue:
        asyncio.create_task(disable_user_job(name, admin_id, server_id))
    apply_backup_schedule()
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(_signal.SIGHUP, apply_backup_schedule)
    except (NotImplementedError, AttributeError):
        pass
    await setup_bot_commands()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
