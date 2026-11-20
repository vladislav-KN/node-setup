# Migrate the original node-setup NGINX proxy

This is a conservative **proxy-only migration for the original x1roko/node-setup layout**, not for the panel, bot, or arbitrary NGINX sites. Docker Compose, Python 3, a currently trusted certificate and a working node must already exist. [Русская инструкция](MIGRATION_RU.md).

## Check first

From this fork on the node server:

```bash
sudo bash migrate.sh --check
```

No arguments also means check only. It reads Docker/NGINX settings and makes ordinary HTTPS GET requests to `/`; it does not write files, restart containers, install packages or alter firewall/renewal jobs. No response bodies or secrets are printed.

The supported proxy is `remnanode-proxy` in its own Compose project, using `/opt/remnanode/nginx/docker-compose.yml`, `nginx.conf`, `fullchain.pem` and `privkey.key`. Domain and transport paths are inferred. The cover backend must be published on `127.0.0.1:8080`; trusted TLS must work through `/dev/shm/nginx.sock` with PROXY protocol and the REALITY fallback on `127.0.0.1:443`.

Both original-template variants are supported: with the global `access_log off;` / `error_log /dev/null crit;` prefix before `map`, or with both lines absent. Arbitrary logging directives and route changes are still refused.

Extra server blocks, custom directives/routes/mounts or a different Compose layout are refused. Public-IP-only listeners, other REALITY targets and direct NGINX TLS arrangements need individual adaptation. NGINX inspection reads current files, not proof of their last reload; live HTTPS responses and a runnable rollback configuration are checked as well.

## Apply

```bash
sudo bash migrate.sh --apply --email you@example.com
# Or DNS-01, with a hidden Cloudflare token prompt:
sudo bash migrate.sh --apply --email you@example.com --tls-mode cloudflare
```

Email also comes from `EMAIL` or the local saved `.env`. The panel key is never requested. HTTP-01 requires correct A/AAAA records and externally reachable port 80. Cloudflare DNS-01 requires DNS Edit + Zone Read for the selected zone; its module is built before stopping NGINX. The token is stored only in the private Compose file, not the Caddy JSON/log. For REALITY the node record must stay **DNS only**.

Before cutover, the script saves configs, certificate/key, proxy inspection, a rollback manifest pinned to the old image ID and a standalone rollback script. It validates Caddy and starts a test instance on another Unix socket with the old valid certificate. Only after the staging check and container/file drift checks does it stop/remove the old proxy and start Caddy. Remnanode and the cover are not recreated.

**HTTPS may pause during certificate issuance, for up to five minutes before rollback.** VPN continuity is not guaranteed: new connections and XHTTP/gRPC traffic may be interrupted. Both the Unix socket and port 443 must return the baseline cover status with trusted TLS, and node/cover IDs, start times and networks must be unchanged. A caught cutover error or interruption attempts automatic rollback. Test a normal VPN client login afterwards.

## Compatibility and files

The active file remains `/opt/remnanode/nginx/docker-compose.yml` (JSON is valid Compose input). Its project, `nginx` service key and `remnanode-proxy` name are preserved, with a Caddy image. Existing certificate hook commands targeting this Compose file therefore restart Caddy rather than resurrect NGINX.

```bash
sudo docker compose -f /opt/remnanode/nginx/docker-compose.yml ps
sudo docker compose -f /opt/remnanode/nginx/docker-compose.yml logs --tail=50 nginx
```

New config/data and backups live in `/opt/remnanode/caddy-migration/<timestamp-ID>/`. Keep this directory private: it contains private keys and possibly a Cloudflare token. Remnanode/API credentials, Xray profile, Docker IPs, cover config, firewall, DNS and existing WARP are unchanged. Existing WARP may still be required by your Xray routes; it is not uninstalled. Fresh `install.sh` installs no WARP.

Legacy Certbot/acme.sh renewal is retained because its files may be mounted in Remnanode for separate direct-TLS inbounds. Those jobs can still restart the node under their existing rules. Caddy independently renews its own certificate. Migrating direct-TLS certificate consumers or disabling shared renewal schedules is outside this proxy migration.

## Rollback

The exact command is printed before cutover:

```bash
sudo bash /opt/remnanode/caddy-migration/<timestamp-ID>/backup/rollback.sh
```

It is self-contained: saved Python scripts do not need the original checkout. Rollback validates the old image/config, removes only its own Caddy proxy and recreates NGINX. It leaves the node/cover and newer legacy certificate files intact. Do not prune the old NGINX image while rollback is needed.

External Compose changes or a proxy replaced outside this migration cause a refusal to overwrite them. Power loss and `kill -9` cannot trigger the automatic handler; use the saved rollback script.

## Validation

Unit tests exercise pre-cutover failures, rollback triggers and refusal to overwrite unrelated state. A separate explicit Linux Docker test performs real container cutover, a legacy Compose restart, manual rollback and injected-failure rollback while checking node/cover identity. Its temporary TLS certificate and TCP relay stand in for public ACME/REALITY fallback; public issuance and a real VPN login still require an actual node.
