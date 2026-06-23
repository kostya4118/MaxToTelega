"""Конфигурация моста из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_owner_id: int | None
    telegram_group_id: int | None
    max_phone: str
    work_dir: str
    max_session: str
    mapping_db: str
    forward_groups: bool
    mark_read: bool
    max_proxy: str | None

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

        phone = os.getenv("MAX_PHONE", "").strip()
        if not phone:
            raise RuntimeError("MAX_PHONE не задан в .env")

        owner_raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        owner_id = int(owner_raw) if owner_raw else None

        group_raw = os.getenv("TELEGRAM_GROUP_ID", "").strip()
        group_id = int(group_raw) if group_raw else None

        work_dir = os.getenv("WORK_DIR", "./data").strip()
        Path(work_dir).mkdir(parents=True, exist_ok=True)

        mapping_db = os.getenv("MAPPING_DB", "./data/mapping.db").strip()
        Path(mapping_db).parent.mkdir(parents=True, exist_ok=True)

        return cls(
            telegram_token=token,
            telegram_owner_id=owner_id,
            telegram_group_id=group_id,
            max_phone=phone,
            work_dir=work_dir,
            max_session=os.getenv("MAX_SESSION", "max_session.db").strip(),
            mapping_db=mapping_db,
            forward_groups=_bool(os.getenv("FORWARD_GROUPS"), default=False),
            mark_read=_bool(os.getenv("MARK_READ"), default=True),
            max_proxy=(os.getenv("MAX_PROXY", "").strip() or None),
        )
