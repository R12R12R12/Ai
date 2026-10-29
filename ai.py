#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Универсальный ИИ-помощник для Telegram (aiogram 3.x, SQLite, Telegram Stars).

Весь код — в одном файле. Все настройки — в блоке «НАСТРОЙКИ» ниже и/или в
переменных окружения (см. .env.example). Языковая модель подключается через
OpenAI-совместимый HTTP-интерфейс (Ollama, vLLM, LM Studio, OpenRouter и др.)
и может быть заменена без переписывания проекта (см. класс LLMBackend).

Запуск:  python ai.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import html
import io
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone, tzinfo
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramForbidden, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:  # необязательно: читаем .env, если установлен python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ======================================================================
#                              НАСТРОЙКИ
# ======================================================================
def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_ids(name: str) -> set[int]:
    result: set[int] = set()
    for part in re.split(r"[,\s;]+", _env(name)):
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return result


# --- Telegram -----------------------------------------------------------
BOT_TOKEN = _env("9197441376:AAHDhrjW-C4H5qfTMPbAzB1ejXMyS6gwKmE")                 # токен от @BotFather (только через окружение!)
ADMIN_IDS = _env_ids("5796754554")             # Telegram ID администраторов: "123456,789012"
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "2525")  # пароль по умолчанию 2525 — СМЕНИТЕ на сервере
ADMIN_SESSION_MINUTES = 60                    # сколько живёт вход в админ-панель
ADMIN_MAX_ATTEMPTS = 5                        # неверных паролей до блокировки
ADMIN_LOCK_MINUTES = 5                       # длительность блокировки попыток

# --- Хранилище ----------------------------------------------------------
DB_PATH = _env("DB_PATH", "bot.db")
LOG_FILE = _env("LOG_FILE", "bot.log")
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
TIMEZONE_NAME = _env("TIMEZONE", "Europe/Moscow")  # граница «суток» для бесплатного лимита

# --- Лимиты и подписки --------------------------------------------------
FREE_DAILY_LIMIT = _env_int("FREE_DAILY_LIMIT", 5)
TARIFFS: dict[int, int] = {14: 100, 30: 300, 120: 800}  # дней -> цена в Telegram Stars (XTR)

# --- Контекст диалога ---------------------------------------------------
CONTEXT_MESSAGES = _env_int("CONTEXT_MESSAGES", 12)    # сколько последних сообщений уходит в модель
HISTORY_KEEP = _env_int("HISTORY_KEEP", 40)            # сколько сообщений хранить в БД на пользователя
MAX_INPUT_CHARS = _env_int("MAX_INPUT_CHARS", 4000)    # максимум символов во входящем тексте
MAX_STORED_CHARS = 4000                                # максимум символов одного сообщения в истории

# --- Языковая модель (OpenAI-совместимый API) ---------------------------
LLM_BASE_URL = _env("LLM_BASE_URL", "http://localhost:11434/v1")  # по умолчанию — локальный Ollama
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "llama3.1:8b")
LLM_VISION_MODEL = _env("LLM_VISION_MODEL")  # модель с поддержкой изображений; пусто = фото отключены
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.4)
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 1500)
LLM_TIMEOUT = _env_float("LLM_TIMEOUT", 120.0)
LLM_RETRIES = _env_int("LLM_RETRIES", 1)

# --- Изображения и документы --------------------------------------------
MAX_PHOTO_MB = _env_float("MAX_PHOTO_MB", 8.0)
IMAGE_DETAIL = _env("IMAGE_DETAIL", "auto")      # auto | low | high (для API, которые это поддерживают)
MAX_DOC_MB = _env_float("MAX_DOC_MB", 10.0)      # лимит скачивания у ботов Telegram — 20 МБ
DOC_MAX_CHARS = _env_int("DOC_MAX_CHARS", 20000)  # сколько символов документа отдаём модели
DOC_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".py", ".js",
    ".sql", ".yaml", ".yml", ".ini", ".pdf", ".docx",
}

# --- Поиск в интернете --------------------------------------------------
SEARCH_PROVIDER = _env("SEARCH_PROVIDER", "ddg").lower()  # ddg | searxng | none
SEARCH_URL = _env("SEARCH_URL")                           # адрес SearXNG, например http://localhost:8080
SEARCH_RESULTS = _env_int("SEARCH_RESULTS", 6)
SEARCH_TIMEOUT = _env_float("SEARCH_TIMEOUT", 15.0)

# ======================================================================
#                      Константы интерфейса и тексты
# ======================================================================
BTN_CHAT = "💬 Чат с ИИ"
BTN_PHOTO = "📷 Анализ фото"
BTN_SEARCH = "🌐 Поиск информации"
BTN_SUB = "⭐ Подписка"
BTN_PROFILE = "👤 Мой профиль"
BTN_HELP = "❓ Помощь"
ALL_BUTTONS = {BTN_CHAT, BTN_PHOTO, BTN_SEARCH, BTN_SUB, BTN_PROFILE, BTN_HELP}

SYSTEM_PROMPT = (
    "Ты — универсальный ИИ-помощник в Telegram. Отвечай на том языке, на котором пишет "
    "пользователь (по умолчанию — русский). Помогай с вопросами, учёбой, текстами, переводами, "
    "идеями, математикой, экономикой и анализом информации; сложное объясняй простыми словами, "
    "задачи решай по шагам. Не выдумывай факты, цитаты, ссылки и цифры: если не уверен или не "
    "знаешь — прямо скажи об этом. У тебя нет доступа к интернету, кроме случаев, когда "
    "результаты поиска приведены в сообщении. Ответ отправляется обычным текстом без "
    "Markdown-разметки: не используй символы ** и #, таблицы и блоки кода с тройными кавычками; "
    "списки оформляй дефисами или цифрами. Отвечай по существу и не слишком длинно."
)

SEARCH_PROMPT = (
    "Ниже в сообщении пользователя приведены результаты веб-поиска. Отвечай, опираясь на них. "
    "Если результатов недостаточно или они противоречат друг другу — скажи об этом. Ссылайся на "
    "источники номерами [1], [2] и в конце перечисли использованные URL."
)

DEFAULT_PHOTO_PROMPT = "Опиши, что изображено на фото, и перепиши весь текст, если он там есть."
DEFAULT_DOC_PROMPT = "Кратко перескажи содержание документа и выдели главное."

logger = logging.getLogger("aibot")
try:
    TZ: tzinfo = ZoneInfo(TIMEZONE_NAME)
except Exception:  # нет tzdata или неверное имя зоны
    TZ = timezone.utc


