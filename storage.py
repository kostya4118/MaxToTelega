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
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_prefs (
                max_chat_id INTEGER PRIMARY KEY,
                muted       INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                max_chat_id    INTEGER PRIMARY KEY,
                thread_id      INTEGER NOT NULL,
                last_max_message_id INTEGER,
                title          TEXT
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS msg_map (
                rowid_alias    INTEGER PRIMARY KEY AUTOINCREMENT,
                max_chat_id    INTEGER NOT NULL,
                max_message_id INTEGER NOT NULL,
                tg_chat_id     INTEGER NOT NULL,
                tg_message_id  INTEGER NOT NULL,
                role           TEXT NOT NULL,
                body           TEXT
            )
            """
        )
        try:
            await self._db.execute("ALTER TABLE msg_map ADD COLUMN body TEXT")
        except Exception:
            pass  # столбец уже есть
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_map_max "
            "ON msg_map (max_message_id)"
        )
        # Миграция для баз, созданных до появления столбца title.
        try:
            await self._db.execute("ALTER TABLE topics ADD COLUMN title TEXT")
        except Exception:
            pass  # столбец уже есть
        await self._db.commit()
        return self

    # ── Темы (forum topics) ──────────────────────────────────────────────

    async def get_topic(self, max_chat_id: int) -> int | None:
        """thread_id темы для чата MAX или None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT thread_id FROM topics WHERE max_chat_id = ?",
            (max_chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def set_topic(
        self, max_chat_id: int, thread_id: int, title: str | None = None
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO topics "
            "(max_chat_id, thread_id, last_max_message_id, title) "
            "VALUES (?, ?, (SELECT last_max_message_id FROM topics "
            "               WHERE max_chat_id = ?), ?)",
            (max_chat_id, thread_id, max_chat_id, title),
        )
        await self._db.commit()

    async def get_topic_title(self, max_chat_id: int) -> str | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT title FROM topics WHERE max_chat_id = ?",
            (max_chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def clear_topic(self, max_chat_id: int) -> None:
        """Удаляет привязку темы (например, тему удалили в Telegram)."""
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM topics WHERE max_chat_id = ?", (max_chat_id,)
        )
        await self._db.commit()

    async def set_topic_title(self, max_chat_id: int, title: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE topics SET title = ? WHERE max_chat_id = ?",
            (title, max_chat_id),
        )
        await self._db.commit()

    async def chat_by_thread(self, thread_id: int) -> int | None:
        """max_chat_id по thread_id темы или None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT max_chat_id FROM topics WHERE thread_id = ?",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def set_last_message(
        self, max_chat_id: int, max_message_id: int
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE topics SET last_max_message_id = ? WHERE max_chat_id = ?",
            (max_message_id, max_chat_id),
        )
        await self._db.commit()

    async def get_last_message(self, max_chat_id: int) -> int | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT last_max_message_id FROM topics WHERE max_chat_id = ?",
            (max_chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ── Настройки уведомлений по чатам MAX ───────────────────────────────

    async def set_muted(self, max_chat_id: int, muted: bool) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO chat_prefs (max_chat_id, muted) "
            "VALUES (?, ?)",
            (max_chat_id, 1 if muted else 0),
        )
        await self._db.commit()

    async def is_muted(self, max_chat_id: int) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT muted FROM chat_prefs WHERE max_chat_id = ?",
            (max_chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row[0]) if row else False

    async def list_muted(self) -> list[int]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT max_chat_id FROM chat_prefs WHERE muted = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]

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

    # ── Связь сообщений MAX -> Telegram (для правок и удалений) ──────────

    async def remember_msg(
        self,
        max_chat_id: int,
        max_message_id: int,
        tg_chat_id: int,
        tg_message_id: int,
        role: str,
        body: str | None = None,
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO msg_map "
            "(max_chat_id, max_message_id, tg_chat_id, tg_message_id, role, "
            " body) VALUES (?, ?, ?, ?, ?, ?)",
            (max_chat_id, max_message_id, tg_chat_id, tg_message_id, role, body),
        )
        await self._db.commit()

    async def get_reaction_target(
        self, max_message_id: int
    ) -> tuple[int, int, str, str | None] | None:
        """Несущее текст/подпись сообщение для правок реакций: (chat, msg, role, body)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT tg_chat_id, tg_message_id, role, body FROM msg_map "
            "WHERE max_message_id = ? AND role IN ('text','caption','user') "
            "ORDER BY rowid_alias LIMIT 1",
            (max_message_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return int(row[0]), int(row[1]), str(row[2]), row[3]

    async def get_msg_map(
        self, max_message_id: int
    ) -> list[tuple[int, int, str]]:
        """[(tg_chat_id, tg_message_id, role)] для сообщения MAX."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT tg_chat_id, tg_message_id, role FROM msg_map "
            "WHERE max_message_id = ? ORDER BY rowid_alias",
            (max_message_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]

    async def max_msg_by_tg(
        self, tg_chat_id: int, tg_message_id: int
    ) -> tuple[int, int] | None:
        """Обратный поиск: по Telegram-сообщению → (max_chat_id, max_message_id)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT max_chat_id, max_message_id FROM msg_map "
            "WHERE tg_chat_id = ? AND tg_message_id = ? LIMIT 1",
            (tg_chat_id, tg_message_id),
        ) as cur:
            row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else None

    async def forget_msg(self, max_message_id: int) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM msg_map WHERE max_message_id = ?", (max_message_id,)
        )
        await self._db.commit()

    async def trim_msg_map(self, keep: int) -> None:
        """Оставляет последние keep записей, старые чистит (анти-разрастание)."""
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM msg_map WHERE rowid_alias <= "
            "(SELECT MAX(rowid_alias) FROM msg_map) - ?",
            (keep,),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
