"""Маршрутизация ответов: какое сообщение в Telegram какому чату MAX соответствует.

Когда мост пересылает сообщение из MAX в Telegram, он запоминает связь
``(telegram_chat_id, telegram_message_id) -> (max_chat_id, max_message_id)``.
Когда ты отвечаешь реплаем на это сообщение в Telegram, мост находит исходный
чат MAX (чтобы отправить ответ) и id исходного сообщения (чтобы отметить его
прочитанным).
"""

from __future__ import annotations

import aiosqlite


class Storage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def create(cls, db_path: str) -> "Storage":
        self = cls(db_path)
        self._db = await aiosqlite.connect(db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                tg_chat_id     INTEGER NOT NULL,
                tg_message_id  INTEGER NOT NULL,
                max_chat_id    INTEGER NOT NULL,
                max_message_id INTEGER,
                PRIMARY KEY (tg_chat_id, tg_message_id)
            )
            """
        )
        await self._db.commit()
        return self

    async def remember(
        self,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
        max_message_id: int | None,
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO mappings "
            "(tg_chat_id, tg_message_id, max_chat_id, max_message_id) "
            "VALUES (?, ?, ?, ?)",
            (tg_chat_id, tg_message_id, max_chat_id, max_message_id),
        )
        await self._db.commit()

    async def resolve(
        self, tg_chat_id: int, tg_message_id: int
    ) -> tuple[int, int | None] | None:
        """Возвращает ``(max_chat_id, max_message_id)`` или ``None``."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT max_chat_id, max_message_id FROM mappings "
            "WHERE tg_chat_id = ? AND tg_message_id = ?",
            (tg_chat_id, tg_message_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return int(row[0]), (int(row[1]) if row[1] is not None else None)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