# ======================================================================
#                              БАЗА ДАННЫХ
# ======================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT    NOT NULL DEFAULT '',
    first_name     TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    banned         INTEGER NOT NULL DEFAULT 0,
    sub_until      INTEGER NOT NULL DEFAULT 0,
    sub_notified   INTEGER NOT NULL DEFAULT 1,
    mode           TEXT    NOT NULL DEFAULT 'chat',
    total_requests INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role    TEXT    NOT NULL,
    content TEXT    NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS payments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    charge_id          TEXT    NOT NULL UNIQUE,
    provider_charge_id TEXT    NOT NULL DEFAULT '',
    payload            TEXT    NOT NULL DEFAULT '',
    amount             INTEGER NOT NULL,
    currency           TEXT    NOT NULL,
    days               INTEGER NOT NULL DEFAULT 0,
    ts                 INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    INTEGER NOT NULL,
    level TEXT    NOT NULL,
    text  TEXT    NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def db_init() -> None:
    """Открывает соединение и создаёт таблицы."""
    global _conn
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)


def db_close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextlib.contextmanager
def _tx():
    """Транзакция с эксклюзивной блокировкой (все операции записи атомарны)."""
    with _lock:
        assert _conn is not None, "База данных не инициализирована"
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
        except BaseException:
            _conn.execute("ROLLBACK")
            raise
        else:
            _conn.execute("COMMIT")


def _query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        assert _conn is not None, "База данных не инициализирована"
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def _query_one(sql: str, args: tuple = ()) -> Optional[dict]:
    rows = _query(sql, args)
    return rows[0] if rows else None


async def adb(func: Callable[..., Any], *args: Any) -> Any:
    """Запускает синхронную функцию БД в потоке, не блокируя event loop."""
    return await asyncio.to_thread(func, *args)


# --- пользователи ---------------------------------------------------------
def db_upsert_user(uid: int, username: str, first_name: str) -> dict:
    now = int(time.time())
    with _tx() as c:
        c.execute(
            """INSERT INTO users(user_id, username, first_name, created_at, last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_seen=excluded.last_seen""",
            (uid, username or "", first_name or "", now, now),
        )
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(row)


def db_get_user(uid: int) -> Optional[dict]:
    return _query_one("SELECT * FROM users WHERE user_id=?", (uid,))


def db_find_user(query: str) -> Optional[dict]:
    query = query.strip()
    if query.lstrip("-").isdigit():
        return db_get_user(int(query))
    return _query_one("SELECT * FROM users WHERE LOWER(username)=?", (query.lstrip("@").lower(),))


def db_set_mode(uid: int, mode: str) -> None:
    with _tx() as c:
        c.execute("UPDATE users SET mode=? WHERE user_id=?", (mode, uid))


def db_set_banned(uid: int, banned: bool) -> bool:
    with _tx() as c:
        cur = c.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, uid))
        return cur.rowcount > 0


def db_list_users(offset: int, limit: int) -> list[dict]:
    return _query(
        "SELECT user_id, username, first_name, banned, sub_until FROM users "
        "ORDER BY created_at DESC, user_id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )


def db_count_users() -> int:
    return _query("SELECT COUNT(*) AS n FROM users")[0]["n"]


def db_broadcast_ids() -> list[int]:
    return [r["user_id"] for r in _query("SELECT user_id FROM users WHERE banned=0")]


# --- квоты ------------------------------------------------------------------
def db_consume_quota(uid: int, limit: int, day: str) -> tuple[str, int]:
    """
    Атомарно проверяет доступ и списывает запрос.
    Возвращает (статус, использовано_сегодня): sub | free | limit | banned | nouser.
    """
    now = int(time.time())
    with _tx() as c:
        user = c.execute("SELECT banned, sub_until FROM users WHERE user_id=?", (uid,)).fetchone()
        if user is None:
            return "nouser", 0
        if user["banned"]:
            return "banned", 0
        if user["sub_until"] > now:
            c.execute("UPDATE users SET total_requests=total_requests+1 WHERE user_id=?", (uid,))
            return "sub", 0
        row = c.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (uid, day)).fetchone()
        used = row["count"] if row else 0
        if used >= limit:
            return "limit", used
        c.execute(
            """INSERT INTO usage(user_id, day, count) VALUES(?,?,1)
               ON CONFLICT(user_id, day) DO UPDATE SET count=count+1""",
            (uid, day),
        )
        c.execute("UPDATE users SET total_requests=total_requests+1 WHERE user_id=?", (uid,))
        return "free", used + 1


def db_refund_quota(uid: int, status: str, day: str) -> None:
    """Возвращает списанный запрос, если ответ получить не удалось."""
    with _tx() as c:
        if status == "free":
            c.execute(
                "UPDATE usage SET count=MAX(count-1,0) WHERE user_id=? AND day=?", (uid, day)
            )
        if status in ("free", "sub"):
            c.execute(
                "UPDATE users SET total_requests=MAX(total_requests-1,0) WHERE user_id=?", (uid,)
            )


def db_used_today(uid: int, day: str) -> int:
    row = _query_one("SELECT count FROM usage WHERE user_id=? AND day=?", (uid, day))
    return row["count"] if row else 0


# --- история диалога --------------------------------------------------------
def db_get_context(uid: int, limit: int) -> list[dict]:
    rows = _query(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
    )
    rows.reverse()
    while rows and rows[0]["role"] != "user":  # контекст должен начинаться с реплики пользователя
        rows.pop(0)
    return rows


def db_add_exchange(uid: int, user_text: str, assistant_text: str) -> None:
    now = int(time.time())
    with _tx() as c:
        c.execute(
            "INSERT INTO messages(user_id, role, content, ts) VALUES(?,?,?,?)",
            (uid, "user", user_text[:MAX_STORED_CHARS], now),
        )
        c.execute(
            "INSERT INTO messages(user_id, role, content, ts) VALUES(?,?,?,?)",
            (uid, "assistant", assistant_text[:MAX_STORED_CHARS], now),
        )
        c.execute(
            """DELETE FROM messages WHERE user_id=? AND id NOT IN
               (SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)""",
            (uid, uid, max(HISTORY_KEEP, 2)),
        )


def db_clear_context(uid: int) -> None:
    with _tx() as c:
        c.execute("DELETE FROM messages WHERE user_id=?", (uid,))


