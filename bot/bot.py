#!/usr/bin/env python3

import asyncio
import os
import sys
import re
import json
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv('/root/telemt/.env')

BOT_TOKEN = os.getenv('BOT_TOKEN')
SUPER_ADMIN_ID = int(os.getenv('SUPER_ADMIN_ID'))
ADMINS_PATH = os.getenv('TELEMT_DIR', '/root/telemt') + '/.telmgr-admins.json'

# Импортируем функции из telmgr
import importlib.util
spec = importlib.util.spec_from_file_location("telmgr", "/usr/local/bin/telmgr.py")
assert spec is not None, "telmgr не найден в /usr/local/bin/telmgr.py"
telmgr = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(telmgr)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


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


# === Keyboards ===

def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить юзера", callback_data="add_user")],
        [InlineKeyboardButton(text="🗑 Удалить юзера", callback_data="delete_user")],
        [InlineKeyboardButton(text="⏸ Откл/Вкл юзера", callback_data="toggle_user")],
        [InlineKeyboardButton(text="⏱ Установить лимит", callback_data="limit_user")],
        [InlineKeyboardButton(text="🔗 Ссылка юзера", callback_data="link_user")],
        [InlineKeyboardButton(text="👥 Мои юзеры", callback_data="my_users")],
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
    if not re.match(r'^\w+$', name):
        await message.answer("❌ Только буквы, цифры и _")
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

    try:
        content = telmgr.read_toml()
        users = telmgr.get_users_from_toml(content)
        if name in users:
            await message.answer("❌ Юзер '" + name + "' уже существует")
            await state.clear()
            return

        secret = telmgr.generate_secret()
        lines = content.splitlines()
        result = []
        inserted = False
        for line in lines:
            result.append(line)
            if line.strip() == "[access.users]" and not inserted:
                result.append(name + ' = "' + secret + '"')
                inserted = True
        telmgr.write_toml("\n".join(result))

        expires = None
        if days > 0:
            expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        meta = telmgr.load_meta()
        meta[name] = {
            "secret": secret,
            "created": datetime.now().strftime("%Y-%m-%d"),
            "expires": expires,
            "disabled": False,
            "admin_id": message.from_user.id,
            "admin_name": message.from_user.full_name,
            "admin_username": message.from_user.username
        }
        telmgr.save_meta(meta)

        if expires:
            telmgr.add_cron(name, expires)

        link = telmgr.build_link(secret)
        text = "✅ Юзер <b>" + name + "</b> добавлен\n"
        if expires:
            text += "📅 Истекает: " + expires + "\n"
        text += "🔗 <code>" + link + "</code>"
        await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))

    except Exception as e:
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
    try:
        telmgr.cmd_delete(name)
        await message.answer("✅ Юзер <b>" + name + "</b> удалён", parse_mode="HTML",
                             reply_markup=main_keyboard(message.from_user.id))
    except SystemExit:
        await message.answer("❌ Юзер '" + name + "' не найден")
    await state.clear()

@dp.callback_query(F.data == "toggle_user")
async def cb_toggle_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера для откл/вкл:")
    await state.set_state(ToggleUser.waiting_name)
    await cb.answer()

@dp.message(ToggleUser.waiting_name)
async def toggle_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    try:
        content = telmgr.read_toml()
        users = telmgr.get_users_from_toml(content)
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        if users[name]['disabled']:
            telmgr.cmd_enable(name)
            await message.answer("✅ Юзер <b>" + name + "</b> включён", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
        else:
            telmgr.cmd_disable(name)
            await message.answer("⏸ Юзер <b>" + name + "</b> отключён", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
    except SystemExit:
        await message.answer("❌ Ошибка")
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
    data = await state.get_data()
    name = data['name']
    try:
        telmgr.cmd_limit(name, days)
        if days == 0:
            await message.answer("✅ Лимит для <b>" + name + "</b> снят", parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
        else:
            expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            await message.answer("✅ Лимит для <b>" + name + "</b>: до " + expires, parse_mode="HTML",
                                 reply_markup=main_keyboard(message.from_user.id))
    except SystemExit:
        await message.answer("❌ Ошибка — проверь имя юзера")
    await state.clear()

@dp.callback_query(F.data == "link_user")
async def cb_link_user(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введи имя юзера:")
    await state.set_state(LinkUser.waiting_name)
    await cb.answer()

@dp.message(LinkUser.waiting_name)
async def link_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    try:
        content = telmgr.read_toml()
        users = telmgr.get_users_from_toml(content)
        if name not in users:
            await message.answer("❌ Юзер '" + name + "' не найден")
            await state.clear()
            return
        link = telmgr.build_link(users[name]['secret'])
        if users[name]['disabled']:
            await message.answer("⚠️ Юзер <b>" + name + "</b> отключён, но ссылка:\n🔗 <code>" + link + "</code>",
                                 parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
        else:
            await message.answer("🔗 <code>" + link + "</code>",
                                 parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    except Exception as e:
        await message.answer("❌ Ошибка: " + str(e))
    await state.clear()

@dp.callback_query(F.data == "my_users")
async def cb_my_users(cb: CallbackQuery):
    meta = telmgr.load_meta()
    content = telmgr.read_toml()
    toml_users = telmgr.get_users_from_toml(content)
    my = {k: v for k, v in meta.items() if v.get('admin_id') == cb.from_user.id}
    for name in my:
        if name in toml_users:
            my[name]['disabled'] = toml_users[name]['disabled']
    text = "👥 <b>Твои юзеры:</b>\n\n" + format_users(my, toml_users)
    await cb.message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(cb.from_user.id))
    await cb.answer()

@dp.callback_query(F.data == "all_users")
async def cb_all_users(cb: CallbackQuery):
    if not is_super_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    meta = telmgr.load_meta()
    toml_content = telmgr.read_toml()
    toml_users = telmgr.get_users_from_toml(toml_content)
    admins_data = load_admins()
    active_admin_ids = set(admins_data.get('admins', {}).keys())

    for name in meta:
        if name in toml_users:
            meta[name]['disabled'] = toml_users[name]['disabled']

    groups = {}
    for name, data in meta.items():
        admin_id = data.get('admin_id')
        admin_username = data.get('admin_username')
        admin_name = data.get('admin_name')
        if admin_id is None:
            key = ("👑 " + cb.from_user.full_name + " (суперадмин / CLI)", False)
        elif str(admin_id) in active_admin_ids:
            if admin_username:
                key = ("👤 @" + admin_username, False)
            else:
                key = ("👤 " + str(admin_name or admin_id), False)
        else:
            if admin_username:
                key = ("👤 @" + admin_username + " (Удалён)", True)
            else:
                key = ("👤 " + str(admin_name or admin_id) + " (Удалён)", True)
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
    meta = telmgr.load_meta()
    admin_users = [k for k, v in meta.items() if str(v.get('admin_id')) == uid]
    for name in admin_users:
        try:
            telmgr.cmd_delete(name)
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

async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
