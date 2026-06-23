"""Мост MAX <-> Telegram.

Левое плечо (MAX -> Telegram): userbot на PyMax слушает входящие сообщения
твоего личного аккаунта MAX и пересылает их тебе в Telegram-бота, включая
фото, файлы и видео.

Правое плечо (Telegram -> MAX): ты отвечаешь *реплаем* на пересланное
сообщение в Telegram; мост находит исходный чат MAX и отправляет туда твой
ответ (текст и/или вложение).

Запуск:  python bridge.py
Первый запуск спросит SMS-код MAX в консоли, дальше сессия сохраняется.
"""

from __future__ import annotations

import asyncio
import logging

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

from config import Config
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# Telegram-лимит на длину подписи к медиа.
TG_CAPTION_LIMIT = 1024


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(config.telegram_token)
        self.dp = Dispatcher()
        # Прокси нужен, если сервер не в стране телефона — иначе MAX считает
        # вход подозрительным (антифрод). Прокси используется и для TCP-входа,
        # и для выгрузки вложений.
        extra = (
            ExtraConfig(proxy=config.max_proxy) if config.max_proxy else None
        )
        if config.max_proxy:
            logger.info("MAX подключается через прокси")
        self.client = Client(
            phone=config.max_phone,
            work_dir=config.work_dir,
            session_name=config.max_session,
            extra_config=extra,
        )
        self.storage: Storage | None = None
        self.http: aiohttp.ClientSession | None = None
        # Кэш чатов MAX по id (дозагружаем те, которых не было при логине).
        self._chat_cache: dict[int, object] = {}
        # Режим тем: каждый чат MAX — своя тема в группе-форуме.
        self.topic_mode = config.telegram_group_id is not None
        # Чтобы один раз залогировать сырой профиль/чат, когда имя не вышло.
        self._unnamed_logged: set[int] = set()
        self._diag_chats: set[int] = set()
        # Сериализуем создание тем, чтобы не наплодить дублей при «залпе».
        self._topic_lock = asyncio.Lock()

        self._register_max_handlers()
        self._register_tg_handlers()

    # ── Запуск ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.storage = await Storage.create(self.config.mapping_db)
        self.http = aiohttp.ClientSession()

        # Две независимые задачи: MAX-клиент (вечный) и Telegram-поллинг
        # (останавливается по SIGINT/SIGTERM — aiogram сам ставит обработчики).
        # Как только ЛЮБАЯ из них завершается (сигнал на остановку или падение
        # MAX-входа), гасим вторую и выходим — чтобы systemd не ждал SIGKILL.
        client_task = asyncio.create_task(self.client.start(), name="max")
        polling_task = asyncio.create_task(
            self.dp.start_polling(self.bot), name="telegram"
        )
        try:
            done, pending = await asyncio.wait(
                {client_task, polling_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.error(
                        "Задача %s упала: %r", task.get_name(), exc
                    )
        finally:
            await self.http.close()
            await self.storage.close()
            await self.bot.session.close()

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
                # Если это похоже на полный URL — как есть, иначе @username.
                return link if ("/" in link or "://" in link) else f"@{link}"
            if user.phone:
                return f"+{user.phone}"
        return f"ID {user_id}"

    async def _user_name(self, user_id: int) -> str:
        """Опознание пользователя MAX по id: из кеша, иначе тянем с сервера.

        Работает даже для тех, кого нет у тебя в контактах. Если имя в профиле
        не задано — показываем @username, затем телефон, затем id.
        """
        user = self.client.get_cached_user(user_id)
        if user is None:
            try:
                user = await self.client.get_user(user_id)
            except Exception:
                logger.debug("get_user(%s) не удался", user_id, exc_info=True)
                user = None
        label = self._label_for(user, user_id)
        # Диагностика: если имя не определилось — один раз покажем, что прислал
        # MAX, чтобы понять, из какого поля брать имя (или что его нет совсем).
        if label == f"ID {user_id}" and user_id not in self._unnamed_logged:
            self._unnamed_logged.add(user_id)
            if user is None:
                logger.info("DIAG профиль %s: get_user вернул None", user_id)
            else:
                try:
                    logger.info(
                        "DIAG профиль %s: %r", user_id, user.model_dump()
                    )
                except Exception:
                    logger.info("DIAG профиль %s: <не сериализуется>", user_id)
        return label

    async def _get_chat(self, chat_id: int):
        """Чат MAX по id: из синка/кеша, иначе дозагружаем с сервера.

        Нужно, потому что MAX на логине отдаёт не все чаты — для остальных
        иначе не знали бы ни тип (группа/диалог), ни название.
        """
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
        # Диалог: показываем собеседника.
        if message.sender is not None:
            return await self._user_name(message.sender)
        return f"чат {message.chat_id}"

    async def _chat_title_by_id(self, chat_id: int) -> str:
        """Человекочитаемое имя чата MAX по его id."""
        chat = await self._get_chat(chat_id)
        name = f"чат {chat_id}"
        if chat is not None:
            if chat.title:
                # Название группы — а иногда MAX так отдаёт и имя собеседника.
                name = chat.title
            else:
                # Личный диалог: показываем собеседника.
                for uid in chat.participants or {}:
                    if uid != self._my_id():
                        name = await self._user_name(uid)
                        break
        # Диагностика: имя не вышло — один раз покажем, что MAX дал про чат.
        if (
            self._is_fallback_name(name)
            and chat is not None
            and chat_id not in self._diag_chats
        ):
            self._diag_chats.add(chat_id)
            try:
                logger.info("DIAG чат %s: %r", chat_id, chat.model_dump())
            except Exception:
                logger.info("DIAG чат %s: <не сериализуется>", chat_id)
        return name

    async def _ensure_thread(self, max_chat_id: int, chat) -> int | None:
        """Возвращает thread_id темы для чата MAX, создавая её при первом разе."""
        existing = await self.storage.get_topic(max_chat_id)
        if existing is not None:
            return existing
        # Блокировка: при «залпе» сообщений из нового чата не создаём дублей.
        async with self._topic_lock:
            existing = await self.storage.get_topic(max_chat_id)
            if existing is not None:
                return existing
            name = (await self._chat_title_by_id(max_chat_id))[:128]
            try:
                topic = await self.bot.create_forum_topic(
                    self.config.telegram_group_id, name
                )
            except Exception:
                logger.exception(
                    "Не удалось создать тему для чата MAX %s. Бот точно админ "
                    "группы с правом «Управление темами», и в группе включены "
                    "Темы?",
                    max_chat_id,
                )
                return None
            await self.storage.set_topic(
                max_chat_id, topic.message_thread_id, name
            )
            logger.info(
                "Создана тема '%s' (thread=%s) для чата MAX %s",
                name, topic.message_thread_id, max_chat_id,
            )
            return topic.message_thread_id

    @staticmethod
    def _is_fallback_name(name: str) -> bool:
        """Имя-заглушка (когда настоящее имя ещё не определилось)."""
        return name.startswith("ID ") or name.startswith("чат ")

    async def _maybe_rename_topic(self, max_chat_id: int, thread: int) -> None:
        """Переименовывает тему, если у чата появилось нормальное имя.

        Темы, заведённые как «ID …»/«чат …» (имя тогда не определилось),
        получают настоящее название, когда MAX начинает его отдавать.
        """
        current = await self.storage.get_topic_title(max_chat_id)
        # Уже нормальное имя — не дёргаем API на каждом сообщении.
        if current and not self._is_fallback_name(current):
            return
        desired = (await self._chat_title_by_id(max_chat_id))[:128]
        if desired == current or self._is_fallback_name(desired):
            return
        try:
            await self.bot.edit_forum_topic(
                self.config.telegram_group_id, thread, name=desired
            )
            await self.storage.set_topic_title(max_chat_id, desired)
            logger.info(
                "Тема переименована %r -> %r (чат MAX %s)",
                current, desired, max_chat_id,
            )
        except Exception:
            logger.debug("Не удалось переименовать тему", exc_info=True)

    # ── MAX -> Telegram ───────────────────────────────────────────────────

    def _register_max_handlers(self) -> None:
        @self.client.on_message()
        async def on_max_message(message: Message, client: Client) -> None:
            try:
                await self._forward_to_telegram(message)
            except Exception:
                logger.exception("Ошибка при пересылке MAX -> Telegram")

    async def _forward_to_telegram(self, message: Message) -> None:
        # Не пересылаем собственные исходящие (иначе эхо-петля).
        if message.sender is not None and message.sender == self._my_id():
            return
        if message.chat_id is None:
            return

        # Куда слать: в группу-форум (режим тем) или в личку владельцу.
        if self.topic_mode:
            dest = self.config.telegram_group_id
        else:
            dest = self.config.telegram_owner_id
            if dest is None:
                logger.warning(
                    "TELEGRAM_OWNER_ID не задан — некуда пересылать. "
                    "Напиши боту /id, чтобы узнать свой id."
                )
                return

        chat = await self._get_chat(message.chat_id)
        is_group = chat is not None and chat.type != ChatType.DIALOG
        if is_group and not self.config.forward_groups:
            return

        # В режиме тем — отдельная тема под каждый чат MAX.
        thread: int | None = None
        if self.topic_mode:
            thread = await self._ensure_thread(message.chat_id, chat)
            if thread is None:
                return  # не удалось создать/найти тему (залогировано)
            await self._maybe_rename_topic(message.chat_id, thread)

        assert self.storage is not None
        silent = await self.storage.is_muted(message.chat_id)

        # Заголовок: в личке — «💬 чат», в теме-группе — «👤 автор»,
        # в теме-диалоге — без заголовка (тема и так названа по человеку).
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
        # Текста нет, но есть нерисуемые вложения — добавляем пометку, чтобы
        # сообщение не выглядело пустым.
        if not message.text and notes:
            parts.extend(notes)
        body = "\n".join(parts)
        sent_ids: list[int] = []

        # Подпись вешаем на первое отправленное; если она длиннее лимита —
        # отправляем её отдельным текстовым сообщением.
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

        # Фото и видео объединяем в альбом (media group).
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
                # У стикеров нет подписи — если она ещё не показана, шлём текст.
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

        # Сохраняем связь для ответов.
        if self.topic_mode:
            # Маршрут ответа — по теме; храним id последнего сообщения MAX
            # для отметки «прочитано».
            await self.storage.set_last_message(message.chat_id, message.id)
        else:
            for mid in sent_ids:
                await self.storage.remember(
                    dest, mid, message.chat_id, message.id
                )

    async def _collect_incoming_media(
        self, message: Message
    ) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """Разбирает вложения MAX.

        Возвращает ``(media, notes)``:
        - media — список ``(kind, filename, bytes)`` для отрисовки в Telegram
          (kind ∈ {photo, video, document, sticker, audio});
        - notes — текстовые пометки для вложений, которые мы не отрисовываем
          (голосовое, контакт, репост, системное и т.п.), чтобы сообщение не
          выглядело пустым.
        """
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
                    # Неизвестный тип — покажем хотя бы код типа для диагностики.
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

    def _register_tg_handlers(self) -> None:
        @self.dp.message(Command("start", "id"))
        async def cmd_start(message: TgMessage) -> None:
            if self.topic_mode:
                hint = (
                    "Режим тем: каждый чат MAX — отдельная тема в этой "
                    "группе. Пиши прямо в нужную тему — уйдёт в тот чат MAX.\n"
                    "Заглушить тему — /mute внутри неё (вернуть — /unmute)."
                )
            else:
                hint = (
                    "*Ответить в MAX* — reply на пересланное сообщение.\n"
                    "*Заглушить чат* — reply + /mute (вернуть — /unmute).\n"
                    "*Список заглушённых* — /muted."
                )
            ids = f"Твой Telegram id: `{message.from_user.id}`\n"
            if message.chat.type in ("group", "supergroup"):
                ids += f"ID этой группы: `{message.chat.id}`\n"
            await message.answer(
                "Мост MAX ↔ Telegram запущен.\n\n" + ids + "\n" + hint,
                parse_mode="Markdown",
            )

        @self.dp.message(Command("mute"))
        async def cmd_mute(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._toggle_mute(message, muted=True)

        @self.dp.message(Command("unmute"))
        async def cmd_unmute(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._toggle_mute(message, muted=False)

        @self.dp.message(Command("muted"))
        async def cmd_muted(message: TgMessage) -> None:
            if self._is_owner(message):
                await self._list_muted(message)

        @self.dp.message()
        async def on_message(message: TgMessage) -> None:
            if not self._is_owner(message):
                return
            # В режиме тем работаем только в нашей группе-форуме.
            if self.topic_mode and message.chat.id != self.config.telegram_group_id:
                await message.answer(
                    "Включён режим тем — пиши в нужную тему группы."
                )
                return
            if not self.topic_mode and message.reply_to_message is None:
                await message.reply(
                    "Чтобы ответить в MAX, сделай *reply* на пересланное "
                    "сообщение.",
                    parse_mode="Markdown",
                )
                return
            try:
                await self._send_to_max(message)
            except Exception:
                logger.exception("Ошибка при отправке Telegram -> MAX")
                await message.reply("⚠️ Не удалось отправить в MAX (см. логи).")

    def _is_owner(self, message: TgMessage) -> bool:
        owner = self.config.telegram_owner_id
        if owner is None:
            return True  # до настройки owner_id принимаем всех (для /id)
        return message.from_user is not None and message.from_user.id == owner

    async def _target_for(
        self, message: TgMessage
    ) -> tuple[int | None, int | None]:
        """(max_chat_id, max_message_id) для входящего из Telegram сообщения."""
        assert self.storage is not None
        if self.topic_mode:
            if message.chat.id != self.config.telegram_group_id:
                return None, None
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

    async def _toggle_mute(self, message: TgMessage, *, muted: bool) -> None:
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

    async def _list_muted(self, message: TgMessage) -> None:
        assert self.storage is not None
        ids = await self.storage.list_muted()
        if not ids:
            await message.reply("Заглушённых чатов нет — все приходят со звуком.")
            return
        titles = [await self._chat_title_by_id(cid) for cid in ids]
        lines = "\n".join(f"• {t}" for t in titles)
        await message.reply(f"🔕 Заглушённые чаты:\n{lines}")

    async def _send_to_max(self, message: TgMessage) -> None:
        max_chat_id, max_message_id = await self._target_for(message)
        if max_chat_id is None:
            if not self.topic_mode:
                await message.reply(
                    "Не знаю, в какой чат MAX это отправить — отвечай реплаем "
                    "именно на пересланное мной сообщение."
                )
            return

        # Пока MAX-клиент не залогинен (старт/переподключение), отправлять
        # нельзя: MAX ответит «Must be ONLINE session» и разорвёт соединение.
        if not self._max_online():
            await message.reply(
                "⏳ MAX ещё переподключается — повтори отправку через "
                "пару секунд."
            )
            return

        text = message.text or message.caption or ""
        attachments = await self._collect_outgoing_media(message)
        if not text and not attachments:
            return  # нечего отправлять (например, сервисное сообщение)

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
                "MAX отклонил отправку в чат %s: %s", max_chat_id, reason
            )
            await message.reply(f"⚠️ MAX не принял сообщение: {reason}")
            return

        # ВАЖНО: отметку «прочитано» (read_message) НЕ вызываем. В текущей
        # версии MAX этот запрос (CHAT_MARK) считается невалидным — id
        # сообщения уходит строкой, а сервер ждёт число — и MAX в ответ
        # РАЗРЫВАЕТ соединение. Это вызывало переподключение на каждый ответ.
        # Пока в PyMax/MAX это не починят, фича отключена ради стабильности.
        _ = max_message_id  # сознательно не используется

        # Подтверждение: в теме — реакцией (чтобы не сорить), в личке — текстом.
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


async def main() -> None:
    config = Config.load()
    bridge = Bridge(config)
    logger.info("Запуск моста MAX <-> Telegram…")
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
