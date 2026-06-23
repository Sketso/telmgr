#!/usr/bin/env python3

import asyncio
import os
import sys
import re
import json
from datetime import datetime, timedelta, timedelta as _td
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

import urllib.request, urllib.error

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
                    if line.strip() == "[access.users]" and not inserted:
                        result_lines.append(f'{name} = "{secret}"')
                        inserted = True
                if not inserted:
                    raise ValueError("Секция [access.users] не найдена")
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

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if "error" in result:
                    raise ValueError(result["error"])
                return result
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read())
                raise ValueError(err_body.get("error", str(e)))
            except (json.JSONDecodeError, AttributeError):
                raise ValueError(str(e))
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


# === FSM States ===

class AddUser(StatesGroup):
    waiting_name = State()
    waiting_days = State()

class DeleteUser(StatesGroup):
    waiting_name = State()

class LimitUser(StatesGroup):
    waiting_name = State()
    waiting_days = State()

class ToggleUser(StatesGroup):
    waiting_name = State()

class LinkUser(StatesGroup):
    waiting_name = State()

class AddServer(StatesGroup):
    waiting_name = State()
    waiting_url  = State()
    waiting_key  = State()


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
        [InlineKeyboardButton(text="🗑 Удалить юзера", callback_data="delete_user")],
        [InlineKeyboardButton(text="⏸ Откл/Вкл юзера", callback_data="toggle_user")],
        [InlineKeyboardButton(text="⏱ Установить лимит", callback_data="limit_user")],
        [InlineKeyboardButton(text="🔗 Ссылка юзера", callback_data="link_user")],
        [
            InlineKeyboardButton(text="👥 Мои юзеры", callback_data="my_users"),
            InlineKeyboardButton(text="⏰ Истекают", callback_data="expiring_users"),
        ],
    ]
    if is_super_admin(user_id):
        buttons.append([InlineKeyboardButton(text="👑 Все юзеры", callback_data="all_users")])
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

def pending_keyboard() -> InlineKeyboardMarkup:
    data = load_admins()
    pending = data.get('pending', {})
    if not pending:
        return None
    buttons = []
    for uid, info in pending.items():
        username = info.get('username')
        full_name = info.get('full_name') or uid
        label = "@" + username if username else full_name
        buttons.append([InlineKeyboardButton(
            text=label + " (" + info.get('requested_at', '') + ")",
            callback_data="approve_admin_" + uid
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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

def format_users(users: dict, toml_users: dict) -> str:
    if not users:
        return "Юзеров нет"
    lines = []
    for name, data in users.items():
        status = "🔴" if data.get('disabled') else "🟢"
        expires = data.get('expires') or "∞"
        lines.append(status + " <b>" + name + "</b> — до " + expires)
    return "\n".join(lines)


# === Handlers ===

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        add_pending(user_id, message.from_user.username, message.from_user.full_name)
        await message.answer("⛔ Нет доступа. Запрос отправлен администратору.")
        username = message.from_user.username
        name = message.from_user.full_name
        label = "@" + username if username else name
        approve_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Дать доступ", callback_data="approve_admin_" + str(user_id))]
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
async def cmd_menu(message: Message):
    if not is_admin(message.from_user.id):
        add_pending(message.from_user.id, message.from_user.username, message.from_user.full_name)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Запросить доступ", callback_data="request_access")]
        ])
        await message.answer("⛔ Нет доступа.", reply_markup=kb)
        return
    await message.answer("Управление Telemt MTProxy:", reply_markup=main_keyboard(message.from_user.id))

@dp.callback_query(F.data == "request_access")
async def cb_request_access(cb: CallbackQuery):
    add_pending(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    await cb.message.answer("✅ Запрос отправлен администратору.")
    username = cb.from_user.username
    name = cb.from_user.full_name
    label = "@" + username if username else name
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Дать доступ", callback_data="approve_admin_" + str(cb.from_user.id))]
    ])
    await bot.send_message(
        SUPER_ADMIN_ID,
        "🔔 Повторный запрос доступа:\n" + label + " (ID: " + str(cb.from_user.id) + ")",
        reply_markup=approve_kb
    )
    await cb.answer()
# === User management ===

@dp.callback_query(F.data == "add_user")
async def cb_add_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя нового юзера:")
    await state.set_state(AddUser.waiting_name)
    await cb.answer()

@dp.message(AddUser.waiting_name)
async def add_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', name):
        await message.answer("❌ Только латинские буквы, цифры и _ (кириллица не поддерживается)")
        return
    await state.update_data(name=name)
    await message.answer("На сколько дней? (0 = бессрочно):")
    await state.set_state(AddUser.waiting_days)

@dp.message(AddUser.waiting_days)
async def add_user_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число")
        return

    data = await state.get_data()
    name = data['name']
    server_id = get_user_server_id(message.from_user.id)
    client = get_client(message.from_user.id)

    try:
        result = await client.add_user(
            name, days,
            message.from_user.id,
            message.from_user.full_name,
            message.from_user.username
        )
        link = result["link"]
        expires = result.get("expires")
        if expires:
            schedule_user_disable(name, expires, message.from_user.id, server_id)
        text = "✅ Юзер <b>" + name + "</b> добавлен\n"
        if expires:
            text += "📅 Истекает: " + expires + "\n"
        text += "🔗 <code>" + link + "</code>"
        await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    except (ValueError, Exception) as e:
        await message.answer("❌ Ошибка: " + str(e))

    await state.clear()

