#!/bin/bash
# Ротация MTProto-прокси на mtproto.zig.
#
# Прокси умеет держать несколько секретов одновременно, поэтому хватает одного
# контейнера на одном порту: каждый запуск заменяет самый старый секрет из трёх,
# две прежние ссылки продолжают работать. Слот выбирается по времени изменения
# файла секрета, внешнее состояние не нужно.
#
# Смена секретов требует перезапуска процесса, поэтому при ротации соединения
# коротко рвутся и клиенты переподключаются сами. Если новый конфиг не заводится,
# скрипт откатывается на предыдущий, чтобы не оставить прокси лежащим.

set -u

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# printf вместо литерала: почтовые и чат-клиенты превращают голый домен в ссылку.
FAKE_DOMAIN="$(printf '%s.%s.%s' www google com)"
HOST_PORT=4515
MAX_CONNECTIONS=128
SLOTS="a b c"
DIR="/etc/mtproto-zig"
CONFIG="${DIR}/config.toml"
CONTAINER="mtproto-proxy"
IMAGE="ghcr.io/sleep3r/mtproto.zig:latest"

# Пути моста: подписчики и копия конфига для кнопки «MTProto прокси» в боте.
BRIDGE_DIR="${BRIDGE_DIR:-/opt/maxtotelega/app}"
BRIDGE_DATA="${BRIDGE_DIR}/data"
SUBS_FILE="${SUBS_FILE:-${BRIDGE_DATA}/proxy_subscribers.txt}"
BRIDGE_COPY="${BRIDGE_COPY:-${BRIDGE_DATA}/mtproto_config.txt}"

# Токен бота держим только в .env моста: копия в скрипте однажды утекла в
# публичный репозиторий. Можно передать и через переменную окружения.
if [ -z "${BOT_TOKEN:-}" ] && [ -r "${BRIDGE_DIR}/.env" ]; then
    BOT_TOKEN=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "${BRIDGE_DIR}/.env" | tr -d '"'"'"' \r')
fi

mkdir -p "$DIR"

# Собирает config.toml из всех непустых файлов секретов.
write_config() {
    {
        echo "[general]"
        echo "use_middle_proxy = true"
        echo ""
        echo "[server]"
        echo "port = 443"
        # Потолок считается прокси от всей памяти хоста, но её делят ещё
        # amnezia-контейнеры и мост. В middleproxy-режиме ~4 МБ на соединение,
        # так что 128 держит расход в пределах ~512 МБ.
        echo "max_connections = ${MAX_CONNECTIONS}"
        # На info пишется каждое рукопожатие — логи растут без нужды.
        echo "log_level = \"warn\""
        echo ""
        echo "[censorship]"
        echo "tls_domain = \"${FAKE_DOMAIN}\""
        echo "mask = true"
        echo ""
        echo "[access.users]"
        for s in $SLOTS; do
            f="${DIR}/secret-${s}.txt"
            [ -s "$f" ] && echo "slot_${s} = \"$(cat "$f")\""
        done
    } > "$CONFIG"
    # Процесс в контейнере работает не от root: с 600 он падает с AccessDenied
    # и контейнер уходит в бесконечный рестарт.
    chmod 644 "$CONFIG"
}

start_container() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1
    docker run -d \
      --name "$CONTAINER" \
      --restart unless-stopped \
      -p ${HOST_PORT}:443 \
      --ulimit nofile=65535:65535 \
      --log-opt max-size=10m --log-opt max-file=3 \
      -v "${CONFIG}:/etc/mtproto-proxy/config.toml:ro" \
      "$IMAGE" >/dev/null 2>&1
    sleep 3
    docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"
}

# Слот без секрета, иначе самый старый по времени изменения файла.
SLOT=""
OLDEST=""
for s in $SLOTS; do
    f="${DIR}/secret-${s}.txt"
    if [ ! -s "$f" ]; then
        SLOT="$s"
        break
    fi
    ts=$(stat -c %Y "$f")
    if [ -z "$OLDEST" ] || [ "$ts" -lt "$OLDEST" ]; then
        OLDEST="$ts"
        SLOT="$s"
    fi
done

echo "🚀 Ротация MTProto прокси — слот ${SLOT^^}, порт ${HOST_PORT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "📌 Домен Fake TLS: ${BLUE}${FAKE_DOMAIN}${NC}"

