# Установщик Remnanode с Caddy

Версия [x1roko/node-setup](https://github.com/x1roko/node-setup) под Caddy. Предназначена для **нового сервера Ubuntu 22.04+ или Debian 11+**.

## Запуск

```bash
git clone https://github.com/vladislav-KN/node-setup.git
cd node-setup
sudo bash install.sh
```

Скрипт запросит домен, email для ACME, `SECRET_KEY` из панели, веб-приложение для сайта, пути XHTTP и gRPC, способ получения сертификата, SSH-порт и включение BBR. Секреты вводятся скрыто и сохраняются с правами `0600`. Python 3 при необходимости устанавливается автоматически; дополнительных Python-пакетов установщику не нужно.

Существующую ноду с NGINX `install.sh` **не переустанавливает**: он остановится перед изменениями. Для исходной установки node-setup добавлен отдельный [скрипт миграции с резервной копией и откатом](MIGRATION_RU.md). Повторный запуск своей новой Caddy-установки через `install.sh` сохраняет сертификаты и резервную копию прежних конфигов в `/opt/remnanode/backups`.

## Схема работы

```text
443 -> Xray / REALITY -> PROXY protocol + TLS -> /dev/shm/nginx.sock -> Caddy
  /xhttppath/* -> /dev/shm/xrxh.socket, HTTP/1.1
  /grpcpath/*  -> /dev/shm/grpc.socket, h2c
  прочие пути -> 127.0.0.1:8080 -> выбранное веб-приложение
80 -> Caddy: проверка сертификата и перенаправление на HTTPS
```

Имя `/dev/shm/nginx.sock` сохранено для совместимости: внутри работает Caddy. Он разбирает PROXY protocol до TLS, передаёт IP клиента и сохраняет исходные пути запросов. **Публичный TCP-порт 443 остаётся у Xray.** Порт 9443 не используется.

Полный профиль Xray нужно настроить в панели Remnawave:

* REALITY: `target: /dev/shm/nginx.sock`, `xver: 1`, `serverNames` — домен ноды.
* XHTTP: входящий сокет `/dev/shm/xrxh.socket`, выбранный путь, например `/xhttppath/`.
* gRPC: входящий сокет `/dev/shm/grpc.socket`, выбранное имя сервиса, например `/grpcpath`.
* TLS завершает Caddy. Внутренние XHTTP/gRPC-сокеты работают без TLS. Старые пути `/etc/nginx/certs` для профилей с прямым TLS не создаются.

## Сертификаты

1. **http** — по умолчанию. Caddy получает и продлевает сертификат через HTTP-01 на порту 80. A/AAAA домена должны вести на сервер, а порт 80 — быть доступным снаружи. TLS-ALPN отключён, поскольку 443 занят Xray.
2. **cloudflare** — DNS-01 через API Cloudflare. Нужен токен с DNS Edit и Zone Read только для нужной зоны. Собирается Caddy `2.11.9` с модулем `caddy-dns/cloudflare v0.2.4`. Для выдачи сертификата входящий порт не нужен; на 80 по-прежнему работает HTTPS-перенаправление.

Для REALITY запись домена в Cloudflare должна быть **DNS only**. Сам по себе DNS-01 не позволяет передавать REALITY через HTTP-прокси Cloudflare. Certbot, acme.sh и задания cron для сертификатов не устанавливаются: ими управляет Caddy.

## Где лежат файлы

| Путь | Назначение |
| --- | --- |
| `/opt/remnanode/compose.json` | Общий Docker Compose |
| `/opt/remnanode/caddy/config/caddy.json` | Рабочий конфиг Caddy в JSON |
| `/opt/remnanode/caddy/data` | Сертификаты и приватные ключи |
| `/opt/remnanode/reality-target.json` | Фрагмент настроек REALITY для панели |
| `.env` рядом с установщиком | Сохранённые параметры и секреты |

Наружу разрешаются SSH-порт, TCP 80, 443 и 2222. Веб-приложение публикуется только на `127.0.0.1:8080`. Существующие правила UFW сохраняются; добавляются нужные разрешения и включается UFW. Административный API Caddy доступен через Unix-сокет внутри контейнера.

```bash
sudo docker compose -f /opt/remnanode/compose.json ps
sudo docker compose -f /opt/remnanode/compose.json logs --tail=50 caddy
sudo docker compose -f /opt/remnanode/compose.json exec -T caddy caddy reload \
  --address unix//run/caddy-admin.sock --config /etc/caddy/caddy.json
```

Логин, пароль и постоянное хранение данных выбранного веб-приложения настраиваются по его документации. Как и в исходном установщике, универсальный шаблон сайта не создаёт его базы данных/тома и резервные копии.

## Проверка без установки

Пример использует фиктивный ключ. Он создаёт только файлы, не устанавливая пакеты и не запуская контейнеры:

```bash
DOMAIN=node.example.com EMAIL=admin@example.com SECRET_KEY=preview-only \
SERVICE_NAME=filebrowser python3 installer.py --render-only --output rendered
docker compose -f rendered/compose.json config --quiet
```

Для тестов нужны Python 3.10+, Caddy и пакеты из `requirements-test.txt`:

```bash
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
bash -n install.sh
```

Если Caddy отсутствует в PATH, задайте `CADDY_BIN`. На Linux проверяются настоящие Unix-сокеты, PROXY protocol перед TLS, передача POST XHTTP и gRPC через HTTP/2 с trailers. На Windows используются TCP-эквиваленты на loopback. Тестовый удостоверяющий центр временный и не устанавливается в системное доверие. Выдача публичного сертификата и VPN-подключение проверяются отдельно на настоящей ноде.
