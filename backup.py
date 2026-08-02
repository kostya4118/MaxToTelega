"""Бэкапы баз моста: снимок, архивация, шифрование и восстановление.

Зависит только от стандартной библиотеки и cryptography, поэтому покрывается
тестами без запуска моста.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime

logger = logging.getLogger("bridge")

ENC_MAGIC = b"MTTENC1\n"
_SALT_LEN = 16
# Права на файлы с сессиями и бэкапами: доступ только владельцу процесса.
PRIVATE_MODE = 0o600


def secure_file(path: str) -> None:
    """Ограничивает доступ к файлу владельцем (сессии MAX = доступ к аккаунту)."""
    try:
        os.chmod(path, PRIVATE_MODE)
    except OSError:
        logger.debug("Не удалось выставить права на %s", path, exc_info=True)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_file(path: str, passphrase: str) -> str:
    """Шифрует файл паролем (PBKDF2 + Fernet). Возвращает путь к .enc."""
    from cryptography.fernet import Fernet

    salt = os.urandom(_SALT_LEN)
    with open(path, "rb") as f:
        token = Fernet(derive_key(passphrase, salt)).encrypt(f.read())
    enc = path + ".enc"
    with open(enc, "wb") as f:
        f.write(ENC_MAGIC)
        f.write(salt)
        f.write(token)
    secure_file(enc)
    os.remove(path)
    return enc


def decrypt_file(path: str, passphrase: str) -> str:
    """Расшифровывает .enc-файл. Возвращает путь к расшифрованному файлу."""
    from cryptography.fernet import Fernet

    with open(path, "rb") as f:
        magic = f.read(len(ENC_MAGIC))
        if magic != ENC_MAGIC:
            raise ValueError("Файл не является зашифрованным бэкапом бота.")
        salt = f.read(_SALT_LEN)
        token = f.read()
    out = path.removesuffix(".enc") if path.endswith(".enc") else path + ".dec"
    data = Fernet(derive_key(passphrase, salt)).decrypt(token)
    with open(out, "wb") as f:
        f.write(data)
    secure_file(out)
    return out


def restore_backup(archive: str, work_dir: str) -> list[str]:
    """Распаковывает *.db из бэкапа в work_dir. Возвращает имена файлов."""
    restored = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".db"):
                continue
            # Только имя файла: защищает от путей вида ../../etc/passwd.
            member.name = os.path.basename(member.name)
            tar.extract(member, path=work_dir)
            secure_file(os.path.join(work_dir, member.name))
            restored.append(member.name)
    return restored


def sqlite_snapshot(src: str, dst: str) -> None:
    """Консистентная копия SQLite-файла даже при открытом соединении."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def build_backup(
    work_dir: str, backup_dir: str, keep: int, passphrase: str | None = None
) -> str:
    """Собирает tar.gz из всех *.db (сессии, реестр, маршрутизация). Ротирует.

    Если задан passphrase — архив шифруется (на выходе .tar.gz.enc).
    """
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = os.path.join(backup_dir, f"backup_{ts}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        for src in glob.glob(os.path.join(work_dir, "*.db")):
            dst = os.path.join(tmp, os.path.basename(src))
            try:
                sqlite_snapshot(src, dst)
            except Exception:
                shutil.copy2(src, dst)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tmp, arcname="data")
    secure_file(archive)
    if passphrase:
        try:
            archive = encrypt_file(archive, passphrase)
        except Exception:
            logger.warning(
                "Не удалось зашифровать бэкап — оставляю без шифрования",
                exc_info=True,
            )
    rotate_backups(backup_dir, keep)
    return archive


def rotate_backups(backup_dir: str, keep: int) -> None:
    """Оставляет последние keep архивов (с .enc или без)."""
    if keep <= 0:
        return
    backups = sorted(glob.glob(os.path.join(backup_dir, "backup_*.tar.gz*")))
    for old in backups[:-keep]:
        try:
            os.remove(old)
        except OSError:
            logger.debug("Не удалось удалить старый бэкап %s", old, exc_info=True)
