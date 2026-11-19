#!/usr/bin/env python3
"""Fresh Remnanode installer: Caddy + REALITY/selfsteal, XHTTP and gRPC."""
import argparse
import dataclasses
import getpass
import http.client
import json
import os
from pathlib import Path
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import warnings

CADDY_IMAGE = "caddy:2.11.9-alpine"
CADDY_BUILDER = "caddy:2.11.9-builder-alpine"
CLOUDFLARE_MODULE = "github.com/caddy-dns/cloudflare@v0.2.4"
NODE_IMAGE = "remnawave/node:latest"
INSTALL_ROOT = Path("/opt/remnanode")
PROJECT = "remnanode-caddy"
SOCKET_PATH = "/dev/shm/nginx.sock"  # Existing Xray REALITY targets remain valid.
SERVICES = [
    ("filebrowser", "filebrowser/filebrowser", 80),
    ("memos", "neosmemo/memos:stable", 5230),
    ("pingvin-share", "stonith404/pingvin-share", 3000),
    ("excalidraw", "excalidraw/excalidraw", 80),
    ("searxng", "searxng/searxng", 8080),
    ("sharry", "jlesage/sharry", 9090),
    ("audiobookshelf", "advplyr/audiobookshelf", 80),
    ("kavita", "jvmilazz0/kavita", 5000),
    ("kodbox", "kodcloud/kodbox", 80),
    ("navidrome", "deluan/navidrome", 4533),
    ("gitea", "gitea/gitea", 3000),
    ("fluffy-web", "aceberg/fluffychat", 80),
]
ENV_KEYS = {"EMAIL", "DOMAIN", "SECRET_KEY", "SERVICE_NAME", "XHTTP_PATH", "GRPC_PATH",
            "TLS_MODE", "CF_Token", "SSH_PORT", "ENABLE_BBR"}


class InstallError(Exception):
    pass


@dataclasses.dataclass
class Settings:
    email: str
    domain: str
    secret_key: str = dataclasses.field(repr=False)
    service: str = "filebrowser"
    xhttp_path: str = "/xhttppath/"
    grpc_path: str = "/grpcpath"
    tls_mode: str = "http"
    cloudflare_token: str = dataclasses.field(default="", repr=False)
    ssh_port: int = 22
    enable_bbr: bool = False

    def validate(self):
        self.domain = self.domain.strip().lower()
        labels = self.domain.split(".")
        if (len(self.domain) > 253 or len(labels) < 2
                or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or all(label.isdigit() for label in labels)):
            raise InstallError("Enter a public DNS domain, without a scheme, port or path.")
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", self.email):
            raise InstallError("Invalid ACME email address.")
        if not self.secret_key or any(ch in self.secret_key for ch in "\r\n\0"):
            raise InstallError("SECRET_KEY must be a non-empty single line.")
        if self.service not in {item[0] for item in SERVICES}:
            raise InstallError("Unknown cover service.")
        if self.tls_mode not in {"http", "cloudflare"}:
            raise InstallError("TLS mode must be http or cloudflare.")
        if self.tls_mode == "cloudflare" and not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", self.cloudflare_token):
            raise InstallError("Cloudflare DNS mode requires a valid API token.")
        self.xhttp_path = normalize_path(self.xhttp_path, trailing=True)
        self.grpc_path = normalize_path(self.grpc_path, trailing=False)
        a, b = self.xhttp_path.rstrip("/"), self.grpc_path
        if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
            raise InstallError("XHTTP and gRPC paths must not overlap.")
        if not 1 <= int(self.ssh_port) <= 65535:
            raise InstallError("SSH port must be between 1 and 65535.")
        if int(self.ssh_port) in {80, 443, 2222, 8080}:
            raise InstallError("SSH port conflicts with a node/web port (80, 443, 2222 or 8080).")
        return self


def normalize_path(value, trailing=False):
    parts = value.strip("/").split("/")
    if not parts or any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9._~-]+", part) for part in parts):
        raise InstallError("Paths must contain safe, non-empty URL segments.")
    result = "/" + "/".join(parts)
    return result + "/" if trailing else result