# --- подписки и платежи -----------------------------------------------------
def db_apply_payment(
    uid: int, charge_id: str, provider_charge_id: str, payload: str,
    amount: int, currency: str, days: int,
) -> Optional[int]:
    """
    Записывает платёж и продлевает подписку одной транзакцией.
    Возвращает новую дату окончания (unix) или None, если платёж уже был учтён.
    Для days=0 платёж только сохраняется (подписка не выдаётся).
    """
    now = int(time.time())
    try:
        with _tx() as c:
            c.execute(
                """INSERT INTO payments(user_id, charge_id, provider_charge_id, payload,
                                        amount, currency, days, ts)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (uid, charge_id, provider_charge_id, payload, amount, currency, days, now),
            )
            row = c.execute("SELECT sub_until FROM users WHERE user_id=?", (uid,)).fetchone()
            current = row["sub_until"] if row else 0
            if days <= 0:
                return current
            new_until = max(now, current) + days * 86400
            c.execute(
                "UPDATE users SET sub_until=?, sub_notified=0 WHERE user_id=?", (new_until, uid)
            )
            return new_until
    except sqlite3.IntegrityError:
        return None  # charge_id уже существует — повторное зачисление запрещено


def db_grant_sub(uid: int, days: int) -> Optional[int]:
    now = int(time.time())
    with _tx() as c:
        row = c.execute("SELECT sub_until FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            return None
        new_until = max(now, row["sub_until"]) + days * 86400
        c.execute("UPDATE users SET sub_until=?, sub_notified=0 WHERE user_id=?", (new_until, uid))
        return new_until


def db_revoke_sub(uid: int) -> bool:
    with _tx() as c:
        cur = c.execute("UPDATE users SET sub_until=0, sub_notified=1 WHERE user_id=?", (uid,))
        return cur.rowcount > 0


def db_expired_unnotified() -> list[int]:
    now = int(time.time())
    with _tx() as c:
        rows = c.execute(
            "SELECT user_id FROM users WHERE sub_until>0 AND sub_until<=? AND sub_notified=0", (now,)
        ).fetchall()
        ids = [r["user_id"] for r in rows]
        if ids:
            c.executemany("UPDATE users SET sub_notified=1 WHERE user_id=?", [(i,) for i in ids])
    return ids


def db_user_payments_sum(uid: int) -> tuple[int, int]:
    row = _query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM payments "
        "WHERE user_id=? AND currency='XTR'", (uid,),
    )
    return (row["n"], row["s"]) if row else (0, 0)


# --- статистика, события, обслуживание ----------------------------------------
def db_stats(day: str) -> dict:
    now = int(time.time())
    one = lambda sql, a=(): _query_one(sql, a) or {}
    return {
        "users": one("SELECT COUNT(*) AS v FROM users").get("v", 0),
        "active": one("SELECT COUNT(*) AS v FROM users WHERE sub_until>?", (now,)).get("v", 0),
        "banned": one("SELECT COUNT(*) AS v FROM users WHERE banned=1").get("v", 0),
        "payments": one("SELECT COUNT(*) AS v FROM payments WHERE days>0").get("v", 0),
        "stars": one(
            "SELECT COALESCE(SUM(amount),0) AS v FROM payments WHERE currency='XTR'"
        ).get("v", 0),
        "today": one("SELECT COALESCE(SUM(count),0) AS v FROM usage WHERE day=?", (day,)).get("v", 0),
    }


def db_add_event(level: str, text: str) -> None:
    if _conn is None:
        return
    with _tx() as c:
        c.execute(
            "INSERT INTO events(ts, level, text) VALUES(?,?,?)",
            (int(time.time()), level, text[:1000]),
        )


def db_recent_events(limit: int = 20) -> list[dict]:
    return _query("SELECT ts, level, text FROM events ORDER BY id DESC LIMIT ?", (limit,))


def db_cleanup() -> None:
    """Периодическая чистка, чтобы база не росла бесконтрольно."""
    cutoff_day = datetime.fromtimestamp(time.time() - 35 * 86400, TZ).strftime("%Y-%m-%d")
    with _tx() as c:
        c.execute("DELETE FROM usage WHERE day<?", (cutoff_day,))
        c.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")
        c.execute("DELETE FROM messages WHERE ts<?", (int(time.time()) - 90 * 86400,))


class DBLogHandler(logging.Handler):
    """Пишет WARNING и выше в таблицу events (для просмотра из админ-панели)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING or _conn is None:
            return
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" | {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            db_add_event(record.levelname, text)
        except Exception:
            pass


async def audit(text: str) -> None:
    """Важное событие: в лог и в таблицу events."""
    logger.info(text)
    with contextlib.suppress(Exception):
        await adb(db_add_event, "INFO", text)


# ======================================================================
#                        ЯЗЫКОВАЯ МОДЕЛЬ (LLM)
# ======================================================================
class LLMError(Exception):
    """Ошибка обращения к языковой модели."""


class UserError(Exception):
    """Ошибка, текст которой безопасно показать пользователю."""


class LLMBackend:
    """
    Интерфейс языковой модели. Чтобы подключить другую модель/провайдера, создайте
    наследника, реализуйте complete() и присвойте его переменной `llm` в main().

    messages — список {"role": "system|user|assistant", "content": str | list}.
    Если vision=True, в последнем сообщении есть изображение (формат OpenAI image_url).
    """

    async def complete(self, messages: list[dict], *, vision: bool = False) -> str:
        raise NotImplementedError


class OpenAICompatibleBackend(LLMBackend):
    """Любой сервер с эндпоинтом POST {LLM_BASE_URL}/chat/completions."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def complete(self, messages: list[dict], *, vision: bool = False) -> str:
        model = (LLM_VISION_MODEL or LLM_MODEL) if vision else LLM_MODEL
        payload = {
            "model": model,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if LLM_API_KEY:
            headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        url = LLM_BASE_URL.rstrip("/") + "/chat/completions"

        last_error = "неизвестная ошибка"
        for attempt in range(LLM_RETRIES + 1):
            if attempt:
                await asyncio.sleep(2 * attempt)
            try:
                resp = await self.client.post(url, json=payload, headers=headers, timeout=LLM_TIMEOUT)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"Неожиданный формат ответа модели: {exc}") from exc
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
            if not content:
                raise LLMError("Модель вернула пустой ответ")
            return content
        raise LLMError(last_error)


llm: LLMBackend = LLMBackend()  # заменяется в main()


def build_system_prompt(extra: str = "") -> str:
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M (%Z)")
    parts = [SYSTEM_PROMPT, f"Текущая дата и время: {now}."]
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


# ======================================================================
#                              ПОИСК В ИНТЕРНЕТЕ
# ======================================================================
class SearchError(Exception):
    pass


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


async def web_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Возвращает список {title, url, snippet}. Провайдер задаётся SEARCH_PROVIDER."""
    if SEARCH_PROVIDER == "none":
        raise SearchError("Поиск отключён администратором.")
    try:
        if SEARCH_PROVIDER == "searxng":
            if not SEARCH_URL:
                raise SearchError("Не задан SEARCH_URL для SearXNG.")
            resp = await client.get(
                SEARCH_URL.rstrip("/") + "/search",
                params={"q": query, "format": "json", "language": "auto"},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            items = resp.json().get("results", [])[:SEARCH_RESULTS]
            return [
                {"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("content", "")}
                for i in items
            ]
        if SEARCH_PROVIDER == "ddg":
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            return _parse_ddg(resp.text)
    except SearchError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError(f"Сервис поиска недоступен ({type(exc).__name__}).") from exc
    raise SearchError(f"Неизвестный SEARCH_PROVIDER: {SEARCH_PROVIDER}")


def _parse_ddg(page: str) -> list[dict]:
    results: list[dict] = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', page)[1:]
    for block in blocks:
        link = re.search(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = html.unescape(link.group(1))
        if "uddg=" in url:
            qs = parse_qs(urlparse(url if url.startswith("http") else "https:" + url).query)
            url = unquote(qs.get("uddg", [url])[0])
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        results.append(
            {"title": _strip_tags(link.group(2)), "url": url, "snippet": _strip_tags(snip.group(1)) if snip else ""}
        )
        if len(results) >= SEARCH_RESULTS:
            break
    if not results:
        raise SearchError("Поиск не вернул результатов (возможно, сервис ограничил запросы).")
    return results


def format_search_results(query: str, results: list[dict]) -> str:
    lines = [f"Вопрос пользователя: {query}", "", "Результаты поиска:"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}")
    return "\n\n".join(lines)


# ======================================================================
#                            ОБРАБОТКА ДОКУМЕНТОВ
# ======================================================================
def extract_document_text(data: bytes, filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UserError("PDF защищён паролем — прочитать его невозможно.")
        chunks: list[str] = []
        total = 0
        for page in reader.pages[:200]:
            text = page.extract_text() or ""
            chunks.append(text)
            total += len(text)
            if total >= DOC_MAX_CHARS:
                break
        result = "\n".join(chunks)
    elif ext == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        result = "\n".join(parts)
    else:
        for enc in ("utf-8-sig", "cp1251"):
            try:
                result = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            result = data.decode("utf-8", errors="replace")
    result = result.strip()
    if not result:
        raise UserError(
            "В документе не найден текст (возможно, это скан). Пришлите фото страницы через «📷 Анализ фото»."
        )
    return result


# ======================================================================
#                              ВСПОМОГАТЕЛЬНОЕ
# ======================================================================
def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def split_text(text: str, limit: int = 4000) -> list[str]:
    parts: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts or ["…"]


async def send_long(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


@contextlib.asynccontextmanager
async def typing(bot: Bot, chat_id: int, action: str = ChatAction.TYPING):
    async def _loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, action)
            await asyncio.sleep(4)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_PHOTO)],
            [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_SUB)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напишите сообщение…",
    )


def tariffs_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for days, price in TARIFFS.items():
        kb.button(text=f"{days} дней — {price} ⭐", callback_data=f"buy:{days}")
    kb.adjust(1)
    return kb.as_markup()


def tariffs_text() -> str:
    lines = [f"• {d} дней — {p} ⭐" for d, p in TARIFFS.items()]
    return (
        "⭐ Подписка снимает дневной лимит запросов.\n\n"
        + "\n".join(lines)
        + f"\n\nБез подписки доступно {FREE_DAILY_LIMIT} запросов в сутки.\n"
        "Оплата — официальными Telegram Stars. Повторная оплата продлевает действующую подписку."
    )


def welcome_text(name: str) -> str:
    return (
        f"Привет, {name}! 👋\n\n"
        "Я универсальный ИИ-помощник. Умею:\n"
        "💬 общаться и отвечать на вопросы на разных языках\n"
        "📚 помогать с учёбой и объяснять сложное простыми словами\n"
        "✍️ писать, редактировать, сокращать и переводить тексты\n"
        "🧮 решать задачи и объяснять решения\n"
        "📊 помогать с бизнесом, финансами и анализом информации\n"
        "📷 разбирать фотографии и распознавать текст на них\n"
        "🌐 искать актуальную информацию в интернете\n"
        "🗂 анализировать присланные документы (PDF, DOCX, TXT и др.)\n\n"
        f"Бесплатно — {FREE_DAILY_LIMIT} запросов в сутки, дальше — подписка за Telegram Stars.\n"
        "Я не всегда прав: если не уверен, скажу об этом. Выберите действие внизу или просто напишите вопрос."
    )


def help_text() -> str:
    return (
        "❓ Помощь\n\n"
        "💬 Чат с ИИ — обычный диалог; я помню контекст последних сообщений.\n"
        "📷 Анализ фото — пришлите фото (в подписи можно задать вопрос).\n"
        "🌐 Поиск информации — напишите запрос, я найду данные в интернете и отвечу со ссылками.\n"
        "🗂 Документы — пришлите файл PDF/DOCX/TXT/CSV и т. д. (подпись — ваше задание).\n"
        "⭐ Подписка — снимает дневной лимит.\n"
        "👤 Мой профиль — остаток запросов и срок подписки.\n\n"
        "Команды: /start, /help, /profile, /subscribe, /reset (очистить контекст), /cancel."
    )


# ======================================================================
#                              ЛОГИКА ЗАПРОСА К ИИ
# ======================================================================
_user_locks: dict[int, asyncio.Lock] = {}
Prepared = dict  # {"content": str, "history": str, "system_extra": str, "image_b64": Optional[str]}


async def process_request(
    message: Message, bot: Bot, prepare: Callable[[], Awaitable[Prepared]]
) -> None:
    """
    Единая точка выполнения платных запросов:
    блокировка от параллельных запросов -> проверка/списание лимита -> подготовка данных ->
    запрос к модели -> сохранение истории -> отправка ответа. При сбое запрос возвращается.
    """
    uid = message.from_user.id
    lock = _user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await message.answer("⏳ Я ещё обрабатываю ваш предыдущий запрос. Подождите немного.")
        return

    async with lock:
        day = today()
        status, used = await adb(db_consume_quota, uid, FREE_DAILY_LIMIT, day)
        if status == "banned":
            await message.answer("🚫 Ваш доступ к боту ограничен.")
            return
        if status == "nouser":
            await message.answer("Нажмите /start, чтобы начать.")
            return
        if status == "limit":
            await message.answer(
                f"⛔ Бесплатный лимит на сегодня исчерпан ({used}/{FREE_DAILY_LIMIT}). "
                "Он обновится завтра, а с подпиской запросов можно делать без ограничений.\n\n"
                + tariffs_text(),
                reply_markup=tariffs_keyboard(),
            )
            return

        try:
            async with typing(bot, message.chat.id):
                prep = await prepare()
                history = await adb(db_get_context, uid, CONTEXT_MESSAGES)
                messages: list[dict] = [
                    {"role": "system", "content": build_system_prompt(prep.get("system_extra", ""))}
                ]
                messages.extend(history)
                image_b64 = prep.get("image_b64")
                if image_b64:
                    content: Any = [
                        {"type": "text", "text": prep["content"]},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + image_b64, "detail": IMAGE_DETAIL},
                        },
                    ]
                else:
                    content = prep["content"]
                messages.append({"role": "user", "content": content})
                reply = await llm.complete(messages, vision=bool(image_b64))
        except UserError as exc:
            await adb(db_refund_quota, uid, status, day)
            await message.answer(f"⚠️ {exc}")
            return
        except SearchError as exc:
            await adb(db_refund_quota, uid, status, day)
            await message.answer(f"⚠️ {exc} Запрос не списан, попробуйте позже.")
            return
        except LLMError as exc:
            await adb(db_refund_quota, uid, status, day)
            logger.error("Ошибка языковой модели (user=%s): %s", uid, exc)
            await message.answer("⚠️ Языковая модель сейчас недоступна. Запрос не списан, попробуйте чуть позже.")
            return
        except Exception:
            await adb(db_refund_quota, uid, status, day)
            logger.exception("Сбой при обработке запроса (user=%s)", uid)
            await message.answer("⚠️ Произошла внутренняя ошибка. Запрос не списан, попробуйте ещё раз.")
            return

        await adb(db_add_exchange, uid, prep["history"], reply)
        if status == "free":
            left = max(FREE_DAILY_LIMIT - used, 0)
            reply += f"\n\n— Осталось бесплатных запросов сегодня: {left}"
        await send_long(message, reply)


