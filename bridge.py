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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
)
from aiogram.types import Message as TgMessage

from pymax import Client, File, Message, Photo, Video
from pymax.types.domain import (
    AudioAttachment,
    FileAttachment,
    PhotoAttachment,
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
        self.client = Client(
            phone=config.max_phone,
            work_dir=config.work_dir,
            session_name=config.max_session,
        )
        self.storage: Storage | None = None
        self.http: aiohttp.ClientSession | None = None

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

    def _user_name(self, user_id: int) -> str:
        for user in self.client.contacts or []:
            if user is not None and user.id == user_id and user.names:
                n = user.names[0]
                label = n.name or " ".join(
                    p for p in (n.first_name, n.last_name) if p
                )
                if label:
                    return label
        return f"ID {user_id}"

    def _chat_label(self, message: Message) -> str:
        """Заголовок: для диалога — имя собеседника, для группы — её название."""
        chat = None
        for c in self.client.chats or []:
            if c.id == message.chat_id:
                chat = c
                break
        if chat is not None and chat.type != ChatType.DIALOG and chat.title:
            sender = (
                self._user_name(message.sender) if message.sender else "?"
            )
            return f"{chat.title} · {sender}"
        # Диалог: показываем собеседника.
        if message.sender is not None:
            return self._user_name(message.sender)
        return f"чат {message.chat_id}"

    def _is_group(self, chat_id: int | None) -> bool:
        for c in self.client.chats or []:
            if c.id == chat_id:
                return c.type != ChatType.DIALOG
        return False

    def _chat_title_by_id(self, chat_id: int) -> str:
        """Человекочитаемое имя чата MAX по его id."""
        for c in self.client.chats or []:
            if c.id == chat_id:
                if c.type != ChatType.DIALOG and c.title:
                    return c.title
                # Личный диалог: показываем собеседника.
                for uid in c.participants or {}:
                    if uid != self._my_id():
                        return self._user_name(uid)
        return f"чат {chat_id}"

    # ── MAX -> Telegram ───────────────────────────────────────────────────

    def _register_max_handlers(self) -> None:
        @self.client.on_message()
        async def on_max_message(message: Message, client: Client) -> None:
            try:
                await self._forward_to_telegram(message)
            except Exception:
                logger.exception("Ошибка при пересылке MAX -> Telegram")

    async def _forward_to_telegram(self, message: Message) -> None:
        owner = self.config.telegram_owner_id
        if owner is None:
            logger.warning(
                "TELEGRAM_OWNER_ID не задан — некуда пересылать. "
                "Напиши боту /id, чтобы узнать свой id."
            )
            return

        # Не пересылаем собственные исходящие (иначе эхо-петля).
        if message.sender is not None and message.sender == self._my_id():
            return

        if message.chat_id is None:
            return

        if self._is_group(message.chat_id) and not self.config.forward_groups:
            return

        # Тихая доставка для заглушённых чатов (см. команды /mute, /unmute).
        assert self.storage is not None
        silent = await self.storage.is_muted(message.chat_id)

        header = self._chat_label(message)
        body = f"💬 {header}"
        if message.text:
            body += f"\n{message.text}"

        # Показываем активность, пока качаем и отправляем вложения.
        await self.bot.send_chat_action(owner, "typing")

        media = await self._collect_incoming_media(message)
        sent_ids: list[int] = []

        # Подпись вешаем на первое отправленное; если она длиннее лимита —
        # отправляем её отдельным текстовым сообщением.
        caption: str | None = body
        caption_used = False
        if len(body) > TG_CAPTION_LIMIT:
            sent = await self.bot.send_message(
                owner, body, disable_notification=silent
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
                        owner, file, caption=cap, disable_notification=silent
                    )
                else:
                    sent = await self.bot.send_video(
                        owner, file, caption=cap, disable_notification=silent
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
                    owner, group, disable_notification=silent
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
                        owner, cap, disable_notification=silent
                    )
                    sent_ids.append(pre.message_id)
                try:
                    sent = await self.bot.send_sticker(
                        owner, file, disable_notification=silent
                    )
                except Exception:
                    sent = await self.bot.send_document(
                        owner, file, disable_notification=silent
                    )
            elif kind == "audio":
                sent = await self.bot.send_audio(
                    owner, file, caption=cap, disable_notification=silent
                )
            else:
                sent = await self.bot.send_document(
                    owner, file, caption=cap, disable_notification=silent
                )
            sent_ids.append(sent.message_id)
            caption_used = True

        if not sent_ids:
            sent = await self.bot.send_message(
                owner, body, disable_notification=silent
            )
            sent_ids.append(sent.message_id)

        # Запоминаем связь: ответ реплаем на любое из этих сообщений уйдёт в
        # этот чат MAX (и отметит исходное сообщение прочитанным).
        assert self.storage is not None
        for mid in sent_ids:
            await self.storage.remember(
                owner, mid, message.chat_id, message.id
            )

    async def _collect_incoming_media(
        self, message: Message
    ) -> list[tuple[str, str, bytes]]:
        """Скачивает вложения MAX. Возвращает список (kind, filename, bytes).

        kind ∈ {photo, video, document, sticker, audio}.
        """
        result: list[tuple[str, str, bytes]] = []
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

                elif isinstance(attach, StickerAttachment):
                    data = await self._download(attach.url)
                    if data:
                        result.append(("sticker", "sticker.webp", data))

                elif isinstance(attach, AudioAttachment):
                    if attach.url:
                        data = await self._download(attach.url)
                        if data:
                            result.append(("audio", "audio.mp3", data))
            except Exception:
                logger.exception("Не удалось скачать вложение из MAX")
        return result

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
            await message.answer(
                "Мост MAX ↔ Telegram запущен.\n\n"
                f"Твой Telegram id: `{message.from_user.id}`\n"
                "Впиши его в TELEGRAM_OWNER_ID в .env и перезапусти мост.\n\n"
                "*Ответить в MAX* — reply на пересланное сообщение.\n"
                "*Заглушить чат* — reply + /mute (звук вернёт /unmute).\n"
                "*Список заглушённых* — /muted.",
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

        @self.dp.message(F.reply_to_message)
        async def on_reply(message: TgMessage) -> None:
            if not self._is_owner(message):
                return
            try:
                await self._send_to_max(message)
            except Exception:
                logger.exception("Ошибка при отправке Telegram -> MAX")
                await message.reply("⚠️ Не удалось отправить в MAX (см. логи).")

        @self.dp.message()
        async def on_plain(message: TgMessage) -> None:
            if not self._is_owner(message):
                return
            await message.reply(
                "Чтобы ответить в MAX, сделай *reply* на пересланное "
                "сообщение.",
                parse_mode="Markdown",
            )

    def _is_owner(self, message: TgMessage) -> bool:
        owner = self.config.telegram_owner_id
        if owner is None:
            return True  # до настройки owner_id принимаем всех (для /id)
        return message.from_user is not None and message.from_user.id == owner

    async def _toggle_mute(self, message: TgMessage, *, muted: bool) -> None:
        assert self.storage is not None
        if message.reply_to_message is None:
            await message.reply(
                "Сделай эту команду *реплаем* на пересланное сообщение из "
                "того чата, который хочешь "
                + ("заглушить." if muted else "вернуть со звуком."),
                parse_mode="Markdown",
            )
            return

        resolved = await self.storage.resolve(
            message.chat.id, message.reply_to_message.message_id
        )
        if resolved is None:
            await message.reply(
                "Не понял, какой это чат MAX — отвечай реплаем именно на "
                "пересланное мной сообщение."
            )
            return

        max_chat_id, _ = resolved
        await self.storage.set_muted(max_chat_id, muted)
        title = self._chat_title_by_id(max_chat_id)
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
        lines = "\n".join(f"• {self._chat_title_by_id(cid)}" for cid in ids)
        await message.reply(f"🔕 Заглушённые чаты:\n{lines}")

    async def _send_to_max(self, message: TgMessage) -> None:
        assert self.storage is not None
        resolved = await self.storage.resolve(
            message.chat.id, message.reply_to_message.message_id
        )
        if resolved is None:
            await message.reply(
                "Не знаю, в какой чат MAX это отправить — отвечай реплаем "
                "именно на пересланное мной сообщение."
            )
            return

        max_chat_id, max_message_id = resolved
        text = message.text or message.caption or ""
        attachments = await self._collect_outgoing_media(message)

        await self.client.send_message(
            chat_id=max_chat_id,
            text=text,
            attachments=attachments or None,
        )

        # Отмечаем исходное сообщение в MAX прочитанным.
        if self.config.mark_read and max_message_id is not None:
            try:
                await self.client.read_message(
                    message_id=max_message_id, chat_id=max_chat_id
                )
            except Exception:
                logger.exception("Не удалось отметить сообщение прочитанным")

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
