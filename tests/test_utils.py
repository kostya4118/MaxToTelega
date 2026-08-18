import pytest

from utils import (
    MAX_LINK_RE,
    describe_control_event,
    is_bot_contact,
    mask_phone,
    normalize_phone,
    parse_general_query,
    parse_max_keyboard,
    topic_link,
    unescape_command,
    username_from_link,
)


class TestNormalizePhone:
    @pytest.mark.parametrize("raw", [
        "+7 917 427-82-00",
        "7 917 427 82 00",
        "89174278200",
        "79174278200",
        "+7(917)427-82-00",
    ])
    def test_common_formats(self, raw):
        assert normalize_phone(raw) == "+79174278200"

    @pytest.mark.parametrize("raw", ["", "   ", "просто текст", "12345", "@user"])
    def test_rejects_non_phones(self, raw):
        assert normalize_phone(raw) is None

    def test_keeps_foreign_numbers(self):
        assert normalize_phone("+380501234567") == "+380501234567"

    def test_eight_prefix_only_for_eleven_digits(self):
        # 8 в начале коротких номеров не подменяем на 7.
        assert normalize_phone("812345678") == "+812345678"


class TestParseGeneralQuery:
    def test_phone_only(self):
        assert parse_general_query("+79174278200") == ("+79174278200", "")

    def test_phone_with_spaces_only(self):
        assert parse_general_query("7 917 427-82-00") == ("+79174278200", "")

    def test_phone_with_message(self):
        assert parse_general_query("7 917 427-82-00 Привет!") == (
            "+79174278200", "Привет!",
        )

    def test_username_with_message(self):
        assert parse_general_query("@someuser Привет") == ("someuser", "Привет")

    def test_username_only(self):
        assert parse_general_query("rb_k_vrachu_bot") == ("rb_k_vrachu_bot", "")

    def test_numeric_id_stays_query(self):
        # Короткое число — не телефон, уходит в поиск как есть.
        query, text = parse_general_query("150144926")
        assert query == "150144926" and text == ""

    def test_empty(self):
        assert parse_general_query("   ") == ("", "")


class TestParseMaxKeyboard:
    def test_real_bot_keyboard(self):
        # Форма, которую реально присылает MAX.
        kbd = {"buttons": [[
            {"type": "CALLBACK", "text": "Принять",
             "payload": "accept_the_agreement", "intent": "DEFAULT"},
        ]]}
        assert parse_max_keyboard(kbd) == [[
            {"text": "Принять", "url": None, "payload": "accept_the_agreement"},
        ]]

    def test_flat_button_list(self):
        kbd = {"buttons": [{"text": "Меню", "payload": "menu"}]}
        rows = parse_max_keyboard(kbd)
        assert len(rows) == 1 and rows[0][0]["text"] == "Меню"

    def test_http_link_becomes_url(self):
        kbd = {"buttons": [[{"text": "Сайт", "url": "https://example.com"}]]}
        assert parse_max_keyboard(kbd)[0][0]["url"] == "https://example.com"

    def test_non_http_scheme_is_not_url(self):
        # Telegram принимает только http(s) — max:// должен стать callback.
        kbd = {"buttons": [[{"text": "В приложении", "url": "max://open"}]]}
        assert parse_max_keyboard(kbd)[0][0]["url"] is None

    def test_button_without_text_gets_placeholder(self):
        kbd = {"buttons": [[{"payload": "x"}]]}
        assert parse_max_keyboard(kbd)[0][0]["text"] == "•"

    def test_long_text_is_truncated(self):
        kbd = {"buttons": [[{"text": "я" * 200, "payload": "x"}]]}
        assert len(parse_max_keyboard(kbd)[0][0]["text"]) == 64

    def test_row_limited_to_eight_buttons(self):
        kbd = {"buttons": [[{"text": str(i)} for i in range(20)]]}
        assert len(parse_max_keyboard(kbd)[0]) == 8

    @pytest.mark.parametrize("kbd", [{}, None, "строка", {"buttons": []}])
    def test_garbage_yields_no_rows(self, kbd):
        assert parse_max_keyboard(kbd) == []

    def test_skips_malformed_buttons(self):
        kbd = {"buttons": [["строка", 42, {"text": "Ок"}]]}
        rows = parse_max_keyboard(kbd)
        assert rows == [[{"text": "Ок", "url": None, "payload": None}]]


class TestIsBotContact:
    def test_detects_bot_flag(self):
        assert is_bot_contact(["TT", "ONEME", "OFFICIAL", "BOT"]) is True

    def test_plain_user(self):
        assert is_bot_contact(["TT", "ONEME"]) is False

    @pytest.mark.parametrize("options", [None, [], set()])
    def test_missing_options(self, options):
        assert is_bot_contact(options) is False


class TestTopicLink:
    def test_supergroup_id(self):
        assert topic_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"

    @pytest.mark.parametrize("group_id,thread_id", [
        (None, 42), (-1001234567890, None), (0, 42),
    ])
    def test_missing_parts(self, group_id, thread_id):
        assert topic_link(group_id, thread_id) is None


class TestMisc:
    def test_unescape_command(self):
        assert unescape_command("//leave") == "/leave"

    def test_single_slash_untouched(self):
        assert unescape_command("/leave") == "/leave"

    def test_plain_text_untouched(self):
        assert unescape_command("привет") == "привет"

    def test_mask_phone(self):
        assert mask_phone("+79174278200") == "…8200"

    def test_mask_short_phone(self):
        assert mask_phone("123") == "123"

    @pytest.mark.parametrize("link,expected", [
        ("https://max.ru/rb_k_vrachu_bot", "rb_k_vrachu_bot"),
        ("https://max.ru/rb_k_vrachu_bot/", "rb_k_vrachu_bot"),
        ("rb_k_vrachu_bot", "rb_k_vrachu_bot"),
        ("@rb_k_vrachu_bot", "rb_k_vrachu_bot"),
    ])
    def test_username_from_link(self, link, expected):
        assert username_from_link(link) == expected

    @pytest.mark.parametrize("text", [
        "https://max.ru/join/abc",
        "загляни на https://oneme.ru/xyz сюда",
        "HTTPS://MAX.RU/ABC",
    ])
    def test_link_regex_matches(self, text):
        assert MAX_LINK_RE.search(text) is not None

    def test_link_regex_ignores_other_hosts(self):
        assert MAX_LINK_RE.search("https://example.com/max.ru") is None


class TestControlEvents:
    def test_known_event(self):
        assert describe_control_event("leave") == "участник вышел"

    def test_case_insensitive(self):
        assert describe_control_event("LEAVE") == "участник вышел"

    def test_unknown_event_is_shown_as_is(self):
        # Набор кодов недокументирован: незнакомое событие должно быть видно.
        assert describe_control_event("pin_v2") == "служебное событие «pin_v2»"

    @pytest.mark.parametrize("event", ["", None])
    def test_empty_event(self, event):
        assert describe_control_event(event) == "системное сообщение"
