"""
MathMind — Telegram-бот с математикой, фото-задачами, Telegram Stars и админкой.

Установка:
  pip install aiogram==3.* sympy aiohttp pillow pytesseract

Запуск:
  export BOT_TOKEN="9197441376:AAEds7jm3dp3arXQH8S-rqOebjWB-FDx39I"
  export ADMIN_IDS="5796754554"       # Telegram ID администраторов через запятую
  python bot.py

Для OCR фотографий на сервере дополнительно установите Tesseract OCR:
  Ubuntu/Debian: sudo apt-get install tesseract-ocr tesseract-ocr-rus
  macOS:         brew install tesseract tesseract-lang

Важно:
- Токен, опубликованный в чате, нужно отозвать через @BotFather и создать новый.
- Пароль /admin сам по себе не защищает от подбора; здесь дополнительно проверяется ADMIN_IDS.
- Встроенный математический движок решает многие символьные задачи, но не является полноценной
  большой языковой моделью. Для свободного диалога нужен отдельный LLM-сервис или локальная модель.
- Википедия/интернет дают справочные материалы, а не гарантируют правильность ответа.
"""

import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import aiohttp
import sympy as sp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ContentType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ==================== НАСТРОЙКИ MATHMIND ====================

# Telegram
BOT_TOKEN = "9197441376:AAEds7jm3dp3arXQH8S-rqOebjWB-FDx39I"

# Администратор: укажи свой настоящий Telegram ID
ADMIN_IDS = {5796754554}

# Пароль команды /admin
ADMIN_PASSWORD = "2525"

# База данных
DB_PATH = "mathmind.sqlite3"

# Лимиты бесплатной версии
FREE_DAILY_LIMIT = 5

# Максимальный размер фото: 8 МБ
MAX_PHOTO_BYTES = 8 * 1024 * 1024

# Язык Википедии: ru или en
WIKIPEDIA_LANG = "ru"

# Подписки в Telegram Stars
TARIFFS = {
    "7":  {"days": 14,  "stars": 100, "title": "7 дней"},
    "30": {"days": 30, "stars": 300, "title": "30 дней"},
    "90": {"days": 90, "stars": 800, "title": "90 дней"},
}

# Проверка настроек
BOT_TOKEN = BOT_TOKEN.strip()

if not BOT_TOKEN or "ВСТАВЬ_" in BOT_TOKEN or any(c.isspace() for c in BOT_TOKEN):
    raise RuntimeError(
        "Ошибка BOT_TOKEN: вставь настоящий токен от @BotFather без пробелов."
    )

if not ADMIN_IDS or 123456789 in ADMIN_IDS:
    raise RuntimeError(
        "Ошибка ADMIN_IDS: укажи свой настоящий Telegram ID."
    )

