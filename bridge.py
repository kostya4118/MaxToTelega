"""Мост MAX <-> Telegram (мультиаккаунт).

Левое плечо (MAX -> Telegram): на каждый MAX-аккаунт — свой userbot (PyMax),
который пересылает входящие в Telegram. В режиме тем каждый чат MAX попадает
в отдельную тему группы-форума этого аккаунта.

Правое плечо (Telegram -> MAX): пишешь в теме (или реплаем в личке) — мост по
группе определяет аккаунт, по теме — чат MAX, и отправляет туда.

Один общий Telegram-бот обслуживает все аккаунты; у каждого аккаунта своя
группа. Маршрутизация — по id группы, в которую пришло сообщение.

Запуск:  python bridge.py
Первый запуск каждого аккаунта спросит SMS-код MAX в консоли.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
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

from config import AccountConfig, Config
from storage import Storage

# Уровень логов берём из LOG_LEVEL (INFO по умолчанию). config импортируется
# выше и уже подгрузил .env, поэтому переменная окружения здесь доступна.
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# Болтливые библиотеки приглушаем до предупреждений (если не включён DEBUG).
if _LOG_LEVEL != "DEBUG":
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("pymax").setLevel(logging.WARNING)

# Автоочистка логов: файл с ротацией по размеру, старые части удаляются сами.
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
        _file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_max_bytes,
            backupCount=int(os.getenv("LOG_BACKUPS", "3")),
            encoding="utf-8",
        )
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    except Exception:
        logger.warning(
            "Не удалось настроить файловый лог %s", _LOG_FILE, exc_info=True
        )

# Telegram-лимит на длину подписи к медиа.
TG_CAPTION_LIMIT = 1024


class Account:
    """Один MAX-аккаунт: свой userbot, своя сессия, своя база, своя группа.

    Общий Telegram-бот и владелец инжектятся снаружи (из MultiBridge).
    """

    def __init__(
        self,
        bot: Bot,
        acc: AccountConfig,
        owner_id: int | None,
        work_dir: str,
    ) -> None:
        self.bot = bot
        self.acc = acc
        self.name = acc.name
        self.owner_id = owner_id
        self.group_id = acc.telegram_group_id
        self.topic_mode = self.group_id is not None
        self.forward_groups = acc.forward_groups

        extra = (
            ExtraConfig(proxy=acc.max_proxy) if acc.max_proxy else None
        )
        if acc.max_proxy:
            logger.info("[%s] MAX подключается через прокси", self.name)
        self.client = Client(
            phone=acc.max_phone,
            work_dir=work_dir,
            session_name=acc.max_session,
            extra_config=extra,
        )

        self.storage: Storage | None = None
        self.http: aiohttp.ClientSession | None = None
        self._chat_cache: dict[int, object] = {}
        self._topic_lock = asyncio.Lock()

    async def init(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.storage = await Storage.create(self.acc.mapping_db)
        self._register_max_handler()

    def client_coro(self):
        return self.client.start()

    # ── Вспомогательное: имена ────────────────────────────────────────────

    def _my_id(self) -> int | None:
        return self.client.me.contact.id if self.client.me else None

    def _max_online(self) -> bool:
        """MAX-клиент залогинен и готов отправлять (не в процессе reconnect)."""
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
        """Лучшее опознание: имя → @username → телефон → ID."""
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
        """Опознание пользователя MAX по id: из кеша, иначе тянем с сервера."""
        user = self.client.get_cached_user(user_id)
        if user is None:
            try:
                user = await self.client.get_user(user_id)
            except Exception:
                logger.debug("get_user(%s) не удался", user_id, exc_info=True)
                user = None
        return self._label_for(user, user_id)

    async def _get_chat(self, chat_id: int):
        """Чат MAX по id: из синка/кеша, иначе дозагружаем с сервера."""
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

    async def _chat_label(self, message: Message, chat=None) -> str:
        """Заголовок: для диалога — имя собеседника, для группы — её название."""
        if chat is None and message.chat_id is not None:
            chat = await self._get_chat(message.chat_id)
        if chat is not None and chat.type != ChatType.DIALOG:
            title = chat.title or "группа"
            sender = (
                await self._user_name(message.sender)
                if message.sender
                else "?"
            )
            return f"{title} · {sender}"
        if message.sender is not None:
            return await self._user_name(message.sender)
        return f"чат {message.chat_id}"

    async def _chat_title_by_id(self, chat_id: int) -> str:
        """Человекочитаемое имя чата MAX по его id."""
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
        # Часто это чат с ботом MAX — у ботов нет контактного профиля.
        if getattr(chat, "has_bots", False):
            return f"🤖 бот {chat_id}"
        return f"чат {chat_id}"

    @staticmethod
    def _is_fallback_name(name: str) -> bool:
        return name.startswith("ID ") or name.startswith("чат ")

    # ── Темы ──────────────────────────────────────────────────────────────

    async def _ensure_thread(self, max_chat_id: int, chat) -> int | None:
        """thread_id темы для чата MAX, создаётся при первом сообщении."""
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
                    "[%s] Не удалось создать тему для чата MAX %s. Бот точно "
                    "админ группы с правом «Управление темами», и Темы включены?",
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
        """Переименовывает тему, когда у чата появилось нормальное имя."""
        current = await self.storage.get_topic_title(max_chat_id)
        if current and not self._is_fallback_name(current):
            return
        desired = (await self._chat_title_by_id(max_chat_id))[:128]
        if desired == current or self._is_fallback_name(desired):
            return
        try:
            await self.bot.edit_forum_topic(
                self.group_id, thread, name=desired
            )
            await self.storage.set_topic_title(max_chat_id, desired)
            logger.info(
                "[%s] Тема переименована %r -> %r (чат MAX %s)",
                self.name, current, desired, max_chat_id,
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
                logger.exception(
                    "[%s] Ошибка при пересылке MAX -> Telegram", self.name
                )

    async def _forward_to_telegram(self, message: Message) -> None:
        if message.sender is not None and message.sender == self._my_id():
            return
        if message.chat_id is None:
            return

        if self.topic_mode:
            dest = self.group_id
        else:
            dest = self.owner_id
            if dest is None:
                logger.warning(
                    "[%s] TELEGRAM_OWNER_ID не задан — некуда пересылать.",
                    self.name,
                )
                return

        chat = await self._get_chat(message.chat_id)
        is_group = chat is not None and chat.type != ChatType.DIALOG
        if is_group and not self.forward_groups:
            return

        thread: int | None = None
        if self.topic_mode:
            thread = await self._ensure_thread(message.chat_id, chat)
            if thread is None:
                return
            await self._maybe_rename_topic(message.chat_id, thread)

        assert self.storage is not None
        silent = await self.storage.is_muted(message.chat_id)

        parts: list[str] = []
        if not self.topic_mode:
            parts.append(f"💬 {await self._chat_label(message, chat)}")
        elif is_group:
            sender = (
                await self._user_name(message.sender)
                if message.sender
                else "?"
            )
            parts.append(f"👤 {sender}")
        if message.text:
            parts.append(message.text)

        await self.bot.send_chat_action(
            dest, "typing", message_thread_id=thread
        )

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

        if self.topic_mode:
            await self.storage.set_last_message(message.chat_id, message.id)
        else:
            for mid in sent_ids:
                await self.storage.remember(
                    dest, mid, message.chat_id, message.id
                )

    async def _collect_incoming_media(
        self, message: Message
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """Разбирает вложения MAX -> (media, notes)."""
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
                            result.append(
                                ("document", attach.name or "file", data)
                            )
                    else:
                        notes.append(f"📎 файл: {attach.name or 'без имени'}")

                elif isinstance(attach, StickerAttachment):
                    data = await self._download(attach.url)
                    if data:
                        result.append(("sticker", "sticker.webp", data))
                    else:
                        notes.append("🩷 стикер")

                elif isinstance(attach, AudioAttachment):
                    if attach.url:
                        data = await self._download(attach.url)
                        if data:
                            result.append(("audio", "audio.mp3", data))
                        else:
                            notes.append("🎤 голосовое / аудио")
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
        assert self.http is not None
        async with self.http.get(url) as resp:
            if resp.status != 200:
                logger.warning("Скачивание %s -> HTTP %s", url, resp.status)
                return None
            return await resp.read()

    # ── Telegram -> MAX ───────────────────────────────────────────────────

    async def handle_tg(self, message: TgMessage) -> None:
        """Обрабатывает сообщение владельца, адресованное этому аккаунту."""
        if self.topic_mode:
            if message.message_thread_id is None:
                return  # General/без темы — игнорируем
        elif message.reply_to_message is None:
            await message.reply(
                "Чтобы ответить в MAX, сделай *reply* на пересланное "
                "сообщение.",
                parse_mode="Markdown",
            )
            return
        try:
            await self._send_to_max(message)
        except Exception:
            logger.exception(
                "[%s] Ошибка при отправке Telegram -> MAX", self.name
            )
            await message.reply("⚠️ Не удалось отправить в MAX (см. логи).")

    async def _target_for(
        self, message: TgMessage
    ) -> tuple[int | None, int | None]:
        """(max_chat_id, max_message_id) для входящего из Telegram сообщения."""
        assert self.storage is not None
        if self.topic_mode:
            thread = message.message_thread_id
            if thread is None:
                return None, None
            max_chat_id = await self.storage.chat_by_thread(thread)
            if max_chat_id is None:
                return None, None
            last = await self.storage.get_last_message(max_chat_id)
            return max_chat_id, last
        if message.reply_to_message is None:
            return None, None
        return await self.storage.resolve(
            message.chat.id, message.reply_to_message.message_id
        ) or (None, None)

    async def toggle_mute(self, message: TgMessage, *, muted: bool) -> None:
        assert self.storage is not None
        max_chat_id, _ = await self._target_for(message)
        if max_chat_id is None:
            where = (
                "внутри нужной темы."
                if self.topic_mode
                else "реплаем на пересланное сообщение из нужного чата."
            )
            await message.reply(f"Отправь эту команду {where}")
            return
        await self.storage.set_muted(max_chat_id, muted)
        title = await self._chat_title_by_id(max_chat_id)
        if muted:
            await message.reply(f"🔕 Чат «{title}» — теперь без звука.")
        else:
            await message.reply(f"🔔 Чат «{title}» — снова со звуком.")

    async def list_muted(self, message: TgMessage) -> None:
        assert self.storage is not None
        ids = await self.storage.list_muted()
        if not ids:
            await message.reply("Заглушённых чатов нет — все приходят со звуком.")
            return
        titles = [await self._chat_title_by_id(cid) for cid in ids]
        lines = "\n".join(f"• {t}" for t in titles)
        await message.reply(f"🔕 Заглушённые чаты:\n{lines}")

    async def _send_to_max(self, message: TgMessage) -> None:
        max_chat_id, _max_message_id = await self._target_for(message)
        if max_chat_id is None:
            if not self.topic_mode:
                await message.reply(
                    "Не знаю, в какой чат MAX это отправить — отвечай реплаем "
                    "именно на пересланное мной сообщение."
                )
            return

        # Пока MAX-клиент не залогинен — не отправляем (иначе обрыв соединения).
        if not self._max_online():
            await message.reply(
                "⏳ MAX ещё переподключается — повтори отправку через "
                "пару секунд."
            )
            return

        text = message.text or message.caption or ""
        attachments = await self._collect_outgoing_media(message)
        if not text and not attachments:
            return

        try:
            await self.client.send_message(
                chat_id=max_chat_id,
                text=text,
                attachments=attachments or None,
            )
        except ApiError as e:
            reason = (
                getattr(e, "localized_message", None)
                or getattr(e, "message", None)
                or str(e)
            )
            logger.warning(
                "[%s] MAX отклонил отправку в чат %s: %s",
                self.name, max_chat_id, reason,
            )
            await message.reply(f"⚠️ MAX не принял сообщение: {reason}")
            return

        # Отметку «прочитано» не вызываем — текущий MAX рвёт на неё соединение.
        if self.topic_mode:
            try:
                await self.bot.set_message_reaction(
                    message.chat.id,
                    message.message_id,
                    [ReactionTypeEmoji(emoji="👍")],
                )
            except Exception:
                logger.debug("Не удалось поставить реакцию", exc_info=True)
        else:
            await message.reply("✅ Отправлено в MAX")

    async def _collect_outgoing_media(
        self, message: TgMessage
    ) -> list[Photo | File | Video]:
        """Скачивает вложения из Telegram и оборачивает в типы PyMax."""
        attachments: list[Photo | File | Video] = []
        if message.photo:
            data = await self._tg_download(message.photo[-1])
            attachments.append(Photo(raw=data, name="photo.jpg"))
        if message.document:
            data = await self._tg_download(message.document)
            name = message.document.file_name or "file"
            attachments.append(File(raw=data, name=name))
        if message.video:
            data = await self._tg_download(message.video)
            name = message.video.file_name or "video.mp4"
            attachments.append(Video(raw=data, name=name))
        return attachments

    async def _tg_download(self, downloadable) -> bytes:
        buffer = await self.bot.download(downloadable)
        assert buffer is not None
        return buffer.read()


class MultiBridge:
    """Общий Telegram-бот, обслуживающий несколько MAX-аккаунтов."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(config.telegram_token)
        self.dp = Dispatcher()
        self.http: aiohttp.ClientSession | None = None
        self.accounts = [
            Account(self.bot, acc, config.telegram_owner_id, config.work_dir)
            for acc in config.accounts
        ]
        self.by_group = {
            a.group_id: a for a in self.accounts if a.group_id is not None
        }
        # Аккаунт без группы (режим лички) — только в single-режиме из .env.
        self.dm_account = next(
            (a for a in self.accounts if a.group_id is None), None
        )
        self._register_handlers()

    def _is_owner(self, message: TgMessage) -> bool:
        owner = self.config.telegram_owner_id
        if owner is None:
            return True
        return message.from_user is not None and message.from_user.id == owner

    def _route(self, message: TgMessage) -> Account | None:
        """Определяет аккаунт по группе (или личке для single-режима)."""
        acc = self.by_group.get(message.chat.id)
        if acc is not None:
            return acc
        if message.chat.type == "private" and self.dm_account is not None:
            return self.dm_account
        return None

    def _register_handlers(self) -> None:
        @self.dp.message(Command("start", "id"))
        async def cmd_start(message: TgMessage) -> None:
            ids = f"Твой Telegram id: `{message.from_user.id}`\n"
            if message.chat.type in ("group", "supergroup"):
                ids += f"ID этой группы: `{message.chat.id}`\n"
                acc = self.by_group.get(message.chat.id)
                if acc is not None:
                    ids += f"Аккаунт этой группы: *{acc.name}*\n"
            hint = (
                "Каждый MAX-аккаунт — своя группа-форум, каждый чат — своя "
                "тема. Пиши в нужной теме, чтобы ответить в тот чат MAX.\n"
                "Заглушить тему — /mute внутри неё (вернуть — /unmute), "
                "список — /muted."
            )
            await message.answer(
                "Мост MAX ↔ Telegram запущен.\n\n" + ids + "\n" + hint,
                parse_mode="Markdown",
            )

        @self.dp.message(Command("mute"))
        async def cmd_mute(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._route_command(message, "mute")

        @self.dp.message(Command("unmute"))
        async def cmd_unmute(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._route_command(message, "unmute")

        @self.dp.message(Command("muted"))
        async def cmd_muted(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._route_command(message, "muted")

        @self.dp.message()
        async def on_message(message: TgMessage) -> None:
            if not self._is_owner(message):
                return
            acc = self._route(message)
            if acc is None:
                if message.chat.type == "private":
                    await message.answer(
                        "Пиши в нужной теме группы своего аккаунта."
                    )
                return
            await acc.handle_tg(message)

    async def _route_command(self, message: TgMessage, cmd: str) -> None:
        acc = self._route(message)
        if acc is None:
            await message.reply(
                "Эту команду нужно отправить внутри темы нужного аккаунта."
            )
            return
        if cmd == "mute":
            await acc.toggle_mute(message, muted=True)
        elif cmd == "unmute":
            await acc.toggle_mute(message, muted=False)
        elif cmd == "muted":
            await acc.list_muted(message)

    async def run(self) -> None:
        self.http = aiohttp.ClientSession()
        for acc in self.accounts:
            await acc.init(self.http)
        client_tasks = [
            asyncio.create_task(acc.client_coro(), name=f"max:{acc.name}")
            for acc in self.accounts
        ]
        for t in client_tasks:
            t.add_done_callback(self._on_client_done)
        try:
            # Поллинг держит жизненный цикл и сам ловит SIGINT/SIGTERM.
            await self.dp.start_polling(self.bot)
        finally:
            for t in client_tasks:
                t.cancel()
            await asyncio.gather(*client_tasks, return_exceptions=True)
            await self.http.close()
            for acc in self.accounts:
                if acc.storage is not None:
                    await acc.storage.close()
            await self.bot.session.close()

    @staticmethod
    def _on_client_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Аккаунт '%s' остановился: %r", task.get_name(), exc)


async def main() -> None:
    config = Config.load()
    logger.info(
        "Запуск моста MAX <-> Telegram. Аккаунтов: %d (%s)",
        len(config.accounts),
        ", ".join(a.name for a in config.accounts),
    )
    bridge = MultiBridge(config)
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