def load_env(path):
    """Read an allowlist of literal KEY=value settings; never execute shell input."""
    result = {}
    if not Path(path).is_file():
        return result
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in ENV_KEYS:
            continue
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise InstallError("Invalid quoted value in saved settings.") from None
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        if not isinstance(value, str):
            raise InstallError("Saved values must be strings.")
        result[key.strip()] = value
    return result


def ask(label, current="", secret=False):
    if secret:
        if current and input(label + " is saved. Reuse it? [Y/n]: ").strip().lower() != "n":
            return current
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(label + " (hidden): ").strip()
            except (EOFError, OSError, getpass.GetPassWarning):
                raise InstallError("A terminal with hidden input is required.") from None
    return input(label + (" [" + str(current) + "]" if current else "") + ": ").strip() or current


def collect(args, env_path):
    saved = load_env(env_path)
    for key in ENV_KEYS:
        if key in os.environ:
            saved[key] = os.environ[key]
    domain = args.domain or saved.get("DOMAIN", "")
    email = args.email or saved.get("EMAIL", "")
    key = saved.get("SECRET_KEY", "")
    service = args.service or saved.get("SERVICE_NAME", "")
    mode = args.tls_mode or saved.get("TLS_MODE", "http")
    token = saved.get("CF_Token", "")
    xhttp = args.xhttp_path or saved.get("XHTTP_PATH", "/xhttppath/")
    grpc = args.grpc_path or saved.get("GRPC_PATH", "/grpcpath")
    ssh = saved.get("SSH_PORT", os.environ.get("SSH_CONNECTION", "").split()[-1] if os.environ.get("SSH_CONNECTION") else "22")
    bbr = saved.get("ENABLE_BBR", "false").lower() in {"1", "true", "yes", "y"}
    if args.render_only:
        if not all((domain, email, key, service)):
            raise InstallError("Render mode requires DOMAIN, EMAIL, SECRET_KEY and SERVICE_NAME (or the corresponding non-secret flags).")
    else:
        email = ask("ACME email", email)
        domain = ask("Node domain", domain)
        key = ask("SECRET_KEY from the Remnawave panel", key, secret=True)
        print("Cover services:")
        for index, item in enumerate(SERVICES, 1):
            print("  {}. {}".format(index, item[0]))
        choice = ask("Service name/number (empty = random)", service)
        if not choice:
            service = random.choice(SERVICES)[0]
        elif choice.isdigit() and 1 <= int(choice) <= len(SERVICES):
            service = SERVICES[int(choice) - 1][0]
        else:
            service = choice
        xhttp = ask("XHTTP path", xhttp)
        grpc = ask("gRPC service path", grpc)
        mode = ask("Certificate validation: http (port 80) or cloudflare (DNS)", mode)
        if mode == "cloudflare":
            token = ask("Cloudflare API token (DNS Edit + Zone Read for this zone)", token, secret=True)
        ssh = ask("SSH port to keep open", ssh)
        bbr = ask("Enable BBR? y/N", "y" if bbr else "n").lower() == "y"
    try:
        port = int(ssh)
    except (ValueError, TypeError):
        raise InstallError("SSH port must be a number.") from None
    return Settings(email, domain, key, service, xhttp, grpc, mode, token, port, bbr).validate()


def proxy(dial, versions=None, streaming=False):
    result = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": dial}],
        "headers": {"request": {"set": {"X-Real-IP": ["{http.request.remote.host}"]}}},
    }
    if versions:
        result["transport"] = {"protocol": "http", "versions": versions,
                               "read_timeout": 315_000_000_000, "write_timeout": 300_000_000_000}
    if streaming:
        result["flush_interval"] = -1
    return result