# ============================================================


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
admin_sessions: set[int] = set()
pending_admin_action: dict[int, str] = {}
pending_broadcast: set[int] = set()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT NOT NULL,
            premium_until TEXT,
            banned INTEGER NOT NULL DEFAULT 0,
            daily_count INTEGER NOT NULL DEFAULT 0,
            daily_date TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            charge_id TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL,
            stars INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ensure_user(user):
    with db() as c:
        c.execute("""
        INSERT INTO users(user_id, username, first_name, joined_at, daily_date)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
        first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or "", iso(now_utc()),
              now_utc().date().isoformat()))


def get_user(user_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_banned(user_id: int) -> bool:
    u = get_user(user_id)
    return bool(u and u["banned"])


def premium_active(user_id: int) -> bool:
    u = get_user(user_id)
    until = parse_dt(u["premium_until"]) if u else None
    return bool(until and until > now_utc())


def premium_until_text(user_id: int) -> str:
    u = get_user(user_id)
    until = parse_dt(u["premium_until"]) if u else None
    return until.astimezone().strftime("%d.%m.%Y %H:%M") if until and until > now_utc() else "не активна"


def grant_premium(user_id: int, days: int):
    with db() as c:
        row = c.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        current = parse_dt(row["premium_until"]) if row else None
        start = current if current and current > now_utc() else now_utc()
        until = start + timedelta(days=days)
        c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (iso(until), user_id))
    return until


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🧮 Решить задачу"), KeyboardButton(text="💎 Подписка")],
        [KeyboardButton(text="💬 Чат"), KeyboardButton(text="📚 Википедия")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="❓ Помощь")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def tariff_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, t in TARIFFS.items():
        rows.append([InlineKeyboardButton(
            text=f"{t['title']} — {t['stars']} ⭐",
            callback_data=f"buy:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="adm:ban"),
         InlineKeyboardButton(text="✅ Разбанить", callback_data="adm:unban")],
        [InlineKeyboardButton(text="💎 Выдать/продлить Premium", callback_data="adm:grant"),
         InlineKeyboardButton(text="🗑 Снять Premium", callback_data="adm:remove")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="💳 Последние оплаты", callback_data="adm:payments")],
    ])


async def safe_send(user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception as e:
        log.info("Не удалось отправить %s: %s", user_id, e)
        return False


def normalize_math(text: str) -> str:
    s = text.strip().lower()
    s = s.replace("×", "*").replace("·", "*").replace("÷", "/")
    s = s.replace("−", "-").replace("^", "**").replace(",", ".")
    s = s.replace("х", "x").replace("у", "y")
    s = re.sub(r"\bреши\b|\bрешить\b|\bвычисли\b|\bнайди\b|\bупрости\b|\bрешение\b", " ", s)
    s = re.sub(r"\bравно\b", "=", s)
    s = re.sub(r"(?<=\d)(?=[a-z])", "*", s)
    s = re.sub(r"(?<=\))(?=[a-z0-9(])", "*", s)
    s = re.sub(r"(?<=[a-z])(?=\()", "*", s)
    return re.sub(r"\s+", "", s)


def safe_sympy_parse(expr: str):
    allowed = {
        "x": sp.Symbol("x"), "y": sp.Symbol("y"), "z": sp.Symbol("z"),
        "a": sp.Symbol("a"), "b": sp.Symbol("b"), "c": sp.Symbol("c"),
        "n": sp.Symbol("n"), "m": sp.Symbol("m"), "pi": sp.pi, "e": sp.E,
        "sqrt": sp.sqrt, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
        "log": sp.log, "ln": sp.log, "abs": sp.Abs,
    }
    if not re.fullmatch(r"[0-9a-zA-Z_+\-*/().=,\s!]+", expr):
        raise ValueError("В выражении есть неподдерживаемые символы.")
    return sp.sympify(expr, locals=allowed, evaluate=True)


def solve_math(raw: str) -> Optional[str]:
    s = normalize_math(raw)
    if not s or not re.search(r"\d|[xyznmabc]", s):
        return None
    if len(s) > 180:
        return "Слишком длинное выражение. Отправь задачу покороче."

    try:
        if "=" in s:
            if s.count("=") != 1:
                return None
            left, right = s.split("=", 1)
            lhs, rhs = safe_sympy_parse(left), safe_sympy_parse(right)
            eq = sp.Eq(lhs, rhs)
            symbols = sorted(eq.free_symbols, key=str)
            if symbols:
                sol = sp.solve(eq, symbols[0])
                if not sol:
                    return "Не удалось найти решение. Проверь условие и знаки."
                steps = [
                    "1. Переносим всё в одну сторону: " + sp.sstr(sp.simplify(lhs-rhs)) + " = 0",
                    "2. Решаем уравнение относительно " + str(symbols[0]) + ".",
                    "3. Ответ: " + ", ".join(f"{symbols[0]} = {sp.sstr(v)}" for v in sol),
                ]
                return "🧮 <b>Решение</b>\n" + "\n".join(steps)
            return "🧮 <b>Проверка равенства</b>\n" + ("Равенство верно." if sp.simplify(lhs-rhs) == 0 else "Равенство неверно.")

        expr = safe_sympy_parse(s)
        simplified = sp.simplify(expr)
        if expr.free_symbols:
            factored = sp.factor(expr)
            return (f"🧮 <b>Упрощение</b>\n"
                    f"Исходное выражение: <code>{sp.sstr(expr)}</code>\n"
                    f"Упрощённый вид: <code>{sp.sstr(simplified)}</code>\n"
                    f"Разложение на множители: <code>{sp.sstr(factored)}</code>")
        return f"🧮 <b>Ответ:</b> <code>{sp.sstr(simplified)}</code>"
    except Exception:
        return None


async def wikipedia_search(query: str) -> str:
    query = query.strip()[:180]
    if not query:
        return "Напиши тему, которую нужно найти в Википедии."
    url = f"https://{WIKIPEDIA_LANG}.wikipedia.org/w/api.php"
    params = {
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "utf8": 1, "srlimit": 3,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as r:
                r.raise_for_status()
                data = await r.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return "В Википедии ничего не нашлось. Попробуй сформулировать запрос иначе."
        out = ["📚 <b>Результаты Википедии</b>"]
        for item in results:
            title = item["title"]
            snippet = re.sub(r"<.*?>", "", item.get("snippet", ""))
            link = "https://ru.wikipedia.org/wiki/" + title.replace(" ", "_")
            out.append(f"\n<b>{title}</b>\n{snippet}…\n{link}")
        return "\n".join(out)
    except Exception:
        return "Не получилось подключиться к Википедии. Проверь интернет-соединение сервера."


async def image_to_text(message: Message) -> str:
    """Попытка OCR через Tesseract. Для рукописных задач может ошибаться."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ("На сервере не установлен OCR. Установи пакеты Pillow и pytesseract, "
                "а также программу Tesseract OCR. Пока можешь перепечатать условие текстом.")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    if buffer.tell() > MAX_PHOTO_BYTES:
        return "Фото слишком большое. Отправь изображение размером до 8 МБ."
    buffer.seek(0)
    try:
        img = Image.open(buffer).convert("RGB")
        text = pytesseract.image_to_string(img, lang="rus+eng")
        text = text.strip()
        if not text:
            return "Не смог распознать текст на фото. Попробуй сделать снимок ровнее и при хорошем освещении."
        return text[:3000]
    except Exception as e:
        log.warning("OCR error: %s", e)
        return "Не получилось распознать фото. Проверь, установлен ли Tesseract и языковой пакет rus."


