#!/usr/bin/env python3
"""Расшифровка бэкапа MaxToTelega (.tar.gz.enc).

Использование:
    python3 decrypt_backup.py backup_YYYYmmdd_HHMMSS.tar.gz.enc [output.tar.gz]

Пароль берётся из переменной BACKUP_PASSPHRASE или спрашивается интерактивно.
Дальше: tar -xzf output.tar.gz  → файлы из data/ положить в свой WORK_DIR.
"""

from __future__ import annotations

import base64
import getpass
import os
import sys

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"MTTENC1\n"


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out = (
        sys.argv[2] if len(sys.argv) > 2
        else (src[:-4] if src.endswith(".enc") else src + ".dec")
    )
    passphrase = os.getenv("BACKUP_PASSPHRASE") or getpass.getpass("Пароль: ")

    raw = open(src, "rb").read()
    if raw[: len(MAGIC)] != MAGIC:
        print("Это не зашифрованный бэкап MaxToTelega.")
        sys.exit(2)
    salt = raw[len(MAGIC):len(MAGIC) + 16]
    token = raw[len(MAGIC) + 16:]
    try:
        data = Fernet(derive_key(passphrase, salt)).decrypt(token)
    except Exception:
        print("Не удалось расшифровать — неверный пароль или повреждён файл.")
        sys.exit(3)
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK -> {out}\nДальше: tar -xzf {out}")


if __name__ == "__main__":
    main()