@dp.callback_query(F.data == "delete_user")
async def cb_delete_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера для удаления:")
    await state.set_state(DeleteUser.waiting_name)
    await cb.answer()

@dp.message(DeleteUser.waiting_name)
async def delete_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        if not owns_user(message.from_user.id, users, name):
            await message.answer("⛔ Юзер <b>" + name + "</b> принадлежит другому админу", parse_mode="HTML")
            await state.clear()
            return
        await client.delete_user(name)
        await message.answer("✅ Юзер <b>" + name + "</b> удалён", parse_mode="HTML",
                             reply_markup=main_keyboard(message.from_user.id))
    except (ValueError, Exception) as e:
        error_msg = str(e)
        await message.answer("❌ Ошибка: " + error_msg)
        if "откат" in error_msg.lower() or "невалидный" in error_msg.lower():
            try:
                await bot.send_message(SUPER_ADMIN_ID, "🚨 Конфиг telemt.toml повреждён и откатан!\nОшибка: " + error_msg)
            except Exception:
                pass
    await state.clear()

@dp.callback_query(F.data == "toggle_user")
async def cb_toggle_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера для откл/вкл:")
    await state.set_state(ToggleUser.waiting_name)
    await cb.answer()

@dp.message(ToggleUser.waiting_name)
async def toggle_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        if not owns_user(message.from_user.id, users, name):
            await message.answer("⛔ Юзер <b>" + name + "</b> принадлежит другому админу", parse_mode="HTML")
            await state.clear()
            return
        if users[name]['disabled']:
            await client.enable_user(name)
            await message.answer("✅ Юзер <b>" + name + "</b> включён", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
        else:
            await client.disable_user(name)
            await message.answer("⏸ Юзер <b>" + name + "</b> отключён", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
    except (ValueError, Exception) as e:
        error_msg = str(e)
        await message.answer("❌ Ошибка: " + error_msg)
        if "откат" in error_msg.lower() or "невалидный" in error_msg.lower():
            try:
                await bot.send_message(SUPER_ADMIN_ID, "🚨 Конфиг telemt.toml повреждён и откатан!\nОшибка: " + error_msg)
            except Exception:
                pass
    await state.clear()

@dp.callback_query(F.data == "limit_user")
async def cb_limit_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера:")
    await state.set_state(LimitUser.waiting_name)
    await cb.answer()

@dp.message(LimitUser.waiting_name)
async def limit_user_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("На сколько дней? (0 = снять лимит):")
    await state.set_state(LimitUser.waiting_days)

@dp.message(LimitUser.waiting_days)
async def limit_user_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число")
        return
    if days < 0:
        await message.answer("❌ Количество дней не может быть отрицательным")
        return
    data = await state.get_data()
    name = data['name']
    server_id = get_user_server_id(message.from_user.id)
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        if not owns_user(message.from_user.id, users, name):
            await message.answer("⛔ Юзер <b>" + name + "</b> принадлежит другому админу", parse_mode="HTML")
            await state.clear()
            return
        await client.set_limit(name, days)
        if days == 0:
            job_id = _disable_job_id(server_id, name)
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            await message.answer("✅ Лимит для <b>" + name + "</b> снят", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
        else:
            expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            schedule_user_disable(name, expires, message.from_user.id, server_id)
            await message.answer("✅ Лимит для <b>" + name + "</b>: до " + expires, parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
    except (ValueError, Exception) as e:
        error_msg = str(e)
        await message.answer("❌ Ошибка: " + error_msg)
        if "откат" in error_msg.lower() or "невалидный" in error_msg.lower():
            try:
                await bot.send_message(SUPER_ADMIN_ID, "🚨 Конфиг telemt.toml повреждён и откатан!\nОшибка: " + error_msg)
            except Exception:
                pass
    await state.clear()
    
@dp.callback_query(F.data == "link_user")
async def cb_link_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера:")
    await state.set_state(LinkUser.waiting_name)
    await cb.answer()

@dp.message(LinkUser.waiting_name)
async def link_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    client = get_client(message.from_user.id)
    try:
        users = await client.get_users()
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        link = await client.get_link(name)
        if users[name]['disabled']:
            await message.answer("⚠️ Юзер <b>" + name + "</b> отключён, но ссылка:\n🔗 <code>" + link + "</code>",
                                 parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
        else:
            await message.answer("🔗 <code>" + link + "</code>",
                                 parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    except (ValueError, Exception) as e:
        await message.answer("❌ Ошибка: " + str(e))
    await state.clear()

@dp.callback_query(F.data == "my_users")
async def cb_my_users(cb: CallbackQuery):
    client = get_client(cb.from_user.id)
    try:
        all_users = await client.get_users()
        my = {k: v for k, v in all_users.items() if v.get('admin_id') == cb.from_user.id}
        text = "👥 <b>Твои юзеры:</b>\n\n" + format_users(my, my)
        await cb.message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(cb.from_user.id))
    except (ValueError, Exception) as e:
        await cb.message.answer("❌ Ошибка: " + str(e))
    await cb.answer()

@dp.callback_query(F.data == "expiring_users")
async def cb_expiring_users(cb: CallbackQuery):
    client = get_client(cb.from_user.id)
    try:
        all_users = await client.get_users()
        soon = []
        for name, data in all_users.items():
            if str(data.get('admin_id')) != str(cb.from_user.id) and not is_super_admin(cb.from_user.id):
                continue
            if data.get('expires'):
                exp = datetime.strptime(data['expires'], "%Y-%m-%d")
                diff = (exp - datetime.now()).days
                if diff <= 5:
                    soon.append((name, data['expires'], diff, data.get('disabled', False)))
        if not soon:
            await cb.message.answer("✅ Нет юзеров с истекающим сроком в ближайшие 5 дней",
                                    reply_markup=main_keyboard(cb.from_user.id))
            await cb.answer()
            return
        lines = ["⏰ <b>Истекают в ближайшие 5 дней:</b>\n"]
        for name, expires, diff, disabled in sorted(soon, key=lambda x: x[2]):
            status = "🔴" if disabled else "🟢"
            emoji = "🔥" if diff <= 1 else "⚠️"
            lines.append(emoji + " " + status + " <b>" + name + "</b> — " + expires + " (через " + str(diff) + " дн.)")
        await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard(cb.from_user.id))
    except (ValueError, Exception) as e:
        await cb.message.answer("❌ Ошибка: " + str(e))
    await cb.answer()

@dp.callback_query(F.data == "all_users")
async def cb_all_users(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    client = get_client(cb.from_user.id)
    try:
        all_users = await client.get_users()
        admins_data = load_admins()
        active_admin_ids = set(admins_data.get('admins', {}).keys())
        groups = {}
        for name, data in all_users.items():
            admin_id = data.get('admin_id')
            admin_username = data.get('admin_username')
            admin_name = data.get('admin_name')
            if admin_id is None:
                key = ("👑 " + cb.from_user.full_name + " (суперадмин / CLI)", False)
            elif str(admin_id) in active_admin_ids:
                key = ("👤 @" + admin_username, False) if admin_username else ("👤 " + str(admin_name or admin_id), False)
            else:
                key = ("👤 @" + admin_username + " (Удалён)", True) if admin_username else ("👤 " + str(admin_name or admin_id) + " (Удалён)", True)
            groups.setdefault(key, {})[name] = data
        lines = ["👑 <b>Все юзеры:</b>\n"]
        for (admin_label, is_deleted), users in groups.items():
            if is_deleted and not users:
                continue
            lines.append("<b>" + admin_label + ":</b>")
            for name, data in users.items():
                status = "🔴" if data.get('disabled') else "🟢"
                expires = data.get('expires') or "∞"
                lines.append("  " + status + " <b>" + name + "</b> — до " + expires)
            lines.append("")
        await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard(cb.from_user.id))
    except (ValueError, Exception) as e:
        await cb.message.answer("❌ Ошибка: " + str(e))
    await cb.answer()

# === Admin management ===

@dp.callback_query(F.data == "add_admin")
async def cb_add_admin(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    kb = pending_keyboard()
    if kb is None:
        await cb.message.answer("Нет pending запросов")
    else:
        await cb.message.answer("Выбери кому дать доступ:", reply_markup=kb)
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
        "Удалить админа <b>" + label + "</b>?\n\nУ него " + str(count) + " юзеров.",
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
        "✅ Переключено на: <b>" + name + "</b>",
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
    await message.answer("Введи API ключ сервера:")
    await state.set_state(AddServer.waiting_key)

@dp.message(AddServer.waiting_key)
async def add_server_key(message: Message, state: FSMContext):
    import secrets as _secrets
    data = await state.get_data()
    name = data["name"]
    url = data["url"]
    api_key = message.text.strip()
    test_client = RemoteServerClient({"name": name, "url": url, "api_key": api_key, "host": "", "port": ""})
    try:
        status = await test_client._req("GET", "/status")
        host = status.get("host", "")
        port = str(status.get("port", ""))
    except ValueError as e:
        await message.answer("❌ Не могу подключиться: " + str(e) + "\nПроверь URL и ключ.")
        await state.clear()
        return
    cfg = load_servers_config()
    sid = _secrets.token_hex(4)
    cfg["servers"][sid] = {"name": name, "url": url, "api_key": api_key, "host": host, "port": port}
    save_servers_config(cfg)
    await message.answer(
        "✅ Сервер <b>" + name + "</b> добавлен!\nХост: " + host + ":" + port,
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


async def main():
    import signal as _signal
    from aiogram.types import BotCommand
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
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="backup", description="Создать бэкап всех серверов сейчас"),
            BotCommand(command="backup_auto", description="Расписание авто-бэкапов"),
        ])
    except Exception as e:
        print(f"set_my_commands failed: {e}")
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
