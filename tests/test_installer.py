import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def settings(**kwargs):
    values = dict(email="admin@example.com", domain="node.example.com",
                  secret_key="secret-for-tests", service="filebrowser")
    values.update(kwargs)
    return installer.Settings(**values).validate()


class InstallerTests(unittest.TestCase):
    def test_no_warp_installer(self):
        self.assertFalse((ROOT / "warp.sh").exists())
        self.assertNotIn("warp", (ROOT / "installer.py").read_text().lower())
        self.assertNotIn("warp", (ROOT / "install.sh").read_text().lower())

    def test_paths_are_normalized_without_stripping_proxy_paths(self):
        cfg = settings(xhttp_path="custom/xhttp", grpc_path="custom.grpc/")
        self.assertEqual(cfg.xhttp_path, "/custom/xhttp/")
        self.assertEqual(cfg.grpc_path, "/custom.grpc")
        document = installer.caddy_config(cfg)
        routes = document["apps"]["http"]["servers"]["selfsteal"]["routes"][0]["handle"][0]["routes"]
        self.assertEqual(routes[0]["match"][0]["path"], ["/custom/xhttp", "/custom/xhttp/*"])
        self.assertNotIn("rewrite", json.dumps(document))

    def test_rejects_injection_and_overlapping_paths(self):
        for kwargs in [
            {"domain": "node.example.com { respond pwned }"},
            {"domain": "https://node.example.com"},
            {"domain": "127.0.0.1"}, {"domain": "bad..example.com"},
            {"xhttp_path": "/"}, {"xhttp_path": "/a/../b"},
            {"xhttp_path": "/test/*"}, {"grpc_path": "/foo; respond 200"},
            {"xhttp_path": "/vpn/", "grpc_path": "/vpn"},
            {"xhttp_path": "/vpn/child/", "grpc_path": "/vpn"},
            {"secret_key": "a\nb"}, {"ssh_port": 443}, {"ssh_port": 0},
            {"tls_mode": "unknown"}, {"tls_mode": "cloudflare", "cloudflare_token": ""},
        ]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(installer.InstallError):
                    settings(**kwargs)

    def test_tls_keeps_443_for_xray_and_proxy_before_tls(self):
        document = installer.caddy_config(settings())
        servers = document["apps"]["http"]["servers"]
        self.assertEqual(servers["redirect"]["listen"], [":80"])
        self.assertEqual(servers["selfsteal"]["listen"], ["unix//dev/shm/nginx.sock"])
        self.assertEqual([w["wrapper"] for w in servers["selfsteal"]["listener_wrappers"]],
                         ["proxy_protocol", "tls"])
        self.assertEqual(servers["selfsteal"]["protocols"], ["h1", "h2"])
        issuer = document["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]
        self.assertEqual(issuer["challenges"]["bind_host"], "0.0.0.0")
        self.assertTrue(issuer["challenges"]["tls-alpn"]["disabled"])

    def test_grpc_upstream_uses_cleartext_http2(self):
        document = installer.caddy_config(settings())
        routes = document["apps"]["http"]["servers"]["selfsteal"]["routes"][0]["handle"][0]["routes"]
        proxy = routes[1]["handle"][0]
        self.assertEqual(proxy["upstreams"], [{"dial": "unix//dev/shm/grpc.socket"}])
        self.assertEqual(proxy["transport"]["versions"], ["h2c"])
        self.assertEqual(routes[0]["handle"][0]["flush_interval"], -1)

    def test_cover_is_only_published_on_loopback(self):
        for name, image, port in installer.SERVICES:
            with self.subTest(service=name):
                document = installer.compose_config(settings(service=name))
                self.assertEqual(document["services"]["cover"]["ports"], ["127.0.0.1:8080:" + str(port)])
                self.assertNotIn("ports", document["services"]["caddy"])
                self.assertEqual(document["services"]["caddy"]["network_mode"], "host")

    def test_secrets_are_not_shell_evaluated_and_round_trip(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            path = Path(folder) / ".env"
            value = 'literal-$(touch do-not-create)-with-$dollar-and-"quotes"'
            cfg = settings(secret_key=value)
            installer.save_settings(cfg, path)
            self.assertEqual(installer.load_env(path)["SECRET_KEY"], value)
            encoded = installer.compose_config(cfg)["services"]["remnanode"]["environment"]["SECRET_KEY"]
            self.assertEqual(encoded.replace("$$", "$"), value)
            self.assertNotIn(value, repr(cfg))

    def test_legacy_env_ignores_executable_lines(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            path = Path(folder) / ".env"
            path.write_text("SECRET_KEY=$(touch should-not-exist)\nUNKNOWN=hello\n# comment\nDOMAIN=node.example.com\n")
            parsed = installer.load_env(path)
            self.assertEqual(parsed["SECRET_KEY"], "$(touch should-not-exist)")
            self.assertNotIn("UNKNOWN", parsed)
            self.assertFalse((Path(folder) / "should-not-exist").exists())

    def test_cloudflare_token_is_not_in_caddy_json(self):
        token = "C" * 40
        cfg = settings(tls_mode="cloudflare", cloudflare_token=token)
        document = installer.caddy_config(cfg)
        self.assertNotIn(token, json.dumps(document))
        provider = document["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]["challenges"]["dns"]["provider"]
        self.assertEqual(provider["api_token"], "{env.CLOUDFLARE_API_TOKEN}")
        compose = installer.compose_config(cfg)
        self.assertIn("build", compose["services"]["caddy"])
        self.assertEqual(compose["services"]["caddy"]["environment"]["CLOUDFLARE_API_TOKEN"], token)

    def test_render_has_no_external_side_effects(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder, patch.object(installer.subprocess, "run") as runner:
            installer.render(settings(), folder)
            self.assertTrue((Path(folder) / "compose.json").is_file())
            self.assertTrue((Path(folder) / "caddy/config/caddy.json").is_file())
            reality = json.loads((Path(folder) / "reality-target.json").read_text())
            self.assertEqual(reality["target"], "/dev/shm/nginx.sock")
            self.assertEqual(reality["xver"], 1)
            runner.assert_not_called()

    def test_render_cli_does_not_print_secret(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            env = dict(os.environ, DOMAIN="node.example.com", EMAIL="admin@example.com",
                       SECRET_KEY="private-cli-test-value", SERVICE_NAME="filebrowser")
            result = subprocess.run([os.sys.executable, str(ROOT / "installer.py"), "--render-only",
                                     "--output", folder], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(env["SECRET_KEY"], result.stdout + result.stderr)

    def test_refuses_existing_node_without_mutating_it(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            path = Path(folder)
            (path / "nginx").mkdir()
            original = path / "nginx/nginx.conf"
            original.write_text("working configuration")
            with patch.object(installer.subprocess, "run") as runner:
                with self.assertRaises(installer.InstallError):
                    installer.preflight(path, settings())
                runner.assert_not_called()
            self.assertEqual(original.read_text(), "working configuration")

    def test_preview_files_are_private_on_linux(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            installer.render(settings(), folder)
            self.assertEqual((Path(folder) / "compose.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((Path(folder) / "caddy/data").stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI not available")
    def test_compose_interpolation_preserves_literal_secret(self):
        secret = 'literal-$dollar-$(not-a-command)-"quoted"'
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            manifest = installer.render(settings(secret_key=secret), folder)
            result = subprocess.run(["docker", "compose", "-f", str(manifest), "config", "--format", "json"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)["services"]["remnanode"]["environment"]["SECRET_KEY"]
            # Compose re-escapes dollars when serializing config for round trips:
            # docker/compose cmd/compose/config.go, escapeDollarSign.
            self.assertEqual(actual.replace("$$", "$"), secret)


if __name__ == "__main__":
    unittest.main()

