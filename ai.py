"""
=====================================================================
 Telegram AI-бот: решение математики (текст + фото), чат, подписки, админка
=====================================================================
Установка:
    pip install aiogram anthropic aiohttp

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    export ANTHROPIC_API_KEY="ключ_API"
    python bot.py

Как это работает (без обучения с нуля):
  * "Мозг" — готовая модель через API (умеет математику и читает фото).
  * Знания — подтягиваем справку из Википедии (ru -> en) и даём модели.
  * Простые фразы ("привет", "спасибо"...) бот определяет сам, БЕЗ токенов.
  * Лимиты, тарифы, пароль админа — всё в блоке НАСТРОЙКИ ниже.
=====================================================================
"""

import asyncio
import base64
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from anthropic import AsyncAnthropic

# =====================================================================
#                              НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "ВСТАВЬ_API_КЛЮЧ")
MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5")

DB_PATH = "bot.db"
ADMIN_PASSWORD = "2525"          # ВАЖНО: лучше сменить на длинный
ADMIN_MAX_ATTEMPTS = 3           # попыток ввода пароля
ADMIN_BLOCK_SECONDS = 600        # блокировка попыток после ошибок
SUPPORT_CONTACT = "@your_username"  # куда писать для оплаты подписки

WIKI_ENABLED = True
WIKI_LANGS = ("ru", "en")
WIKI_MAX_CHARS = 1800

# ---- Тарифы (что даёт каждый план) ----
PLANS = {
    "free": {
        "title": "🆓 Бесплатный",
        "daily_requests": 10,        # запросов в сутки
        "daily_tokens": 20_000,      # токенов в сутки (вход+выход)
        "daily_photos": 3,           # фото-задач в сутки
        "max_output_tokens": 800,    # длина одного ответа
        "history_len": 4,            # сколько прошлых сообщений помнит
    },
    "pro": {
        "title": "💎 Premium",
        "daily_requests": 200,
        "daily_tokens": 400_000,
        "daily_photos": 50,
        "max_output_tokens": 2500,
        "history_len": 12,
    },
}

# ---- Тарифы для покупки (название, дней, цена) ----
TARIFFS = [
    ("7 дней", 7, "199 ₽"),
    ("30 дней", 30, "499 ₽"),
    ("90 дней", 90, "1199 ₽"),
]

# ---- Тексты кнопок меню ----
BTN_MATH = "🧮 Решить задачу"
BTN_CHAT = "💬 Чат"
BTN_SUB = "💎 Подписка"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"

# ---- Системные промпты ----
MATH_PROMPT = (
    "Ты — точный репетитор по математике. Решай задачу пошагово и понятно, "
    "на языке пользователя (по умолчанию русский). Не используй LaTeX и markdown — "
    "только обычный текст и символы: ^, √, ×, ÷, ≤, ≥, π, дроби вида a/b. "
    "Проверь ответ. В конце напиши строку 'Ответ: ...'. "
    "Если на фото неразборчиво — скажи, что именно не видно."
)
CHAT_PROMPT = (
    "Ты — дружелюбный и умный ассистент в Telegram. Отвечай кратко и по делу, "
    "на языке пользователя. Не используй markdown и LaTeX — только обычный текст. "
    "Если дана справка из Википедии — опирайся на неё, но не выдумывай факты, "
    "которых нет. Если не знаешь — честно скажи."
)

# ---- Быстрые ответы без нейросети (экономят токены) ----
SMALLTALK = [
    (r"(привет|прив|хай|здорово|здравствуй|здравствуйте|добрый день|добрый вечер|"
     r"доброе утро|hi|hello|hey|ку|салют|хэллоу)",
     ["Привет! 👋 Пришли задачу текстом или фото — решу.",
      "Здравствуй! Чем помочь? Можно скинуть задачу по математике 🧮"]),
    (r"(пока|до свидания|бай|bye|до встречи)",
     ["Пока! Возвращайся с новыми задачами 👋"]),
    (r"(спасибо|благодарю|спс|thx|thanks|благодарствую)",
     ["Пожалуйста! 😊", "Всегда рад помочь!"]),
    (r"(как дела|как ты|что нового|как жизнь|как поживаешь)",
     ["Отлично, готов решать задачи! А у тебя?"]),
    (r"(кто ты|ты кто|что ты умеешь|что умеешь|что ты можешь)",
     ["Я ИИ-бот: решаю математику по тексту и фото, отвечаю на вопросы "
      "и ищу факты в Википедии. Просто напиши мне!"]),
]

