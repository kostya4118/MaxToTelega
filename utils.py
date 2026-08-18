"""Чистые функции моста: разбор ввода, клавиатур MAX и ссылок.

Модуль намеренно не зависит ни от PyMax, ни от aiogram — это позволяет
покрыть логику тестами без сетевых клиентов и учётных записей.
"""

from __future__ import annotations

import re

TG_CAPTION_LIMIT = 1024
TG_UPLOAD_LIMIT = 45 * 1024 * 1024  # запас под лимит бота Telegram (~50 МБ)

PHONE_RE = re.compile(r"^\+\d{7,15}$")

MAX_LINK_RE = re.compile(
    r"https?://(?:[\w.-]*\.)?(?:max\.ru|oneme\.ru|o\.ru)/\S+",
    re.IGNORECASE,
)


def normalize_phone(raw: str) -> str | None:
    """Приводит номер телефона любого формата к +7XXXXXXXXXX.

    Примеры входных форматов:
      +7 917 427-82-00  →  +79174278200
      7 917 427 82 00   →  +79174278200
      89174278200       →  +79174278200
      79174278200       →  +79174278200
    """
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    phone = "+" + digits
    return phone if PHONE_RE.match(phone) else None


def looks_like_phone(raw: str) -> bool:
    """Отличает телефон от числового MAX ID.

    ID пользователя MAX — девятизначное число, поэтому короткие числа без «+»
    телефоном не считаем: иначе поиск уходит в search_by_phone вместо get_user.
    """
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return False
    return raw.strip().startswith("+") or len(digits) >= 10


def parse_general_query(text: str) -> tuple[str, str]:
    """Делит сообщение из General на «кого искать» и «что написать».

    Телефон может быть многословным («7 917 427-82-00 Привет»), поэтому
    номер собирается из токенов с цифрами, а остальное считается текстом
    первого сообщения. Если номер не распознан, первым токеном считается
    username.

    Возвращает (запрос, первое сообщение).
    """
    text = text.strip()
    if not text:
        return "", ""

    tokens = text.split()

    # В строке нет букв — значит она целиком номер, текста сообщения нет.
    if re.search(r"\d", text) and not re.search(r"[^\W\d_]", text):
        normalized = normalize_phone(text) if looks_like_phone(text) else None
        if normalized:
            return normalized, ""
        return tokens[0].lstrip("@"), " ".join(tokens[1:])

    # Иначе номер заканчивается там, где начинается слово без цифр.
    phone_tokens: list[str] = []
    rest_tokens: list[str] = []
    for i, token in enumerate(tokens):
        if re.search(r"\d", token) or token.startswith("+"):
            phone_tokens.append(token)
        else:
            rest_tokens = tokens[i:]
            break

    raw_phone = " ".join(phone_tokens)
    normalized = (
        normalize_phone(raw_phone)
        if phone_tokens and looks_like_phone(raw_phone)
        else None
    )
    if normalized:
        return normalized, " ".join(rest_tokens)
    return tokens[0].lstrip("@"), " ".join(tokens[1:])


def parse_max_keyboard(keyboard: dict) -> list[list[dict]]:
    """Нормализует клавиатуру бота MAX в ряды кнопок.

    MAX присылает клавиатуру сырым словарём:
    {"buttons": [[{"type": "CALLBACK", "text": "…", "payload": "…"}]]}

    Возвращает ряды словарей {text, url, payload}, где url заполнен только
    для http(s)-ссылок — остальные схемы Telegram не принимает.
    """
    if not isinstance(keyboard, dict):
        return []

    raw_rows = keyboard.get("buttons") or keyboard.get("rows") or []
    if not isinstance(raw_rows, list):
        return []
    if raw_rows and isinstance(raw_rows[0], dict):
        raw_rows = [raw_rows]  # пришёл плоский список кнопок

    rows: list[list[dict]] = []
    for raw_row in raw_rows[:12]:
        if not isinstance(raw_row, list):
            continue
        row: list[dict] = []
        for button in raw_row[:8]:
            if not isinstance(button, dict):
                continue
            text = str(
                button.get("text")
                or button.get("title")
                or button.get("caption")
                or "•"
            )[:64]
            url = button.get("url") or button.get("link")
            if not (isinstance(url, str) and url.startswith("http")):
                url = None
            row.append({
                "text": text,
                "url": url,
                "payload": button.get("payload") or button.get("callback"),
            })
        if row:
            rows.append(row)
    return rows


def is_bot_contact(options) -> bool:
    """MAX помечает ботов флагом BOT в options контакта."""
    if not options:
        return False
    return "BOT" in {str(option).upper() for option in options}


def topic_link(group_id: int | None, thread_id: int | None) -> str | None:
    """Ссылка на тему форума: https://t.me/c/<id без -100>/<тема>."""
    if not group_id or not thread_id:
        return None
    chat_part = str(group_id)
    if chat_part.startswith("-100"):
        chat_part = chat_part[4:]
    else:
        chat_part = chat_part.lstrip("-")
    return f"https://t.me/c/{chat_part}/{thread_id}"


def unescape_command(text: str) -> str:
    """«//leave» → «/leave»: способ отправить в MAX команду, занятую мостом."""
    return text[1:] if text.startswith("//") else text


def mask_phone(phone: str) -> str:
    return f"…{phone[-4:]}" if phone and len(phone) > 4 else (phone or "?")


def username_from_link(link: str) -> str:
    """Достаёт username из ссылки вида https://max.ru/rb_k_vrachu_bot."""
    from urllib.parse import urlparse

    path = urlparse(link).path if "//" in link else link
    return path.strip("/").split("/")[-1].lstrip("@")


# Служебные события MAX (ControlAttachment.event). Набор кодов недокументирован,
# поэтому незнакомые показываем как есть и пишем в лог — так их видно и можно
# добавить сюда позже.
CONTROL_EVENT_TEXTS = {
    "new": "чат создан",
    "add": "участники добавлены",
    "remove": "участник удалён",
    "leave": "участник вышел",
    "join": "участник вступил",
    "title": "название чата изменено",
    "icon": "иконка чата изменена",
    "pin": "сообщение закреплено",
    "unpin": "закрепление снято",
    "call": "звонок",
    "hangup": "звонок завершён",
    "system": "системное сообщение",
}


def describe_control_event(event: str) -> str:
    """Человекочитаемое описание служебного события MAX."""
    key = (event or "").strip().lower()
    known = CONTROL_EVENT_TEXTS.get(key)
    if known:
        return known
    return f"служебное событие «{event}»" if event else "системное сообщение"


def parse_proxy_link(content: str) -> str | None:
    """Достаёт ссылку на MTProto-прокси из файла.

    Понимает и «голую» ссылку в файле, и конфиг вида KEY=VALUE, где нужное
    значение лежит в LINK (так его пишет скрипт ротации).
    """
    if not content:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("LINK="):
            value = line[len("LINK="):].strip().strip('"\'')
            if value:
                return value
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(("tg://proxy", "https://t.me/proxy")):
            return line
    return None


def tg_proxy_web_link(link: str) -> str | None:
    """Превращает tg://proxy?… в https://t.me/proxy?… — такая ссылка кликается везде."""
    prefix = "tg://proxy?"
    if link and link.startswith(prefix):
        return "https://t.me/proxy?" + link[len(prefix):]
    return None
