"""Конфигурация мультитенантного моста.

Из ``.env`` берутся только общие настройки бота. Сами MAX-аккаунты пользователи
добавляют через бота (хранятся в реестре ``registry.db``). Поля LEGACY_* нужны
лишь для одноразового импорта аккаунта из старого single-режима — чтобы при
обновлении не пришлось входить заново.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    work_dir: str
    registry_db: str
    admin_ids: list[int]

    # Для одноразовой миграции старого single-аккаунта (если реестр пуст).
    legacy_owner_id: int | None
    legacy_phone: str | None
    legacy_group_id: int | None
    legacy_session: str
    legacy_mapping_db: str
    legacy_proxy: str | None

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

        work_dir = os.getenv("WORK_DIR", "./data").strip()
        Path(work_dir).mkdir(parents=True, exist_ok=True)

        registry_db = os.getenv(
            "REGISTRY_DB", os.path.join(work_dir, "registry.db")
        ).strip()
        Path(registry_db).parent.mkdir(parents=True, exist_ok=True)

        owner_raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        group_raw = os.getenv("TELEGRAM_GROUP_ID", "").strip()
        legacy_owner_id = int(owner_raw) if owner_raw else None

        # Админы (одобряют регистрации). По умолчанию — владелец из .env.
        admin_raw = os.getenv("ADMIN_TG_ID", "").strip()
        if admin_raw:
            admin_ids = [
                int(x) for x in admin_raw.replace(";", ",").split(",")
                if x.strip().lstrip("-").isdigit()
            ]
        elif legacy_owner_id is not None:
            admin_ids = [legacy_owner_id]
        else:
            admin_ids = []

        return cls(
            telegram_token=token,
            work_dir=work_dir,
            registry_db=registry_db,
            admin_ids=admin_ids,
            legacy_owner_id=int(owner_raw) if owner_raw else None,
            legacy_phone=(os.getenv("MAX_PHONE", "").strip() or None),
            legacy_group_id=int(group_raw) if group_raw else None,
            legacy_session=os.getenv("MAX_SESSION", "max_session.db").strip(),
            legacy_mapping_db=os.getenv(
                "MAPPING_DB", os.path.join(work_dir, "mapping.db")
            ).strip(),
            legacy_proxy=(os.getenv("MAX_PROXY", "").strip() or None),
        )
