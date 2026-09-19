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
- **Логи** — `data/bridge.log` в архив не попадает намеренно.

## MTProto-прокси

Если прокси живёт на том же сервере, он переносится отдельно: скрипт ротации,
его cron и `mtproto_config.txt`. После переезда IP меняется, поэтому старые
ссылки перестанут работать — скрипт сгенерирует новые и разошлёт подписчикам
из `data/proxy_subscribers.txt` (он лежит в архиве).

## Откат

Старый сервер не гаси сутки. Сервис там остановлен и убран из автозапуска, так
что мешать он не будет. Если что-то пойдёт не так — останови новый сервис и
верни старый:

```bash
sudo systemctl enable --now maxtotelega
```

Учти: после повторного входа на новом сервере сессии на старом уже
недействительны, и аккаунтам снова понадобится вход по SMS.
