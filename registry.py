"""Реестр аккаунтов мультитенантного моста.

Хранит, какой Telegram-пользователь какой MAX-аккаунт добавил, к какой группе
он привязан и где лежат его файлы сессии и маршрутизации. Одна общая база на
весь бот; данные самих переписок — в отдельных per-account базах.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite


class Registry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def create(cls, db_path: str) -> "Registry":
        self = cls(db_path)
        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_tg_id INTEGER NOT NULL,
                name        TEXT,
                phone       TEXT NOT NULL,
                group_id    INTEGER,
                session     TEXT,
                mapping_db  TEXT,
                proxy       TEXT,
                status      TEXT NOT NULL DEFAULT 'active',
                created_at  INTEGER NOT NULL
            )
            """
        )
        await self._db.commit()
        return self

    async def add(
        self,
        owner_tg_id: int,
        name: str,
        phone: str,
        *,
        group_id: int | None = None,
        session: str | None = None,
        mapping_db: str | None = None,
        proxy: str | None = None,
        status: str = "active",
    ) -> int:
        assert self._db is not None
        cur = await self._db.execute(
            "INSERT INTO accounts "
            "(owner_tg_id, name, phone, group_id, session, mapping_db, proxy, "
            " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_tg_id, name, phone, group_id, session, mapping_db, proxy,
             status, int(time.time())),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def set_group(self, account_id: int, group_id: int | None) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE accounts SET group_id = ? WHERE id = ?",
            (group_id, account_id),
        )
        await self._db.commit()

    async def set_status(self, account_id: int, status: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            (status, account_id),
        )
        await self._db.commit()

    async def set_name(self, account_id: int, name: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE accounts SET name = ? WHERE id = ?", (name, account_id)
        )
        await self._db.commit()

    async def get(self, account_id: int) -> dict[str, Any] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def by_group(self, group_id: int) -> dict[str, Any] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM accounts WHERE group_id = ?", (group_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_by_owner(self, owner_tg_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM accounts WHERE owner_tg_id = ? ORDER BY id",
            (owner_tg_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_all(self) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM accounts ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def remove(self, account_id: int) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM accounts WHERE id = ?", (account_id,)
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
