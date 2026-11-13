"""Deployment contract checks; never connects to a real device."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adbtv.config import load_config, DEFAULTS

ROOT = Path(__file__).resolve().parent.parent


def script_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def config(self, text="{}"):
        path = self.root / "config.yaml"
        path.write_text(text)
        return str(path)

    def test_explicit_missing_config_never_falls_back(self):
        with self.assertRaises(FileNotFoundError):
            load_config(str(self.root / "missing.yaml"))
        os.environ["AI_TV_CONFIG"] = str(self.root / "missing.yaml")
        with self.assertRaises(FileNotFoundError):
            load_config()

    def test_data_env_and_execution_override(self):
        os.environ["AI_TV_DATA_DIR"] = str(self.root / "data")
        cfg = load_config(self.config("paths: {data_dir: /unused}\nexecution: {state_dir: custom}\n"))
        data = self.root / "data"
        self.assertEqual(cfg["paths"]["data_dir"], str(data))
        self.assertEqual(cfg["execution"]["state_dir"], str(data / "custom"))
        self.assertEqual(cfg["server"]["token_file"], str(data / "custom/console.token"))
        self.assertEqual(cfg["memory"]["path"], str(data / "memory.sqlite3"))
        self.assertEqual(cfg["agent"]["log_dir"], str(data / "logs"))
        self.assertEqual(cfg["scheduler"]["store"], str(data / "tasks.json"))
        self.assertEqual(cfg["adb"]["key_home"], str(data / ".android"))
        self.assertFalse(data.exists(), "loading config must not create directories")

    def test_explicit_feature_paths_preserved(self):
        os.environ["AI_TV_DATA_DIR"] = str(self.root / "data")
        cfg = load_config(self.config("memory: {path: /custom/m.sqlite3}\nserver: {token_file: /custom/t}\nagent: {log_dir: /custom/log}\nscheduler: {store: /custom/tasks}\nadb: {key_home: /custom/.android}\n"))
        self.assertEqual(cfg["memory"]["path"], "/custom/m.sqlite3")
        self.assertEqual(cfg["server"]["token_file"], "/custom/t")
        self.assertEqual(cfg["agent"]["log_dir"], "/custom/log")
        self.assertEqual(cfg["scheduler"]["store"], "/custom/tasks")
        self.assertEqual(cfg["adb"]["key_home"], "/custom/.android")

    def test_readonly_source_uses_user_data(self):
        source = self.root / "source"
        source.mkdir()
        (source / "pyproject.toml").touch()
        source.chmod(0o555)
        os.environ["XDG_DATA_HOME"] = str(self.root / "xdg")
        try:
            with patch("adbtv.config.project_root", return_value=source):
                cfg = load_config(self.config())
            self.assertEqual(cfg["paths"]["data_dir"], str(self.root / "xdg/ai-tv-butler"))
            self.assertFalse((source / "state").exists())
        finally:
            source.chmod(0o755)

    def test_local_source_paths_remain_compatible(self):
        with patch("adbtv.config.project_root", return_value=ROOT):
            cfg = load_config(self.config())
        self.assertEqual(cfg["execution"]["state_dir"], str(ROOT / "state/runtime"))
        self.assertEqual(cfg["agent"]["log_dir"], str(ROOT / "logs"))
        self.assertNotIn("paths", DEFAULTS)

    def test_fake_state_isolated(self):
        cfg = load_config(self.config(f"adb:\n  fake_state: {self.root}/simulation/tv.json\n"))
        self.assertEqual(cfg["execution"]["state_dir"], str(self.root / "simulation/runtime"))

    def test_clean_preserves_all_runtime_and_unknown_files(self):
        cleaner = script_module("clean_cache")
        for name in ("state/runtime/tasks/job.json", "state/tasks.json.lock", "state/memory.sqlite3",
                     "state/runtime/console.token", "state/runtime/locks/device.lock", "logs/history.log",
                     "adbtv/__pycache__/keep.json", "deploy/data/__pycache__/keep.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        bytecode = self.root / "adbtv/__pycache__/config.cpython-311.pyc"
        bytecode.write_bytes(b"cache")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "danger.pyc").write_bytes(b"keep")
        (self.root / "adbtv/link").symlink_to(outside, target_is_directory=True)
        before = {p for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(cleaner.cache_targets(self.root), [bytecode])
        cleaner.clean(self.root)
        self.assertEqual({p for p in self.root.rglob("*") if p.is_file()}, before - {bytecode})

    def test_packaged_simulator_runs_without_tests(self):
        env = dict(os.environ, FAKE_ADB_STATE=str(self.root / "sim/tv.json"))
        result = subprocess.run([sys.executable, "-m", "adbtv.simulator", "shell", "input", "text", "hello"],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "sim/tv.json").read_text())["query"], "hello")

    def test_health_probe_uses_effective_host_port(self):
        from unittest.mock import MagicMock
        health = script_module("healthcheck")
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(health, "load_config", return_value={"server": {"host": "0.0.0.0", "port": 9234}}), \
                patch.object(health.urllib.request, "build_opener", return_value=opener):
            health.main()
        opener.open.assert_called_once_with("http://127.0.0.1:9234/health", timeout=3)

    def test_setup_preserves_existing_config(self):
        setup = script_module("setup_deploy")
        data = self.root / "data"
        setup.prepare(data)
        cfg = data / "config.yaml"
        cfg.write_text("private: keep\n")
        setup.prepare(data)
        self.assertEqual(cfg.read_text(), "private: keep\n")
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        linked = self.root / "linked"
        linked.symlink_to(data, target_is_directory=True)
        with self.assertRaises(PermissionError):
            setup.prepare(linked)


if __name__ == "__main__":
    unittest.main()


class DefaultConfigIntegrityTests(unittest.TestCase):
    """DEFAULTS must not silently shadow an app entry with a duplicate key."""

    def test_no_duplicate_app_aliases(self):
        from adbtv.config import DEFAULTS
        import ast
        source = (Path(__file__).resolve().parents[1] / "adbtv/config.py").read_text()
        start = source.index('"apps": {')
        block = source[start:source.index("\n    },\n", start)]
        import re
        keys = re.findall(r'^\s{8}"([a-z_]+)":\s*\{', block, re.M)
        self.assertEqual(len(keys), len(set(keys)), f"重复应用别名: {keys}")

    def test_feiniu_keeps_legacy_flow_fallback(self):
        from adbtv.config import DEFAULTS
        entry = DEFAULTS["apps"]["feiniu_tv"]
        self.assertEqual(entry["package"], "com.trim.tv")
        self.assertIn("flow", entry, "smart_navigation=false 时依赖 flow 兜底")
        self.assertTrue(entry["flow"], "flow 不能为空")


class ComposeVoiceServiceTests(unittest.TestCase):
    """Voice service must share data/locks with console+scheduler and be optional."""

    def setUp(self):
        import yaml
        self.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        self.services = self.compose["services"]

    def test_service_set_includes_voice_and_media(self):
        self.assertEqual(set(self.services),
                         {"console", "scheduler", "media", "voice"})

    def test_image_version_is_current(self):
        from adbtv import __version__
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertIn(__version__, service["image"])

    def test_voice_uses_forced_listen_and_healthcheck(self):
        voice = self.services["voice"]
        self.assertEqual(voice["command"][:2], ["voice", "listen"])
        self.assertIn("--force", voice["command"])
        self.assertEqual(voice["healthcheck"]["test"][-1], "voice")

    def test_all_services_share_same_data_and_home(self):
        for name, service in self.services.items():
            with self.subTest(service=name):
                env = service["environment"]
                self.assertEqual(env["AI_TV_CONFIG"], "/data/config.yaml")
                self.assertEqual(env["HOME"], "/data")     # .android / ADB 授权共用
                self.assertIn("${BUTLER_DATA_DIR:-./deploy/data}:/data:rw", service["volumes"])
                self.assertTrue(service["read_only"])
                self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_every_service_mounts_library_read_only(self):
        """console 建索引、scheduler/voice 跑任务、media 读字节，都需要库可见。"""
        for name, service in self.services.items():
            with self.subTest(service=name):
                library = [v for v in service["volumes"] if v.endswith(":/media:ro")]
                self.assertEqual(len(library), 1, f"{name} 看不到媒体库会导致空索引")
                self.assertIn("MEDIA_ROOT", library[0])

    def test_library_mount_is_read_only(self):
        for name, service in self.services.items():
            for volume in service["volumes"]:
                if "/media" in volume:
                    with self.subTest(service=name):
                        self.assertTrue(volume.endswith(":ro"), "媒体库必须只读挂载")

    def test_voice_restarts_and_waits_for_start_period(self):
        voice = self.services["voice"]
        self.assertEqual(voice["restart"], "unless-stopped")
        self.assertGreaterEqual(voice["healthcheck"].get("start_period", "0s"), "10s")


class VoiceHeartbeatTests(unittest.TestCase):
    def test_heartbeat_reports_lifecycle(self):
        import tempfile, time
        from adbtv.voice import VoiceListener
        tmp = tempfile.mkdtemp()
        cfg = {"adb": {"serial": "x"}, "execution": {"state_dir": tmp},
               "voice": {"listener": {"state_file": tmp + "/v.json"}}}
        self.assertEqual(VoiceListener.worker_status(cfg)["status"], "unknown")
        listener = VoiceListener(cfg, lambda u: None)
        listener.start()
        try:
            time.sleep(0.2)
            status = VoiceListener.worker_status(cfg)
            self.assertTrue(status["online"])
            self.assertEqual(status["status"], "listening")
        finally:
            listener.stop()
        after = VoiceListener.worker_status(cfg)
        self.assertFalse(after["online"])
        self.assertEqual(after["status"], "stopped")

    def test_stale_heartbeat_is_offline(self):
        import json, tempfile, time
        from adbtv.voice import VoiceListener
        tmp = tempfile.mkdtemp()
        cfg = {"execution": {"state_dir": tmp}, "adb": {}}
        path = Path(tmp) / "voice_worker.json"
        path.write_text(json.dumps({"pid": 1, "updated": time.time() - 120,
                                    "status": "listening"}))
        status = VoiceListener.worker_status(cfg)
        self.assertFalse(status["online"])
        self.assertEqual(status["status"], "stale")


class HealthcheckCommandTests(unittest.TestCase):
    """The exact commands used by docker-compose must run and report correctly."""

    def run_healthcheck(self, service, cfg_path, data_dir):
        env = dict(os.environ, AI_TV_CONFIG=str(cfg_path), AI_TV_DATA_DIR=str(data_dir),
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(ROOT / "scripts/healthcheck.py"), service],
                              capture_output=True, text=True, env=env, timeout=60)

    def make_config(self, tmp, voice_enabled=True):
        import yaml
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())
        cfg["adb"] = {"serial": "192.0.2.20:5555", "binary": "adb",
                      "simulator": "inprocess", "fake_state": str(Path(tmp) / "tv.json")}
        cfg["voice"]["listener"]["enabled"] = voice_enabled
        cfg["voice"]["listener"]["state_file"] = ""
        cfg["execution"]["state_dir"] = str(Path(tmp) / "runtime")
        cfg["server"]["host"] = "127.0.0.1"
        cfg["server"]["port"] = 1              # 无服务，console 检查应失败
        # Never probe a real NAS media service while testing an absent service.
        cfg["media"].update(serve_port=1, advertise_host="127.0.0.1")
        path = Path(tmp) / "config.yaml"
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        return path

    def test_voice_is_an_accepted_service_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_healthcheck("voice", self.make_config(tmp), tmp)
            self.assertNotIn("invalid choice", result.stderr)
            self.assertIn("listener_online", result.stdout)

    def test_voice_reports_offline_without_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_healthcheck("voice", self.make_config(tmp), tmp)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('"listener_online": false', result.stdout)

    def test_voice_reports_online_and_adb_state(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self.make_config(tmp)
            runtime = Path(tmp) / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "voice_worker.json").write_text(json.dumps({
                "pid": os.getpid(), "updated": time.time(), "status": "listening",
                "stats": {"last_line_at": time.time()}}))
            result = self.run_healthcheck("voice", cfg_path, tmp)
            self.assertIn('"listener_online": true', result.stdout)
            self.assertIn("adb_connected", result.stdout)

    def test_media_reports_failure_when_no_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_healthcheck("media", self.make_config(tmp), tmp)
            self.assertNotEqual(result.returncode, 0)

    def test_media_reports_success_against_live_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            root.mkdir()
            (root / "a.mp4").write_bytes(b"x")
            from adbtv.media_server import MediaServer
            server = MediaServer([str(root)], host="127.0.0.1", port=0,
                                 state_dir=Path(tmp) / "runtime").start()
            self.addCleanup(server.stop)
            import yaml
            cfg_path = self.make_config(tmp)
            cfg = yaml.safe_load(cfg_path.read_text())
            cfg["media"].update({"serve_port": server.port, "advertise_host": "127.0.0.1"})
            cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
            result = self.run_healthcheck("media", cfg_path, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduler_and_console_still_selectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            for service in ("console", "scheduler"):
                result = self.run_healthcheck(service, self.make_config(tmp), tmp)
                self.assertNotIn("invalid choice", result.stderr, service)


class ComposeServiceSetTests(unittest.TestCase):
    """V3.4.2 adds a dedicated media service; workers must not bind the port."""

    def setUp(self):
        import yaml
        self.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        self.services = self.compose["services"]

    def test_media_service_exists_and_is_healthy(self):
        media = self.services["media"]
        self.assertEqual(media["command"], ["media", "serve"])
        self.assertEqual(media["healthcheck"]["test"][-1], "media")

    def test_workers_do_not_run_media_serve(self):
        for name in ("console", "scheduler", "voice"):
            with self.subTest(service=name):
                self.assertNotIn("media", self.services[name]["command"])

    def test_all_services_share_data_for_signing_key(self):
        for name, service in self.services.items():
            with self.subTest(service=name):
                # /data 必须共享（签名密钥、会话、设备锁都在这），其余挂载可选。
                self.assertIn("${BUTLER_DATA_DIR:-./deploy/data}:/data:rw",
                              service["volumes"])

    def test_image_version_matches_code_version(self):
        # v4.3: the released version must agree across code, package metadata
        # and every compose image tag. Exact tag equality (not a substring
        # match) is the invariant that keeps rollback tags unambiguous.
        import tomllib
        from adbtv import __version__
        self.assertEqual(__version__, "4.4.0")
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(declared, __version__)
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertEqual(service["image"], f"ai-tv-butler:{__version__}")


class PackageAuditTests(unittest.TestCase):
    """A public package must not carry the operator's own configuration."""

    def audit(self, mode):
        spec = importlib.util.spec_from_file_location(
            "pkg_" + mode, ROOT / "scripts" / "package.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, module.iter_files(mode), module.audit(module.iter_files(mode), mode)

    def test_public_package_excludes_local_config(self):
        _, files, problems = self.audit("public")
        relative = {rel for _, rel in files}
        self.assertNotIn("config.yaml", relative)
        self.assertNotIn("config.local.yaml", relative)
        self.assertEqual(problems, [], "公开包不应含敏感信息")

    def test_public_package_excludes_runtime_data(self):
        _, files, _ = self.audit("public")
        relative = {rel for _, rel in files}
        for forbidden in ("state/memory.sqlite3", "state/runtime/console.token",
                          "state/media_sessions.json", "state/media_key"):
            self.assertNotIn(forbidden, relative)

    def test_private_mode_keeps_config(self):
        module, files, problems = self.audit("private")
        relative = {rel for _, rel in files}
        self.assertEqual(problems, [])
        # 私人包允许带 config.yaml（若存在），但绝不含运行期状态
        self.assertNotIn("state/memory.sqlite3", relative)

    def test_audit_rejects_planted_secret(self):
        module, _, _ = self.audit("public")
        with tempfile.TemporaryDirectory() as tmp:
            leaked = Path(tmp) / "leak.md"
            # 动态拼接，避免测试源码本身触发打包审计
            address = ".".join(["192", "168", "99", "7"])
            token = "MA_" + "x" * 24
            leaked.write_text(f"电视地址 {address} 令牌 {token}")
            problems = module.audit([(leaked, "leak.md")], "public")
            self.assertTrue(problems, "审计必须拦下真实地址与令牌")


class ComposeCommandParsingTests(unittest.TestCase):
    """Each compose service command must be accepted by the CLI parser.

    Docker itself cannot run in the build sandbox (no-new-privileges blocks the
    daemon socket), so this validates the command lines and, separately, the
    healthcheck entrypoints that the containers invoke.
    """

    def parse(self, argv):
        from adbtv import cli
        parser = cli.build_parser()
        return parser.parse_args(argv)

    def test_service_commands_parse(self):
        for argv in (["serve"], ["schedule", "run"], ["voice", "listen", "--force"],
                     ["media", "serve"]):
            with self.subTest(argv=argv):
                args = self.parse(argv)
                self.assertTrue(callable(getattr(args, "func", None)))

    def test_compose_commands_match_parser(self):
        import yaml
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        for name, service in compose["services"].items():
            with self.subTest(service=name):
                self.parse(list(service["command"]))


class SingleMediaOwnerTests(unittest.TestCase):
    """Only one process may own the media port; a second must fail clearly."""

    def test_second_bind_reports_in_use(self):
        import tempfile
        from pathlib import Path as _Path
        from adbtv.media_server import MediaServer
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _Path(tmp.name) / "lib"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"x")
        first = MediaServer([str(root)], host="127.0.0.1", port=0,
                            state_dir=_Path(tmp.name) / "state").start()
        self.addCleanup(first.stop)
        second = MediaServer([str(root)], host="127.0.0.1", port=first.port,
                             state_dir=_Path(tmp.name) / "state")
        with self.assertRaises(OSError) as caught:
            second.start()
        self.assertIn(getattr(caught.exception, "errno", None), (48, 98))
