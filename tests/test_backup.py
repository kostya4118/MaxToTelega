import os
import sqlite3
import stat
import tarfile

import pytest

from backup import (
    ENC_MAGIC,
    build_backup,
    decrypt_file,
    encrypt_file,
    restore_backup,
    rotate_backups,
    sqlite_snapshot,
)


def make_db(path, value="ok"):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


def read_db(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT v FROM t").fetchone()[0]
    finally:
        conn.close()


class TestEncryption:
    def test_roundtrip(self, tmp_path):
        src = tmp_path / "secret.db"
        src.write_bytes(b"session data")

        enc = encrypt_file(str(src), "hunter2")
        assert enc.endswith(".enc")
        assert not src.exists(), "исходник должен удаляться после шифрования"

        out = decrypt_file(enc, "hunter2")
        assert open(out, "rb").read() == b"session data"

    def test_encrypted_file_has_magic_header(self, tmp_path):
        src = tmp_path / "a.db"
        src.write_bytes(b"x")
        enc = encrypt_file(str(src), "pass")
        assert open(enc, "rb").read(len(ENC_MAGIC)) == ENC_MAGIC

    def test_ciphertext_is_not_plaintext(self, tmp_path):
        src = tmp_path / "a.db"
        src.write_bytes(b"super secret token")
        enc = encrypt_file(str(src), "pass")
        assert b"super secret token" not in open(enc, "rb").read()

    def test_wrong_passphrase_fails(self, tmp_path):
        src = tmp_path / "a.db"
        src.write_bytes(b"data")
        enc = encrypt_file(str(src), "right")
        with pytest.raises(Exception):
            decrypt_file(enc, "wrong")

    def test_plain_file_rejected(self, tmp_path):
        plain = tmp_path / "not-a-backup.tar.gz"
        plain.write_bytes(b"just a file")
        with pytest.raises(ValueError, match="зашифрованным"):
            decrypt_file(str(plain), "pass")

    def test_salt_differs_between_runs(self, tmp_path):
        first, second = tmp_path / "1.db", tmp_path / "2.db"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        enc1 = open(encrypt_file(str(first), "p"), "rb").read()
        enc2 = open(encrypt_file(str(second), "p"), "rb").read()
        assert enc1 != enc2, "одинаковые файлы не должны давать одинаковый шифротекст"

    def test_encrypted_file_is_private(self, tmp_path):
        src = tmp_path / "a.db"
        src.write_bytes(b"x")
        enc = encrypt_file(str(src), "pass")
        assert stat.S_IMODE(os.stat(enc).st_mode) == 0o600


class TestSnapshot:
    def test_copies_open_database(self, tmp_path):
        src = tmp_path / "live.db"
        make_db(str(src), "value")
        conn = sqlite3.connect(str(src))  # держим соединение открытым
        try:
            dst = tmp_path / "copy.db"
            sqlite_snapshot(str(src), str(dst))
            assert read_db(str(dst)) == "value"
        finally:
            conn.close()


class TestBuildBackup:
    def test_archives_all_databases(self, tmp_path):
        work, backups = tmp_path / "work", tmp_path / "backups"
        work.mkdir()
        make_db(str(work / "registry.db"))
        make_db(str(work / "session.db"))
        (work / "bridge.log").write_text("не должен попасть в архив")

        archive = build_backup(str(work), str(backups), keep=5)

        with tarfile.open(archive) as tar:
            names = [os.path.basename(n) for n in tar.getnames()]
        assert "registry.db" in names and "session.db" in names
        assert "bridge.log" not in names

    def test_encrypts_when_passphrase_given(self, tmp_path):
        work, backups = tmp_path / "work", tmp_path / "backups"
        work.mkdir()
        make_db(str(work / "registry.db"))
        archive = build_backup(str(work), str(backups), keep=5, passphrase="secret")
        assert archive.endswith(".tar.gz.enc")

    def test_backup_restore_cycle(self, tmp_path):
        work, backups, target = tmp_path / "work", tmp_path / "b", tmp_path / "restored"
        work.mkdir()
        target.mkdir()
        make_db(str(work / "registry.db"), "before")

        archive = build_backup(str(work), str(backups), keep=5)
        restored = restore_backup(archive, str(target))

        assert restored == ["registry.db"]
        assert read_db(str(target / "registry.db")) == "before"

    def test_encrypted_backup_restore_cycle(self, tmp_path):
        work, backups, target = tmp_path / "work", tmp_path / "b", tmp_path / "restored"
        work.mkdir()
        target.mkdir()
        make_db(str(work / "registry.db"), "payload")

        archive = build_backup(str(work), str(backups), keep=5, passphrase="pw")
        plain = decrypt_file(archive, "pw")
        restore_backup(plain, str(target))

        assert read_db(str(target / "registry.db")) == "payload"

    def test_restored_files_are_private(self, tmp_path):
        work, backups, target = tmp_path / "work", tmp_path / "b", tmp_path / "restored"
        work.mkdir()
        target.mkdir()
        make_db(str(work / "registry.db"))
        archive = build_backup(str(work), str(backups), keep=5)
        restore_backup(archive, str(target))
        mode = stat.S_IMODE(os.stat(target / "registry.db").st_mode)
        assert mode == 0o600


class TestRestoreSafety:
    def test_path_traversal_is_neutralized(self, tmp_path):
        """Архив с ../ в пути не должен писать за пределы каталога."""
        evil = tmp_path / "evil.tar.gz"
        payload = tmp_path / "payload.db"
        make_db(str(payload))
        with tarfile.open(evil, "w:gz") as tar:
            tar.add(str(payload), arcname="../../escaped.db")

        target = tmp_path / "restored"
        target.mkdir()
        restored = restore_backup(str(evil), str(target))

        assert restored == ["escaped.db"]
        assert (target / "escaped.db").exists()
        assert not (tmp_path.parent / "escaped.db").exists()


class TestRotation:
    def test_keeps_only_recent(self, tmp_path):
        for i in range(5):
            (tmp_path / f"backup_2026010{i}_000000.tar.gz").write_bytes(b"x")
        rotate_backups(str(tmp_path), keep=2)
        left = sorted(p.name for p in tmp_path.glob("backup_*"))
        assert left == [
            "backup_20260103_000000.tar.gz",
            "backup_20260104_000000.tar.gz",
        ]

    def test_zero_keep_disables_rotation(self, tmp_path):
        (tmp_path / "backup_20260101_000000.tar.gz").write_bytes(b"x")
        rotate_backups(str(tmp_path), keep=0)
        assert len(list(tmp_path.glob("backup_*"))) == 1

    def test_rotation_covers_encrypted(self, tmp_path):
        for i in range(3):
            (tmp_path / f"backup_2026010{i}_000000.tar.gz.enc").write_bytes(b"x")
        rotate_backups(str(tmp_path), keep=1)
        assert [p.name for p in tmp_path.glob("backup_*")] == [
            "backup_20260102_000000.tar.gz.enc"
        ]
