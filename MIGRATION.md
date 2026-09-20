# Перенос моста на другой сервер

Сервис состоит из кода (git), каталога `data/` и файла `.env` — всё остальное
ставится заново. Переезд занимает около двадцати минут.

**Главный риск не технический.** MAX может счесть вход с нового IP
подозрительным и отозвать сессии — тогда каждому аккаунту понадобится
повторный вход по SMS. Переезжай тогда, когда телефоны под рукой. Темы,
привязки групп и история маршрутизации при этом сохраняются.

## 1. Новый сервер: окружение

```bash
apt update && apt install -y git python3-venv python3-pip
useradd -r -s /usr/sbin/nologin -d /opt/maxtotelega maxbridge
mkdir -p /opt/maxtotelega/app
git clone https://github.com/kostya4118/MaxToTelega.git /opt/maxtotelega/app
cd /opt/maxtotelega/app
python3 -m venv .venv
.venv/bin/pip install -U pip -r requirements.txt
```

Нужен Python 3.10 или новее.

## 2. Старый сервер: остановить и упаковать

Останавливать обязательно: два процесса с одним токеном бота дерутся за
`getUpdates`, и сообщения теряются у обоих.

```bash
sudo systemctl stop maxtotelega
sudo systemctl disable maxtotelega
cd /opt/maxtotelega/app
sudo tar czf /root/mtt-migrate.tar.gz .env data
sudo ls -lh /root/mtt-migrate.tar.gz
```

## 3. Перенести архив

```bash
scp -P <порт> /root/mtt-migrate.tar.gz root@<новый-ip>:/root/
```

В архиве лежат сессии MAX — это полный доступ к аккаунтам пользователей.
Удали его с обоих серверов сразу после распаковки.

## 4. Новый сервер: распаковать и закрыть права

```bash
cd /opt/maxtotelega/app
tar xzf /root/mtt-migrate.tar.gz
chown -R maxbridge:maxbridge /opt/maxtotelega
chmod 600 .env data/*.db
rm /root/mtt-migrate.tar.gz
```

## 5. Новый сервер: сервис

```bash
sudo tee /etc/systemd/system/maxtotelega.service > /dev/null <<'EOF'
[Unit]
Description=MaxToTelega bridge (MAX <-> Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=maxbridge
Group=maxbridge
WorkingDirectory=/opt/maxtotelega/app
ExecStart=/opt/maxtotelega/app/.venv/bin/python -u bridge.py
Restart=always
RestartSec=10
TimeoutStopSec=30
MemoryMax=400M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/maxtotelega/app/data
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now maxtotelega
sudo journalctl -u maxtotelega -f
```

## 6. Проверить

В логах ждём `Поднято аккаунтов: N`, дальше должно быть тихо. Затем напиши в
MAX на каждый номер — сообщения придут в те же темы, что и раньше.

Если в логах `FAIL_LOGIN_TOKEN` — MAX не принял вход с нового IP. Бот сам
пришлёт владельцу аккаунта кнопку «📲 Войти заново по SMS»: нажми её и введи
код. Можно и вручную — `/relogin <id>` в личке бота.

## Что переносить не нужно

- **Telegram** — бот ходит наружу сам, вебхуков нет, IP сервера нигде не
  записан. Перенастраивать ничего не надо.
- **Виртуальное окружение** — ставится заново на шаге 1, переносить `.venv`
  между серверами нельзя.
- **Логи** — `data/bridge.log` уезжает вместе с каталогом. Если когда-то
  включался `LOG_LEVEL=DEBUG`, в нём лежат токены сессий MAX: после распаковки
  очисти его через `truncate -s 0 data/bridge.log`.

## MTProto-прокси

Если прокси живёт на том же сервере, переноси его **после** моста: список
подписчиков лежит в `data/proxy_subscribers.txt`, и скрипт ротации должен
найти его на месте, чтобы разослать новые ссылки.

Перед упаковкой моста сними cron прокси на старом сервере — иначе он
продолжит слать людям ссылки на уходящую машину:

```bash
crontab -l | grep -v start-mtproxy | crontab -
```

Скрипт ротации лежит в этом же репозитории — `scripts/start-mtproxy.sh`.
Отдельно качать ничего не нужно:

```bash
curl -fsSL https://get.docker.com | sh
apt install -y xxd || apt install -y vim-common
ln -sf /opt/maxtotelega/app/scripts/start-mtproxy.sh /root/start-mtproxy.sh
```

Пути к данным моста скрипт берёт сам: подписчиков из
`data/proxy_subscribers.txt`, копию конфига кладёт в `data/mtproto_config.txt`
(из неё кнопка «📡 MTProto прокси» в боте отдаёт актуальную ссылку). Токен
бота читается из `.env` моста — в самом скрипте его нет и быть не должно.

Если мост стоит не в `/opt/maxtotelega/app`, укажи каталог:

```bash
BRIDGE_DIR=/путь/к/мосту bash /root/start-mtproxy.sh
```

Запускаем и смотрим, что ссылки ушли:

```bash
bash /root/start-mtproxy.sh    # ждём «📨 Разослано подписчикам: N»
docker ps --format '{{.Names}}  {{.Status}}  {{.Ports}}'
```

Контейнер должен быть `Up`, а не `Restarting`. Затем cron:

```bash
(crontab -l 2>/dev/null; \
 echo '0 3 */3 * * /root/start-mtproxy.sh >> /var/log/mtproxy-rotate.log 2>&1') | crontab -
```

Старый контейнер прокси оставь на пару дней, пока подписчики не переключатся,
и только потом `docker rm -f mtproto-proxy`.

### Если контейнер циклически перезапускается

```
✗ Failed to load config '/etc/mtproto-proxy/config.toml': error.AccessDenied
```

Процесс внутри контейнера работает не от root, а скрипт создаёт конфиг с
правами `600`. Лечится так:

```bash
chmod 644 /etc/mtproto-zig/config.toml
docker restart mtproto-proxy
```

В `scripts/start-mtproxy.sh` это уже исправлено. Если используешь свою копию
скрипта — замени в ней `chmod 600 "$CONFIG"` на `chmod 644 "$CONFIG"`. Права
на файл секрета (`chmod 600 "$SECRET_FILE"`) трогать не нужно: его читает
только скрипт.

## Откат

Старый сервер не гаси сутки. Сервис там остановлен и убран из автозапуска, так
что мешать он не будет. Если что-то пойдёт не так — останови новый сервис и
верни старый:

```bash
sudo systemctl enable --now maxtotelega
```

Учти: после повторного входа на новом сервере сессии на старом уже
недействительны, и аккаунтам снова понадобится вход по SMS.