def caddy_config(settings):
    issuer = {"module": "acme", "email": settings.email}
    if settings.tls_mode == "cloudflare":
        issuer["challenges"] = {"dns": {"provider": {"name": "cloudflare", "api_token": "{env.CLOUDFLARE_API_TOKEN}"}}}
    else:
        # Explicitly bind HTTP-01 on TCP, not on the TLS Unix listener.
        issuer["challenges"] = {"bind_host": "0.0.0.0", "tls-alpn": {"disabled": True}}
    routes = []
    for path, handler in [
        (settings.xhttp_path.rstrip("/"), proxy("unix//dev/shm/xrxh.socket", ["1.1"], True)),
        (settings.grpc_path, proxy("unix//dev/shm/grpc.socket", ["h2c"])),
    ]:
        routes.append({"match": [{"path": [path, path + "/*"]}], "handle": [handler], "terminal": True})
    routes.append({"handle": [proxy("127.0.0.1:8080")], "terminal": True})
    return {
        "admin": {"listen": "unix//run/caddy-admin.sock"},
        "apps": {
            "http": {"servers": {
                "redirect": {
                    "listen": [":80"],
                    "routes": [{"match": [{"host": [settings.domain]}],
                                "handle": [{"handler": "static_response", "status_code": 308,
                                            "headers": {"Location": ["https://" + settings.domain + "{http.request.uri}"]}}]}],
                    "automatic_https": {"disable_redirects": True},
                },
                "selfsteal": {
                    "listen": ["unix/" + SOCKET_PATH],
                    "listener_wrappers": [{"wrapper": "proxy_protocol", "timeout": 2_000_000_000,
                                           "fallback_policy": "REJECT"}, {"wrapper": "tls"}],
                    "tls_connection_policies": [{}],
                    "protocols": ["h1", "h2"],
                    "routes": [{"match": [{"host": [settings.domain]}],
                                "handle": [{"handler": "subroute", "routes": routes}], "terminal": True}],
                    "automatic_https": {"disable_redirects": True},
                },
            }},
            "tls": {"certificates": {"automate": [settings.domain]},
                    "automation": {"policies": [{"subjects": [settings.domain], "issuers": [issuer]}]}},
        },
    }


def compose_config(settings):
    _, image, port = next(item for item in SERVICES if item[0] == settings.service)
    caddy = {
        "image": CADDY_IMAGE, "container_name": "remnanode-proxy", "restart": "unless-stopped",
        "network_mode": "host",
        "volumes": ["./caddy/config:/etc/caddy:ro", "./caddy/data:/data",
                    "./caddy/runtime:/config", "/dev/shm:/dev/shm:rw"],
        "entrypoint": ["/bin/sh", "-ec"],
        "command": [
            'if [ -e /dev/shm/nginx.sock ] && [ ! -S /dev/shm/nginx.sock ]; then '
            'echo "Refusing to replace a non-socket path" >&2; exit 1; fi; '
            'rm -f /dev/shm/nginx.sock; exec caddy run --config /etc/caddy/caddy.json'
        ],
    }
    if settings.tls_mode == "cloudflare":
        caddy["image"] = "node-setup-caddy:2.11.9-cloudflare-0.2.4"
        caddy["build"] = {"context": "./caddy", "dockerfile": "Dockerfile"}
        caddy["environment"] = {"CLOUDFLARE_API_TOKEN": settings.cloudflare_token.replace("$", "$$")}
    return {"name": PROJECT, "services": {
        "cover": {"image": image, "container_name": settings.service, "restart": "unless-stopped",
                  "ports": ["127.0.0.1:8080:" + str(port)]},
        "caddy": caddy,
        "remnanode": {
            "image": NODE_IMAGE, "container_name": "remnanode", "hostname": "remnanode",
            "restart": "always", "network_mode": "host", "cap_add": ["NET_ADMIN"],
            "volumes": ["/dev/shm:/dev/shm:rw", "/var/log/remnanode:/var/log/remnanode"],
            "ulimits": {"nofile": {"soft": 1048576, "hard": 1048576}},
            "environment": {"NODE_PORT": "2222", "SECRET_KEY": settings.secret_key.replace("$", "$$")},
        },
    }}


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def render(settings, output):
    settings.validate()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name in ("caddy/config", "caddy/data", "caddy/runtime"):
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    write_private(output / "compose.json", json.dumps(compose_config(settings), indent=2) + "\n")
    write_private(output / "caddy/config/caddy.json", json.dumps(caddy_config(settings), indent=2) + "\n")
    if settings.tls_mode == "cloudflare":
        write_private(output / "caddy/Dockerfile",
                      "FROM " + CADDY_BUILDER + " AS builder\n"
                      "RUN xcaddy build v2.11.9 --with " + CLOUDFLARE_MODULE + "\n"
                      "FROM " + CADDY_IMAGE + "\nCOPY --from=builder /usr/bin/caddy /usr/bin/caddy\n")
    write_private(output / "reality-target.json", json.dumps({
        "target": SOCKET_PATH, "xver": 1, "serverNames": [settings.domain]}, indent=2) + "\n")
    return output / "compose.json"


