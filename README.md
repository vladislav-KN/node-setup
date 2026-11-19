# Remnanode installer with Caddy

Installs Remnanode, a cover web application and Caddy on a **fresh Ubuntu 22.04+ or Debian 11+ server**. Based on [x1roko/node-setup](https://github.com/x1roko/node-setup), under the original MIT license.

[Русский](docs/README_RU.md) · [中文](docs/README_CN.md)

## Install

```bash
git clone https://github.com/vladislav-KN/node-setup.git
cd node-setup
sudo bash install.sh
```

The installer asks for the domain, ACME email, panel `SECRET_KEY`, cover application, XHTTP/gRPC paths, certificate validation method, SSH port and optional BBR. Keys/tokens use hidden input and are saved with mode `0600`. Python 3 is bootstrapped when missing; normal runtime needs only its standard library.

This is an installer, **not an in-place migration tool**. It refuses an existing NGINX node or containers owned by another stack. Existing nodes need a separate migration with backups. Reruns of this Caddy installation retain its certificate data and save previous configurations under `/opt/remnanode/backups`.

## Traffic layout

```text
Internet :443 -> Xray / REALITY -> PROXY protocol + TLS
                                -> /dev/shm/nginx.sock -> Caddy
                                   /xhttppath/* -> /dev/shm/xrxh.socket (HTTP/1.1)
                                   /grpcpath/*  -> /dev/shm/grpc.socket (h2c)
                                   other paths -> 127.0.0.1:8080 -> cover app
Internet :80  -> Caddy HTTP-01 validation / HTTPS redirect
```

The old socket name is intentional: existing REALITY `target` values remain usable. Caddy parses PROXY protocol **before TLS**, preserves paths and forwards the client address. Caddy binds HTTPS only to the Unix socket; **it never takes Xray's TCP port 443**. Port 9443 is not used.

The installer does not create your complete Xray profile. Configure it in the Remnawave panel after installation:

* REALITY target `/dev/shm/nginx.sock`, `xver: 1`, server name equal to your domain.
* XHTTP inbound socket `/dev/shm/xrxh.socket`, path equal to the chosen XHTTP path.
* gRPC inbound socket `/dev/shm/grpc.socket`, service path equal to the chosen gRPC path.
* TLS termination and public certificate renewal belong to Caddy. The cleartext XHTTP/gRPC backends do not need certificate mounts. Old `/etc/nginx/certs` paths are not generated for direct-TLS profiles.

## Certificates

* **http** (default): Caddy obtains and renews certificates through HTTP-01 on TCP 80. A/AAAA records must route to this server and external port 80 must be reachable. TLS-ALPN is disabled because Xray owns 443.
* **cloudflare**: Caddy uses DNS-01. Provide an API token restricted to the zone with DNS Edit and Zone Read. The installer builds Caddy `2.11.9` with `caddy-dns/cloudflare v0.2.4`. No external challenge port is required for issuance; port 80 still serves the redirect.

For REALITY, keep the node's DNS record **DNS only** in Cloudflare: DNS-01 validation does not make REALITY traffic compatible with Cloudflare's HTTP proxy. No Certbot, acme.sh, certificate-copy hooks or renewal cron jobs are installed.

## Files and ports

| Path | Purpose |
| --- | --- |
| `/opt/remnanode/compose.json` | The complete Docker Compose stack |
| `/opt/remnanode/caddy/config/caddy.json` | Active Caddy JSON configuration |
| `/opt/remnanode/caddy/data` | Persistent certificates and private keys |
| `/opt/remnanode/reality-target.json` | REALITY target fragment for the panel |
| `.env` beside `install.sh` | Private saved installation settings |

| Port | Purpose |
| --- | --- |
| TCP 80 | Caddy validation / redirect |
| TCP 443 | Xray, configured in the panel |
| TCP 2222 | Remnanode API |
| `127.0.0.1:8080` | Cover backend; not publicly published |
| Configured SSH port | Preserved in UFW |

The installer adds UFW allow rules for SSH, 80, 443 and 2222, then enables UFW without resetting existing rules. Caddy's admin API uses a private Unix socket inside its container.

```bash
sudo docker compose -f /opt/remnanode/compose.json ps
sudo docker compose -f /opt/remnanode/compose.json logs --tail=50 caddy
sudo docker compose -f /opt/remnanode/compose.json exec -T caddy caddy reload \
  --address unix//run/caddy-admin.sock --config /etc/caddy/caddy.json
```

Choose/configure the cover application's credentials and persistence according to its own documentation. The generic cover service keeps the original upstream installer behavior; its application data is not provisioned or backed up by this project.

## Preview and checks

Generate a preview without installing packages, changing the firewall or starting containers. The example key is deliberately a dummy:

```bash
DOMAIN=node.example.com EMAIL=admin@example.com SECRET_KEY=preview-only \
SERVICE_NAME=filebrowser python3 installer.py --render-only --output rendered
docker compose -f rendered/compose.json config --quiet
```

With Python 3.10+, a Caddy binary and the test dependencies:

```bash
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
bash -n install.sh
```

Set `CADDY_BIN` when Caddy is not on PATH. Linux integration checks use real Unix sockets, PROXY protocol, TLS, XHTTP POSTs and gRPC over HTTP/2 with trailers. Windows checks use loopback TCP equivalents because Python on Windows lacks Unix socket support. Tests use a disposable internal CA without installing it in the system trust store. Public ACME issuance and live VPN traffic still need a real domain/node.