# ======================================================================
#                              TELEGRAM: ОБРАБОТЧИКИ
# ======================================================================
router = Router()
http_client: Optional[httpx.AsyncClient] = None


class AccessMiddleware(BaseMiddleware):
    """Регистрирует пользователя, ограничивает работу личными чатами и блокирует забаненных."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        row = await adb(db_upsert_user, user.id, user.username or "", user.first_name or "")
        data["db_user"] = row
        if row["banned"] and user.id not in ADMIN_IDS:
            if isinstance(event, Message):
                if event.successful_payment:  # оплаченное зачисляем в любом случае
                    return await handler(event, data)
                await event.answer("🚫 Ваш доступ к боту ограничен.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Доступ ограничен.", show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(ok=False, error_message="Доступ к боту ограничен.")
            return None
        return await handler(event, data)


# --- команды и кнопки ---------------------------------------------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await adb(db_set_mode, message.from_user.id, "chat")
    await message.answer(welcome_text(message.from_user.first_name or "друг"), reply_markup=main_keyboard())


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(help_text(), reply_markup=main_keyboard())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard())


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    await adb(db_clear_context, message.from_user.id)
    await message.answer("🧹 Контекст диалога очищен. Начинаем с чистого листа.")


@router.message(F.text == BTN_CHAT)
async def btn_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await adb(db_set_mode, message.from_user.id, "chat")
    await message.answer("💬 Режим чата. Задайте любой вопрос или поставьте задачу.")


@router.message(F.text == BTN_PHOTO)
async def btn_photo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await adb(db_set_mode, message.from_user.id, "photo")
    if not LLM_VISION_MODEL:
        await message.answer(
            "📷 Анализ фото сейчас не настроен администратором (не задана модель с поддержкой изображений)."
        )
        return
    await message.answer(
        "📷 Пришлите фотографию. В подписи можно задать вопрос (например, «Что написано на табличке?»). "
        "Без подписи я опишу изображение и распознаю текст."
    )


@router.message(F.text == BTN_SEARCH)
async def btn_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    if SEARCH_PROVIDER == "none":
        await message.answer("🌐 Поиск в интернете отключён администратором.")
        return
    await adb(db_set_mode, message.from_user.id, "search")
    await message.answer(
        "🌐 Режим поиска. Напишите, что нужно найти, — я поищу в интернете и отвечу со ссылками.\n"
        "Чтобы вернуться к обычному диалогу, нажмите «💬 Чат с ИИ»."
    )


@router.message(Command("subscribe"))
@router.message(F.text == BTN_SUB)
async def cmd_subscribe(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await adb(db_get_user, message.from_user.id)
    extra = ""
    if user and user["sub_until"] > time.time():
        extra = f"\n\n✅ Ваша подписка активна до {fmt_ts(user['sub_until'])}."
    await message.answer(tariffs_text() + extra, reply_markup=tariffs_keyboard())


@router.message(Command("profile"))
@router.message(F.text == BTN_PROFILE)
async def cmd_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    user = await adb(db_get_user, uid)
    if user is None:
        await message.answer("Нажмите /start, чтобы начать.")
        return
    used = await adb(db_used_today, uid, today())
    if user["sub_until"] > time.time():
        sub = f"✅ активна до {fmt_ts(user['sub_until'])}"
        left = "без ограничений"
    else:
        sub = "нет"
        left = f"{max(FREE_DAILY_LIMIT - used, 0)} из {FREE_DAILY_LIMIT}"
    await message.answer(
        f"👤 Профиль\n\nID: {uid}\nПодписка: {sub}\nЗапросов осталось сегодня: {left}\n"
        f"Всего запросов: {user['total_requests']}\nВ боте с {fmt_ts(user['created_at'])}"
    )


# --- оплата Telegram Stars --------------------------------------------------------
def parse_payload(payload: str, amount: int, currency: str, uid: int) -> Optional[int]:
    """Проверяет payload счёта; возвращает число дней или None, если платёж не соответствует тарифу."""
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "sub" or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        return None
    days = int(parts[1])
    if int(parts[2]) != uid or TARIFFS.get(days) != amount or currency != "XTR":
        return None
    return days


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot) -> None:
    try:
        days = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return
    price = TARIFFS.get(days)
    if price is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    uid = callback.from_user.id
    await bot.send_invoice(
        chat_id=uid,
        title=f"Подписка на {days} дн.",
        description=f"Безлимитные запросы к ИИ на {days} дн. Повторная оплата продлевает подписку.",
        payload=f"sub:{days}:{uid}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{days} дней", amount=price)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    days = parse_payload(query.invoice_payload, query.total_amount, query.currency, query.from_user.id)
    if days is None:
        logger.warning("Отклонён pre_checkout: user=%s payload=%s amount=%s %s",
                       query.from_user.id, query.invoice_payload, query.total_amount, query.currency)
        await query.answer(ok=False, error_message="Счёт устарел или некорректен. Откройте «⭐ Подписка» заново.")
        return
    await query.answer(ok=True)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        with contextlib.suppress(Exception):
            await bot.send_message(admin_id, text)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    uid = message.from_user.id
    days = parse_payload(sp.invoice_payload, sp.total_amount, sp.currency, uid)
    new_until = await adb(
        db_apply_payment, uid, sp.telegram_payment_charge_id, sp.provider_payment_charge_id or "",
        sp.invoice_payload, sp.total_amount, sp.currency, days or 0,
    )
    if new_until is None:
        logger.warning("Повторный платёж отклонён: user=%s charge=%s", uid, sp.telegram_payment_charge_id)
        await message.answer("ℹ️ Этот платёж уже был зачислен ранее — повторно подписка не начисляется.")
        return
    if days is None:
        logger.error("Платёж не распознан: user=%s charge=%s payload=%s amount=%s %s",
                     uid, sp.telegram_payment_charge_id, sp.invoice_payload, sp.total_amount, sp.currency)
        await notify_admins(
            bot, f"⚠️ Нераспознанный платёж: user={uid}, charge={sp.telegram_payment_charge_id}, "
                 f"{sp.total_amount} {sp.currency}, payload={sp.invoice_payload}"
        )
        await message.answer(
            "⚠️ Платёж получен, но не удалось определить тариф. Обратитесь к администратору, "
            f"указав ID платежа: {sp.telegram_payment_charge_id}"
        )
        return
    await audit(f"Платёж: user={uid}, {sp.total_amount} XTR, {days} дн., charge={sp.telegram_payment_charge_id}")
    await message.answer(
        f"✅ Оплата прошла успешно! Подписка на {days} дн. активна до {fmt_ts(new_until)}.\nСпасибо! 🙌",
        reply_markup=main_keyboard(),
    )
    await notify_admins(bot, f"💰 Новый платёж: user={uid}, {sp.total_amount} ⭐, {days} дн.")


# ======================================================================
#                              АДМИНИСТРАТИВНАЯ ПАНЕЛЬ
# ======================================================================
class AdminSt(StatesGroup):
    password = State()
    find_user = State()
    broadcast = State()


_admin_sessions: dict[int, float] = {}
_admin_fails: dict[int, tuple[int, float]] = {}  # uid -> (число ошибок, заблокирован до)
_bg_tasks: set[asyncio.Task] = set()


def admin_ok(uid: int) -> bool:
    """Админ: ID в списке И действующая парольная сессия."""
    if uid not in ADMIN_IDS:
        return False
    if _admin_sessions.get(uid, 0) > time.time():
        _admin_sessions[uid] = time.time() + ADMIN_SESSION_MINUTES * 60  # скользящая сессия
        return True
    return False


def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.button(text="👥 Пользователи", callback_data="adm:users:0")
    kb.button(text="🔎 Найти пользователя", callback_data="adm:find")
    kb.button(text="📣 Рассылка", callback_data="adm:bc")
    kb.button(text="🧾 События и ошибки", callback_data="adm:events")
    kb.button(text="🚪 Выйти", callback_data="adm:logout")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    return kb.as_markup()


async def edit_or_send(callback: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await callback.message.answer(text, reply_markup=kb)


async def render_user_card(uid: int) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    user = await adb(db_get_user, uid)
    if user is None:
        return "Пользователь не найден.", back_kb()
    used = await adb(db_used_today, uid, today())
    pay_n, pay_sum = await adb(db_user_payments_sum, uid)
    active = user["sub_until"] > time.time()
    text = (
        f"👤 Пользователь {uid}\n"
        f"Username: @{user['username'] or '—'}\n"
        f"Имя: {user['first_name'] or '—'}\n"
        f"Статус: {'🚫 заблокирован' if user['banned'] else '✅ активен'}\n"
        f"Подписка: {('до ' + fmt_ts(user['sub_until'])) if active else 'нет'}\n"
        f"Запросов сегодня (бесплатных): {used}/{FREE_DAILY_LIMIT}\n"
        f"Всего запросов: {user['total_requests']}\n"
        f"Платежей: {pay_n} на {pay_sum} ⭐\n"
        f"Регистрация: {fmt_ts(user['created_at'])}\n"
        f"Последняя активность: {fmt_ts(user['last_seen'])}"
    )
    kb = InlineKeyboardBuilder()
    if user["banned"]:
        kb.button(text="✅ Разблокировать", callback_data=f"adm:u:unban:{uid}")
    else:
        kb.button(text="🚫 Заблокировать", callback_data=f"adm:u:ban:{uid}")
    for days in TARIFFS:
        kb.button(text=f"➕ {days} дн.", callback_data=f"adm:u:grant:{uid}:{days}")
    kb.button(text="❌ Снять подписку", callback_data=f"adm:u:revoke:{uid}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1, len(TARIFFS), 1, 1)
    return text, kb.as_markup()


async def run_broadcast(bot: Bot, admin_chat: int, text: str) -> None:
    ids = await adb(db_broadcast_ids)
    sent = failed = 0
    for uid in ids:
        for attempt in range(2):
            try:
                await bot.send_message(uid, text)
                sent += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except (TelegramForbidden, TelegramBadRequest):
                failed += 1
                break
            except Exception:
                failed += 1
                break
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    await audit(f"Рассылка завершена: доставлено {sent}, не доставлено {failed}")
    with contextlib.suppress(Exception):
        await bot.send_message(admin_chat, f"📣 Рассылка завершена.\nДоставлено: {sent}\nНе доставлено: {failed}")


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    await state.clear()
    if uid not in ADMIN_IDS:
        logger.warning("Попытка входа в /admin от неадминистратора: user=%s", uid)
        await message.answer("Неизвестная команда. Нажмите /help.")
        return
    if admin_ok(uid):
        await message.answer("🛠 Админ-панель", reply_markup=admin_menu_kb())
        return
    fails, locked_until = _admin_fails.get(uid, (0, 0.0))
    if locked_until > time.time():
        await message.answer(f"⛔ Слишком много попыток. Повторите через {int((locked_until - time.time()) // 60) + 1} мин.")
        return
    await state.set_state(AdminSt.password)
    await message.answer("🔐 Введите пароль администратора (сообщение с паролем будет удалено). /cancel — отмена.")


@router.message(AdminSt.password, F.text)
async def admin_password(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await state.clear()
        return
    fails, locked_until = _admin_fails.get(uid, (0, 0.0))
    if locked_until > time.time():
        await state.clear()
        await message.answer("⛔ Попытки временно заблокированы.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    entered = message.text.strip().encode("utf-8")
    if ADMIN_PASSWORD and hmac.compare_digest(entered, ADMIN_PASSWORD.encode("utf-8")):
        _admin_fails.pop(uid, None)
        _admin_sessions[uid] = time.time() + ADMIN_SESSION_MINUTES * 60
        await state.clear()
        await audit(f"Вход в админ-панель: admin={uid}")
        await message.answer("✅ Доступ разрешён.\n🛠 Админ-панель", reply_markup=admin_menu_kb())
        return
    fails += 1
    logger.warning("Неверный пароль админ-панели: admin=%s, попытка %s", uid, fails)
    if fails >= ADMIN_MAX_ATTEMPTS:
        _admin_fails[uid] = (0, time.time() + ADMIN_LOCK_MINUTES * 60)
        await state.clear()
        await message.answer(f"⛔ Слишком много неверных попыток. Блокировка на {ADMIN_LOCK_MINUTES} мин.")
        return
    _admin_fails[uid] = (fails, 0.0)
    await message.answer(f"❌ Неверный пароль. Осталось попыток: {ADMIN_MAX_ATTEMPTS - fails}.")


@router.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    uid = callback.from_user.id
    if not admin_ok(uid):
        await callback.answer("Сессия истекла или нет доступа. Используйте /admin.", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        await state.clear()
        await edit_or_send(callback, "🛠 Админ-панель", admin_menu_kb())

    elif action == "stats":
        s = await adb(db_stats, today())
        await edit_or_send(
            callback,
            "📊 Статистика\n\n"
            f"Пользователей: {s['users']}\n"
            f"Активных подписчиков: {s['active']}\n"
            f"Заблокированных: {s['banned']}\n"
            f"Платежей: {s['payments']}\n"
            f"Получено Stars: {s['stars']} ⭐\n"
            f"Бесплатных запросов сегодня: {s['today']}",
            back_kb(),
        )

    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        per_page = 15
        total = await adb(db_count_users)
        pages = max((total + per_page - 1) // per_page, 1)
        page = min(page, pages - 1)
        rows = await adb(db_list_users, page * per_page, per_page)
        now = time.time()
        lines = [
            f"{r['user_id']} | @{r['username'] or '—'} | {r['first_name'] or '—'}"
            f"{' 🚫' if r['banned'] else ''}{' ⭐' if r['sub_until'] > now else ''}"
            for r in rows
        ]
        kb = InlineKeyboardBuilder()
        if page > 0:
            kb.button(text="◀️", callback_data=f"adm:users:{page - 1}")
        if page < pages - 1:
            kb.button(text="▶️", callback_data=f"adm:users:{page + 1}")
        kb.button(text="⬅️ В меню", callback_data="adm:menu")
        kb.adjust(2, 1)
        await edit_or_send(
            callback,
            f"👥 Пользователи (стр. {page + 1}/{pages}, всего {total})\n\n" + ("\n".join(lines) or "Пока пусто."),
            kb.as_markup(),
        )

    elif action == "find":
        await state.set_state(AdminSt.find_user)
        await edit_or_send(callback, "🔎 Отправьте Telegram ID или @username пользователя. /cancel — отмена.")

    elif action == "u" and len(parts) >= 4:
        sub_action, target = parts[2], int(parts[3]) if parts[3].lstrip("-").isdigit() else 0
        if target in ADMIN_IDS and sub_action in ("ban", "revoke"):
            await callback.answer("Эту операцию нельзя применять к администратору.", show_alert=True)
            return
        if sub_action == "ban":
            ok = await adb(db_set_banned, target, True)
            await audit(f"Админ {uid} заблокировал {target}" if ok else f"Блокировка: {target} не найден")
        elif sub_action == "unban":
            ok = await adb(db_set_banned, target, False)
            await audit(f"Админ {uid} разблокировал {target}" if ok else f"Разблокировка: {target} не найден")
        elif sub_action == "grant" and len(parts) >= 5 and parts[4].isdigit() and int(parts[4]) in TARIFFS:
            days = int(parts[4])
            new_until = await adb(db_grant_sub, target, days)
            if new_until:
                await audit(f"Админ {uid} выдал {target} подписку +{days} дн. (до {fmt_ts(new_until)})")
                with contextlib.suppress(Exception):
                    await bot.send_message(target, f"🎁 Вам выдана подписка на {days} дн. Действует до {fmt_ts(new_until)}.")
        elif sub_action == "revoke":
            if await adb(db_revoke_sub, target):
                await audit(f"Админ {uid} снял подписку у {target}")
        text, kb = await render_user_card(target)
        await edit_or_send(callback, text, kb)

    elif action == "bc":
        await state.set_state(AdminSt.broadcast)
        await edit_or_send(callback, "📣 Отправьте текст рассылки (до 4000 символов). /cancel — отмена.")

    elif action == "bcgo":
        data = await state.get_data()
        text = data.get("broadcast_text")
        await state.clear()
        if not text:
            await edit_or_send(callback, "Текст рассылки не найден.", back_kb())
        else:
            await audit(f"Админ {uid} запустил рассылку ({len(text)} символов)")
            task = asyncio.create_task(run_broadcast(bot, callback.message.chat.id, text))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
            await edit_or_send(callback, "📣 Рассылка запущена. Итог пришлю отдельным сообщением.", back_kb())

    elif action == "events":
        events = await adb(db_recent_events, 20)
        lines = [f"{fmt_ts(e['ts'])} [{e['level']}] {e['text'][:160]}" for e in events]
        await edit_or_send(callback, "🧾 Последние события\n\n" + ("\n\n".join(lines) or "Пока пусто."), back_kb())

    elif action == "logout":
        _admin_sessions.pop(uid, None)
        await state.clear()
        await edit_or_send(callback, "Вы вышли из админ-панели.")

    await callback.answer()


@router.message(AdminSt.find_user, F.text)
async def admin_find_user(message: Message, state: FSMContext) -> None:
    if not admin_ok(message.from_user.id):
        await state.clear()
        await message.answer("Сессия истекла. Используйте /admin.")
        return
    query = message.text.strip()
    if query.startswith("/"):
        await message.answer("Отправьте Telegram ID или @username, либо /cancel.")
        return
    user = await adb(db_find_user, query)
    if user is None:
        await message.answer("Пользователь не найден. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    text, kb = await render_user_card(user["user_id"])
    await message.answer(text, reply_markup=kb)


@router.message(AdminSt.broadcast, F.text)
async def admin_broadcast_text(message: Message, state: FSMContext) -> None:
    if not admin_ok(message.from_user.id):
        await state.clear()
        await message.answer("Сессия истекла. Используйте /admin.")
        return
    text = message.text.strip()
    if text.startswith("/") or not text:
        await message.answer("Отправьте текст рассылки, либо /cancel.")
        return
    if len(text) > 4000:
        await message.answer("Слишком длинный текст (максимум 4000 символов).")
        return
    await state.update_data(broadcast_text=text)
    total = len(await adb(db_broadcast_ids))
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Отправить ({total})", callback_data="adm:bcgo")
    kb.button(text="⬅️ Отмена", callback_data="adm:menu")
    kb.adjust(1)
    await message.answer(f"Предпросмотр рассылки:\n\n{text}", reply_markup=kb.as_markup())


# ======================================================================
#                      ТЕКСТ, ФОТО, ДОКУМЕНТЫ, ОШИБКИ
# ======================================================================
@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    if not LLM_VISION_MODEL:
        await message.answer("📷 Анализ фото не настроен администратором (не задана модель с поддержкой изображений).")
        return
    caption = (message.caption or "").strip()[:MAX_INPUT_CHARS]
    limit = int(MAX_PHOTO_MB * 1024 * 1024)
    candidates = [p for p in message.photo if (p.file_size or 0) <= limit]
    if not candidates:
        await message.answer(f"Фото слишком большое (максимум {MAX_PHOTO_MB:g} МБ).")
        return
    photo = max(candidates, key=lambda p: p.width * p.height)

    async def prepare() -> Prepared:
        buffer = await bot.download(photo)
        raw = buffer.read() if buffer else b""
        if not raw:
            raise UserError("Не удалось скачать фото. Попробуйте отправить его ещё раз.")
        if len(raw) > limit:
            raise UserError(f"Фото слишком большое (максимум {MAX_PHOTO_MB:g} МБ).")
        prompt = caption or DEFAULT_PHOTO_PROMPT
        return {
            "content": prompt,
            "history": f"[Пользователь прислал фото] {prompt}",
            "image_b64": base64.b64encode(raw).decode("ascii"),
        }

    await process_request(message, bot, prepare)


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    doc = message.document
    name = doc.file_name or "document"
    ext = os.path.splitext(name.lower())[1]
    if ext not in DOC_EXTENSIONS:
        await message.answer("Поддерживаются форматы: " + ", ".join(sorted(DOC_EXTENSIONS)) + ".\nДля изображений используйте «📷 Анализ фото».")
        return
    limit = int(MAX_DOC_MB * 1024 * 1024)
    if (doc.file_size or 0) > limit:
        await message.answer(f"Файл слишком большой (максимум {MAX_DOC_MB:g} МБ).")
        return
    caption = (message.caption or "").strip()[:MAX_INPUT_CHARS]

    async def prepare() -> Prepared:
        buffer = await bot.download(doc)
        raw = buffer.read() if buffer else b""
        if not raw:
            raise UserError("Не удалось скачать файл. Попробуйте ещё раз.")
        try:
            text = await asyncio.to_thread(extract_document_text, raw, name)
        except UserError:
            raise
        except Exception as exc:
            logger.warning("Не удалось прочитать документ %s: %s", name, exc)
            raise UserError("Не удалось прочитать файл — возможно, он повреждён или в нестандартном формате.") from exc
        truncated = len(text) > DOC_MAX_CHARS
        text = text[:DOC_MAX_CHARS]
        task = caption or DEFAULT_DOC_PROMPT
        note = "\n\n(Документ обрезан: показана только его первая часть.)" if truncated else ""
        return {
            "content": f"Документ «{name}»:\n\n{text}{note}\n\nЗадание: {task}",
            "history": f"[Документ «{name}»]\n{text[:2500]}\n\nЗадание: {task}",
        }

    await process_request(message, bot, prepare)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Нажмите /help, чтобы увидеть список доступных.")


@router.message(F.text)
async def on_text(message: Message, bot: Bot, db_user: dict) -> None:
    text = message.text.strip()
    if not text:
        return
    if len(text) > MAX_INPUT_CHARS:
        await message.answer(f"Сообщение слишком длинное ({len(text)} симв.). Максимум — {MAX_INPUT_CHARS}.")
        return

    if db_user.get("mode") == "search" and SEARCH_PROVIDER != "none":
        async def prepare_search() -> Prepared:
            assert http_client is not None
            results = await web_search(http_client, text)
            return {
                "content": format_search_results(text, results),
                "history": f"[Поиск в интернете] {text}",
                "system_extra": SEARCH_PROMPT,
            }

        await process_request(message, bot, prepare_search)
        return

    async def prepare_chat() -> Prepared:
        return {"content": text, "history": text}

    await process_request(message, bot, prepare_chat)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Я понимаю текст, фотографии и документы. Отправьте одно из них или нажмите /help.")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.error("Необработанная ошибка: %s", event.exception, exc_info=event.exception)
    update = event.update
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target is not None:
        with contextlib.suppress(Exception):
            await target.answer("⚠️ Произошла ошибка. Попробуйте ещё раз чуть позже.")
    return True


# ======================================================================
#                          ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# ======================================================================
async def subscription_watcher(bot: Bot) -> None:
    """Автоматически завершает подписки (по времени) и уведомляет пользователей; чистит БД."""
    last_cleanup = 0.0
    while True:
        try:
            for uid in await adb(db_expired_unnotified):
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        uid,
                        "⌛ Срок вашей подписки истёк. Продлить её можно в разделе «⭐ Подписка» — "
                        "срок прибавится к текущему.",
                    )
            if time.time() - last_cleanup > 6 * 3600:
                await adb(db_cleanup)
                last_cleanup = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фонового обслуживания")
        await asyncio.sleep(300)


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"Не удалось открыть лог-файл {LOG_FILE}: {exc}", file=sys.stderr)
    root.addHandler(DBLogHandler())
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    global http_client, llm
    setup_logging()
    if not BOT_TOKEN:
        logger.error("Не задан BOT_TOKEN. Укажите токен в переменной окружения или файле .env.")
        sys.exit(1)
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS пуст — админ-панель недоступна.")
    if ADMIN_PASSWORD == "2525":
        logger.warning("Используется пароль админ-панели по умолчанию (2525). Смените ADMIN_PASSWORD!")

    db_init()
    http_client = httpx.AsyncClient(follow_redirects=True)
    llm = OpenAICompatibleBackend(http_client)  # <- здесь можно подставить свою реализацию LLMBackend

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    middleware = AccessMiddleware()
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.pre_checkout_query.outer_middleware(middleware)
    dp.include_router(router)

    watcher: Optional[asyncio.Task] = None
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s (id=%s), модель: %s, зона: %s", me.username, me.id, LLM_MODEL, TIMEZONE_NAME)
        await bot.delete_webhook(drop_pending_updates=False)
        watcher = asyncio.create_task(subscription_watcher(bot))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if watcher:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await http_client.aclose()
        await bot.session.close()
        db_close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
