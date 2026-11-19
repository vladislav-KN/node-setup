#!/usr/bin/env python3
"""Migrate the original node-setup NGINX proxy; default is a read-only check."""
import argparse
import contextlib
import copy
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid

import installer

ROOT = Path("/opt/remnanode")
PROXY = "remnanode-proxy"
NODE = "remnanode"
ROLE = "io.node-setup.role"
TRANSACTION = "io.node-setup.migration"

# Compare tokens, not whitespace/comments. Custom directives must be reviewed.
LEGACY_CONFIG = """
access_log off; error_log /dev/null crit;
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
server { listen 80; server_name DOMAIN; return 301 https://$host$request_uri; }
server {
 listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
 server_name DOMAIN; http2 on;
 ssl_certificate /etc/nginx/certs/fullchain.pem;
 ssl_certificate_key /etc/nginx/certs/privkey.key;
 location XHTTP {
  client_max_body_size 0; proxy_buffering off; proxy_request_buffering off;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header Host $host; proxy_http_version 1.1;
  proxy_set_header Connection "keep-alive";
  client_body_timeout 5m; proxy_read_timeout 315s; proxy_send_timeout 5m;
  proxy_pass http://unix:/dev/shm/xrxh.socket;
 }
 location GRPC {
  client_max_body_size 0; client_body_timeout 5m;
  grpc_set_header X-Real-IP $proxy_protocol_addr;
  grpc_set_header X-Forwarded-For $proxy_protocol_addr;
  grpc_set_header Host $host; grpc_socket_keepalive on;
  grpc_read_timeout 315s; grpc_send_timeout 5m;
  grpc_pass grpc://unix:/dev/shm/grpc.socket;
 }
 location / {
  proxy_pass http://127.0.0.1:8080;
  proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection $connection_upgrade;
 }
}
"""


def fail(message):
    raise installer.InstallError(message)


def run(args, optional=False):
    """Capture all Docker output: inspect/config/log output can contain secrets."""
    result = subprocess.run([str(value) for value in args], capture_output=True, text=True)
    if result.returncode and not optional:
        fail("A Docker operation failed. Inspect local Docker logs; secrets were not printed.")
    return result


def inspect(name, optional=False):
    result = run(["docker", "inspect", name], optional=optional)
    return None if result.returncode else json.loads(result.stdout)[0]


