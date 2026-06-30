"""Мультитенантный мост MAX <-> Telegram.

Один общий Telegram-бот; любой пользователь может добавить свои MAX-аккаунты
прямо через бота (вход по SMS/2FA вводится в чат). У каждого MAX-аккаунта —
своя группа-форум: каждый чат MAX становится отдельной темой.

Команды бота (в личке):
  /add       — добавить MAX-аккаунт (спросит телефон, код из SMS, 2FA)
  /accounts  — список твоих аккаунтов
  /remove N  — удалить аккаунт N
В группе аккаунта:
  /bind [N]  — привязать эту группу к аккаунту
  /mute /unmute /muted — управление уведомлениями темы
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    ReactionTypeEmoji,
)
from aiogram.types import Message as TgMessage
from aiogram.types import MessageReactionUpdated

from pymax import (
    ApiError,
    Client,
    ExtraConfig,
    File,
    Message,
    MessageDeleteEvent,
    Photo,
    Video,
)
from pymax.types.domain import (
    AudioAttachment,
    CallAttachment,
    ContactAttachment,
    ControlAttachment,
    FileAttachment,
    PhotoAttachment,
    ShareAttachment,
    StickerAttachment,
    VideoAttachment,
)
from pymax.types.domain.enums import ChatType

from config import Config
from registry import Registry
from storage import Storage

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FMT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Логгер моста делаем НЕЗАВИСИМЫМ от root: PyMax при создании каждого Client
# вызывает свой configure_logging и переопределяет root-настройки, заглушая
# наши логи. Свой хендлер + propagate=False это исключает.
logger = logging.getLogger("bridge")
logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
logger.propagate = False
_bridge_stream = logging.StreamHandler()
_bridge_stream.setFormatter(_LOG_FMT)
logger.addHandler(_bridge_stream)


def _quiet_libs() -> None:
    """Приглушает болтливые библиотеки. PyMax сбрасывает уровни при создании
    Client, поэтому вызывается повторно после старта каждого аккаунта."""
    if _LOG_LEVEL != "DEBUG":
        logging.getLogger("aiogram").setLevel(logging.WARNING)
        logging.getLogger("pymax").setLevel(logging.WARNING)


_quiet_libs()


class _BenignPymaxNoise(logging.Filter):
    """Глушит ожидаемые ошибки MAX (нет доступа к пересланным файлам/видео).

    Мост их уже обрабатывает (показывает пометку), а в логах они только шумят.
    На уровне DEBUG ничего не фильтруем.
    """

    MARKERS = ("error.user.file.access", "video.not.found")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(m in msg for m in self.MARKERS)


if _LOG_LEVEL != "DEBUG":
    logging.getLogger("pymax.app").addFilter(_BenignPymaxNoise())

_log_file_env = os.getenv("LOG_FILE", "").strip()
if _log_file_env.lower() in ("off", "none"):
    _LOG_FILE = ""
elif _log_file_env:
    _LOG_FILE = _log_file_env
else:
    _LOG_FILE = os.path.join(os.getenv("WORK_DIR", "./data"), "bridge.log")
if _LOG_FILE:
    from logging.handlers import RotatingFileHandler

    try:
        os.makedirs(os.path.dirname(_LOG_FILE) or ".", exist_ok=True)
        _max_bytes = int(float(os.getenv("LOG_MAX_MB", "5")) * 1024 * 1024)
        _fh = RotatingFileHandler(
            _LOG_FILE, maxBytes=_max_bytes,
            backupCount=int(os.getenv("LOG_BACKUPS", "3")), encoding="utf-8",
        )
        _fh.setFormatter(_LOG_FMT)
        logging.getLogger().addHandler(_fh)   # root: aiogram/pymax
        logger.addHandler(_fh)                # bridge (т.к. propagate=False)
    except Exception:
        logger.warning("Не удалось настроить файловый лог %s", _LOG_FILE)

TG_CAPTION_LIMIT = 1024
PHONE_RE = re.compile(r"^\+\d{7,15}$")
AUTH_TIMEOUT = 300  # сек на ввод кода/пароля
TG_UPLOAD_LIMIT = 45 * 1024 * 1024  # запас под лимит бота Telegram (~50 МБ)


async def _retry_after_middleware(make_request, bot, method):
    """Session-middleware: при flood-control (429) ждём и переотправляем."""
    for _ in range(5):
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(
                "Telegram flood-control (%s): пауза %s сек",
                type(method).__name__, wait,
            )
            await asyncio.sleep(wait)
    return await make_request(bot, method)


_ENC_MAGIC = b"MTTENC1\n"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_file(path: str, passphrase: str) -> str:
    """Шифрует файл паролем (PBKDF2 + Fernet). Возвращает путь к .enc."""
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(
        open(path, "rb").read()
    )
    enc = path + ".enc"
    with open(enc, "wb") as f:
        f.write(_ENC_MAGIC)
        f.write(salt)
        f.write(token)
    os.remove(path)
    return enc


def _sqlite_snapshot(src: str, dst: str) -> None:
    """Консистентная копия SQLite-файла даже при открытом соединении."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _build_backup(
    work_dir: str, backup_dir: str, keep: int, passphrase: str | None = None
) -> str:
    """Собирает tar.gz из всех *.db (сессии, реестр, маршрутизация). Ротирует.

    Если задан passphrase — архив шифруется (на выходе .tar.gz.enc).
    """
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = os.path.join(backup_dir, f"backup_{ts}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        for src in glob.glob(os.path.join(work_dir, "*.db")):
            dst = os.path.join(tmp, os.path.basename(src))
            try:
                _sqlite_snapshot(src, dst)
            except Exception:
                import shutil
                shutil.copy2(src, dst)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tmp, arcname="data")
    if passphrase:
        try:
            archive = _encrypt_file(archive, passphrase)
        except Exception:
            logger.warning(
                "Не удалось зашифровать бэкап — оставляю без шифрования",
                exc_info=True,
            )
    # Ротация: оставляем последние keep архивов (с .enc или без).
    if keep > 0:
        backups = sorted(
            glob.glob(os.path.join(backup_dir, "backup_*.tar.gz*"))
        )
        for old in backups[:-keep]:
            try:
                os.remove(old)
            except Exception:
                pass
    return archive


@dataclass
class Conv:
    """Состояние диалога с пользователем (онбординг аккаунта)."""

    step: str  # 'phone' | 'login' | 'code' | 'password'
    account_id: int | None = None
    future: asyncio.Future | None = None


class TelegramAuth:
    """Провайдер кода SMS и 2FA-пароля MAX — спрашивает у пользователя в Telegram."""

    def __init__(self, manager: "Manager", tg_id: int) -> None:
        self._m = manager
        self._tg = tg_id

    async def get_code(self, phone: str) -> str:
        return await self._m.await_input(
            self._tg, "code",
            f"📩 Введи код из SMS, отправленной на {phone}:",
        )

    async def get_password(self, hint: str | None = None) -> str:
        prompt = "🔐 Введи пароль приложения (2FA)"
        if hint:
            prompt += f" (подсказка: {hint})"
        return await self._m.await_input(self._tg, "password", prompt + ":")


