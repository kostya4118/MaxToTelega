import pytest

from registry import Registry


@pytest.fixture
async def registry(tmp_path):
    reg = await Registry.create(str(tmp_path / "registry.db"))
    yield reg
    await reg.close()


class TestAccounts:
    async def test_add_and_get(self, registry):
        account_id = await registry.add(111, "MAX …8200", "+79174278200")
        acc = await registry.get(account_id)
        assert acc["owner_tg_id"] == 111
        assert acc["phone"] == "+79174278200"

    async def test_list_by_owner_isolates_users(self, registry):
        await registry.add(111, "a", "+70000000001")
        await registry.add(222, "b", "+70000000002")
        assert len(await registry.list_by_owner(111)) == 1

    async def test_bind_group_and_lookup(self, registry):
        account_id = await registry.add(111, "a", "+70000000001")
        await registry.set_group(account_id, -1001234567890)
        acc = await registry.by_group(-1001234567890)
        assert acc["id"] == account_id

    async def test_remove(self, registry):
        account_id = await registry.add(111, "a", "+70000000001")
        await registry.remove(account_id)
        assert await registry.get(account_id) is None


class TestBans:
    async def test_ban_and_check(self, registry):
        await registry.ban(999)
        assert await registry.is_banned(999) is True
        assert await registry.is_banned(1) is False

    async def test_unban(self, registry):
        await registry.ban(999)
        await registry.unban(999)
        assert await registry.is_banned(999) is False


class TestConversations:
    """Незавершённый онбординг должен переживать перезапуск процесса."""

    async def test_save_and_list(self, registry):
        await registry.save_conv(111, "code", account_id=7, phone="+79174278200")
        convs = await registry.list_convs()
        assert convs == [{
            "tg_id": 111, "step": "code", "account_id": 7,
            "phone": "+79174278200", "updated_at": convs[0]["updated_at"],
        }]

    async def test_save_is_idempotent_per_user(self, registry):
        await registry.save_conv(111, "phone")
        await registry.save_conv(111, "code", account_id=7)
        convs = await registry.list_convs()
        assert len(convs) == 1 and convs[0]["step"] == "code"

    async def test_drop(self, registry):
        await registry.save_conv(111, "code")
        await registry.drop_conv(111)
        assert await registry.list_convs() == []

    async def test_clear(self, registry):
        await registry.save_conv(111, "code")
        await registry.save_conv(222, "phone")
        await registry.clear_convs()
        assert await registry.list_convs() == []

    async def test_survives_reopen(self, tmp_path):
        path = str(tmp_path / "reg.db")
        first = await Registry.create(path)
        await first.save_conv(111, "code", account_id=7, phone="+79174278200")
        await first.close()

        second = await Registry.create(path)
        try:
            convs = await second.list_convs()
            assert convs[0]["step"] == "code"
            assert convs[0]["phone"] == "+79174278200"
        finally:
            await second.close()
