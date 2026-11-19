# 使用 Caddy 的 Remnanode 安装器

基于 [x1roko/node-setup](https://github.com/x1roko/node-setup)，保留 MIT 许可证。适用于全新的 Ubuntu 22.04+ 或 Debian 11+ 服务器。

```bash
git clone https://github.com/vladislav-KN/node-setup.git
cd node-setup
sudo bash install.sh
```

安装器会询问域名、ACME 邮箱、面板提供的 `SECRET_KEY`、网站应用、XHTTP/gRPC 路径、证书验证方式、SSH 端口和可选 BBR。密钥隐藏输入，配置文件权限为 `0600`。必要时自动安装 Python 3；安装器本身只使用 Python 标准库。

`install.sh` 不会迁移现有 NGINX 节点。检测到旧安装或其他项目的容器时会停止。原版 node-setup 节点可使用独立的 `migrate.sh`，包含备份和自动回滚，详见[迁移指南（英语）](MIGRATION_EN.md)。重复运行本 Caddy 安装会保留证书，并备份旧配置。

## 流量和证书

* 公网 TCP 443 保留给 Xray / REALITY。
* REALITY 目标保持 `/dev/shm/nginx.sock`，`xver: 1`；该 Unix 套接字实际由 Caddy 监听。
* Caddy 在 TLS 握手之前解析 PROXY protocol，并转发真实客户端地址。
* XHTTP 转发到 `/dev/shm/xrxh.socket`，使用 HTTP/1.1，保留请求路径。
* gRPC 转发到 `/dev/shm/grpc.socket`，使用 h2c，保留服务路径。
* 其他路径转发到仅监听 `127.0.0.1:8080` 的网站应用。
* TCP 80 用于 HTTP-01 验证及 HTTPS 重定向。9443 不使用。

默认 `http` 验证要求 A/AAAA 指向本服务器，公网 80 可达。TLS-ALPN 被禁用，因为 443 属于 Xray。可选择 `cloudflare` DNS-01 验证，提供仅限目标区域的 DNS Edit 和 Zone Read API 令牌；会构建带 `caddy-dns/cloudflare v0.2.4` 的 Caddy 2.11.9。DNS 验证无需入站挑战端口，但 80 仍用于重定向。

REALITY 的 DNS 记录应设为 Cloudflare **DNS only**。DNS-01 不会让 REALITY 支持 Cloudflare HTTP 代理。Caddy 自动签发和续期证书，无需 Certbot、acme.sh 或证书 cron 任务。

完整 Xray 配置仍需在 Remnawave 面板设置。内部 XHTTP/gRPC 套接字不使用 TLS；旧 `/etc/nginx/certs` 路径不再生成。

## 文件和管理

* `/opt/remnanode/compose.json`：整个 Compose 项目。
* `/opt/remnanode/caddy/config/caddy.json`：Caddy JSON 配置。
* `/opt/remnanode/caddy/data`：持久化证书和私钥。
* `/opt/remnanode/reality-target.json`：REALITY 目标片段。
* 安装器旁的 `.env`：私有安装设置。

UFW 保留现有规则，添加 SSH、80、443、2222 的 TCP 规则并启用。Caddy 管理接口使用容器内部 Unix 套接字。

```bash
sudo docker compose -f /opt/remnanode/compose.json ps
sudo docker compose -f /opt/remnanode/compose.json logs --tail=50 caddy
sudo docker compose -f /opt/remnanode/compose.json exec -T caddy caddy reload \
  --address unix//run/caddy-admin.sock --config /etc/caddy/caddy.json
```

请按所选网站应用的文档配置密码和持久化存储。通用网站模板与原安装器一样，不负责数据库/卷及其备份。

## 预览与测试

以下示例使用虚拟密钥，只生成文件，不修改系统服务：

```bash
DOMAIN=node.example.com EMAIL=admin@example.com SECRET_KEY=preview-only \
SERVICE_NAME=filebrowser python3 installer.py --render-only --output rendered
docker compose -f rendered/compose.json config --quiet
```

测试需要 Python 3.10+、Caddy 和测试依赖：

```bash
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
bash -n install.sh
```

可用 `CADDY_BIN` 指定二进制文件。Linux 测试使用真实 Unix 套接字；Windows 使用 loopback TCP 等价连接。测试覆盖 PROXY protocol、TLS、XHTTP POST 和 gRPC HTTP/2 trailers，并且不会安装测试 CA 到系统。公网 ACME 签发和实际 VPN 连接仍需在真实节点验证。