def allowed_math_request(user_id: int) -> tuple[bool, str]:
    if premium_active(user_id) or is_admin(user_id):
        return True, ""
    today = now_utc().date().isoformat()
    with db() as c:
        row = c.execute("SELECT daily_count, daily_date FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return True, ""
        count = row["daily_count"] if row["daily_date"] == today else 0
        if count >= FREE_DAILY_LIMIT:
            return False, f"Лимит бесплатных математических запросов на сегодня исчерпан ({FREE_DAILY_LIMIT}). Открой «💎 Подписка»."
        c.execute("UPDATE users SET daily_count=?, daily_date=? WHERE user_id=?",
                  (count + 1, today, user_id))
    return True, ""


def log_request(user_id: int, kind: str):
    with db() as c:
        c.execute("INSERT INTO requests(user_id,kind,created_at) VALUES(?,?,?)",
                  (user_id, kind, iso(now_utc())))


async def show_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён. Админ-доступ разрешён только ID из ADMIN_IDS.")
        return
    # Не сбрасываем admin_sessions: она подтверждает пароль для callback-кнопок.
    if pending_admin_action.get(message.from_user.id) == "password":
        pending_admin_action.pop(message.from_user.id, None)
    await message.answer("🛠 <b>Админ-панель</b>\nВыбери действие:", reply_markup=admin_keyboard())


@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        await message.answer("🚫 Ваш доступ к боту ограничен.")
        return
    await message.answer(
        "Привет! Я <b>MathMind</b> — бот для математических задач.\n\n"
        "Отправь выражение или уравнение текстом, например <code>2x + 5 = 15</code>, "
        "или фото задачи. Кнопка «💎 Подписка» открывает Premium.\n\n"
        "Важно: фото обрабатывается OCR-распознаванием текста; рукописные записи могут распознаваться неверно.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(Command("admin"))
async def admin_command(message: Message):
    ensure_user(message.from_user)
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Доступ запрещён: ваш Telegram ID не добавлен в ADMIN_IDS.")
        return
    admin_sessions.add(uid)
    pending_admin_action[uid] = "password"
    await message.answer("Введи пароль админ-панели:")


@dp.message(F.text)
async def text_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    user = message.from_user
    ensure_user(user)
    uid = user.id
    text_in = (message.text or "").strip()

    if text_in == "/cancel":
        pending_broadcast.discard(uid)
        pending_admin_action.pop(uid, None)
        admin_sessions.discard(uid)
        await message.answer("Действие отменено.", reply_markup=main_keyboard(uid))
        return

    if is_banned(uid):
        await message.answer("🚫 Ваш доступ к боту ограничен.")
        return

    if uid in admin_sessions and pending_admin_action.get(uid) == "password":
        if text_in == ADMIN_PASSWORD:
            pending_admin_action.pop(uid, None)
            await message.answer("Пароль принят.", reply_markup=main_keyboard(uid))
            await show_admin(message)
        else:
            admin_sessions.discard(uid)
            pending_admin_action.pop(uid, None)
            await message.answer("Неверный пароль.")
        return

    if uid in pending_broadcast:
        if not is_admin(uid):
            pending_broadcast.discard(uid)
            return
        pending_broadcast.discard(uid)
        with db() as c:
            ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE banned=0")]
        sent = 0
        for target in ids:
            if await safe_send(target, "📣 <b>Сообщение от администрации</b>\n\n" + text_in):
                sent += 1
            await asyncio.sleep(0.04)
        await message.answer(f"Рассылка завершена. Доставлено: {sent} из {len(ids)}.")
        return

    action = pending_admin_action.get(uid)
    if action and is_admin(uid):
        if action in ("ban", "unban", "grant", "remove"):
            parts = text_in.split()
            if not parts or not parts[0].isdigit():
                await message.answer("Формат: <code>TELEGRAM_ID</code>" +
                                     (" <code>ДНИ</code>" if action == "grant" else ""))
                return
            target = int(parts[0])
            if action == "ban":
                with db() as c:
                    c.execute("UPDATE users SET banned=1 WHERE user_id=?", (target,))
                answer = f"Пользователь {target} заблокирован, если он есть в базе."
            elif action == "unban":
                with db() as c:
                    c.execute("UPDATE users SET banned=0 WHERE user_id=?", (target,))
                answer = f"Пользователь {target} разблокирован, если он есть в базе."
            elif action == "grant":
                days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
                if not 1 <= days <= 3650:
                    await message.answer("Укажи число дней от 1 до 3650.")
                    return
                until = grant_premium(target, days)
                answer = f"Premium выдан/продлён пользователю {target} до {until.strftime('%d.%m.%Y %H:%M UTC')}."
            else:
                with db() as c:
                    c.execute("UPDATE users SET premium_until=NULL WHERE user_id=?", (target,))
                answer = f"Premium снят у пользователя {target}, если он есть в базе."
            pending_admin_action.pop(uid, None)
            await message.answer(answer, reply_markup=main_keyboard(uid))
            return

    if text_in in ("💎 Подписка", "/subscribe"):
        await message.answer(
            f"💎 <b>Premium</b>\nТекущий статус: <b>{premium_until_text(uid)}</b>\n\nВыбери тариф и оплати счёт в Telegram Stars:",
            reply_markup=tariff_keyboard(),
        )
        return
    if text_in in ("👤 Профиль", "/profile"):
        await message.answer(
            f"👤 <b>Профиль</b>\nID: <code>{uid}</code>\n"
            f"Premium до: <b>{premium_until_text(uid)}</b>\n"
            f"Лимит без Premium: {FREE_DAILY_LIMIT} математических запросов в сутки.",
            reply_markup=main_keyboard(uid),
        )
        return
    if text_in in ("❓ Помощь", "/help"):
        await message.answer(
            "📌 <b>Как пользоваться</b>\n"
            "• Отправь математическое выражение или уравнение текстом.\n"
            "• Отправь фото с печатным условием задачи.\n"
            "• Нажми «📚 Википедия» и напиши тему для поиска.\n"
            "• «💎 Подписка» — оплата Premium в Telegram Stars.\n"
            "Примеры: <code>2x+5=15</code>, <code>(3a^2+6a)/3a</code>, <code>25*4</code>.",
            reply_markup=main_keyboard(uid),
        )
        return
    if text_in in ("🛠 Админ-панель", "/admin"):
        await show_admin(message)
        return
    if text_in in ("🧮 Решить задачу", "💬 Чат"):
        await message.answer("Отправь задачу текстом или фотографией. Например: <code>2x + 5 = 15</code>.")
        return
    if text_in == "📚 Википедия":
        await message.answer("Напиши тему для поиска в Википедии.")
        return

    # Команды/слова для справочного поиска.
    if text_in.lower().startswith(("вики ", "википедия ", "найди в википедии ")):
        query = re.sub(r"^(вики|википедия|найди в википедии)\s+", "", text_in, flags=re.I)
        await message.answer(await wikipedia_search(query), disable_web_page_preview=True)
        return

    math_answer = solve_math(text_in)
    if math_answer:
        ok, reason = allowed_math_request(uid)
        if not ok:
            await message.answer(reason, reply_markup=main_keyboard(uid))
            return
        log_request(uid, "text_math")
        await message.answer(math_answer)
        return

    # Простая обработка приветствий/обычных сообщений без выдумывания полноценной LLM.
    lower = re.sub(r"[^\w\s]", "", text_in.lower()).strip()
    if lower in {"привет", "првт", "здравствуй", "здравствуйте", "хай", "hello", "hi", "добрый день", "добрый вечер"}:
        await message.answer("Привет! 👋 Я могу помочь с математикой. Пришли задачу текстом или фото.")
        return
    if lower in {"спасибо", "спс", "благодарю"}:
        await message.answer("Пожалуйста! 😊 Если есть задача — отправляй.")
        return
    if lower in {"как дела", "как дела бот"}:
        await message.answer("Работаю! 🙂 Давай решим задачу или найдем справку в Википедии.")
        return

    # Если это похоже на математическую запись, не скрываем ошибку за поиском.
    if re.search(r"[=+\-*/^()]|\d", text_in):
        await message.answer(
            "Не смог надёжно разобрать выражение. Проверь знаки и скобки или отправь задачу фото.\n"
            "Пример: <code>2*x + 5 = 15</code>."
        )
    else:
        await message.answer(
            "Я пока не полноценная универсальная нейросеть для свободного диалога. "
            "Могу решать многие математические выражения и искать справки в Википедии.\n\n"
            "Чтобы найти тему, напиши: <code>вики искусственный интеллект</code>."
        )


@dp.message(F.photo)
async def photo_handler(message: Message):
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        await message.answer("🚫 Ваш доступ к боту ограничен.")
        return
    ok, reason = allowed_math_request(uid)
    if not ok:
        await message.answer(reason)
        return
    await message.answer("📷 Читаю текст на фото…")
    extracted = await image_to_text(message)
    if extracted.startswith(("На сервере", "Не получилось", "Не смог распознать", "Фото слишком")):
        await message.answer(extracted)
        return
    log_request(uid, "photo_math")
    answer = solve_math(extracted)
    if answer:
        await message.answer(f"<b>Распознанный текст:</b>\n<code>{extracted[:1200]}</code>\n\n{answer}")
    else:
        await message.answer(
            f"📝 <b>Распознанный текст:</b>\n<code>{extracted[:2500]}</code>\n\n"
            "Не получилось автоматически решить распознанный текст. Проверь, верно ли распознано условие, и отправь его текстом."
        )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_tariff(callback: CallbackQuery):
    uid = callback.from_user.id
    ensure_user(callback.from_user)
    if is_banned(uid):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    tariff = TARIFFS.get(key)
    if not tariff:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return
    payload = f"premium:{uid}:{key}:{int(time.time())}"
    try:
        await bot.send_invoice(
            chat_id=uid,
            title=f"MathMind Premium — {tariff['title']}",
            description=f"Доступ к Premium на {tariff['days']} дней.",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=tariff["title"], amount=tariff["stars"])],
            provider_token="",
            start_parameter=f"premium_{key}",
        )
        await callback.answer()
    except Exception as e:
        log.exception("Не удалось выставить счёт")
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload
    try:
        parts = payload.split(":")
        valid = len(parts) == 4 and parts[0] == "premium" and int(parts[1]) == pre_checkout_query.from_user.id
        key = parts[2] if valid else ""
        valid = valid and key in TARIFFS
        if valid:
            valid = (pre_checkout_query.currency == "XTR" and
                     pre_checkout_query.total_amount == TARIFFS[key]["stars"])
        await pre_checkout_query.answer(ok=bool(valid), error_message=None if valid else "Счёт недействителен. Откройте подписку заново.")
    except Exception:
        await pre_checkout_query.answer(ok=False, error_message="Не удалось проверить счёт. Попробуйте снова.")


