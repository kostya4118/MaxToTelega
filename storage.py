"""Маршрутизация ответов: какое сообщение в Telegram какому чату MAX соответствует.

Когда мост пересылает сообщение из MAX в Telegram, он запоминает связь
``(telegram_chat_id, telegram_message_id) -> max_chat_id``. Когда ты отвечаешь
реплаем на это сообщение в Telegram, мост находит исходный чат MAX и отправляет
туда ответ.
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
                tg_chat_id    INTEGER NOT NULL,
                tg_message_id INTEGER NOT NULL,
                max_chat_id   INTEGER NOT NULL,
                PRIMARY KEY (tg_chat_id, tg_message_id)
            )
            """
        )
        await self._db.commit()
        return self

    async def remember(
        self, tg_chat_id: int, tg_message_id: int, max_chat_id: int
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO mappings "
            "(tg_chat_id, tg_message_id, max_chat_id) VALUES (?, ?, ?)",
            (tg_chat_id, tg_message_id, max_chat_id),
        )
        await self._db.commit()

    async def resolve(
        self, tg_chat_id: int, tg_message_id: int
    ) -> int | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT max_chat_id FROM mappings "
            "WHERE tg_chat_id = ? AND tg_message_id = ?",
            (tg_chat_id, tg_message_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