def tokens(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    result = []
    for token in lexer:
        result.extend(token if token and all(ch in "{};" for ch in token) else [token])
    return result


def parse_legacy(text):
    names = re.findall(r"\bserver_name\s+([^;]+);", text)
    paths = re.findall(r"\blocation\s+([^\s{}]+)\s*\{", text)
    if len(names) != 2 or names[0].strip() != names[1].strip() or len(paths) != 3 or paths[2] != "/":
        fail("Unsupported NGINX routes. Only the original single-domain node-setup template is migrated.")
    settings = installer.Settings("admin@example.com", names[0].strip(), "unused",
                                  xhttp_path=paths[0], grpc_path=paths[1]).validate()
    expected = LEGACY_CONFIG.replace("DOMAIN", settings.domain).replace("XHTTP", settings.xhttp_path).replace("GRPC", settings.grpc_path)
    if tokens(text) != tokens(expected):
        fail("Custom NGINX directives detected. No services changed; this configuration needs a separate migration.")
    return settings


def snapshot(container):
    return {"id": container["Id"], "started": container["State"]["StartedAt"],
            "restarts": container.get("RestartCount", 0),
            "networks": container.get("NetworkSettings", {}).get("Networks", {})}


def healthy(container):
    state = container.get("State", {})
    return state.get("Running") and not state.get("Paused") and state.get("Health", {}).get("Status", "healthy") == "healthy"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def discover(root=ROOT):
    root = Path(root).resolve()
    legacy = root / "nginx"
    compose = legacy / "docker-compose.yml"
    proxy = inspect(PROXY)
    labels = proxy.get("Config", {}).get("Labels") or {}
    if labels.get(ROLE) == "caddy-migrated":
        fail("This proxy has already been migrated. Its Compose file is in " + str(compose))
    project = labels.get("com.docker.compose.project", "")
    files = labels.get("com.docker.compose.project.config_files", "")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)
            or labels.get("com.docker.compose.service") != "nginx"
            or files != str(compose) or not healthy(proxy)
            or proxy["HostConfig"].get("NetworkMode") != "host"):
        fail("Unsupported proxy layout. Expected the running original node-setup NGINX Compose service.")
    expected_mounts = {
        "/etc/nginx/conf.d/default.conf": str(legacy / "nginx.conf"),
        "/etc/nginx/certs/fullchain.pem": str(legacy / "fullchain.pem"),
        "/etc/nginx/certs/privkey.key": str(legacy / "privkey.key"),
        "/dev/shm": "/dev/shm",
    }
    if {m["Destination"]: m["Source"] for m in proxy["Mounts"]} != expected_mounts:
        fail("Custom NGINX mounts detected. No services changed.")
    for path in [compose, legacy / "nginx.conf", legacy / "fullchain.pem", legacy / "privkey.key"]:
        if not path.is_file() or path.is_symlink():
            fail("Expected regular original NGINX configuration/certificate files.")
    config = (legacy / "nginx.conf").read_text()
    settings = parse_legacy(config)
    live = run(["docker", "exec", proxy["Id"], "cat", "/etc/nginx/conf.d/default.conf"]).stdout
    if tokens(live) != tokens(config):
        fail("The mounted NGINX configuration differs from its host file.")
    expanded = run(["docker", "exec", proxy["Id"], "nginx", "-T"]).stdout
    if tokens(expanded).count("server") != 2:
        fail("Extra NGINX server blocks detected. No services changed.")
    normalized = json.loads(run(["docker", "compose", "-p", project, "-f", compose,
                                 "config", "--format", "json"]).stdout)
    if set(normalized["services"]) != {"nginx"}:
        fail("The legacy proxy Compose file must contain only its nginx service.")
    service = normalized["services"]["nginx"]
    expected_command = ["sh", "-c", 'rm -f /dev/shm/nginx.sock && exec nginx -g "daemon off;"']
    allowed = {"image", "container_name", "network_mode", "restart", "command", "volumes"}
    if (set(service) - allowed or service.get("container_name") != PROXY
            or service.get("network_mode") != "host" or service.get("command") != expected_command
            or proxy["Config"].get("Cmd") != expected_command
            or proxy["Config"].get("Entrypoint") != ["/docker-entrypoint.sh"]):
        fail("Unsupported custom proxy Compose settings.")
    mounts = service.get("volumes", [])
    if ({m["target"]: m.get("source") for m in mounts} != expected_mounts
            or any(m.get("type") != "bind" or bool(m.get("read_only")) != (m["target"] != "/dev/shm") for m in mounts)
            or any(bool(m.get("RW")) != (m["Destination"] == "/dev/shm") for m in proxy["Mounts"])):
        fail("On-disk Compose mounts differ from the supported running proxy.")
    normalized["name"] = project
    normalized["services"]["nginx"]["image"] = proxy["Image"]
    # config serialization re-escapes dollars; it is used as a Compose document.
    node = inspect(NODE)
    covers = [json.loads(line) for line in run(["docker", "ps", "--format", "{{json .ID}}", "--filter", "publish=8080"]).stdout.splitlines() if line.strip()]
    candidates = []
    for container_id in covers:
        item = inspect(container_id)
        ports = item.get("NetworkSettings", {}).get("Ports", {})
        if any(p.get("HostIp") == "127.0.0.1" and p.get("HostPort") == "8080"
               for bindings in ports.values() for p in (bindings or [])):
            candidates.append(item)
    if len(candidates) != 1 or not healthy(node) or not healthy(candidates[0]):
        fail("Expected a healthy Remnanode and one running cover backend on 127.0.0.1:8080.")
    cover = candidates[0]
    for item in (node, cover):
        if (item.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") == project:
            fail("The proxy must have a separate Compose project from Remnanode and the cover backend.")
    return {"root": root, "compose": compose, "project": project, "proxy": proxy,
            "settings": settings, "rollback_compose": normalized,
            "untouched": {NODE: snapshot(node), cover["Name"].lstrip("/"): snapshot(cover)},
            "hashes": {str(path): digest(path) for path in [compose, legacy / "nginx.conf", legacy / "fullchain.pem", legacy / "privkey.key"]}}


def probe(settings, address=installer.SOCKET_PATH, proxy_protocol=True):
    network = socket.AF_UNIX if isinstance(address, str) else socket.AF_INET
    with socket.socket(network, socket.SOCK_STREAM) as raw:
        raw.settimeout(5)
        raw.connect(address)
        if proxy_protocol:
            raw.sendall(b"PROXY TCP4 127.0.0.1 127.0.0.1 50000 443\r\n")
        with ssl.create_default_context().wrap_socket(raw, server_hostname=settings.domain) as tls:
            tls.sendall(("GET / HTTP/1.1\r\nHost: " + settings.domain + "\r\nConnection: close\r\n\r\n").encode())
            response = http.client.HTTPResponse(tls)
            response.begin()
            response.read(1024 * 1024)
            if not 200 <= response.status < 500:
                fail("Cover service did not return a usable response.")
            return response.status


def assert_untouched(plan):
    for name, before in plan["untouched"].items():
        current = inspect(name)
        if not healthy(current) or snapshot(current) != before:
            fail("Remnanode/cover state changed during migration; the proxy will be rolled back.")


def render(plan, folder, transaction):
    folder = Path(folder)
    settings = plan["settings"]
    # Reuse the tested proxy service, never render or start node/cover services.
    installer.render(settings, folder)
    service = installer.compose_config(settings)["services"]["caddy"]
    service["container_name"] = PROXY
    service["labels"] = {ROLE: "caddy-migrated", TRANSACTION: transaction}
    service["volumes"] = [str(folder / "caddy/config") + ":/etc/caddy:ro",
                          str(folder / "caddy/data") + ":/data",
                          str(folder / "caddy/runtime") + ":/config", "/dev/shm:/dev/shm:rw"]
    if "build" in service:
        service["build"]["context"] = str(folder / "caddy")
    document = {"name": plan["project"], "services": {"nginx": service}}
    installer.write_private(folder / "compose.json", json.dumps(document, indent=2) + "\n")
    return document


def compose(plan, manifest=None, project=None):
    return ["docker", "compose", "-p", project or plan["project"], "-f", manifest or plan["compose"]]


def stage(plan, folder, document, transaction):
    """Exercise the same Docker image/TLS routes on a second socket, before cutover."""
    folder = Path(folder)
    stage_root = folder / "stage"
    stage_root.mkdir(mode=0o700)
    for name in ("data", "runtime"):
        (stage_root / name).mkdir(mode=0o700)
    socket_path = "/dev/shm/node-setup-check-" + transaction + ".sock"
    cfg = installer.caddy_config(plan["settings"])
    cfg["admin"] = {"disabled": True, "config": {"persist": False}}
    servers = cfg["apps"]["http"]["servers"]
    del servers["redirect"]
    servers["selfsteal"]["listen"] = ["unix/" + socket_path]
    servers["selfsteal"]["automatic_https"] = {"disable": True}
    cfg["apps"]["tls"] = {"certificates": {"load_files": [
        {"certificate": "/legacy/fullchain.pem", "key": "/legacy/privkey.key"}]}}
    installer.write_private(stage_root / "caddy.json", json.dumps(cfg))
    service = copy.deepcopy(document["services"]["nginx"])
    service.pop("build", None)
    service.pop("environment", None)
    service["container_name"] = "node-setup-check-" + transaction
    service["restart"] = "no"
    service["entrypoint"] = ["caddy"]
    service["command"] = ["run", "--config", "/etc/caddy/caddy.json"]
    service["labels"] = {ROLE: "migration-check", TRANSACTION: transaction}
    service["volumes"] = [str(stage_root) + ":/etc/caddy:ro",
                          str(stage_root / "data") + ":/data", str(stage_root / "runtime") + ":/config",
                          str(plan["root"] / "nginx") + ":/legacy:ro", "/dev/shm:/dev/shm:rw"]
    manifest = stage_root / "compose.json"
    installer.write_private(manifest, json.dumps({"services": {"check": service}}))
    base = compose(plan, manifest, "node-setup-check-" + transaction)
    try:
        run(base + ["up", "-d", "--no-deps", "--pull", "never", "check"])
        deadline = time.monotonic() + 30
        while True:
            try:
                if probe(plan["settings"], socket_path) != plan["baseline"]:
                    fail("The staged Caddy cover response differs from NGINX.")
                return
            except (OSError, ssl.SSLError):
                if time.monotonic() >= deadline:
                    fail("Staged Caddy TLS check failed. NGINX is still running.")
                time.sleep(0.5)
    finally:
        run(base + ["down"], optional=False)
        path = Path(socket_path)
        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


def save_state(backup, state):
    installer.write_private(Path(backup) / "state.json", json.dumps(state, indent=2) + "\n")


def prepare_backup(plan, folder, document, transaction):
    backup = Path(folder) / "backup"
    backup.mkdir(mode=0o700)
    for name in ("docker-compose.yml", "nginx.conf", "fullchain.pem", "privkey.key"):
        installer.write_private(backup / name, (plan["root"] / "nginx" / name).read_text())
    installer.write_private(backup / "proxy-inspect.json", json.dumps(plan["proxy"], indent=2))
    installer.write_private(backup / "rollback-compose.json", json.dumps(plan["rollback_compose"], indent=2))
    for name in ("migrate.py", "installer.py"):
        installer.write_private(backup / name, (Path(__file__).resolve().parent / name).read_text())
    installer.write_private(backup / "rollback.sh", '#!/bin/sh\nset -eu\nSCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec python3 "$SCRIPT_DIR/migrate.py" --rollback "$SCRIPT_DIR"\n')
    state = {"version": 1, "phase": "prepared", "root": str(plan["root"]),
             "compose": str(plan["compose"]), "project": plan["project"],
             "transaction": transaction, "old_id": plan["proxy"]["Id"],
             "old_image": plan["proxy"]["Image"], "domain": plan["settings"].domain,
             "old_compose_hash": digest(plan["compose"]),
             "config_hash": digest(plan["root"] / "nginx/nginx.conf"),
             "new_compose_hash": hashlib.sha256((json.dumps(document, indent=2) + "\n").encode()).hexdigest()}
    save_state(backup, state)
    return backup, state


def rollback(backup):
    backup = Path(backup).resolve()
    state = json.loads((backup / "state.json").read_text())
    root = Path(state["root"])
    if (state.get("version") != 1 or root != ROOT
            or Path(state["compose"]) != root / "nginx/docker-compose.yml"
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", state["project"])):
        fail("Unsupported rollback state.")
    current = inspect(PROXY, optional=True)
    labels = (current or {}).get("Config", {}).get("Labels") or {}
    allowed_hashes = {state["old_compose_hash"], state["new_compose_hash"], digest(backup / "rollback-compose.json")}
    if digest(state["compose"]) not in allowed_hashes:
        fail("The proxy Compose file changed outside this migration. Review it before rollback.")
    if current and current["Image"] == state["old_image"] and current["Id"] == state["old_id"]:
        # Failure between stopping NGINX and removing its container.
        run(["docker", "start", current["Id"]])
        installer.write_private(state["compose"], (backup / "docker-compose.yml").read_text())
    else:
        if current and (labels.get(TRANSACTION) != state["transaction"] or labels.get(ROLE) != "caddy-migrated"):
            if state["phase"] == "rolled_back" and current["Image"] == state["old_image"]:
                print("NGINX has already been restored.")
                return
            fail("The proxy was replaced outside this migration. Rollback will not stop it.")
        if digest(root / "nginx/nginx.conf") != state["config_hash"]:
            fail("The legacy NGINX configuration changed. Review it before rollback.")
        # Validate the pinned old image and its current certificate mounts first.
        base = ["docker", "compose", "-p", state["project"], "-f", backup / "rollback-compose.json"]
        run(base + ["run", "--rm", "--no-deps", "--pull", "never", "--entrypoint", "nginx", "nginx", "-t"])
        state["phase"] = "rolling_back"
        save_state(backup, state)
        if current:
            run(["docker", "stop", current["Id"]])
            run(["docker", "rm", current["Id"]])
        # Recreate at the original Compose path/project with the saved image ID.
        installer.write_private(state["compose"], (backup / "rollback-compose.json").read_text())
        run(["docker", "compose", "-p", state["project"], "-f", state["compose"],
             "up", "-d", "--no-deps", "--pull", "never", "nginx"])
        installer.write_private(state["compose"], (backup / "docker-compose.yml").read_text())
    restored = inspect(PROXY)
    if restored["Image"] != state["old_image"] or not healthy(restored):
        fail("NGINX rollback did not become ready. The backup and pinned image manifest were retained.")
    settings = installer.Settings("admin@example.com", state["domain"], "unused").validate()
    wait_ready(settings, timeout=30)
    state["phase"] = "rolled_back"
    state["old_id"] = restored["Id"]
    save_state(backup, state)
    print("NGINX restored. Remnanode and the cover container were not recreated.")


def wait_ready(settings, expected=None, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            statuses = [probe(settings), probe(settings, ("127.0.0.1", 443), False)]
            if expected is not None and any(status != expected for status in statuses):
                fail("The HTTPS cover response changed after cutover.")
            return
        except (OSError, ssl.SSLError, http.client.HTTPException):
            time.sleep(1)
    fail("Publicly trusted TLS/REALITY fallback did not become ready in time.")


def migrate(plan):
    transaction = uuid.uuid4().hex[:12]
    folder = plan["root"] / "caddy-migration" / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + transaction)
    folder.mkdir(parents=True, mode=0o700)
    document = render(plan, folder, transaction)
    manifest = folder / "compose.json"
    backup, state = prepare_backup(plan, folder, document, transaction)
    print("Backup and standalone rollback: " + str(backup / "rollback.sh"), flush=True)
    print("Preparing the Caddy image and validating its configuration; NGINX is still running...", flush=True)
    base = compose(plan, manifest)
    run(base + ["config", "--quiet"])
    if plan["settings"].tls_mode == "cloudflare":
        run(base + ["build", "--pull", "nginx"])
    else:
        run(base + ["pull", "nginx"])
    run(base + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "caddy", "nginx",
                "validate", "--config", "/etc/caddy/caddy.json"])
    stage(plan, folder, document, transaction)
    assert_untouched(plan)
    if snapshot(inspect(PROXY)) != snapshot(plan["proxy"]) or any(digest(path) != value for path, value in plan["hashes"].items()):
        fail("The existing proxy/files changed during preparation. NGINX was not stopped.")
    print("Caddy staging passed. Switching the proxy; HTTPS may pause for certificate issuance (up to five minutes).", flush=True)
    state["phase"] = "switching"
    save_state(backup, state)
    try:
        run(["docker", "stop", plan["proxy"]["Id"]])
        run(["docker", "rm", plan["proxy"]["Id"]])
        installer.write_private(plan["compose"], manifest.read_text())
        run(compose(plan) + ["up", "-d", "--no-deps", "--pull", "never", "nginx"])
        wait_ready(plan["settings"], plan["baseline"])
        assert_untouched(plan)
        state["phase"] = "complete"
        save_state(backup, state)
    except BaseException:
        print("Cutover failed; restoring NGINX...", flush=True)
        try:
            rollback(backup)
        except BaseException:
            print("Automatic rollback failed. Run: sudo bash " + str(backup / "rollback.sh"), file=sys.stderr)
        raise
    print("SUCCESS: Caddy is live. Node/cover IDs, start times and networks are unchanged.")
    print("Existing Compose path, project and proxy name preserved for renewal hooks.")
    print("Legacy certificate renewal is retained for certificates mounted in Remnanode.")
    print("Manual rollback: sudo bash " + str(backup / "rollback.sh"))


@contextlib.contextmanager
def exclusive_lock():
    import fcntl
    with open("/run/lock/node-setup-migrate.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another migration or rollback is running.")
        yield


def interrupted(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Read-only checks (default)")
    action.add_argument("--apply", action="store_true", help="Back up and migrate this node's proxy")
    action.add_argument("--rollback", type=Path, metavar="BACKUP_DIR")
    parser.add_argument("--email", help="ACME email; also read from EMAIL or the saved .env")
    parser.add_argument("--tls-mode", choices=["http", "cloudflare"])
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent / ".env")
    args = parser.parse_args()
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            fail("Run on the node with sudo. The default --check does not change services or files.")
        if not shutil.which("docker"):
            fail("Docker is required. No packages or services were changed.")
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        if args.rollback:
            with exclusive_lock():
                rollback(args.rollback)
            return 0
        plan = discover()
        settings = plan["settings"]
        plan["baseline"] = probe(settings)
        if probe(settings, ("127.0.0.1", 443), False) != plan["baseline"]:
            fail("The direct socket and local public port 443 do not serve the same cover response.")
        assert_untouched(plan)
        print("Original NGINX layout, rollback config, certificate, cover and REALITY fallback checked.")
        print("Domain: " + settings.domain + "; XHTTP: " + settings.xhttp_path + "; gRPC: " + settings.grpc_path)
        if not args.apply:
            print("READ-ONLY CHECK PASSED. To migrate, run sudo bash migrate.sh --apply --email YOUR_EMAIL")
            return 0
        saved = installer.load_env(args.env_file)
        settings.email = args.email or os.environ.get("EMAIL") or saved.get("EMAIL") or installer.ask("ACME email")
        settings.tls_mode = args.tls_mode or os.environ.get("TLS_MODE") or saved.get("TLS_MODE", "http")
        if settings.tls_mode == "cloudflare":
            settings.cloudflare_token = os.environ.get("CF_Token") or saved.get("CF_Token") or installer.ask("Cloudflare DNS API token", secret=True)
        settings.validate()
        with exclusive_lock():
            migrate(plan)
    except (installer.InstallError, EOFError, KeyboardInterrupt) as exc:
        print("STOPPED: " + (str(exc) if isinstance(exc, installer.InstallError) else "Input interrupted."), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
