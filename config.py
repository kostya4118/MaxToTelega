"""Конфигурация моста.

Поддерживает два режима:
- Мультиаккаунт: файл ``accounts.json`` со списком MAX-аккаунтов (у каждого
  своя группа-форум в Telegram). Общий бот и владелец берутся из ``.env``.
- Один аккаунт: если ``accounts.json`` нет — аккаунт собирается из переменных
  ``.env`` (как раньше), полная обратная совместимость.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    return s or "acc"


@dataclass(frozen=True)
class AccountConfig:
    """Настройки одного MAX-аккаунта."""

    name: str
    max_phone: str
    max_session: str        # имя файла сессии внутри work_dir
    mapping_db: str         # полный путь к SQLite моста для этого аккаунта
    telegram_group_id: int | None
    max_proxy: str | None
    forward_groups: bool


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_owner_id: int | None
    work_dir: str
    accounts: list[AccountConfig]

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

        owner_raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        owner_id = int(owner_raw) if owner_raw else None

        work_dir = os.getenv("WORK_DIR", "./data").strip()
        Path(work_dir).mkdir(parents=True, exist_ok=True)

        accounts_file = os.getenv("ACCOUNTS_FILE", "accounts.json").strip()
        if accounts_file and os.path.exists(accounts_file):
            accounts = cls._load_accounts_file(accounts_file, work_dir)
        else:
            accounts = [cls._account_from_env(work_dir)]

        # Каталоги под базы создаём заранее.
        for acc in accounts:
            Path(acc.mapping_db).parent.mkdir(parents=True, exist_ok=True)

        return cls(
            telegram_token=token,
            telegram_owner_id=owner_id,
            work_dir=work_dir,
            accounts=accounts,
        )

    # ── Загрузчики ───────────────────────────────────────────────────────

    @staticmethod
    def _account_from_env(work_dir: str) -> AccountConfig:
        phone = os.getenv("MAX_PHONE", "").strip()
        if not phone:
            raise RuntimeError("MAX_PHONE не задан в .env")
        group_raw = os.getenv("TELEGRAM_GROUP_ID", "").strip()
        return AccountConfig(
            name="default",
            max_phone=phone,
            max_session=os.getenv("MAX_SESSION", "max_session.db").strip(),
            mapping_db=os.getenv(
                "MAPPING_DB", os.path.join(work_dir, "mapping.db")
            ).strip(),
            telegram_group_id=int(group_raw) if group_raw else None,
            max_proxy=(os.getenv("MAX_PROXY", "").strip() or None),
            forward_groups=_bool(os.getenv("FORWARD_GROUPS"), default=False),
        )

    @staticmethod
    def _load_accounts_file(path: str, work_dir: str) -> list[AccountConfig]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"{path}: ожидался непустой список аккаунтов")

        accounts: list[AccountConfig] = []
        seen_slugs: set[str] = set()
        for index, entry in enumerate(data):
            name = str(entry.get("name") or f"acc{index + 1}")
            slug = _slug(name)
            # Гарантируем уникальность имён файлов сессий/баз.
            base_slug = slug
            counter = 2
            while slug in seen_slugs:
                slug = f"{base_slug}{counter}"
                counter += 1
            seen_slugs.add(slug)

            phone = str(entry.get("phone") or "").strip()
            if not phone:
                raise RuntimeError(f"{path}: у аккаунта '{name}' нет phone")

            group_id = entry.get("group_id")
            session = str(entry.get("session") or f"{slug}.db")
            mapping_db = str(
                entry.get("mapping_db")
                or os.path.join(work_dir, f"{slug}_map.db")
            )
            accounts.append(
                AccountConfig(
                    name=name,
                    max_phone=phone,
                    max_session=session,
                    mapping_db=mapping_db,
                    telegram_group_id=(
                        int(group_id) if group_id is not None else None
                    ),
                    max_proxy=(str(entry["proxy"]).strip()
                               if entry.get("proxy") else None),
                    forward_groups=bool(entry.get("forward_groups", False)),
                )
            )
        return accounts
