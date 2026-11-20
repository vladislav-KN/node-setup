"""Explicit CI-only test of real Docker cutover, hook compatibility and rollback.

The TCP relay represents Xray's unauthenticated REALITY fallback; it does not
test a VPN login. Certificates use a disposable test CA, never public ACME.
"""
import copy
import json
import os
from pathlib import Path
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import installer
import migrate

ENABLED = sys.platform == "linux" and os.environ.get("NODE_SETUP_DOCKER_TESTS") == "1"


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.connect(installer.SOCKET_PATH)
                upstream.sendall(b"PROXY TCP4 127.0.0.1 127.0.0.1 50000 443\r\n")
                while True:
                    ready, _, _ = select.select([self.request, upstream], [], [], 10)
                    if not ready:
                        return
                    for stream in ready:
                        data = stream.recv(65536)
                        if not data:
                            return
                        (upstream if stream is self.request else self.request).sendall(data)
        except (OSError, ConnectionError):
            pass


@unittest.skipUnless(ENABLED, "Explicit Linux Docker fixture only")
class DockerMigration(unittest.TestCase):
    def command(self, args):
        result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return result.stdout

    def test_live_cutover_legacy_restart_and_both_rollback_paths(self):
        config = migrate.LEGACY_CONFIG.replace("DOMAIN", "node.example.com").replace("XHTTP", "/xhttppath/").replace("GRPC", "/grpcpath")
        self.exercise_migration(config)

    def test_live_migration_without_global_log_overrides(self):
        self.exercise_migration((ROOT / "tests/fixtures/nginx-without-log-overrides.conf").read_text())

    def exercise_migration(self, nginx_config):
        self.assertIsNotNone(shutil.which("openssl"))
        self.assertFalse(Path(installer.SOCKET_PATH).exists(), "Refuse to touch an existing selfsteal socket")
        # Abort before creating containers if the disposable runner's ports are busy.
        for port in (80, 443, 8080):
            with socket.socket() as handle:
                handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                handle.bind(("127.0.0.1", port))
        tag = uuid.uuid4().hex[:10]
        proxy_name, node_name, cover_name = ("migration-proxy-" + tag, "migration-node-" + tag, "migration-cover-" + tag)
        project = "migration-proxy-" + tag
        created = []
        relay = None
        with tempfile.TemporaryDirectory(prefix="docker-migration-", dir=ROOT.parent) as temporary:
            root = Path(temporary)
            legacy = root / "nginx"
            legacy.mkdir()
            cert = legacy / "fullchain.pem"
            key = legacy / "privkey.key"
            self.command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                          "-subj", "/CN=node.example.com", "-addext", "subjectAltName=DNS:node.example.com",
                          "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", key, "-out", cert])
            os.chmod(key, 0o600)
            (legacy / "nginx.conf").write_text(nginx_config)
            manifest = legacy / "docker-compose.yml"
            original = {"name": project, "services": {"nginx": {
                "image": "nginx:1.28-alpine", "container_name": proxy_name, "network_mode": "host", "restart": "always",
                "volumes": [str(legacy / "nginx.conf") + ":/etc/nginx/conf.d/default.conf:ro",
                            str(cert) + ":/etc/nginx/certs/fullchain.pem:ro",
                            str(key) + ":/etc/nginx/certs/privkey.key:ro", "/dev/shm:/dev/shm:rw"],
                "command": ["sh", "-c", 'rm -f /dev/shm/nginx.sock && exec nginx -g "daemon off;"']}}}
            manifest.write_text(json.dumps(original))
            original_bytes = manifest.read_bytes()
            class TCPRelay(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
                allow_reuse_address = True
            try:
                self.command(["docker", "pull", "nginx:1.28-alpine"])
                self.command(["docker", "pull", installer.CADDY_IMAGE])
                self.command(["docker", "run", "-d", "--name", cover_name, "--label", "com.docker.compose.project=migration-cover-" + tag,
                              "-p", "127.0.0.1:8080:80", "nginx:1.28-alpine"])
                created.append(cover_name)
                self.command(["docker", "run", "-d", "--name", node_name, "--network", "host",
                              "--label", "com.docker.compose.project=migration-node-" + tag,
                              "--mount", "type=bind,src=" + str(cert) + ",dst=/etc/nginx/certs/fullchain.pem,readonly",
                              "--mount", "type=bind,src=" + str(key) + ",dst=/etc/nginx/certs/privkey.key,readonly",
                              "nginx:1.28-alpine", "sh", "-c", "while :; do sleep 3600; done"])
                created.append(node_name)
                self.command(["docker", "compose", "-p", project, "-f", manifest, "up", "-d"])
                created.append(proxy_name)
                relay = TCPRelay(("127.0.0.1", 443), Relay)
                threading.Thread(target=relay.serve_forever, daemon=True).start()
                original_render = migrate.render
                original_ready = migrate.wait_ready
                def test_certificate_render(plan, folder, transaction):
                    document = original_render(plan, folder, transaction)
                    # Only the test fixture substitutes certificate issuance.
                    cfg_path = Path(folder) / "caddy/config/caddy.json"
                    cfg = json.loads(cfg_path.read_text())
                    cfg["apps"]["tls"] = {"certificates": {"load_files": [
                        {"certificate": "/legacy/fullchain.pem", "key": "/legacy/privkey.key"}]}}
                    for server in cfg["apps"]["http"]["servers"].values():
                        server["automatic_https"] = {"disable": True}
                    installer.write_private(cfg_path, json.dumps(cfg))
                    document["services"]["nginx"]["volumes"].append(str(legacy) + ":/legacy:ro")
                    installer.write_private(Path(folder) / "compose.json", json.dumps(document, indent=2) + "\n")
                    return document
                with patch.object(migrate, "ROOT", root), patch.object(migrate, "PROXY", proxy_name), patch.object(migrate, "NODE", node_name), patch.object(migrate, "render", side_effect=test_certificate_render), patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                    settings = installer.Settings("admin@example.com", "node.example.com", "unused")
                    original_ready(settings, 200, timeout=20)
                    plan = migrate.discover(root)
                    plan["baseline"] = migrate.probe(settings)
                    node_before, cover_before = migrate.snapshot(migrate.inspect(node_name)), migrate.snapshot(migrate.inspect(cover_name))
                    old_image = plan["proxy"]["Image"]
                    migrate.migrate(plan)
                    current = migrate.inspect(proxy_name)
                    self.assertEqual(current["Config"]["Labels"][migrate.ROLE], "caddy-migrated")
                    self.assertEqual(migrate.snapshot(migrate.inspect(node_name)), node_before)
                    self.assertEqual(migrate.snapshot(migrate.inspect(cover_name)), cover_before)
                    # The original certificate reload command must now restart Caddy only.
                    self.command(["docker", "compose", "-f", manifest, "restart"])
                    original_ready(settings, 200, timeout=20)
                    self.assertEqual(migrate.snapshot(migrate.inspect(node_name)), node_before)
                    backup = next((root / "caddy-migration").glob("*/backup"))
                    migrate.rollback(backup)
                    self.assertEqual(manifest.read_bytes(), original_bytes)
                    self.assertEqual(migrate.inspect(proxy_name)["Image"], old_image)
                    self.assertEqual(migrate.snapshot(migrate.inspect(node_name)), node_before)
                    self.assertEqual(migrate.snapshot(migrate.inspect(cover_name)), cover_before)
                    # Repeated rollback must leave the restored proxy running.
                    restored = migrate.snapshot(migrate.inspect(proxy_name))
                    migrate.rollback(backup)
                    self.assertEqual(migrate.snapshot(migrate.inspect(proxy_name)), restored)
                    # A failure after a real successful Caddy start must restore NGINX.
                    plan = migrate.discover(root)
                    plan["baseline"] = 200
                    def fail_caddy(settings, expected=None, timeout=300):
                        original_ready(settings, expected, timeout=20)
                        if migrate.inspect(proxy_name)["Config"]["Labels"].get(migrate.ROLE) == "caddy-migrated":
                            raise installer.InstallError("injected post-cutover failure")
                    with patch.object(migrate, "wait_ready", side_effect=fail_caddy):
                        with self.assertRaisesRegex(installer.InstallError, "injected post-cutover"):
                            migrate.migrate(plan)
                    self.assertEqual(migrate.inspect(proxy_name)["Image"], old_image)
                    self.assertEqual(manifest.read_bytes(), original_bytes)
                    self.assertEqual(migrate.snapshot(migrate.inspect(node_name)), node_before)
                    self.assertEqual(migrate.snapshot(migrate.inspect(cover_name)), cover_before)
                    original_ready(settings, 200, timeout=20)
            finally:
                if relay:
                    relay.shutdown()
                    relay.server_close()
                # Every name is unique and added only after this fixture created it.
                for name in reversed(created):
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                if proxy_name in created and Path(installer.SOCKET_PATH).exists():
                    Path(installer.SOCKET_PATH).unlink()


if __name__ == "__main__":
    unittest.main()
