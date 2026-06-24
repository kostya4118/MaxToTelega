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
import logging
import os
import re
from dataclasses import dataclass

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    ReactionTypeEmoji,
)
from aiogram.types import Message as TgMessage

from pymax import ApiError, Client, ExtraConfig, File, Message, Photo, Video
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
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

if _LOG_LEVEL != "DEBUG":
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("pymax").setLevel(logging.WARNING)

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
        _fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_fh)
    except Exception:
        logger.warning("Не удалось настроить файловый лог %s", _LOG_FILE)

TG_CAPTION_LIMIT = 1024
PHONE_RE = re.compile(r"^\+\d{7,15}$")
AUTH_TIMEOUT = 300  # сек на ввод кода/пароля


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

        dest = self.group_id
        chat = await self._get_chat(message.chat_id)
        is_group = chat is not None and chat.type != ChatType.DIALOG
        if is_group and not self.forward_groups:
            return

        thread = await self._ensure_thread(message.chat_id, chat)
        if thread is None:
            return
        await self._maybe_rename_topic(message.chat_id, thread)

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

        media, notes = await self._collect_incoming_media(message)
        if not message.text and notes:
            parts.extend(notes)
        body = "\n".join(parts)
        sent_ids: list[int] = []

        caption: str | None = body or None
        caption_used = False
        if body and len(body) > TG_CAPTION_LIMIT:
            sent = await self.bot.send_message(
                dest, body, message_thread_id=thread,
                disable_notification=silent,
            )
            sent_ids.append(sent.message_id)
            caption = None
            caption_used = True

        album = [m for m in media if m[0] in ("photo", "video")]
        others = [m for m in media if m[0] not in ("photo", "video")]

        if album:
            cap = None if caption_used else caption
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
                sent_ids.append(sent.message_id)
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
                sent_ids.extend(m.message_id for m in msgs)
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
                    sent_ids.append(pre.message_id)
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
            elif kind == "audio":
                sent = await self.bot.send_audio(
                    dest, file, caption=cap, message_thread_id=thread,
                    disable_notification=silent,
                )
            else:
                sent = await self.bot.send_document(
                    dest, file, caption=cap, message_thread_id=thread,
                    disable_notification=silent,
                )
            sent_ids.append(sent.message_id)
            caption_used = True

        if not sent_ids:
            sent = await self.bot.send_message(
                dest, body or "📭 (пустое сообщение)",
                message_thread_id=thread, disable_notification=silent,
            )
            sent_ids.append(sent.message_id)

        await self.storage.set_last_message(message.chat_id, message.id)

    async def _collect_incoming_media(
        self, message: Message
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        result: list[tuple[str, str, bytes]] = []
        notes: list[str] = []
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
                    notes.append("👤 контакт")
                elif isinstance(attach, ShareAttachment):
                    notes.append("🔗 ссылка / репост")
                elif isinstance(attach, CallAttachment):
                    notes.append("📞 звонок")
                elif isinstance(attach, ControlAttachment):
                    notes.append("ℹ️ системное сообщение")
                else:
                    type_name = getattr(
                        getattr(attach, "type", None), "value", "вложение"
                    )
                    notes.append(f"📎 {type_name}")
            except Exception:
                logger.exception("Не удалось обработать вложение из MAX")
                notes.append("📎 вложение (ошибка обработки)")
        return result, notes

    async def _download(self, url: str) -> bytes | None:
        async with self.http.get(url) as resp:
            if resp.status != 200:
                logger.warning("Скачивание %s -> HTTP %s", url, resp.status)
                return None
            return await resp.read()

    # ── Telegram -> MAX ───────────────────────────────────────────────────

    async def handle_tg(self, message: TgMessage) -> None:
        if message.message_thread_id is None:
            return  # General/без темы
        try:
            await self._send_to_max(message)
        except Exception:
            logger.exception("[%s] Ошибка отправки TG->MAX", self.name)
            await message.reply("⚠️ Не удалось отправить в MAX (см. логи).")

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
            await self.client.send_message(
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
        try:
            await self.bot.set_message_reaction(
                message.chat.id, message.message_id,
                [ReactionTypeEmoji(emoji="👍")],
            )
        except Exception:
            logger.debug("Не удалось поставить реакцию", exc_info=True)

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
        self._register_handlers()

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
        return worker

    async def _on_account_started(self, account_id: int) -> None:
        worker = self.workers.get(account_id)
        if worker is None:
            return
        await self.registry.set_status(account_id, "active")
        if account_id in self.pending_announce:
            self.pending_announce.discard(account_id)
            self.conv.pop(worker.owner_tg_id, None)
            await self.bot.send_message(
                worker.owner_tg_id,
                f"✅ Аккаунт «{worker.name}» вошёл в MAX!\n\n"
                "Теперь создай группу-форум, добавь меня админом с правом "
                "«Управление темами», и напиши в группе /bind — привяжу её к "
                "этому аккаунту.",
            )

    def _on_client_done(self, account_id: int, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        asyncio.create_task(self._account_stopped(account_id, exc))

    async def _account_stopped(self, account_id: int, exc) -> None:
        worker = self.workers.get(account_id)
        owner = worker.owner_tg_id if worker else None
        name = worker.name if worker else f"MAX {account_id}"
        if account_id in self.pending_announce:
            # упал во время онбординга — откатываем
            self.pending_announce.discard(account_id)
            if owner:
                self.conv.pop(owner, None)
                try:
                    await self.bot.send_message(
                        owner, f"❌ Вход не удался: {exc}. Попробуй заново: /add"
                    )
                except Exception:
                    pass
            await self._cleanup_account(account_id, delete=True)
        else:
            if exc is not None:
                logger.error("Аккаунт '%s' остановился: %r", name, exc)
                if owner:
                    try:
                        await self.bot.send_message(
                            owner,
                            f"⚠️ Аккаунт «{name}» остановился. Если MAX "
                            "разлогинил сессию — добавь заново через /add.",
                        )
                    except Exception:
                        pass
            await self._cleanup_account(account_id, delete=False)

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
                "*/add* — добавить MAX-аккаунт (спрошу телефон и код из SMS)\n"
                "*/accounts* — твои аккаунты\n"
                "*/remove N* — удалить аккаунт\n\n"
                "У каждого аккаунта — своя группа-форум: после /add создаёшь "
                "группу, добавляешь меня админом и пишешь там /bind.",
                parse_mode="Markdown",
            )

        @dp.message(Command("add"))
        async def cmd_add(message: TgMessage) -> None:
            if message.chat.type != "private":
                await message.reply("Добавляй аккаунт в личке со мной.")
                return
            self.conv[message.from_user.id] = Conv(step="phone")
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

        try:
            await self.dp.start_polling(self.bot)
        finally:
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
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
