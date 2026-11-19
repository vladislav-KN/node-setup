import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import installer
import migrate


def legacy_config():
    return migrate.LEGACY_CONFIG.replace("DOMAIN", "node.example.com").replace("XHTTP", "/xhttppath/").replace("GRPC", "/grpcpath")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        legacy = self.root / "nginx"
        legacy.mkdir()
        for name, data in {"docker-compose.yml": "original compose\n", "nginx.conf": legacy_config(),
                           "fullchain.pem": "test certificate\n", "privkey.key": "test key\n"}.items():
            (legacy / name).write_text(data)
        self.proxy = {"Id": "old-proxy-id", "Image": "sha256:old-image",
                      "State": {"Running": True, "StartedAt": "original-start"},
                      "RestartCount": 0, "NetworkSettings": {"Networks": {}}}
        self.plan = {"root": self.root, "compose": legacy / "docker-compose.yml", "project": "nginx",
                     "proxy": self.proxy, "settings": migrate.parse_legacy(legacy_config()),
                     "baseline": 200, "untouched": {}, "hashes": {},
                     "rollback_compose": {"name": "nginx", "services": {"nginx": {"image": self.proxy["Image"]}}}}

    def test_accepts_only_original_routes_including_whitespace_and_comments(self):
        settings = migrate.parse_legacy("# harmless comment\n" + legacy_config().replace(";", ";\n"))
        self.assertEqual(settings.domain, "node.example.com")
        self.assertEqual(settings.xhttp_path, "/xhttppath/")
        self.assertEqual(settings.grpc_path, "/grpcpath")
        for text in [legacy_config() + "server { listen 9443; }",
                     legacy_config().replace("proxy_buffering off", "proxy_buffering on"),
                     legacy_config().replace("/grpcpath {", "/grpcpath/ {"),
                     legacy_config().replace("server_name node.example.com", "server_name node.example.com other.example.com"),
                     legacy_config().replace("http://127.0.0.1:8080", "http://127.0.0.1:9000")]:
            with self.subTest(text=text), self.assertRaises(installer.InstallError):
                migrate.parse_legacy(text)

    def test_render_contains_only_the_compatible_proxy_service(self):
        folder = self.root / "new"
        document = migrate.render(self.plan, folder, "unit-test")
        self.assertEqual(document["name"], "nginx")
        self.assertEqual(set(document["services"]), {"nginx"})
        service = document["services"]["nginx"]
        self.assertEqual(service["container_name"], "remnanode-proxy")
        self.assertEqual(service["labels"][migrate.TRANSACTION], "unit-test")
        self.assertNotIn("SECRET_KEY", json.dumps(document))
        self.assertNotIn("2222", json.dumps(document))
        self.assertEqual(service["volumes"][0], str(folder / "caddy/config") + ":/etc/caddy:ro")

    def test_backup_has_pinned_image_original_files_and_standalone_rollback(self):
        folder = self.root / "new"
        document = migrate.render(self.plan, folder, "unit-test")
        backup, state = migrate.prepare_backup(self.plan, folder, document, "unit-test")
        self.assertEqual((backup / "privkey.key").read_text(), "test key\n")
        self.assertEqual(json.loads((backup / "rollback-compose.json").read_text())["services"]["nginx"]["image"], "sha256:old-image")
        self.assertEqual(state["phase"], "prepared")
        self.assertTrue((backup / "installer.py").is_file())
        self.assertTrue((backup / "migrate.py").is_file())
        self.assertIn('--rollback "$SCRIPT_DIR"', (backup / "rollback.sh").read_text())
        if os.name != "nt":
            self.assertEqual((backup / "privkey.key").stat().st_mode & 0o777, 0o600)

    def test_failures_before_switch_never_stop_nginx_or_call_rollback(self):
        original = self.plan["compose"].read_bytes()
        calls = []

        def execute(args, optional=False):
            calls.append([str(x) for x in args])
            return types.SimpleNamespace(returncode=0, stdout="")

        with patch.object(migrate, "run", side_effect=execute), patch.object(migrate, "stage", side_effect=installer.InstallError("staging failed")), patch.object(migrate, "rollback") as restore, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(installer.InstallError):
                migrate.migrate(self.plan)
            restore.assert_not_called()
        self.assertFalse(any(call[1:2] in [["stop"], ["rm"]] for call in calls))
        self.assertEqual(self.plan["compose"].read_bytes(), original)

    def test_every_cutover_failure_calls_rollback_after_durable_state(self):
        for failure in ("stop", "rm", "up", "probe", "interrupt"):
            with self.subTest(failure=failure):
                calls = []
                def execute(args, optional=False):
                    args = [str(x) for x in args]
                    calls.append(args)
                    if args[1:2] == [failure] or (failure == "up" and "up" in args):
                        backups = list((self.root / "caddy-migration").glob("*/backup/state.json"))
                        self.assertTrue(any(json.loads(p.read_text())["phase"] == "switching" for p in backups))
                        raise installer.InstallError("injected cutover failure")
                    return types.SimpleNamespace(returncode=0, stdout="")
                effect = KeyboardInterrupt() if failure == "interrupt" else installer.InstallError("probe failed") if failure == "probe" else None
                with patch.object(migrate, "run", side_effect=execute), patch.object(migrate, "stage"), patch.object(migrate, "inspect", return_value=self.proxy), patch.object(migrate, "wait_ready", side_effect=effect), patch.object(migrate, "rollback") as restore, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises((installer.InstallError, KeyboardInterrupt)):
                        migrate.migrate(self.plan)
                    restore.assert_called_once()
                self.assertFalse(any("remnanode" in call[2:] for call in calls if call[1] in {"stop", "rm", "restart"}))

    def test_rollback_refuses_an_unrelated_replacement_container(self):
        folder = self.root / "new"
        document = migrate.render(self.plan, folder, "unit-test")
        backup, _ = migrate.prepare_backup(self.plan, folder, document, "unit-test")
        unrelated = {"Id": "foreign", "Image": "foreign-image", "Config": {"Labels": {}}}
        with patch.object(migrate, "ROOT", self.root), patch.object(migrate, "inspect", return_value=unrelated), patch.object(migrate, "run") as execute:
            with self.assertRaisesRegex(installer.InstallError, "outside this migration"):
                migrate.rollback(backup)
            execute.assert_not_called()

    def test_rollback_refuses_external_compose_changes(self):
        folder = self.root / "new"
        document = migrate.render(self.plan, folder, "unit-test")
        backup, _ = migrate.prepare_backup(self.plan, folder, document, "unit-test")
        self.plan["compose"].write_text("operator changed compose")
        with patch.object(migrate, "ROOT", self.root), patch.object(migrate, "inspect", return_value=None), patch.object(migrate, "run") as execute:
            with self.assertRaisesRegex(installer.InstallError, "Compose file changed"):
                migrate.rollback(backup)
            execute.assert_not_called()

    def test_check_is_read_only_and_needs_no_email_or_token(self):
        with patch.object(sys, "argv", ["migrate.py"]), patch.object(sys, "platform", "linux"), patch.object(os, "geteuid", return_value=0, create=True), patch.object(migrate.signal, "signal"), patch.object(migrate.shutil, "which", return_value="docker"), patch.object(migrate, "discover", return_value=self.plan), patch.object(migrate, "probe", return_value=200), patch.object(migrate, "migrate") as apply, patch.object(installer, "write_private") as write, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migrate.main(), 0)
            apply.assert_not_called()
            write.assert_not_called()
            self.assertIn("READ-ONLY CHECK PASSED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