@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    payment = message.successful_payment
    uid = message.from_user.id
    payload = payment.invoice_payload
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != "premium" or not parts[1].isdigit() or int(parts[1]) != uid:
        await message.answer("Платёж получен, но не удалось проверить тариф. Напиши в поддержку.")
        return
    key = parts[2]
    tariff = TARIFFS.get(key)
    if not tariff or payment.currency != "XTR" or payment.total_amount != tariff["stars"]:
        await message.answer("Платёж получен, но параметры счёта не совпали. Обратись к администратору.")
        return
    try:
        with db() as c:
            c.execute(
                "INSERT INTO payments(user_id,charge_id,payload,stars,created_at) VALUES(?,?,?,?,?)",
                (uid, payment.telegram_payment_charge_id, payload, payment.total_amount, iso(now_utc()))
            )
        until = grant_premium(uid, tariff["days"])
        await message.answer(
            f"✅ <b>Оплата прошла!</b>\nPremium активирован до <b>{until.astimezone().strftime('%d.%m.%Y %H:%M')}</b>.\n"
            "Если подписка уже была активна, срок продлён.",
            reply_markup=main_keyboard(uid),
        )
    except sqlite3.IntegrityError:
        await message.answer("Этот платёж уже учтён. Подписка не была активирована повторно.")
    except Exception:
        log.exception("Ошибка обработки платежа")
        await message.answer("Оплата получена, но возникла ошибка активации. Сообщи администратору.")