# Сохраняем прежнее состояние — понадобится для отката.
SECRET_FILE="${DIR}/secret-${SLOT}.txt"
[ -f "$CONFIG" ] && cp -a "$CONFIG" "${CONFIG}.bak"
[ -f "$SECRET_FILE" ] && cp -a "$SECRET_FILE" "${SECRET_FILE}.bak"

SECRET_HEX=$(openssl rand -hex 16)
printf '%s' "$SECRET_HEX" > "$SECRET_FILE"
chmod 600 "$SECRET_FILE"
echo -e "🔑 Новый секрет слота ${SLOT^^}: ${YELLOW}${SECRET_HEX}${NC}"

write_config
echo -n "📦 Перезапуск прокси... "

if ! start_container; then
    echo -e "${RED}❌ не поднялся${NC}"
    docker logs "$CONTAINER" 2>&1 | tail -20

    if [ -f "${CONFIG}.bak" ]; then
        echo -n "↩️  Откат на прежний конфиг... "
        mv -f "${CONFIG}.bak" "$CONFIG"
        if [ -f "${SECRET_FILE}.bak" ]; then
            mv -f "${SECRET_FILE}.bak" "$SECRET_FILE"
        else
            rm -f "$SECRET_FILE"
        fi
        if start_container; then
            echo -e "${GREEN}прокси работает на прежних ссылках${NC}"
        else
            echo -e "${RED}не удалось — прокси лежит, разбирайся руками${NC}"
        fi
    fi
    exit 1
fi

echo -e "${GREEN}✅ работает${NC}"
rm -f "${CONFIG}.bak" "${SECRET_FILE}.bak"

DOMAIN_HEX=$(printf '%s' "$FAKE_DOMAIN" | xxd -ps | tr -d '\n')
SERVER_IP=$(curl -s -4 ifconfig.me)
SECRET="ee${SECRET_HEX}${DOMAIN_HEX}"
LINK="tg://proxy?server=${SERVER_IP}&port=${HOST_PORT}&secret=${SECRET}"
echo "$LINK" > "${DIR}/link-${SLOT}.txt"

MESSAGE="🔄 Новая ссылка на прокси

🌐 Сервер: ${SERVER_IP}
🔌 Порт: ${HOST_PORT}
🔑 Секрет: ${SECRET}

🔗 ${LINK}

Две прежние ссылки ещё работают, но отключатся через пару ротаций — переключись сейчас."

if [ -z "${BOT_TOKEN:-}" ]; then
    echo -e "${YELLOW}⚠️  Нет токена бота (TELEGRAM_BOT_TOKEN в ${BRIDGE_DIR}/.env)"
    echo -e "   — ссылки подписчикам не разосланы.${NC}"
elif [ -f "$SUBS_FILE" ]; then
    while IFS= read -r chat_id || [ -n "$chat_id" ]; do
        [ -z "$chat_id" ] && continue
        curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
          --data-urlencode "chat_id=${chat_id}" \
          --data-urlencode "text=${MESSAGE}" > /dev/null
    done < "$SUBS_FILE"
    echo "📨 Разослано подписчикам: $(grep -c . "$SUBS_FILE")"
fi

{
    echo "SERVER=${SERVER_IP}"
    echo "ACTIVE_SLOT=${SLOT}"
    echo "PORT=${HOST_PORT}"
    echo "SECRET=${SECRET}"
    echo "DOMAIN=${FAKE_DOMAIN}"
    echo "LINK=${LINK}"
    for s in $SLOTS; do
        [ "$s" = "$SLOT" ] && continue
        l=$(cat "${DIR}/link-${s}.txt" 2>/dev/null)
        [ -n "$l" ] && echo "LINK_${s^^}=${l}"
    done
} > ~/mtproto_config.txt

# Копия для моста: сервис работает под maxbridge, в /root ему хода нет.
if [ -d "$(dirname "$BRIDGE_COPY")" ]; then
    install -m 644 ~/mtproto_config.txt "$BRIDGE_COPY"
fi

echo ""
echo "📊 СЕЙЧАС РАБОТАЮТ:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
for s in $SLOTS; do
    l=$(cat "${DIR}/link-${s}.txt" 2>/dev/null)
    [ -z "$l" ] && continue
    [ -s "${DIR}/secret-${s}.txt" ] || continue
    if [ "$s" = "$SLOT" ]; then
        echo -e "🆕 слот ${s^^}:"
        echo -e "${GREEN}${l}${NC}"
    else
        echo -e "♻️  слот ${s^^}:"
        echo -e "${YELLOW}${l}${NC}"
    fi
done
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