# =====================================================================
#                           БАЗА ДАННЫХ
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined INTEGER,
    banned INTEGER DEFAULT 0,
    sub_until INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'chat',
    req_date TEXT DEFAULT '',
    requests_today INTEGER DEFAULT 0,
    photos_today INTEGER DEFAULT 0,
    tokens_today INTEGER DEFAULT 0,
    requests_total INTEGER DEFAULT 0,
    tokens_total INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
"""


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.c.commit()

    def _run(self, sql, args=(), one=False, fetch=False):
        cur = self.c.execute(sql, args)
        self.c.commit()
        if fetch:
            return cur.fetchone() if one else cur.fetchall()
        return cur

    # ---- пользователи ----
    def upsert_user(self, uid, username, first_name):
        if not self._run("SELECT 1 FROM users WHERE id=?", (uid,), one=True, fetch=True):
            self._run(
                "INSERT INTO users (id, username, first_name, joined, req_date) VALUES (?,?,?,?,?)",
                (uid, username, first_name, int(time.time()), today_str()),
            )
        else:
            self._run("UPDATE users SET username=?, first_name=? WHERE id=?",
                      (username, first_name, uid))

    def get_user(self, uid):
        """Возвращает пользователя; сбрасывает дневные счётчики в новый день."""
        u = self._run("SELECT * FROM users WHERE id=?", (uid,), one=True, fetch=True)
        if u and u["req_date"] != today_str():
            self._run(
                "UPDATE users SET req_date=?, requests_today=0, photos_today=0, tokens_today=0 WHERE id=?",
                (today_str(), uid),
            )
            u = self._run("SELECT * FROM users WHERE id=?", (uid,), one=True, fetch=True)
        return u

    def find_user(self, text: str):
        text = text.strip().lstrip("@")
        if text.isdigit():
            return self.get_user(int(text))
        row = self._run("SELECT id FROM users WHERE lower(username)=lower(?)", (text,),
                        one=True, fetch=True)
        return self.get_user(row["id"]) if row else None

    def set_mode(self, uid, mode):
        self._run("UPDATE users SET mode=? WHERE id=?", (mode, uid))

    def set_ban(self, uid, banned: bool):
        self._run("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, uid))

    def add_days(self, uid, days: int):
        u = self.get_user(uid)
        base = max(int(time.time()), u["sub_until"] or 0)
        new_until = base + days * 86400
        self._run("UPDATE users SET sub_until=? WHERE id=?", (new_until, uid))
        return new_until

    def remove_sub(self, uid):
        self._run("UPDATE users SET sub_until=0 WHERE id=?", (uid,))

    def add_usage(self, uid, tokens: int, photo: bool):
        self._run(
            "UPDATE users SET requests_today=requests_today+1, requests_total=requests_total+1,"
            " tokens_today=tokens_today+?, tokens_total=tokens_total+?,"
            " photos_today=photos_today+? WHERE id=?",
            (tokens, tokens, 1 if photo else 0, uid),
        )

    def all_user_ids(self):
        return [r["id"] for r in self._run("SELECT id FROM users WHERE banned=0", fetch=True)]

    # ---- админы ----
    def is_admin(self, uid) -> bool:
        return bool(self._run("SELECT 1 FROM admins WHERE user_id=?", (uid,), one=True, fetch=True))

    def set_admin(self, uid, value: bool):
        if value:
            self._run("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
        else:
            self._run("DELETE FROM admins WHERE user_id=?", (uid,))

    # ---- статистика ----
    def stats(self) -> dict:
        now = int(time.time())
        q = lambda sql, a=(): self._run(sql, a, one=True, fetch=True)[0] or 0
        return {
            "users": q("SELECT COUNT(*) FROM users"),
            "new_today": q("SELECT COUNT(*) FROM users WHERE joined>=?", (now - now % 86400,)),
            "banned": q("SELECT COUNT(*) FROM users WHERE banned=1"),
            "premium": q("SELECT COUNT(*) FROM users WHERE sub_until>?", (now,)),
            "active_today": q("SELECT COUNT(*) FROM users WHERE req_date=? AND requests_today>0",
                              (today_str(),)),
            "req_today": q("SELECT SUM(requests_today) FROM users WHERE req_date=?", (today_str(),)),
            "req_total": q("SELECT SUM(requests_total) FROM users"),
            "tok_today": q("SELECT SUM(tokens_today) FROM users WHERE req_date=?", (today_str(),)),
            "tok_total": q("SELECT SUM(tokens_total) FROM users"),
        }


db = DB(DB_PATH)

# =====================================================================
#                       ЛОГИКА ПЛАНОВ И ЛИМИТОВ
# =====================================================================


def is_premium(u) -> bool:
    return (u["sub_until"] or 0) > time.time()


def plan_of(u) -> dict:
    return PLANS["pro" if is_premium(u) else "free"]


def check_limits(u, is_photo: bool):
    """Возвращает текст ошибки или None, если можно."""
    plan = plan_of(u)
    hint = "\n\nОбновление лимитов — завтра, либо оформи 💎 Подписку."
    if u["requests_today"] >= plan["daily_requests"]:
        return f"⛔ Дневной лимит запросов исчерпан ({plan['daily_requests']})." + hint
    if u["tokens_today"] >= plan["daily_tokens"]:
        return "⛔ Дневной лимит токенов исчерпан." + hint
    if is_photo and u["photos_today"] >= plan["daily_photos"]:
        return f"⛔ Лимит фото-задач на сегодня исчерпан ({plan['daily_photos']})." + hint
    return None


def profile_text(u) -> str:
    plan = plan_of(u)
    sub = f"до {fmt_date(u['sub_until'])}" if is_premium(u) else "нет"
    return (
        f"👤 Профиль\n"
        f"ID: {u['id']}\n"
        f"Тариф: {plan['title']}\n"
        f"Подписка: {sub}\n\n"
        f"Сегодня использовано:\n"
        f"• запросов: {u['requests_today']}/{plan['daily_requests']}\n"
        f"• фото: {u['photos_today']}/{plan['daily_photos']}\n"
        f"• токенов: {u['tokens_today']}/{plan['daily_tokens']}\n\n"
        f"Всего запросов: {u['requests_total']}"
    )


def subscription_text() -> str:
    f, p = PLANS["free"], PLANS["pro"]
    lines = [
        "💎 Подписка Premium\n",
        f"{f['title']}: {f['daily_requests']} запросов, {f['daily_photos']} фото, "
        f"ответ до {f['max_output_tokens']} ток.",
        f"{p['title']}: {p['daily_requests']} запросов, {p['daily_photos']} фото, "
        f"ответ до {p['max_output_tokens']} ток., длинная память диалога.\n",
        "Тарифы:",
    ]
    lines += [f"• {name} — {price}" for name, _, price in TARIFFS]
    lines.append(f"\nДля оплаты напиши: {SUPPORT_CONTACT}\nПосле оплаты подписку включит администратор.")
    return "\n".join(lines)


# =====================================================================
#                    БЫСТРЫЕ ОТВЕТЫ (БЕЗ НЕЙРОСЕТИ)
# =====================================================================


def normalize(text: str) -> str:
    text = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def smalltalk_reply(text: str):
    t = normalize(text)
    for pattern, answers in SMALLTALK:
        if re.fullmatch(pattern, t):
            return random.choice(answers)
    return None


MATH_RE = re.compile(
    r"\d\s*[\+\-\*/\^=×÷:]\s*[\d(xхyу]|[xхyу]\s*[\^²=]|[=∫√∑π]|"
    r"\b(реши|решить|уравнени|интеграл|производн|вычисли|упрост|корень|корни|"
    r"синус|косинус|тангенс|логарифм|sin|cos|tan|log|предел|матриц|дроб|процент)",
    re.IGNORECASE,
)


def looks_like_math(text: str) -> bool:
    return bool(MATH_RE.search(text))


# =====================================================================
#                              ВИКИПЕДИЯ
# =====================================================================
WIKI_HEADERS = {"User-Agent": "TgAIBot/1.0 (educational project)"}
WIKI_CLEAN = re.compile(
    r"^(что такое|кто такой|кто такая|кто такие|что значит|расскажи про|расскажи о|"
    r"расскажи|объясни|что за|what is|who is|tell me about)\s+", re.IGNORECASE)


async def wiki_context(query: str):
    query = WIKI_CLEAN.sub("", query.strip().rstrip("?!. "))[:100]
    if len(query) < 3:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(headers=WIKI_HEADERS, timeout=timeout) as s:
            for lang in WIKI_LANGS:
                api = f"https://{lang}.wikipedia.org/w/api.php"
                async with s.get(api, params={
                    "action": "query", "list": "search", "srsearch": query,
                    "srlimit": 1, "format": "json"}) as r:
                    data = await r.json()
                hits = data.get("query", {}).get("search", [])
                if not hits:
                    continue
                title = hits[0]["title"]
                async with s.get(api, params={
                    "action": "query", "prop": "extracts", "exintro": 1,
                    "explaintext": 1, "titles": title, "format": "json"}) as r:
                    data = await r.json()
                pages = data.get("query", {}).get("pages", {})
                extract = next(iter(pages.values()), {}).get("extract", "")
                if extract:
                    return f"[{title} — Wikipedia {lang}]\n{extract[:WIKI_MAX_CHARS]}"
    except Exception as e:
        logging.warning("Wikipedia error: %s", e)
    return None


# =====================================================================
#                               ИИ
# =====================================================================
llm = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
history: dict[int, list[dict]] = {}   # память диалога (в оперативке)


def get_history(uid: int, limit: int) -> list[dict]:
    h = history.get(uid, [])[-limit:]
    while h and h[0]["role"] != "user":   # диалог должен начинаться с user
        h = h[1:]
    return h


def push_history(uid: int, user_text: str, answer: str):
    h = history.setdefault(uid, [])
    h.append({"role": "user", "content": user_text})
    h.append({"role": "assistant", "content": answer})
    del h[:-40]


async def ask_ai(system: str, messages: list[dict], max_tokens: int):
    resp = await llm.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system, messages=messages)
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    return text or "Не удалось получить ответ.", tokens


# =====================================================================
#                             КЛАВИАТУРЫ
# =====================================================================


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MATH), KeyboardButton(text=BTN_CHAT)],
            [KeyboardButton(text=BTN_SUB), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def admin_kb() -> InlineKeyboardMarkup:
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("📊 Статистика", "adm:stats"), b("🔎 Инфо о юзере", "adm:info")],
        [b("🚫 Бан", "adm:ban"), b("✅ Разбан", "adm:unban")],
        [b("➕ Выдать/продлить", "adm:give"), b("➖ Забрать подписку", "adm:remove")],
        [b("📢 Рассылка", "adm:broadcast")],
        [b("🚪 Выйти из админки", "adm:logout")],
    ])


# =====================================================================
#                          МИДДЛВАРЬ (БАН)
# =====================================================================


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if user and not user.is_bot:
            db.upsert_user(user.id, user.username, user.first_name)
            u = db.get_user(user.id)
            if u["banned"] and not db.is_admin(user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Вы заблокированы.", show_alert=True)
                else:
                    await event.answer("🚫 Вы заблокированы администратором.")
                return
        return await handler(event, data)


# =====================================================================
#                            ХЭНДЛЕРЫ
# =====================================================================
router = Router()
bot = Bot(BOT_TOKEN)
admin_attempts: dict[int, dict] = {}   # {uid: {"fails": n, "until": ts}}


class AdminSt(StatesGroup):
    password = State()
    ban = State()
    unban = State()
    give = State()
    remove = State()
    info = State()
    broadcast = State()


async def send_long(message: Message, text: str, **kw):
    """Telegram режет сообщения >4096 символов — делим на части."""
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], **kw)


async def notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


# ------------------------- Команды -------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or "друг"
    await message.answer(
        f"Привет, {name}! 👋\n\n"
        "Я ИИ-бот. Умею:\n"
        "🧮 решать математику по тексту и по ФОТО\n"
        "💬 отвечать на вопросы (с помощью Википедии)\n\n"
        "Просто отправь задачу или вопрос. Кнопки — внизу.",
        reply_markup=main_kb(),
    )


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• 🧮 Решить задачу — режим математики: пришли текст или фото задачи.\n"
        "• 💬 Чат — обычные вопросы, факты из Википедии.\n"
        "• 💎 Подписка — тарифы и лимиты.\n"
        "• 👤 Профиль — твои лимиты на сегодня.\n\n"
        "Совет: на фото пиши задачу чётко, при хорошем свете.",
        reply_markup=main_kb(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb())


# ------------------------- Админка -------------------------


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    uid = message.from_user.id
    if db.is_admin(uid):
        await state.clear()
        await message.answer("🛠 Админ-панель", reply_markup=admin_kb())
        return
    a = admin_attempts.get(uid, {"fails": 0, "until": 0})
    if a["until"] > time.time():
        await message.answer("⏳ Слишком много попыток. Попробуй позже.")
        return
    await state.set_state(AdminSt.password)
    await message.answer("🔐 Введи пароль администратора (или /cancel):")


@router.message(AdminSt.password)
async def admin_password(message: Message, state: FSMContext):
    uid = message.from_user.id
    try:
        await message.delete()   # не оставляем пароль в чате
    except Exception:
        pass
    if (message.text or "").strip() == ADMIN_PASSWORD:
        admin_attempts.pop(uid, None)
        db.set_admin(uid, True)
        await state.clear()
        await message.answer("✅ Доступ разрешён.\n🛠 Админ-панель", reply_markup=admin_kb())
        return
    a = admin_attempts.setdefault(uid, {"fails": 0, "until": 0})
    a["fails"] += 1
    if a["fails"] >= ADMIN_MAX_ATTEMPTS:
        a["fails"] = 0
        a["until"] = time.time() + ADMIN_BLOCK_SECONDS
        await state.clear()
        await message.answer("⛔ Неверный пароль. Попытки заблокированы на 10 минут.")
    else:
        await message.answer(f"❌ Неверный пароль. Осталось попыток: {ADMIN_MAX_ATTEMPTS - a['fails']}")


ADMIN_PROMPTS = {
    "ban": (AdminSt.ban, "🚫 Отправь ID или @username для БАНА:"),
    "unban": (AdminSt.unban, "✅ Отправь ID или @username для РАЗБАНА:"),
    "give": (AdminSt.give, "➕ Отправь: ID_или_@username КОЛ-ВО_ДНЕЙ\nНапример: 123456789 30"),
    "remove": (AdminSt.remove, "➖ Отправь ID или @username, у кого забрать подписку:"),
    "info": (AdminSt.info, "🔎 Отправь ID или @username:"),
    "broadcast": (AdminSt.broadcast, "📢 Отправь текст рассылки (всем не забаненным):"),
}


@router.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not db.is_admin(uid):
        await call.answer("Нет доступа. Используй /admin", show_alert=True)
        return
    action = call.data.split(":")[1]

    if action == "stats":
        s = db.stats()
        await call.message.answer(
            "📊 Статистика\n\n"
            f"Пользователей: {s['users']} (новых сегодня: {s['new_today']})\n"
            f"Активных сегодня: {s['active_today']}\n"
            f"Premium: {s['premium']}\n"
            f"Забанено: {s['banned']}\n\n"
            f"Запросов сегодня: {s['req_today']}\n"
            f"Запросов всего: {s['req_total']}\n"
            f"Токенов сегодня: {s['tok_today']}\n"
            f"Токенов всего: {s['tok_total']}",
            reply_markup=admin_kb(),
        )
    elif action == "logout":
        db.set_admin(uid, False)
        await state.clear()
        await call.message.answer("🚪 Вы вышли из админки.", reply_markup=main_kb())
    elif action in ADMIN_PROMPTS:
        st, prompt = ADMIN_PROMPTS[action]
        await state.set_state(st)
        await call.message.answer(prompt + "\n\n(/cancel — отмена)")
    await call.answer()


def admin_only(message: Message) -> bool:
    return db.is_admin(message.from_user.id)


def user_card(u) -> str:
    sub = f"до {fmt_date(u['sub_until'])}" if is_premium(u) else "нет"
    return (f"ID: {u['id']}\n@{u['username'] or '-'} | {u['first_name'] or '-'}\n"
            f"Бан: {'да' if u['banned'] else 'нет'}\nПодписка: {sub}\n"
            f"Запросов всего: {u['requests_total']} | токенов: {u['tokens_total']}\n"
            f"Регистрация: {fmt_date(u['joined'])}")


@router.message(AdminSt.ban)
async def adm_ban(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    u = db.find_user(message.text or "")
    if not u:
        return await message.answer("Не нашёл такого пользователя. Попробуй ещё или /cancel")
    if db.is_admin(u["id"]):
        return await message.answer("Нельзя банить администратора.")
    db.set_ban(u["id"], True)
    await state.clear()
    await message.answer(f"🚫 Забанен: {u['id']}", reply_markup=admin_kb())
    await notify(u["id"], "🚫 Вы заблокированы администратором.")


@router.message(AdminSt.unban)
async def adm_unban(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    u = db.find_user(message.text or "")
    if not u:
        return await message.answer("Не нашёл такого пользователя. Попробуй ещё или /cancel")
    db.set_ban(u["id"], False)
    await state.clear()
    await message.answer(f"✅ Разбанен: {u['id']}", reply_markup=admin_kb())
    await notify(u["id"], "✅ Вы разблокированы. Можно пользоваться ботом!")


@router.message(AdminSt.give)
async def adm_give(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        return await message.answer("Формат: ID_или_@username ДНИ (например: 123456789 30)")
    u = db.find_user(parts[0])
    if not u:
        return await message.answer("Не нашёл такого пользователя.")
    days = int(parts[1])
    if days <= 0 or days > 3650:
        return await message.answer("Дни: от 1 до 3650.")
    until = db.add_days(u["id"], days)
    await state.clear()
    await message.answer(f"➕ Подписка для {u['id']} до {fmt_date(until)}", reply_markup=admin_kb())
    await notify(u["id"], f"💎 Вам выдана подписка Premium до {fmt_date(until)}!")


@router.message(AdminSt.remove)
async def adm_remove(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    u = db.find_user(message.text or "")
    if not u:
        return await message.answer("Не нашёл такого пользователя.")
    db.remove_sub(u["id"])
    await state.clear()
    await message.answer(f"➖ Подписка снята у {u['id']}", reply_markup=admin_kb())
    await notify(u["id"], "ℹ️ Ваша подписка Premium была отключена.")


@router.message(AdminSt.info)
async def adm_info(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    u = db.find_user(message.text or "")
    if not u:
        return await message.answer("Не нашёл такого пользователя.")
    await state.clear()
    await message.answer("🔎 Пользователь\n\n" + user_card(u), reply_markup=admin_kb())


@router.message(AdminSt.broadcast)
async def adm_broadcast(message: Message, state: FSMContext):
    if not admin_only(message):
        return await state.clear()
    text = message.text or ""
    await state.clear()
    ids = db.all_user_ids()
    await message.answer(f"📢 Отправляю {len(ids)} пользователям...")
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f"Готово: доставлено {ok}/{len(ids)}", reply_markup=admin_kb())


# ------------------------- Кнопки меню -------------------------


@router.message(F.text == BTN_MATH)
async def btn_math(message: Message):
    db.set_mode(message.from_user.id, "math")
    await message.answer("🧮 Режим математики включён.\nПришли задачу текстом или ФОТО.")


@router.message(F.text == BTN_CHAT)
async def btn_chat(message: Message):
    db.set_mode(message.from_user.id, "chat")
    await message.answer("💬 Режим чата включён. Спрашивай что угодно!")


@router.message(F.text == BTN_SUB)
async def btn_sub(message: Message):
    await message.answer(subscription_text())


@router.message(F.text == BTN_PROFILE)
async def btn_profile(message: Message):
    await message.answer(profile_text(db.get_user(message.from_user.id)))


# ------------------------- Основная обработка ИИ -------------------------


async def process_ai(message: Message, text: str, image_b64: str | None = None):
    uid = message.from_user.id
    u = db.get_user(uid)
    plan = plan_of(u)

    err = check_limits(u, is_photo=image_b64 is not None)
    if err:
        return await message.answer(err)

    is_math = image_b64 is not None or u["mode"] == "math" or looks_like_math(text)
    system = MATH_PROMPT if is_math else CHAT_PROMPT

    await bot.send_chat_action(message.chat.id, "typing")

    user_content = text
    if not is_math and WIKI_ENABLED:
        wiki = await wiki_context(text)
        if wiki:
            user_content = f"Справка из Википедии:\n{wiki}\n\nВопрос: {text}"

    if image_b64:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": image_b64}},
            {"type": "text", "text": text or "Реши задачу на фото."},
        ]
    else:
        content = user_content

    messages = get_history(uid, plan["history_len"]) + [{"role": "user", "content": content}]

    try:
        answer, tokens = await ask_ai(system, messages, plan["max_output_tokens"])
    except Exception as e:
        logging.exception("AI error: %s", e)
        return await message.answer("⚠️ Ошибка ИИ. Попробуй ещё раз чуть позже (лимит не списан).")

    db.add_usage(uid, tokens, photo=image_b64 is not None)
    push_history(uid, text or "[фото-задача]", answer)
    await send_long(message, answer)


@router.message(F.photo)
async def on_photo(message: Message):
    await bot.send_chat_action(message.chat.id, "typing")
    buf = await bot.download(message.photo[-1])
    b64 = base64.b64encode(buf.read()).decode()
    await process_ai(message, message.caption or "", image_b64=b64)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    text = message.text.strip()
    quick = smalltalk_reply(text)
    if quick:                       # приветствия и т.п. — без токенов
        return await message.answer(quick, reply_markup=main_kb())
    await process_ai(message, text)


# =====================================================================
#                               ЗАПУСК
# =====================================================================


async def main():
    logging.basicConfig(level=logging.INFO)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