def save_settings(settings, path):
    values = {"EMAIL": settings.email, "DOMAIN": settings.domain, "SECRET_KEY": settings.secret_key,
              "SERVICE_NAME": settings.service, "XHTTP_PATH": settings.xhttp_path,
              "GRPC_PATH": settings.grpc_path, "TLS_MODE": settings.tls_mode,
              "SSH_PORT": str(settings.ssh_port), "ENABLE_BBR": str(settings.enable_bbr).lower()}
    if settings.tls_mode == "cloudflare":
        values["CF_Token"] = settings.cloudflare_token
    write_private(path, "\n".join(key + "=" + json.dumps(value) for key, value in values.items()) + "\n")


def command(args, capture=False):
    result = subprocess.run([str(part) for part in args], text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        raise InstallError("Command failed: " + str(args[0]) + ". No configuration secrets were printed.")
    return result.stdout if capture else ""


def owned_container(name):
    result = subprocess.run(["docker", "inspect", "--format",
                             '{{index .Config.Labels "com.docker.compose.project"}}', name],
                            capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == PROJECT


def preflight(root, settings):
    root = Path(root)
    managed = (root / ".caddy-installer").is_file()
    if (root / "nginx").exists() or (root.exists() and any(root.iterdir()) and not managed):
        raise InstallError("Existing/legacy node detected. This installer does not migrate running NGINX nodes.")
    if shutil.which("docker"):
        for name in ("remnanode", "remnanode-proxy", settings.service):
            result = subprocess.run(["docker", "inspect", name], capture_output=True)
            if result.returncode == 0 and not owned_container(name):
                raise InstallError("An existing container is not managed by this installer: " + name)
    if Path(SOCKET_PATH).exists() and not (managed and shutil.which("docker") and owned_container("remnanode-proxy")):
        raise InstallError("The existing selfsteal socket is not owned by this installation.")
    if not (managed and shutil.which("docker") and owned_container("remnanode-proxy")):
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", 80))
            except OSError:
                raise InstallError("Port 80 is already occupied. No existing service was stopped.") from None


def wait_for_tls(settings, timeout=300):
    deadline = time.monotonic() + timeout
    print("Waiting for a public certificate on the selfsteal socket (up to five minutes)...", flush=True)
    while time.monotonic() < deadline:
        try:
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(5)
            try:
                raw.connect(SOCKET_PATH)
                raw.sendall(b"PROXY TCP4 127.0.0.1 127.0.0.1 50000 443\r\n")
                with ssl.create_default_context().wrap_socket(raw, server_hostname=settings.domain) as tls:
                    tls.sendall(("GET / HTTP/1.1\r\nHost: " + settings.domain + "\r\nConnection: close\r\n\r\n").encode())
                    status = int(tls.recv(256).split(b" ", 2)[1])
                    if 200 <= status < 500:
                        print("Public TLS verified; cover service HTTP " + str(status), flush=True)
                        return
            finally:
                raw.close()
        except (OSError, ssl.SSLError, ValueError, IndexError):
            pass
        time.sleep(3)
    raise InstallError("TLS/cover check timed out. Check domain A/AAAA records and port 80, or use Cloudflare DNS mode. Existing generated files and certificate data were retained.")


def install(settings, env_path):
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise InstallError("Installation requires Linux and root. Use --render-only for an unprivileged preview.")
    os.umask(0o077)
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            release[key] = value.strip('"')
    minimum = {"ubuntu": 22, "debian": 11}
    if release.get("ID") not in minimum or int(release.get("VERSION_ID", "0").split(".")[0]) < minimum[release["ID"]]:
        raise InstallError("Supported systems: Ubuntu 22.04+ and Debian 11+.")
    preflight(INSTALL_ROOT, settings)
    print("Installing dependencies...", flush=True)
    command(["apt-get", "update"])
    command(["apt-get", "install", "-y", "ca-certificates", "curl", "ufw"])
    if not shutil.which("docker"):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="node-setup-") as temporary:
            installer = Path(temporary) / "docker-install.sh"
            command(["curl", "-fsSL", "https://get.docker.com", "-o", installer])
            command(["sh", installer])
    available = subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
    if not available:
        command(["apt-get", "install", "-y", "docker-compose-plugin"])
    if (INSTALL_ROOT / "compose.json").is_file():
        import datetime
        backup = INSTALL_ROOT / "backups" / datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(INSTALL_ROOT / "compose.json", backup / "compose.json")
        shutil.copytree(INSTALL_ROOT / "caddy/config", backup / "config")
    compose_path = render(settings, INSTALL_ROOT)
    write_private(INSTALL_ROOT / ".caddy-installer", "node-setup-caddy-v1\n")
    save_settings(settings, env_path)
    base = ["docker", "compose", "-p", PROJECT, "-f", str(compose_path)]
    command(base + ["config", "--quiet"])
    command(base + ["pull", "cover", "remnanode"])
    if settings.tls_mode == "cloudflare":
        command(base + ["build", "--pull", "caddy"])
    else:
        command(base + ["pull", "caddy"])
    # Override both entrypoint and command so validation never removes a live socket.
    command(base + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "caddy", "caddy",
                    "validate", "--config", "/etc/caddy/caddy.json"])
    if settings.enable_bbr:
        write_private("/etc/sysctl.d/99-remnanode-bbr.conf",
                      "net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n")
        command(["sysctl", "--system"], capture=True)
    for port in sorted({settings.ssh_port, 80, 443, 2222}):
        command(["ufw", "allow", str(port) + "/tcp"])
    command(["ufw", "--force", "enable"])
    command(base + ["up", "-d", "cover", "caddy"])
    command(base + ["exec", "-T", "caddy", "caddy", "reload",
                    "--address", "unix//run/caddy-admin.sock", "--config", "/etc/caddy/caddy.json"])
    wait_for_tls(settings)
    command(base + ["up", "-d", "--no-deps", "remnanode"])
    time.sleep(2)
    running = command(["docker", "inspect", "--format", "{{.State.Running}}", "remnanode"], capture=True).strip()
    if running != "true":
        raise InstallError("Remnanode did not stay running; check the node logs locally.")
    print("\nINSTALLATION COMPLETE")
    print("Domain: " + settings.domain)
    print("Node API: 2222; Xray keeps public port 443.")
    print("REALITY target: " + SOCKET_PATH + " (xver=1)")
    print("XHTTP: /dev/shm/xrxh.socket, path " + settings.xhttp_path)
    print("gRPC: /dev/shm/grpc.socket, service path " + settings.grpc_path)
    print("Caddy manages certificate issuance and renewal; port 9443 is not used.")
    print("Configure the Xray inbounds/profile in the Remnawave panel before testing VPN traffic.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-only", action="store_true", help="Generate configs without changing system services")
    parser.add_argument("--output", type=Path, default=Path("rendered"))
    parser.add_argument("--domain")
    parser.add_argument("--email")
    parser.add_argument("--service", choices=[item[0] for item in SERVICES])
    parser.add_argument("--tls-mode", choices=["http", "cloudflare"])
    parser.add_argument("--xhttp-path")
    parser.add_argument("--grpc-path")
    args = parser.parse_args()
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        if not args.render_only and (sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0):
            raise InstallError("Run installation on Linux with sudo; --render-only needs no root.")
        settings = collect(args, env_path)
        if args.render_only:
            render(settings, args.output)
            print("Configuration preview created at " + str(args.output.resolve()))
        else:
            install(settings, env_path)
    except (InstallError, EOFError, KeyboardInterrupt) as exc:
        print("STOPPED: " + (str(exc) if isinstance(exc, InstallError) else "Input interrupted."), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