class Account:
    """Один MAX-аккаунт: свой userbot, своя база, своя группа-форум."""

    def __init__(
        self,
        bot: Bot,
        http: aiohttp.ClientSession,
        *,
        account_id: int,
        owner_tg_id: int,
        name: str,
        group_id: int | None,
        client: Client,
        storage: Storage,
        forward_groups: bool = True,
    ) -> None:
        self.bot = bot
        self.http = http
        self.account_id = account_id
        self.owner_tg_id = owner_tg_id
        self.name = name
        self.group_id = group_id  # привязывается через /bind, может быть None
        self.client = client
        self.storage = storage
        self.forward_groups = forward_groups
        self._chat_cache: dict[int, object] = {}
        self._topic_lock = asyncio.Lock()
        self._warned_no_group = False
        self._diag_empty: set[int] = set()
        self._sent_since_trim = 0
        self._reaction_diag_done = False
        self._seen_opcodes: set[int] = set()
        self._last_chat_reaction: tuple | None = None
        self._diag_attaches: set[str] = set()
        # Темы, созданные только что: Telegram авто-закрепляет первое
        # сообщение — снимем его после отправки.
        self._fresh_topics: set[int] = set()
        self._register_max_handler()

    # ── имена ─────────────────────────────────────────────────────────────

    def _my_id(self) -> int | None:
        return self.client.me.contact.id if self.client.me else None

    def _max_online(self) -> bool:
        app = getattr(self.client, "_app", None)
        return bool(getattr(app, "started", False))

    @staticmethod
    def _name_of(user) -> str | None:
        if user and user.names:
            n = user.names[0]
            label = n.name or " ".join(
                p for p in (n.first_name, n.last_name) if p
            )
            return label or None
        return None

    @classmethod
    def _label_for(cls, user, user_id: int) -> str:
        if user is not None:
            name = cls._name_of(user)
            if name:
                return name
            link = (user.link or "").strip()
            if link:
                return link if ("/" in link or "://" in link) else f"@{link}"
            if user.phone:
                return f"+{user.phone}"
        return f"ID {user_id}"

    async def _user_name(self, user_id: int) -> str:
        user = self.client.get_cached_user(user_id)
        if user is None:
            try:
                user = await self.client.get_user(user_id)
            except Exception:
                logger.debug("get_user(%s) не удался", user_id, exc_info=True)
                user = None
        return self._label_for(user, user_id)

    async def _get_chat(self, chat_id: int):
        for c in self.client.chats or []:
            if c.id == chat_id:
                return c
        if chat_id in self._chat_cache:
            return self._chat_cache[chat_id]
        try:
            chat = await self.client.get_chat(chat_id)
        except Exception:
            logger.debug("get_chat(%s) не удался", chat_id, exc_info=True)
            chat = None
        self._chat_cache[chat_id] = chat
        return chat

    @staticmethod
    def _is_fallback_name(name: str) -> bool:
        return name.startswith("ID ") or name.startswith("чат ")

    async def _chat_title_by_id(self, chat_id: int) -> str:
        chat = await self._get_chat(chat_id)
        if chat is None:
            return f"чат {chat_id}"
        if chat.title:
            return chat.title
        for uid in chat.participants or {}:
            if uid != self._my_id():
                name = await self._user_name(uid)
                if not self._is_fallback_name(name):
                    return name
                break
        if getattr(chat, "has_bots", False):
            return f"🤖 бот {chat_id}"
        return f"чат {chat_id}"

    # ── темы ──────────────────────────────────────────────────────────────

    async def _ensure_thread(self, max_chat_id: int, chat) -> int | None:
        existing = await self.storage.get_topic(max_chat_id)
        if existing is not None:
            return existing
        async with self._topic_lock:
            existing = await self.storage.get_topic(max_chat_id)
            if existing is not None:
                return existing
            name = (await self._chat_title_by_id(max_chat_id))[:128]
            try:
                topic = await self.bot.create_forum_topic(self.group_id, name)
            except Exception:
                logger.exception(
                    "[%s] Не удалось создать тему для чата MAX %s "
                    "(бот админ группы с правом «Управление темами»?)",
                    self.name, max_chat_id,
                )
                return None
            await self.storage.set_topic(
                max_chat_id, topic.message_thread_id, name
            )
            self._fresh_topics.add(max_chat_id)
            logger.info(
                "[%s] Создана тема '%s' (thread=%s) для чата MAX %s",
                self.name, name, topic.message_thread_id, max_chat_id,
            )
            return topic.message_thread_id

    async def _maybe_rename_topic(self, max_chat_id: int, thread: int) -> None:
        current = await self.storage.get_topic_title(max_chat_id)
        if current and not self._is_fallback_name(current):
            return
        desired = (await self._chat_title_by_id(max_chat_id))[:128]
        if desired == current or self._is_fallback_name(desired):
            return
        try:
            await self.bot.edit_forum_topic(self.group_id, thread, name=desired)
            await self.storage.set_topic_title(max_chat_id, desired)
            logger.info(
                "[%s] Тема переименована %r -> %r", self.name, current, desired
            )
        except Exception:
            logger.debug("Не удалось переименовать тему", exc_info=True)

    # ── MAX -> Telegram ───────────────────────────────────────────────────

    def _register_max_handler(self) -> None:
        @self.client.on_message()
        async def on_max_message(message: Message, client: Client) -> None:
            try:
                await self._forward_to_telegram(message)
            except Exception:
                logger.exception("[%s] Ошибка пересылки MAX->TG", self.name)

        # Необязательные обработчики — есть не во всех версиях PyMax.
        # Регистрируем, только если метод доступен (иначе просто пропускаем).
        async def on_max_edit(message: Message, client: Client) -> None:
            try:
                await self._mirror_edit(message)
            except Exception:
                logger.exception("[%s] Ошибка зеркалирования правки", self.name)

        async def on_max_delete(
            event: MessageDeleteEvent, client: Client
        ) -> None:
            try:
                await self._mirror_delete(event)
            except Exception:
                logger.exception("[%s] Ошибка зеркалирования удаления", self.name)

        async def on_max_reaction(event, client: Client) -> None:
            try:
                await self._apply_reaction(
                    getattr(event, "message_id", None),
                    getattr(event, "counters", None),
                    getattr(event, "total_count", 0),
                )
            except Exception:
                logger.debug("[%s] Ошибка зеркалирования реакции",
                             self.name, exc_info=True)

        async def on_max_raw(frame, client: Client) -> None:
            try:
                op = getattr(frame, "opcode", None)
                payload = getattr(frame, "payload", None) or {}
                if op not in self._seen_opcodes:
                    self._seen_opcodes.add(op)
                    logger.debug(
                        "[%s] raw opcode=%s payload=%r",
                        self.name, op, str(payload)[:600],
                    )
                if op in (155, 156):
                    await self._handle_raw_reaction(payload)
                elif op == 135:
                    await self._handle_chat_reaction(payload)
            except Exception:
                logger.debug("[%s] raw-обработка не удалась",
                             self.name, exc_info=True)

        self._register_optional("on_message_edit", on_max_edit)
        self._register_optional("on_message_delete", on_max_delete)
        self._register_optional("on_reaction_update", on_max_reaction)
        # on_raw — диагностика опкодов и фолбэк-обработка реакций (опкод 155).
        self._register_optional("on_raw", on_max_raw)

    def _register_optional(self, hook: str, handler) -> None:
        """Регистрирует обработчик, если такой хук есть в этой версии PyMax."""
        factory = getattr(self.client, hook, None)
        if not callable(factory):
            logger.info("[%s] хук %s недоступен в этой версии PyMax",
                        self.name, hook)
            return
        try:
            factory()(handler)
            logger.info("[%s] хук %s подключён", self.name, hook)
        except Exception:
            logger.info("[%s] не удалось подключить %s",
                        self.name, hook, exc_info=True)

    async def _mirror_edit(self, message: Message) -> None:
        """Применяет правку сообщения MAX к его копии в Telegram."""
        if self.group_id is None or message.chat_id is None:
            return
        rows = await self.storage.get_msg_map(message.id)
        if not rows:
            return
        # Заново собираем заголовок + новый текст (медиа не трогаем).
        chat = await self._get_chat(message.chat_id)
        is_group = chat is not None and chat.type != ChatType.DIALOG
        parts: list[str] = []
        if is_group:
            sender = (
                await self._user_name(message.sender)
                if message.sender else "?"
            )
            parts.append(f"👤 {sender}")
        if message.text:
            parts.append(message.text)
        new_body = "\n".join(parts)

        for tg_chat, tg_msg, role in rows:
            if role == "text":
                try:
                    await self.bot.edit_message_text(
                        new_body or "📭 (пусто)",
                        chat_id=tg_chat, message_id=tg_msg,
                    )
                except Exception:
                    logger.debug("edit_text не удался", exc_info=True)
                return
            if role == "caption":
                try:
                    await self.bot.edit_message_caption(
                        chat_id=tg_chat, message_id=tg_msg,
                        caption=new_body or None,
                    )
                except Exception:
                    logger.debug("edit_caption не удался", exc_info=True)
                return
        # Носителя текста нет (например, альбом без подписи) — правку не
        # применить, тихо пропускаем.

    async def _mirror_delete(self, event: MessageDeleteEvent) -> None:
        """Удаляет в Telegram копии удалённых в MAX сообщений."""
        if self.group_id is None:
            return
        for max_msg_id in event.message_ids or []:
            rows = await self.storage.get_msg_map(max_msg_id)
            if not rows:
                continue
            for tg_chat, tg_msg, _role in rows:
                try:
                    await self.bot.delete_message(tg_chat, tg_msg)
                except Exception:
                    logger.debug("delete_message не удался", exc_info=True)
            await self.storage.forget_msg(max_msg_id)

    @staticmethod
    def _counter_field(c, key):
        return c.get(key) if isinstance(c, dict) else getattr(c, key, None)

    async def _apply_reaction(self, message_id, counters, total_count) -> None:
        """Отражает реакции MAX на копии сообщения в Telegram.

        - Входящее (сообщение бота): реакции нельзя поставить видимо, поэтому
          дописываем их строкой к тексту/подписи.
        - Своё (сообщение пользователя, роль 'user'): бот ставит видимую
          реакцию через set_message_reaction.
        """
        if self.group_id is None or message_id is None:
            return
        try:
            max_msg_id = int(message_id)
        except (TypeError, ValueError):
            return
        target = await self.storage.get_reaction_target(max_msg_id)
        if target is None:
            logger.debug(
                "[%s] реакция msg=%s — нет несущей копии (старое/медиа-сообщение)",
                self.name, max_msg_id,
            )
            return
        tg_chat, tg_msg, role, body = target

        # Доминирующая реакция (с наибольшим счётчиком).
        dominant = ""
        best = -1
        for c in counters or []:
            emoji = (self._counter_field(c, "reaction") or "").strip()
            cnt = self._counter_field(c, "count") or 0
            if emoji and cnt > best:
                best, dominant = cnt, emoji
        reactions = [ReactionTypeEmoji(emoji=dominant)] if dominant else []
        logger.debug(
            "[%s] реакция msg=%s role=%s emoji=%r",
            self.name, max_msg_id, role, dominant,
        )
        try:
            await self.bot.set_message_reaction(tg_chat, tg_msg, reactions)
        except Exception as e:
            if not reactions and "EMPTY" in str(e).upper():
                logger.debug("нечего снимать")
            else:
                logger.info("[%s] реакцию не поставить: %s", self.name, e)

    async def _handle_raw_reaction(self, payload: dict) -> None:
        """Разбирает фрейм реакции (опкод 156): messageId + reactionInfo.

        Приходит тому, кто ПОСТАВИЛ реакцию.
        """
        if not isinstance(payload, dict):
            return
        message_id = payload.get("messageId")
        ri = payload.get("reactionInfo")
        if not isinstance(ri, dict):
            ri = {}
        counters = ri.get("counters") or []
        total = ri.get("totalCount") or 0
        await self._apply_reaction(message_id, counters, total)

    async def _handle_chat_reaction(self, payload: dict) -> None:
        """Обновление чата (опкод 135) приходит тому, чьё сообщение отреагировали.

        Несёт lastReactedMessageId + lastReaction (последняя реакция в чате).
        """
        chat = payload.get("chat") if isinstance(payload, dict) else None
        if not isinstance(chat, dict):
            return
        mid = chat.get("lastReactedMessageId")
        reaction = (chat.get("lastReaction") or "").strip()
        if mid is None:
            # Поля очищены — реакцию сняли. Снимаем с предыдущего сообщения.
            if self._last_chat_reaction is not None:
                prev_mid, _ = self._last_chat_reaction
                self._last_chat_reaction = None
                await self._apply_reaction(prev_mid, [], 0)
            return
        # Опкод 135 приходит и не на реакции — дедуп по (msg, эмодзи).
        key = (mid, reaction)
        if key == self._last_chat_reaction:
            return
        self._last_chat_reaction = key
        counters = [{"reaction": reaction, "count": 1}] if reaction else []
        await self._apply_reaction(mid, counters, 1 if reaction else 0)

    async def _forward_to_telegram(self, message: Message) -> None:
        if message.sender is not None and message.sender == self._my_id():
            return
        if message.chat_id is None:
            return
        if self.group_id is None:
            if not self._warned_no_group:
                self._warned_no_group = True
                try:
                    await self.bot.send_message(
                        self.owner_tg_id,
                        f"📨 В аккаунт «{self.name}» приходят сообщения, но "
                        "группа ещё не привязана. Создай группу-форум, добавь "
                        "меня админом и напиши там /bind.",
                    )
                except Exception:
                    pass
            return

        # Реакции в этой версии MAX приходят как «пустое» сообщение с
        # обновлённым reaction_info. Не постим пустышку — зеркалим реакцию.
        extra = getattr(message, "model_extra", None) or {}
        has_content = bool(message.text) or bool(message.attaches) or isinstance(
            extra.get("link"), dict
        )
        ri = getattr(message, "reaction_info", None)
        if ri is not None and not has_content:
            counters = getattr(ri, "counters", None) or []
            total = getattr(ri, "total_count", 0) or 0
            logger.info(
                "[%s] reaction-update msg=%s total=%s counters=%s",
                self.name, message.id, total,
                [(self._counter_field(c, "reaction"),
                  self._counter_field(c, "count")) for c in counters],
            )
            await self._apply_reaction(message.id, counters, total)
            return

        dest = self.group_id
        chat = await self._get_chat(message.chat_id)
        is_group = chat is not None and chat.type != ChatType.DIALOG
        if is_group and not self.forward_groups:
            return

        thread = await self._ensure_thread(message.chat_id, chat)
        if thread is None:
            return
        await self._maybe_rename_topic(message.chat_id, thread)

        try:
            await self._deliver(message, chat, is_group, dest, thread)
        except TelegramBadRequest as e:
            if "thread not found" not in str(e).lower():
                raise
            # Тему удалили в Telegram — пересоздаём и повторяем один раз.
            logger.info(
                "[%s] Тема чата %s пропала — пересоздаю",
                self.name, message.chat_id,
            )
            await self.storage.clear_topic(message.chat_id)
            thread = await self._ensure_thread(message.chat_id, chat)
            if thread is None:
                return
            await self._deliver(message, chat, is_group, dest, thread)

    async def _deliver(
        self, message: Message, chat, is_group: bool, dest: int, thread: int
    ) -> None:
        silent = await self.storage.is_muted(message.chat_id)

        parts: list[str] = []
        if is_group:
            sender = (
                await self._user_name(message.sender)
                if message.sender else "?"
            )
            parts.append(f"👤 {sender}")
        if message.text:
            parts.append(message.text)

        await self.bot.send_chat_action(dest, "typing", message_thread_id=thread)

        media, notes, specials = await self._collect_incoming_media(message)
        if not message.text and notes:
            parts.extend(notes)

        # Пересланное / ответ: контент лежит во вложенном message (extra "link").
        fwd_media, fwd_parts = await self._collect_forward(message)
        if fwd_media or fwd_parts:
            parts.extend(fwd_parts)
            media = fwd_media + media

        body = "\n".join(parts)
        # records: (tg_message_id, role) — role ∈ {text, caption, media}.
        # Для правок редактируем носитель текста (text/caption), для удалений —
        # удаляем все.
        records: list[tuple[int, str]] = []

        caption: str | None = body or None
        caption_used = False
        if body and len(body) > TG_CAPTION_LIMIT:
            sent = await self.bot.send_message(
                dest, body, message_thread_id=thread,
                disable_notification=silent,
            )
            records.append((sent.message_id, "text"))
            caption = None
            caption_used = True

        album = [m for m in media if m[0] in ("photo", "video")]
        others = [m for m in media if m[0] not in ("photo", "video")]

        if album:
            cap = None if caption_used else caption
            role = "caption" if cap else "media"
            if len(album) == 1:
                kind, filename, data = album[0]
                file = BufferedInputFile(data, filename=filename)
                if kind == "photo":
                    sent = await self.bot.send_photo(
                        dest, file, caption=cap, message_thread_id=thread,
                        disable_notification=silent,
                    )
                else:
                    sent = await self.bot.send_video(
                        dest, file, caption=cap, message_thread_id=thread,
                        disable_notification=silent,
                    )
                records.append((sent.message_id, role))
            else:
                group: list[InputMediaPhoto | InputMediaVideo] = []
                for index, (kind, filename, data) in enumerate(album):
                    file = BufferedInputFile(data, filename=filename)
                    item_cap = cap if index == 0 else None
                    if kind == "photo":
                        group.append(InputMediaPhoto(media=file, caption=item_cap))
                    else:
                        group.append(InputMediaVideo(media=file, caption=item_cap))
                msgs = await self.bot.send_media_group(
                    dest, group, message_thread_id=thread,
                    disable_notification=silent,
                )
                for i, m in enumerate(msgs):
                    records.append((m.message_id, role if i == 0 else "media"))
            caption_used = True

        for kind, filename, data in others:
            file = BufferedInputFile(data, filename=filename)
            cap = None if caption_used else caption
            if kind == "sticker":
                if cap:
                    pre = await self.bot.send_message(
                        dest, cap, message_thread_id=thread,
                        disable_notification=silent,
                    )
                    records.append((pre.message_id, "text"))
                try:
                    sent = await self.bot.send_sticker(
                        dest, file, message_thread_id=thread,
                        disable_notification=silent,
                    )
                except Exception:
                    sent = await self.bot.send_document(
                        dest, file, message_thread_id=thread,
                        disable_notification=silent,
                    )
                records.append((sent.message_id, "media"))
            elif kind == "audio":
                sent = await self.bot.send_audio(
                    dest, file, caption=cap, message_thread_id=thread,
                    disable_notification=silent,
                )
                records.append((sent.message_id, "caption" if cap else "media"))
            else:
                sent = await self.bot.send_document(
                    dest, file, caption=cap, message_thread_id=thread,
                    disable_notification=silent,
                )
                records.append((sent.message_id, "caption" if cap else "media"))
            caption_used = True

        # Спец-вложения (контакт/гео/опрос) — отдельными методами Telegram.
        for kind, data in specials:
            try:
                if kind == "contact":
                    sent = await self.bot.send_contact(
                        dest, phone_number=data["phone"],
                        first_name=data["first"],
                        last_name=data.get("last") or None,
                        message_thread_id=thread, disable_notification=silent,
                    )
                elif kind == "location":
                    sent = await self.bot.send_location(
                        dest, latitude=data["lat"], longitude=data["lon"],
                        message_thread_id=thread, disable_notification=silent,
                    )
                elif kind == "poll":
                    opts = data["options"]
                    try:
                        from aiogram.types import InputPollOption
                        opts = [InputPollOption(text=o) for o in opts]
                    except Exception:
                        pass
                    sent = await self.bot.send_poll(
                        dest, question=data["question"],
                        options=opts, is_anonymous=True,
                        message_thread_id=thread, disable_notification=silent,
                    )
                else:
                    continue
                records.append((sent.message_id, "media"))
            except Exception:
                logger.info("[%s] спец-вложение %s не отправить",
                            self.name, kind, exc_info=True)

        if not records:
            if message.chat_id not in self._diag_empty:
                self._diag_empty.add(message.chat_id)
                try:
                    extra = getattr(message, "model_extra", None) or {}
                    logger.debug(
                        "[%s] DIAG пустое сообщение chat=%s type=%s "
                        "extra_keys=%s reaction_info=%r link=%r",
                        self.name, message.chat_id, message.type,
                        list(extra.keys()),
                        getattr(message, "reaction_info", None),
                        str(extra.get("link"))[:800],
                    )
                except Exception:
                    logger.debug("diag dump failed", exc_info=True)
            sent = await self.bot.send_message(
                dest, body or "📭 (пустое сообщение)",
                message_thread_id=thread, disable_notification=silent,
            )
            records.append((sent.message_id, "text"))

        for mid, role in records:
            # Текст храним у несущей роли — чтобы потом дописать реакции.
            await self.storage.remember_msg(
                message.chat_id, message.id, dest, mid, role,
                body=(body if role in ("text", "caption") else None),
            )
        await self.storage.set_last_message(message.chat_id, message.id)
        self._sent_since_trim += 1
        if self._sent_since_trim >= 500:
            self._sent_since_trim = 0
            await self.storage.trim_msg_map(20000)

        # Telegram авто-закрепляет первое сообщение новой темы — снимаем.
        if message.chat_id in self._fresh_topics:
            self._fresh_topics.discard(message.chat_id)
            try:
                await self.bot.unpin_all_forum_topic_messages(dest, thread)
            except Exception:
                logger.debug("unpin темы не удался", exc_info=True)

    async def _collect_incoming_media(
        self, message: Message
    ) -> tuple[list[tuple[str, str, bytes]], list[str], list[tuple[str, dict]]]:
        result: list[tuple[str, str, bytes]] = []
        notes: list[str] = []
        specials: list[tuple[str, dict]] = []  # ('contact'|'location'|'poll', data)
        for attach in message.attaches:
            try:
                if isinstance(attach, PhotoAttachment):
                    data = await self._download(attach.base_url)
                    if data:
                        result.append(("photo", "photo.jpg", data))
                elif isinstance(attach, VideoAttachment):
                    info = await self.client.get_video_by_id(
                        message.chat_id, message.id, attach.video_id
                    )
                    if info and info.url:
                        data = await self._download(info.url)
                        if data:
                            result.append(("video", "video.mp4", data))
                    else:
                        notes.append("🎬 видео")
                elif isinstance(attach, FileAttachment):
                    info = await self.client.get_file_by_id(
                        message.chat_id, message.id, attach.file_id
                    )
                    if info and info.url:
                        data = await self._download(info.url)
                        if data:
                            result.append(("document", attach.name or "file", data))
                    else:
                        notes.append(f"📎 файл: {attach.name or 'без имени'}")
                elif isinstance(attach, StickerAttachment):
                    data = await self._download(attach.url)
                    if data:
                        result.append(("sticker", "sticker.webp", data))
                    else:
                        notes.append("🩷 стикер")
                elif isinstance(attach, AudioAttachment):
                    data = await self._download(attach.url) if attach.url else None
                    if data:
                        result.append(("audio", "audio.mp3", data))
                    else:
                        notes.append("🎤 голосовое / аудио")
                elif isinstance(attach, ContactAttachment):
                    nm = attach.name or " ".join(
                        p for p in (attach.first_name, attach.last_name) if p
                    )
                    phone = None
                    try:
                        u = await self.client.get_user(attach.contact_id)
                        if u and u.phone:
                            phone = u.phone
                    except Exception:
                        logger.debug("get_user для контакта не удался",
                                     exc_info=True)
                    logger.debug(
                        "[%s] контакт %s phone_found=%s",
                        self.name, attach.contact_id, bool(phone),
                    )
                    if phone:
                        specials.append(("contact", {
                            "phone": f"+{phone}",
                            "first": attach.first_name or nm or "Контакт",
                            "last": attach.last_name or "",
                        }))
                    else:
                        notes.append(
                            f"👤 Контакт: {nm}" if nm else "👤 контакт"
                        )
                elif isinstance(attach, ShareAttachment):
                    notes.append("🔗 ссылка / репост")
                elif isinstance(attach, CallAttachment):
                    notes.append("📞 звонок")
                elif isinstance(attach, ControlAttachment):
                    notes.append("ℹ️ системное сообщение")
                else:
                    t = getattr(attach, "type", None)
                    type_name = (
                        t.value if hasattr(t, "value") else str(t or "вложение")
                    )
                    extra = getattr(attach, "model_extra", None) or {}
                    if type_name == "POLL":
                        title = str(extra.get("title") or "Опрос")[:300]
                        answers = extra.get("answers") or []
                        options = [
                            str(a.get("text") or "")[:100]
                            for a in answers
                            if isinstance(a, dict) and a.get("text")
                        ][:10]
                        if len(options) >= 2:
                            specials.append(("poll", {
                                "question": title, "options": options,
                            }))
                        else:
                            notes.append(f"📊 Опрос: {title}")
                    elif type_name in ("LOCATION", "GEO", "GEOLOCATION"):
                        lat = extra.get("latitude", extra.get("lat"))
                        lon = extra.get("longitude",
                                        extra.get("lon", extra.get("lng")))
                        if lat is not None and lon is not None:
                            specials.append(("location", {
                                "lat": lat, "lon": lon,
                            }))
                        else:
                            notes.append("📍 геолокация")
                    else:
                        notes.append(f"📎 {type_name}")
                    self._diag_attach(attach)
            except Exception:
                logger.exception("Не удалось обработать вложение из MAX")
                notes.append("📎 вложение (ошибка обработки)")
        return result, notes, specials

    def _diag_attach(self, attach) -> None:
        """Один раз на тип логирует сырое вложение — чтобы отрисовать его потом."""
        t = getattr(attach, "type", None)
        key = t.value if hasattr(t, "value") else str(t)
        if key in self._diag_attaches:
            return
        self._diag_attaches.add(key)
        try:
            logger.info(
                "[%s] DIAG attach %s: %r",
                self.name, key, str(attach.model_dump())[:900],
            )
        except Exception:
            pass

    async def _collect_forward(
        self, message: Message
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """Разбирает пересланное/ответ из extra-поля 'link' (вложенный message)."""
        extra = getattr(message, "model_extra", None) or {}
        link = extra.get("link")
        if not isinstance(link, dict):
            return [], []
        nested = link.get("message")
        if not isinstance(nested, dict):
            return [], []

        ltype = str(link.get("type") or "").upper()
        orig = nested.get("sender")
        who = await self._user_name(orig) if isinstance(orig, int) else None
        parts: list[str] = []
        if ltype == "REPLY":
            parts.append("↩️ В ответ" + (f" {who}" if who else ""))
        else:
            parts.append("↪️ Переслано" + (f" от {who}" if who else ""))
        ntext = str(nested.get("text") or "").strip()
        if ntext:
            parts.append(ntext)

        src_chat_id = link.get("chatId")
        src_msg_id = nested.get("id")
        media, notes = await self._collect_nested_media(
            nested, src_chat_id, src_msg_id
        )
        parts.extend(notes)
        return media, parts

    async def _collect_nested_media(
        self, nested: dict, src_chat_id, src_msg_id
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """Вложения вложенного (пересланного) сообщения."""
        result: list[tuple[str, str, bytes]] = []
        notes: list[str] = []
        for a in nested.get("attaches", []) or []:
            if not isinstance(a, dict):
                continue
            t = str(a.get("_type") or "").upper()
            try:
                if t == "PHOTO" and a.get("baseUrl"):
                    data = await self._download(a["baseUrl"])
                    if data:
                        result.append(("photo", "photo.jpg", data))
                elif t == "STICKER" and a.get("url"):
                    data = await self._download(a["url"])
                    if data:
                        result.append(("sticker", "sticker.webp", data))
                elif t == "AUDIO" and a.get("url"):
                    data = await self._download(a["url"])
                    if data:
                        result.append(("audio", "audio.mp3", data))
                elif t == "FILE":
                    data = await self._fetch_by_id(
                        "file", src_chat_id, src_msg_id, a.get("fileId")
                    )
                    if data:
                        result.append(
                            ("document", a.get("name") or "file", data)
                        )
                    else:
                        notes.append(f"📎 файл: {a.get('name') or 'без имени'}")
                elif t == "VIDEO":
                    data = await self._fetch_by_id(
                        "video", src_chat_id, src_msg_id, a.get("videoId")
                    )
                    if data:
                        result.append(("video", "video.mp4", data))
                    else:
                        notes.append("🎬 видео")
                elif t == "SHARE":
                    notes.append("🔗 ссылка / репост")
                elif t:
                    notes.append(f"📎 {t.lower()}")
            except Exception:
                logger.exception("Вложение пересланного не обработано")
                notes.append("📎 вложение")
        return result, notes

    async def _fetch_by_id(self, kind, chat_id, msg_id, attach_id) -> bytes | None:
        """Тянет файл/видео пересланного по его родному чату (если есть доступ)."""
        if not (chat_id and msg_id and attach_id):
            return None
        try:
            if kind == "file":
                info = await self.client.get_file_by_id(
                    chat_id, msg_id, attach_id
                )
            else:
                info = await self.client.get_video_by_id(
                    chat_id, msg_id, attach_id
                )
            if info and info.url:
                return await self._download(info.url)
        except Exception:
            logger.debug("forward %s fetch failed", kind, exc_info=True)
        return None

    async def _download(self, url: str) -> bytes | None:
        async with self.http.get(url) as resp:
            if resp.status != 200:
                logger.warning("Скачивание %s -> HTTP %s", url, resp.status)
                return None
            return await resp.read()

    # ── Telegram -> MAX ───────────────────────────────────────────────────

    async def handle_tg(self, message: TgMessage) -> None:
        if message.message_thread_id is None:
            await self._handle_general(message)
            return
        try:
            await self._send_to_max(message)
        except Exception:
            logger.exception("[%s] Ошибка отправки TG->MAX", self.name)
            await message.reply("⚠️ Не удалось отправить в MAX (см. логи).")

    async def _handle_general(self, message: TgMessage) -> None:
        """General-тема группы: инициация нового чата MAX.

        Формат:
          +79991234567          — найти по телефону, создать чат
          +79991234567 Привет!  — то же + отправить первое сообщение
          username              — найти по ссылке/нику MAX
          username Привет!      — то же + первое сообщение
        """
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        query = parts[0].lstrip("@")
        first_text = parts[1] if len(parts) > 1 else ""

        hint = await message.reply("🔍 Ищу пользователя в MAX…")

        try:
            user = await self._find_max_user(query)
        except Exception as e:
            await hint.edit_text(f"⚠️ Ошибка поиска: {e}")
            return

        if user is None:
            await hint.edit_text(
                "❌ Пользователь не найден.\n\n"
                "Варианты:\n"
                "• Номер телефона: +79991234567\n"
                "• Username MAX: someusername\n"
                "• MAX user ID (число): 123456789\n\n"
                "Если знаешь MAX user ID — введи его напрямую, это самый надёжный способ."
            )
            return

        user_id: int = getattr(user, "id", None) or user.contact.id
        # get_chat_id — XOR двух ID; берём my_id из me.contact.id или me.id.
        me = self.client.me
        if me is None:
            await hint.edit_text("⚠️ Аккаунт MAX ещё не готов. Попробуй через несколько секунд.")
            return
        my_id = (
            me.contact.id
            if getattr(me, "contact", None) is not None
            else getattr(me, "id", None)
        )
        if my_id is None:
            await hint.edit_text("⚠️ Не удалось определить ID аккаунта MAX.")
            return
        max_chat_id = my_id ^ user_id

        # Уже есть тема — проверяем реальным сообщением, что она жива.
        existing_thread = await self.storage.get_topic(max_chat_id)
        if existing_thread is not None:
            try:
                probe = await self.bot.send_message(
                    self.group_id, "…", message_thread_id=existing_thread
                )
                await self.bot.delete_message(self.group_id, probe.message_id)
                name = self._label_for(user, user_id)
                await hint.edit_text(
                    f"💬 Чат с «{name}» уже есть — тема #{existing_thread}."
                )
                return
            except TelegramBadRequest as e:
                if "thread not found" in str(e).lower():
                    await self.storage.clear_topic(max_chat_id)
                    existing_thread = None
                else:
                    raise

        # Отправляем первое сообщение в MAX (это создаёт диалог).
        send_text = first_text or "👋"
        try:
            sent = await self.client.send_message(
                chat_id=max_chat_id, text=send_text
            )
        except ApiError as e:
            reason = (
                getattr(e, "localized_message", None)
                or getattr(e, "message", None) or str(e)
            )
            await hint.edit_text(f"⚠️ MAX не принял сообщение: {reason}")
            return

        # Создаём тему в Telegram.
        thread = await self._ensure_thread(max_chat_id, None)
        if thread is None:
            await hint.edit_text("⚠️ Сообщение отправлено, но тему создать не удалось.")
            return

        # Запоминаем исходящее сообщение (для реакций собеседника).
        if sent is not None and getattr(sent, "id", None) is not None:
            await self.storage.remember_msg(
                max_chat_id, sent.id,
                self.group_id, 0,  # tg_message_id неизвестен — пишем 0
                "user",
            )

        name = self._label_for(user, user_id)
        await hint.edit_text(
            f"✅ Чат с «{name}» создан! Пиши в новой теме.\n"
            + (f'Первое сообщение отправлено: «{send_text}»' if first_text else "")
        )
        logger.info(
            "[%s] Инициирован новый чат MAX %s -> тема %s",
            self.name, max_chat_id, thread,
        )

    async def _find_max_user(self, query: str):
        """Ищет пользователя MAX по телефону (+7…) или username/ссылке.

        Возвращает объект user (с полем contact.id) или None.
        """
        raw_digits = query.lstrip("+")

        # 1) Поиск по телефону через search_by_phone.
        is_phone = PHONE_RE.match(query) or (raw_digits.isdigit() and len(raw_digits) >= 7)
        if is_phone:
            phone = query if query.startswith("+") else f"+{raw_digits}"
            try:
                result = await self.client.search_by_phone(phone)
                if result is not None:
                    # Может вернуть user-объект или список — нормализуем.
                    if isinstance(result, list):
                        result = result[0] if result else None
                    if result is not None:
                        logger.info("[%s] _find_max_user: нашли через search_by_phone(%s)",
                                    self.name, phone)
                        return result
            except Exception:
                logger.debug("search_by_phone(%s) не удался", phone, exc_info=True)

        # 2) Числовой MAX user ID — прямой поиск.
        if raw_digits.isdigit():
            uid = int(raw_digits)
            try:
                user = await self.client.get_user(uid)
                if user is not None:
                    logger.info("[%s] _find_max_user: нашли через get_user(%s)", self.name, uid)
                    return user
            except Exception:
                logger.debug("get_user(%s) не удался", uid, exc_info=True)

        # 3) Поиск по username/имени среди известных чатов (обновляем список).
        q = query.lower().lstrip("@")
        try:
            await self.client.get_chats()
        except Exception:
            pass

        for chat in self.client.chats or []:
            for uid in chat.participants or {}:
                if uid == self._my_id():
                    continue
                cached = self.client.get_cached_user(uid)
                if cached is None:
                    try:
                        cached = await self.client.get_user(uid)
                    except Exception:
                        continue
                if cached is None:
                    continue
                link = (getattr(cached, "link", None) or "").lower().strip("/").split("/")[-1]
                phone_raw = str(getattr(cached, "phone", "") or "").lstrip("+")
                name_str = (self._name_of(cached) or "").lower()
                if link == q or phone_raw == raw_digits or name_str == q:
                    logger.info("[%s] _find_max_user: нашли в кэше чатов uid=%s",
                                self.name, uid)
                    return cached

        logger.info("[%s] _find_max_user: не нашли %r", self.name, query)
        return None

    async def handle_tg_reaction(self, tg_message_id, new_reaction) -> None:
        """TG → MAX реакции ОТКЛЮЧЕНЫ.

        Текущая версия MAX отвергает add_reaction/remove_reaction с ошибкой
        proto.payload («Expected number at 24» — id сообщения уходит строкой,
        а сервер ждёт число) и РАЗРЫВАЕТ соединение — как и read_message.
        Пока в PyMax/MAX это не починят, отправку реакций в MAX не делаем.
        """
        return

    async def _target_for(self, message: TgMessage) -> tuple[int | None, int | None]:
        thread = message.message_thread_id
        if thread is None:
            return None, None
        max_chat_id = await self.storage.chat_by_thread(thread)
        if max_chat_id is None:
            return None, None
        last = await self.storage.get_last_message(max_chat_id)
        return max_chat_id, last

    async def toggle_mute(self, message: TgMessage, *, muted: bool) -> None:
        max_chat_id, _ = await self._target_for(message)
        if max_chat_id is None:
            await message.reply("Отправь эту команду внутри нужной темы.")
            return
        await self.storage.set_muted(max_chat_id, muted)
        title = await self._chat_title_by_id(max_chat_id)
        await message.reply(
            f"🔕 Чат «{title}» — теперь без звука."
            if muted else f"🔔 Чат «{title}» — снова со звуком."
        )

    async def list_muted(self, message: TgMessage) -> None:
        ids = await self.storage.list_muted()
        if not ids:
            await message.reply("Заглушённых чатов нет.")
            return
        titles = [await self._chat_title_by_id(cid) for cid in ids]
        await message.reply(
            "🔕 Заглушённые чаты:\n" + "\n".join(f"• {t}" for t in titles)
        )

    async def _send_to_max(self, message: TgMessage) -> None:
        max_chat_id, _ = await self._target_for(message)
        if max_chat_id is None:
            return
        if not self._max_online():
            await message.reply(
                "⏳ MAX ещё переподключается — повтори через пару секунд."
            )
            return
        text = message.text or message.caption or ""
        attachments = await self._collect_outgoing_media(message)
        if not text and not attachments:
            return
        try:
            sent = await self.client.send_message(
                chat_id=max_chat_id, text=text,
                attachments=attachments or None,
            )
        except ApiError as e:
            reason = (
                getattr(e, "localized_message", None)
                or getattr(e, "message", None) or str(e)
            )
            logger.warning("[%s] MAX отклонил отправку: %s", self.name, reason)
            await message.reply(f"⚠️ MAX не принял сообщение: {reason}")
            return
        # Запоминаем связь со своим (исходящим) сообщением — чтобы показать
        # реакции собеседника на него. Это сообщение ПОЛЬЗОВАТЕЛЯ, поэтому на
        # него бот может поставить видимую реакцию (роль 'user').
        if sent is not None and getattr(sent, "id", None) is not None:
            await self.storage.remember_msg(
                max_chat_id, sent.id, message.chat.id, message.message_id,
                "user",
            )
            logger.debug(
                "[%s] исходящее сохранено: maxmsg=%s -> tg=%s",
                self.name, sent.id, message.message_id,
            )
        else:
            logger.debug(
                "[%s] send_message не вернул id — связь не сохранена",
                self.name,
            )
        # Успешная отправка — без подтверждения (👍 убрали). Об ошибке выше
        # пользователь узнаёт отдельным ответом.

    async def _collect_outgoing_media(
        self, message: TgMessage
    ) -> list[Photo | File | Video]:
        attachments: list[Photo | File | Video] = []
        if message.photo:
            data = await self._tg_download(message.photo[-1])
            attachments.append(Photo(raw=data, name="photo.jpg"))
        if message.document:
            data = await self._tg_download(message.document)
            attachments.append(
                File(raw=data, name=message.document.file_name or "file")
            )
        if message.video:
            data = await self._tg_download(message.video)
            attachments.append(
                Video(raw=data, name=message.video.file_name or "video.mp4")
            )
        return attachments

    async def _tg_download(self, downloadable) -> bytes:
        buffer = await self.bot.download(downloadable)
        assert buffer is not None
        return buffer.read()


class Manager:
    """Общий бот: онбординг аккаунтов, маршрутизация, жизненный цикл."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(config.telegram_token)
        # Авто-ретрай при flood-control Telegram (429).
        self.bot.session.middleware(_retry_after_middleware)
        self.dp = Dispatcher()
        self.http: aiohttp.ClientSession | None = None
        self.registry: Registry | None = None
        self.workers: dict[int, Account] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.by_group: dict[int, Account] = {}
        self.conv: dict[int, Conv] = {}
        self.pending_announce: set[int] = set()
        # Модерация регистраций: админы одобряют новые аккаунты.
        self.admin_ids: set[int] = set(config.admin_ids)
        self.pending_reqs: dict[int, dict] = {}  # req_id -> {requester, phone}
        self._req_counter = 0
        # Антифлуд: попытки /add и кулдауны «тяжёлых» команд (по монотонным сек).
        self._add_times: dict[int, list[float]] = {}
        self._cmd_times: dict[tuple[int, str], float] = {}
        self._register_handlers()

    # ── антифлуд ──────────────────────────────────────────────────────────

    def _cmd_cooldown(self, tg: int, key: str, seconds: int) -> int:
        """0 если можно, иначе сколько секунд ещё ждать. Запоминает срабатывание."""
        if tg in self.admin_ids:
            return 0
        now = time.monotonic()
        last = self._cmd_times.get((tg, key), 0.0)
        if now - last < seconds:
            return int(seconds - (now - last)) + 1
        self._cmd_times[(tg, key)] = now
        return 0

    async def _check_add_quota(self, tg: int) -> str | None:
        """None если /add разрешён, иначе текст отказа. Учитывает попытку."""
        if tg in self.admin_ids:
            return None
        accs = await self.registry.list_by_owner(tg)
        if len(accs) >= self.config.max_accounts:
            return (
                f"У тебя уже {len(accs)} аккаунт(ов) — это максимум "
                f"({self.config.max_accounts}). Удали лишний: /remove N"
            )
        now = time.monotonic()
        hist = [t for t in self._add_times.get(tg, []) if now - t < 3600]
        if hist and now - hist[-1] < self.config.add_cooldown:
            wait = int(self.config.add_cooldown - (now - hist[-1])) + 1
            return f"⏳ Слишком часто. Подожди {wait} сек и повтори /add."
        if len(hist) >= self.config.add_per_hour:
            return (
                "🚧 Лимит регистраций на час исчерпан. Попробуй позже."
            )
        self._add_times[tg] = hist + [now]
        return None

    # ── ввод кода/пароля из диалога ───────────────────────────────────────

    async def await_input(self, tg_id: int, step: str, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        prev = self.conv.get(tg_id)
        account_id = prev.account_id if prev else None
        self.conv[tg_id] = Conv(step=step, account_id=account_id, future=fut)
        await self.bot.send_message(tg_id, prompt)
        try:
            return await asyncio.wait_for(fut, AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await self.bot.send_message(
                tg_id, "⌛ Время на ввод вышло. Начни заново: /add"
            )
            raise

    # ── жизненный цикл аккаунта ───────────────────────────────────────────

    def _session_name(self, acc: dict) -> str:
        return acc["session"] or f"acc_{acc['id']}.db"

    def _mapping_db(self, acc: dict) -> str:
        return acc["mapping_db"] or os.path.join(
            self.config.work_dir, f"acc_{acc['id']}_map.db"
        )

    def _build_client(self, acc: dict) -> Client:
        auth = TelegramAuth(self, acc["owner_tg_id"])
        extra = ExtraConfig(proxy=acc["proxy"]) if acc["proxy"] else None
        client = Client(
            phone=acc["phone"],
            work_dir=self.config.work_dir,
            session_name=self._session_name(acc),
            extra_config=extra,
            sms_code_provider=auth,
            password_provider=auth,
        )
        account_id = acc["id"]

        @client.on_start()
        async def _on_start(c: Client) -> None:
            await self._on_account_started(account_id)

        return client

    async def _start_account(self, account_id: int) -> Account | None:
        acc = await self.registry.get(account_id)
        if acc is None:
            return None
        client = self._build_client(acc)
        storage = await Storage.create(self._mapping_db(acc))
        worker = Account(
            self.bot, self.http,
            account_id=account_id,
            owner_tg_id=acc["owner_tg_id"],
            name=acc["name"] or f"MAX {account_id}",
            group_id=acc["group_id"],
            client=client,
            storage=storage,
        )
        self.workers[account_id] = worker
        if acc["group_id"] is not None:
            self.by_group[acc["group_id"]] = worker
        task = asyncio.create_task(client.start(), name=f"acc:{account_id}")
        self.tasks[account_id] = task
        task.add_done_callback(
            lambda t, aid=account_id: self._on_client_done(aid, t)
        )
        _quiet_libs()  # PyMax сбрасывает уровни логов при создании Client
        return worker

    async def _on_account_started(self, account_id: int) -> None:
        worker = self.workers.get(account_id)
        if worker is None:
            return
        await self.registry.set_status(account_id, "active")
        if account_id in self.pending_announce:
            self.pending_announce.discard(account_id)
            self.conv.pop(worker.owner_tg_id, None)
            if worker.group_id is None:
                text = (
                    f"✅ Аккаунт «{worker.name}» вошёл в MAX!\n\n"
                    "Теперь создай группу-форум, добавь меня админом с правом "
                    "«Управление темами», и напиши в группе /bind — привяжу её "
                    "к этому аккаунту."
                )
            else:
                text = f"✅ Аккаунт «{worker.name}» снова на связи."
            await self.bot.send_message(worker.owner_tg_id, text)

    def _on_client_done(self, account_id: int, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        asyncio.create_task(self._account_stopped(account_id, exc))

    async def _account_stopped(self, account_id: int, exc) -> None:
        acc = await self.registry.get(account_id)
        worker = self.workers.get(account_id)
        owner = (worker.owner_tg_id if worker else None) or (
            acc["owner_tg_id"] if acc else None
        )
        name = (worker.name if worker else None) or (
            acc["name"] if acc else f"MAX {account_id}"
        )
        onboarding = account_id in self.pending_announce
        self.pending_announce.discard(account_id)
        # Аккаунт НЕ удаляем — оставляем, чтобы можно было задать прокси и
        # повторить вход (/setproxy, /relogin) или удалить вручную (/remove).
        await self._cleanup_account(account_id, delete=False)
        if acc is not None:
            await self.registry.set_status(
                account_id, "failed" if onboarding else "stopped"
            )
        if not owner:
            return
        if onboarding:
            self.conv.pop(owner, None)
            text = (
                f"❌ Вход в MAX не удался: {exc}\n\n"
                "• Если ты в другой стране — задай прокси своего региона и "
                "повтори вход:\n"
                f"   /setproxy {account_id} http://user:pass@ip:port\n"
                f"   /relogin {account_id}\n"
                f"• Или удали заявку: /remove {account_id}"
            )
        else:
            logger.error("Аккаунт '%s' остановился: %r", name, exc)
            text = (
                f"⚠️ Аккаунт «{name}» остановился (возможно, MAX сбросил "
                f"сессию). Перезапустить вход: /relogin {account_id} "
                f"(или удалить: /remove {account_id})."
            )
        try:
            await self.bot.send_message(owner, text)
        except Exception:
            pass

    async def _restart_account(self, account_id: int, *, announce: bool) -> None:
        """Останавливает и заново запускает аккаунт (после смены прокси и т.п.)."""
        await self._cleanup_account(account_id, delete=False)
        if announce:
            self.pending_announce.add(account_id)
        await self._start_account(account_id)

    async def _cleanup_account(self, account_id: int, *, delete: bool) -> None:
        task = self.tasks.pop(account_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task  # дождёмся закрытия сессии MAX перед удалением файлов
            except (asyncio.CancelledError, Exception):
                pass
        worker = self.workers.pop(account_id, None)
        if worker is not None:
            if worker.group_id is not None:
                self.by_group.pop(worker.group_id, None)
            try:
                await worker.storage.close()
            except Exception:
                pass
        if delete:
            acc = await self.registry.get(account_id)
            if acc is not None:
                for path in (
                    os.path.join(self.config.work_dir, self._session_name(acc)),
                    self._mapping_db(acc),
                ):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
            await self.registry.remove(account_id)

    # ── Telegram-хендлеры ─────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        dp = self.dp

        @dp.message(Command("start", "help"))
        async def cmd_start(message: TgMessage) -> None:
            await message.answer(
                "Привет! Я зеркалю переписку MAX в Telegram.\n\n"
                "/add — добавить MAX-аккаунт (спрошу телефон и код из SMS)\n"
                "/accounts — твои аккаунты\n"
                "/remove N — удалить аккаунт\n"
                "/setproxy N <url> — задать прокси (или off — убрать)\n"
                "/relogin N — повторить вход (после прокси / сброса сессии)\n\n"
                "У каждого аккаунта — своя группа-форум: после /add создаёшь "
                "группу, добавляешь меня админом и пишешь там /bind."
            )

        @dp.message(Command("setproxy"))
        async def cmd_setproxy(
            message: TgMessage, command: CommandObject
        ) -> None:
            if message.chat.type != "private":
                await message.reply("Команду /setproxy шли в личке.")
                return
            tg = message.from_user.id
            args = (command.args or "").split(maxsplit=1)
            if not args or not args[0].isdigit():
                await message.reply(
                    "Использование:\n"
                    "/setproxy N http://user:pass@ip:port\n"
                    "/setproxy N off — убрать прокси"
                )
                return
            account_id = int(args[0])
            acc = await self.registry.get(account_id)
            if acc is None or acc["owner_tg_id"] != tg:
                await message.reply("Нет такого аккаунта среди твоих.")
                return
            raw = args[1].strip() if len(args) > 1 else ""
            if raw.lower() in ("", "off", "none", "-"):
                proxy = None
            elif re.match(r"^(https?|socks5|socks4)://", raw):
                proxy = raw
            else:
                await message.reply(
                    "Прокси должен начинаться с http://, https:// или socks5://"
                )
                return
            wait = self._cmd_cooldown(tg, "restart", 15)
            if wait:
                await message.reply(f"⏳ Слишком часто. Подожди {wait} сек.")
                return
            await self.registry.set_proxy(account_id, proxy)
            await message.reply(
                f"🌐 Прокси для #{account_id} "
                f"{'убран' if proxy is None else 'сохранён'}. Перезапускаю "
                "аккаунт — если потребуется вход, пришлю запрос кода."
            )
            await self._restart_account(account_id, announce=True)

        @dp.message(Command("relogin"))
        async def cmd_relogin(
            message: TgMessage, command: CommandObject
        ) -> None:
            if message.chat.type != "private":
                await message.reply("Команду /relogin шли в личке.")
                return
            tg = message.from_user.id
            arg = (command.args or "").strip()
            if not arg.isdigit():
                await message.reply("Использование: /relogin N (номер из /accounts)")
                return
            account_id = int(arg)
            acc = await self.registry.get(account_id)
            if acc is None or acc["owner_tg_id"] != tg:
                await message.reply("Нет такого аккаунта среди твоих.")
                return
            wait = self._cmd_cooldown(tg, "restart", 15)
            if wait:
                await message.reply(f"⏳ Слишком часто. Подожди {wait} сек.")
                return
            await message.reply("🔄 Перезапускаю вход в MAX…")
            await self._restart_account(account_id, announce=True)

        @dp.message(Command("add"))
        async def cmd_add(message: TgMessage) -> None:
            if message.chat.type != "private":
                await message.reply("Добавляй аккаунт в личке со мной.")
                return
            tg = message.from_user.id
            if await self.registry.is_banned(tg):
                await message.answer("🚫 Доступ к боту ограничен.")
                return
            reason = await self._check_add_quota(tg)
            if reason:
                await message.answer(reason)
                return
            self.conv[tg] = Conv(step="phone")
            await message.answer(
                "Пришли номер телефона MAX в международном формате, например "
                "+79991234567"
            )

        @dp.message(Command("accounts"))
        async def cmd_accounts(message: TgMessage) -> None:
            accs = await self.registry.list_by_owner(message.from_user.id)
            if not accs:
                await message.answer("У тебя пока нет аккаунтов. Добавь: /add")
                return
            lines = []
            for a in accs:
                grp = "✅ группа" if a["group_id"] else "⚠️ нет группы"
                online = "🟢" if a["id"] in self.workers else "⚪️"
                lines.append(
                    f"{online} #{a['id']} «{a['name']}» {a['phone']} — {grp}"
                )
            await message.answer(
                "Твои аккаунты:\n" + "\n".join(lines)
                + "\n\nУдалить: /remove N"
            )

        @dp.message(Command("remove"))
        async def cmd_remove(message: TgMessage, command: CommandObject) -> None:
            tg = message.from_user.id
            arg = (command.args or "").strip()
            accs = await self.registry.list_by_owner(tg)
            if not accs:
                await message.reply("У тебя нет аккаунтов. Добавить: /add")
                return
            if not arg.isdigit():
                lines = "\n".join(
                    f"• #{a['id']} «{a['name']}» {a['phone']}" for a in accs
                )
                await message.reply(
                    "Укажи номер аккаунта: /remove N\n\n" + lines
                )
                return
            account_id = int(arg)
            acc = await self.registry.get(account_id)
            if acc is None or acc["owner_tg_id"] != tg:
                await message.reply("Нет такого аккаунта среди твоих.")
                return
            name = acc["name"]
            await self._cleanup_account(account_id, delete=True)
            await message.reply(
                f"🗑 Аккаунт #{account_id} «{name}» удалён: сессия MAX и темы "
                "стёрты, группа отвязана (саму группу можешь удалить вручную)."
            )

        @dp.message(Command("bind"))
        async def cmd_bind(message: TgMessage, command: CommandObject) -> None:
            if message.chat.type not in ("group", "supergroup"):
                await message.reply("Команду /bind надо отправить в группе.")
                return
            tg = message.from_user.id
            group_id = message.chat.id
            existing = await self.registry.by_group(group_id)
            if existing is not None:
                await message.reply(
                    f"Эта группа уже привязана к аккаунту «{existing['name']}»."
                )
                return
            free = [
                a for a in await self.registry.list_by_owner(tg)
                if a["group_id"] is None
            ]
            if command.args and command.args.strip().isdigit():
                account_id = int(command.args.strip())
                acc = await self.registry.get(account_id)
                if acc is None or acc["owner_tg_id"] != tg:
                    await message.reply("Нет такого твоего аккаунта.")
                    return
                if acc["group_id"] is not None:
                    await message.reply("У этого аккаунта уже есть группа.")
                    return
            elif len(free) == 1:
                account_id = free[0]["id"]
            elif not free:
                await message.reply(
                    "Сначала добавь аккаунт в личке: /add (или у всех уже "
                    "есть группы)."
                )
                return
            else:
                opts = "\n".join(
                    f"• #{a['id']} «{a['name']}»" for a in free
                )
                await message.reply(
                    "У тебя несколько аккаунтов без группы. Укажи номер: "
                    f"/bind N\n{opts}"
                )
                return
            await self.registry.set_group(account_id, group_id)
            worker = self.workers.get(account_id)
            if worker is not None:
                worker.group_id = group_id
                self.by_group[group_id] = worker
            name = (await self.registry.get(account_id))["name"]
            await message.reply(
                f"✅ Группа привязана к аккаунту «{name}». Сообщения MAX будут "
                "приходить сюда отдельными темами."
            )

        @dp.message(Command("mute"))
        async def cmd_mute(message: TgMessage) -> None:
            await self._route_command(message, "mute")

        @dp.message(Command("unmute"))
        async def cmd_unmute(message: TgMessage) -> None:
            await self._route_command(message, "unmute")

        @dp.message(Command("muted"))
        async def cmd_muted(message: TgMessage) -> None:
            await self._route_command(message, "muted")

        @dp.message(Command("admin"))
        async def cmd_admin(message: TgMessage, command: CommandObject) -> None:
            if message.from_user.id not in self.admin_ids:
                return  # тихо игнорируем для не-админов
            args = (command.args or "").split()
            if not args:
                await self._admin_dashboard(message)
                return
            sub = args[0].lower()
            if sub == "list":
                await self._admin_list(message)
            elif sub == "user" and len(args) > 1 and args[1].lstrip("-").isdigit():
                await self._admin_list(message, owner=int(args[1]))
            elif sub in ("stop", "start", "remove") and len(args) > 1 \
                    and args[1].isdigit():
                await self._admin_action(message, sub, int(args[1]))
            else:
                await message.reply(
                    "Подкоманды: /admin list | user <tg_id> | "
                    "stop N | start N | remove N"
                )

        @dp.message(Command("ban"))
        async def cmd_ban(message: TgMessage, command: CommandObject) -> None:
            if message.from_user.id not in self.admin_ids:
                return
            arg = (command.args or "").strip()
            if not arg.lstrip("-").isdigit():
                await message.reply("Использование: /ban <tg_id>")
                return
            target = int(arg)
            if target in self.admin_ids:
                await message.reply("Админа банить нельзя.")
                return
            await self.registry.ban(target)
            stopped = 0
            for a in await self.registry.list_by_owner(target):
                await self._cleanup_account(a["id"], delete=False)
                await self.registry.set_status(a["id"], "banned")
                stopped += 1
            await message.reply(
                f"🚫 Пользователь {target} забанен. Остановлено аккаунтов: "
                f"{stopped}. Разбан: /unban {target}"
            )
            try:
                await self.bot.send_message(
                    target, "🚫 Доступ к боту ограничен администратором."
                )
            except Exception:
                pass

        @dp.message(Command("unban"))
        async def cmd_unban(message: TgMessage, command: CommandObject) -> None:
            if message.from_user.id not in self.admin_ids:
                return
            arg = (command.args or "").strip()
            if not arg.lstrip("-").isdigit():
                await message.reply("Использование: /unban <tg_id>")
                return
            await self.registry.unban(int(arg))
            await message.reply(f"✅ Пользователь {arg} разбанен.")

        @dp.message(Command("banned"))
        async def cmd_banned(message: TgMessage) -> None:
            if message.from_user.id not in self.admin_ids:
                return
            ids = await self.registry.list_bans()
            if not ids:
                await message.reply("Забаненных нет.")
                return
            await message.reply(
                "🚫 Забанены:\n" + "\n".join(str(i) for i in ids)
            )

        @dp.message(Command("backup"))
        async def cmd_backup(message: TgMessage) -> None:
            if message.from_user.id not in self.admin_ids:
                return
            await message.reply("📦 Делаю бэкап…")
            try:
                path = await self._backup_now()
            except Exception as e:
                logger.exception("Бэкап не удался")
                await message.reply(f"❌ Бэкап не удался: {e}")
                return
            size = os.path.getsize(path)
            if size <= TG_UPLOAD_LIMIT:
                await message.answer_document(
                    FSInputFile(path),
                    caption=f"📦 Бэкап ({size // 1024} КБ)",
                )
            else:
                await message.reply(
                    f"✅ Бэкап сохранён на сервере:\n{path}\n"
                    f"({size // 1024 // 1024} МБ — слишком большой для отправки "
                    "в Telegram, скопируй с сервера вручную)."
                )

        @dp.message_reaction()
        async def on_tg_reaction(event: MessageReactionUpdated) -> None:
            acc = self.by_group.get(event.chat.id)
            if acc is None:
                return
            user = event.user
            if user is None or user.id != acc.owner_tg_id:
                return
            try:
                await acc.handle_tg_reaction(
                    event.message_id, event.new_reaction
                )
            except Exception:
                logger.debug("Ошибка реакции TG->MAX", exc_info=True)

        @dp.callback_query()
        async def on_callback(cb: CallbackQuery) -> None:
            data = cb.data or ""
            if data.startswith("approve:") or data.startswith("deny:"):
                await self._handle_approval(cb)
            else:
                await cb.answer()

        @dp.message()
        async def on_message(message: TgMessage) -> None:
            tg = message.from_user.id if message.from_user else None
            if tg is None:
                return
            # 1) шаги диалога онбординга
            conv = self.conv.get(tg)
            if conv is not None:
                if conv.step == "phone":
                    await self._on_phone(message)
                    return
                if (
                    conv.step in ("code", "password")
                    and conv.future is not None
                    and not conv.future.done()
                ):
                    conv.future.set_result((message.text or "").strip())
                    return
                if conv.step == "waiting":
                    await message.answer(
                        "⏳ Твоя заявка ещё на рассмотрении у администратора."
                    )
                    return
            # 2) сообщение в привязанной группе -> в MAX
            worker = self.by_group.get(message.chat.id)
            if worker is not None and tg == worker.owner_tg_id:
                await worker.handle_tg(message)
                return
            # 3) личка без активного диалога
            if message.chat.type == "private":
                await message.answer("Не понял. Команды: /add, /accounts, /help")

    async def _route_command(self, message: TgMessage, cmd: str) -> None:
        worker = self.by_group.get(message.chat.id)
        if worker is None or message.from_user.id != worker.owner_tg_id:
            await message.reply("Эта команда работает в группе твоего аккаунта.")
            return
        if cmd == "mute":
            await worker.toggle_mute(message, muted=True)
        elif cmd == "unmute":
            await worker.toggle_mute(message, muted=False)
        elif cmd == "muted":
            await worker.list_muted(message)

    # ── админ-панель ──────────────────────────────────────────────────────

    @staticmethod
    def _mask_phone(phone: str) -> str:
        return f"…{phone[-4:]}" if phone and len(phone) > 4 else (phone or "?")

    async def _admin_dashboard(self, message: TgMessage) -> None:
        accs = await self.registry.list_all()
        bans = await self.registry.list_bans()
        users = len({a["owner_tg_id"] for a in accs})
        online = sum(1 for a in accs if a["id"] in self.workers)
        status: dict[str, int] = {}
        for a in accs:
            status[a["status"]] = status.get(a["status"], 0) + 1
        st = ", ".join(f"{k}: {v}" for k, v in sorted(status.items())) or "—"
        await message.answer(
            "👑 Админ-панель\n"
            f"Пользователей: {users}\n"
            f"Аккаунтов: {len(accs)} (🟢 онлайн {online})\n"
            f"Статусы: {st}\n"
            f"Забанено: {len(bans)}\n\n"
            "Подробно: /admin list | /admin user <tg_id>\n"
            "Управление: /admin stop N | start N | remove N\n"
            "Доступ: /ban <tg_id> | /unban <tg_id> | /banned"
        )

    async def _admin_list(self, message: TgMessage, owner: int | None = None) -> None:
        accs = await self.registry.list_all()
        if owner is not None:
            accs = [a for a in accs if a["owner_tg_id"] == owner]
        if not accs:
            await message.answer("Аккаунтов нет.")
            return
        lines = []
        for a in accs[:60]:
            dot = "🟢" if a["id"] in self.workers else "⚪️"
            grp = "✅" if a["group_id"] else "⚠️"
            lines.append(
                f"{dot} #{a['id']} «{a['name']}» {self._mask_phone(a['phone'])} "
                f"— owner {a['owner_tg_id']} — {grp} — {a['status']}"
            )
        extra = f"\n… и ещё {len(accs) - 60}" if len(accs) > 60 else ""
        await message.answer("\n".join(lines) + extra)

    async def _admin_action(
        self, message: TgMessage, action: str, account_id: int
    ) -> None:
        acc = await self.registry.get(account_id)
        if acc is None:
            await message.reply(f"Аккаунта #{account_id} нет.")
            return
        if action == "stop":
            await self._cleanup_account(account_id, delete=False)
            await self.registry.set_status(account_id, "stopped")
            await message.reply(f"⏹ Аккаунт #{account_id} остановлен.")
        elif action == "start":
            await self._restart_account(account_id, announce=True)
            await message.reply(f"▶️ Аккаунт #{account_id} запускается…")
        elif action == "remove":
            await self._cleanup_account(account_id, delete=True)
            await message.reply(f"🗑 Аккаунт #{account_id} удалён.")

    # ── бэкап ─────────────────────────────────────────────────────────────

    async def _backup_now(self) -> str:
        return await asyncio.to_thread(
            _build_backup,
            self.config.work_dir,
            self.config.backup_dir,
            self.config.backup_keep,
            self.config.backup_passphrase,
        )

    async def _backup_loop(self) -> None:
        hours = self.config.backup_interval
        if hours <= 0:
            return
        while True:
            await asyncio.sleep(hours * 3600)
            try:
                path = await self._backup_now()
                logger.info("Авто-бэкап создан: %s", path)
            except Exception:
                logger.exception("Авто-бэкап не удался")

    async def _on_phone(self, message: TgMessage) -> None:
        tg = message.from_user.id
        phone = (message.text or "").strip().replace(" ", "")
        if not PHONE_RE.match(phone):
            await message.reply(
                "Не похоже на номер. Формат: +79991234567. Попробуй ещё раз "
                "или /add заново."
            )
            return
        # Один номер — один раз: два userbot'а на один MAX-аккаунт ведут к
        # сбросу сессий со стороны MAX (антифрод «два устройства»).
        existing = await self.registry.get_by_phone(phone)
        if existing is not None:
            self.conv.pop(tg, None)
            if existing["owner_tg_id"] == tg:
                await message.reply(
                    f"У тебя уже есть аккаунт с этим номером — "
                    f"#{existing['id']} «{existing['name']}». "
                    f"Удалить: /remove {existing['id']}"
                )
            else:
                await message.reply(
                    "Этот номер уже подключён к боту. Один номер — один раз."
                )
            return
        # Админ добавляет себе — без одобрения.
        if tg in self.admin_ids:
            await self._begin_login(tg, phone)
            return
        if not self.admin_ids:
            self.conv.pop(tg, None)
            await message.reply(
                "Регистрация сейчас недоступна — не настроен администратор."
            )
            return

        # Иначе — заявка на одобрение админу.
        self._req_counter += 1
        req_id = self._req_counter
        self.pending_reqs[req_id] = {"requester": tg, "phone": phone}
        self.conv[tg] = Conv(step="waiting")
        await message.answer(
            "⏳ Заявка отправлена администратору. Как одобрит — пришлю запрос "
            "кода из SMS."
        )
        u = message.from_user
        who = f"@{u.username}" if u.username else (u.full_name or f"id {tg}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Одобрить", callback_data=f"approve:{req_id}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить", callback_data=f"deny:{req_id}"
            ),
        ]])
        for admin in self.admin_ids:
            try:
                await self.bot.send_message(
                    admin,
                    "🆕 Запрос на добавление MAX-аккаунта\n"
                    f"От: {who} (tg id {tg})\n"
                    f"Телефон: {phone}",
                    reply_markup=kb,
                )
            except Exception:
                logger.debug("Не доставить заявку админу %s", admin)

    async def _begin_login(self, requester_tg: int, phone: str) -> None:
        """Создаёт аккаунт и запускает вход (после одобрения или для админа)."""
        name = f"MAX …{phone[-4:]}"
        account_id = await self.registry.add(
            requester_tg, name, phone, status="login"
        )
        self.conv[requester_tg] = Conv(step="login", account_id=account_id)
        self.pending_announce.add(account_id)
        await self.bot.send_message(
            requester_tg,
            "📲 Запускаю вход в MAX. Сейчас придёт SMS — пришли мне код сюда.",
        )
        await self._start_account(account_id)

    async def _handle_approval(self, cb: CallbackQuery) -> None:
        if cb.from_user.id not in self.admin_ids:
            await cb.answer("Это не для тебя.", show_alert=True)
            return
        action, _, rid = (cb.data or "").partition(":")
        req = self.pending_reqs.pop(int(rid), None) if rid.isdigit() else None
        if req is None:
            await cb.answer("Заявка уже обработана или устарела.")
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        requester = req["requester"]
        phone = req["phone"]
        base = cb.message.text or "Заявка"
        if action == "approve":
            try:
                await cb.message.edit_text(base + "\n\n✅ Одобрено")
            except Exception:
                pass
            await cb.answer("Одобрено")
            await self._begin_login(requester, phone)
        else:
            self.conv.pop(requester, None)
            try:
                await cb.message.edit_text(base + "\n\n❌ Отклонено")
            except Exception:
                pass
            await cb.answer("Отклонено")
            try:
                await self.bot.send_message(
                    requester, "❌ Заявка на добавление аккаунта отклонена."
                )
            except Exception:
                pass

    # ── запуск ────────────────────────────────────────────────────────────

    async def _migrate_legacy(self) -> None:
        """Импорт старого single-аккаунта из .env, если реестр пуст."""
        if await self.registry.list_all():
            return
        c = self.config
        if not (c.legacy_phone and c.legacy_owner_id):
            return
        session_path = os.path.join(c.work_dir, c.legacy_session)
        if not os.path.exists(session_path):
            return
        await self.registry.add(
            c.legacy_owner_id, "default", c.legacy_phone,
            group_id=c.legacy_group_id, session=c.legacy_session,
            mapping_db=c.legacy_mapping_db, proxy=c.legacy_proxy,
            status="active",
        )
        logger.info("Импортирован аккаунт из .env (single -> мультитенант).")

    async def run(self) -> None:
        self.http = aiohttp.ClientSession()
        self.registry = await Registry.create(self.config.registry_db)
        await self._migrate_legacy()

        for acc in await self.registry.list_all():
            await self._start_account(acc["id"])
        logger.info("Поднято аккаунтов: %d", len(self.workers))

        backup_task = asyncio.create_task(self._backup_loop(), name="backup")

        try:
            # allowed_updates с message_reaction — иначе Telegram не шлёт
            # события реакций (нужны для TG -> MAX).
            await self.dp.start_polling(
                self.bot,
                allowed_updates=self.dp.resolve_used_update_types(),
            )
        finally:
            backup_task.cancel()
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(
                backup_task, *self.tasks.values(), return_exceptions=True
            )
            await self.http.close()
            for worker in self.workers.values():
                try:
                    await worker.storage.close()
                except Exception:
                    pass
            await self.registry.close()
            await self.bot.session.close()


async def main() -> None:
    config = Config.load()
    logger.info("Запуск мультитенантного моста MAX <-> Telegram…")
    manager = Manager(config)
    await manager.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