@dp.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid) or not premium_admin_authorized(uid):
        await callback.answer("Нет доступа. Сначала введи /admin и пароль.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action == "stats":
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            banned = c.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
            premium = c.execute("SELECT COUNT(*) n FROM users WHERE premium_until>?", (iso(now_utc()),)).fetchone()["n"]
            payments = c.execute("SELECT COUNT(*) n, COALESCE(SUM(stars),0) total FROM payments").fetchone()
            reqs = c.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"]
        await callback.message.answer(
            "📊 <b>Статистика</b>\n"
            f"Пользователей: {users}\nЗаблокировано: {banned}\nАктивный Premium: {premium}\n"
            f"Математических запросов: {reqs}\nОплат: {payments['n']}\nПолучено: {payments['total']} ⭐"
        )
    elif action == "users":
        with db() as c:
            rows = c.execute("SELECT user_id, username, first_name, premium_until, banned FROM users ORDER BY joined_at DESC LIMIT 30").fetchall()
        if not rows:
            await callback.message.answer("Пользователей пока нет.")
        else:
            lines = ["👥 <b>Последние 30 пользователей</b>"]
            for r in rows:
                name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Без имени")
                until = parse_dt(r["premium_until"])
                status = f"Premium до {until.strftime('%d.%m.%Y')}" if until and until > now_utc() else "без Premium"
                lines.append(f"<code>{r['user_id']}</code> — {name} — {status} — {'бан' if r['banned'] else 'активен'}")
            await callback.message.answer("\n".join(lines))
    elif action in ("ban", "unban", "grant", "remove"):
        pending_admin_action[uid] = action
        prompts = {
            "ban": "Отправь Telegram ID пользователя для блокировки.",
            "unban": "Отправь Telegram ID пользователя для разблокировки.",
            "grant": "Отправь ID и число дней, например: <code>123456789 30</code>.",
            "remove": "Отправь Telegram ID пользователя, у которого снять Premium.",
        }
        await callback.message.answer(prompts[action])
    elif action == "broadcast":
        pending_broadcast.add(uid)
        await callback.message.answer("Отправь следующим сообщением текст рассылки. Отменить: /cancel.")
    elif action == "payments":
        with db() as c:
            rows = c.execute("""
                SELECT user_id, stars, created_at, payload FROM payments
                ORDER BY id DESC LIMIT 15
            """).fetchall()
        if not rows:
            await callback.message.answer("Оплат пока нет.")
        else:
            lines = ["💳 <b>Последние 15 оплат</b>"]
            for r in rows:
                lines.append(f"ID <code>{r['user_id']}</code> — {r['stars']} ⭐ — {r['created_at'][:16]}")
            await callback.message.answer("\n".join(lines))
    await callback.answer()


def premium_admin_authorized(uid: int) -> bool:
    # Сессия считается активной после успешного ввода пароля.
    return uid in admin_sessions and pending_admin_action.get(uid) != "password"


@dp.message(Command("cancel"))
async def cancel(message: Message):
    uid = message.from_user.id
    pending_broadcast.discard(uid)
    pending_admin_action.pop(uid, None)
    admin_sessions.discard(uid)
    await message.answer("Действие отменено.", reply_markup=main_keyboard(uid))


async def main():
    init_db()
    log.info("MathMind запускается")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Aiogram сам закрывает сессию при штатном завершении polling.
        pass
