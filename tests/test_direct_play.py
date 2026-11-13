"""Direct library playback: Range server + TV player intent (temp trees only)."""
import http.client
import socket
import tempfile
import time
import unittest
from pathlib import Path

from adbtv.media_server import MediaServer
from adbtv.adb import CmdResult


def _payload(size=4096):
    return bytes((i * 7) % 251 for i in range(size))


class MediaServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lib"
        (self.root / "短剧" / "测试剧 (2026)" / "Season 01").mkdir(parents=True)
        self.file = self.root / "短剧" / "测试剧 (2026)" / "Season 01" / "测试剧 S01E001.mp4"
        self.file.write_bytes(_payload())
        self.server = MediaServer([str(self.root)], host="127.0.0.1", port=0).start()
        self.addCleanup(self.server.stop)

    def request(self, path, headers=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_url_for_and_full_get(self):
        url = self.server.url_for(str(self.file), host="127.0.0.1")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        path = url[url.index("/", 7):]
        response, body = self.request(path)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(len(body), self.file.stat().st_size)
        self.assertEqual(response.getheader("Content-Type"), "video/mp4")

    def test_range_returns_206_with_exact_bytes(self):
        url = self.server.url_for(str(self.file), host="127.0.0.1")
        path = url[url.index("/", 7):]
        response, body = self.request(path, {"Range": "bytes=100-199"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("Content-Range"),
                         f"bytes 100-199/{self.file.stat().st_size}")
        self.assertEqual(body, _payload()[100:200])

    def test_suffix_range_and_unsatisfiable(self):
        url = self.server.url_for(str(self.file), host="127.0.0.1")
        path = url[url.index("/", 7):]
        response, body = self.request(path, {"Range": "bytes=-50"})
        self.assertEqual(response.status, 206)
        self.assertEqual(body, _payload()[-50:])
        response, _ = self.request(path, {"Range": "bytes=999999-1000000"})
        self.assertEqual(response.status, 416)

    def test_head_has_no_body_but_length(self):
        url = self.server.url_for(str(self.file), host="127.0.0.1")
        path = url[url.index("/", 7):]
        response, body = self.request(path, method="HEAD")
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(response.getheader("Content-Length")), self.file.stat().st_size)

    def test_traversal_and_missing_are_rejected(self):
        for path in ("/../etc/passwd", "/%2e%2e/etc/passwd", "/nope.mp4", "/"):
            response, _ = self.request(path)
            self.assertIn(response.status, (404, 400), path)

    def test_plain_path_requires_credential_but_signed_url_works(self):
        server = MediaServer([str(self.root)], host="127.0.0.1", port=0, token="secret-token")
        server.start(); self.addCleanup(server.stop)
        signed = server.url_for(str(self.file), host="127.0.0.1")
        signed_path = signed[signed.index("/", 7):]
        plain_path = signed_path.split("?", 1)[0]
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", plain_path)
        self.assertEqual(conn.getresponse().status, 401)   # 无签名无令牌
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", plain_path, headers={"X-Auth-Token": "secret-token"})
        self.assertEqual(conn.getresponse().status, 200)   # 显式令牌仍可用
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", signed_path)
        self.assertEqual(conn.getresponse().status, 200)   # 电视用签名链接
        conn.close()

    def test_explicit_internal_binding_is_used_consistently(self):
        self.assertEqual(self.server.advertise_host(), "127.0.0.1")


class LaunchVideoTests(unittest.TestCase):
    def test_launch_video_uses_view_intent_and_reports_player(self):
        from unittest.mock import Mock
        from adbtv.device import TVDevice
        from adbtv.config import DEFAULTS
        adb = Mock()
        adb.shell.return_value = CmdResult([], 0, "Starting: Intent { ... }", "")
        device = TVDevice(adb, DEFAULTS)
        result = device.launch_video("http://192.0.2.10:8899/a.mp4")
        self.assertTrue(result["ok"])
        self.assertIn("mivideoplayer", result["package"])
        args = adb.shell.call_args[0][0]
        self.assertIn("android.intent.action.VIEW", args)
        self.assertIn("http://192.0.2.10:8899/a.mp4", args)

    def test_launch_video_rejects_non_http(self):
        from unittest.mock import Mock
        from adbtv.device import TVDevice
        from adbtv.config import DEFAULTS
        device = TVDevice(Mock(), DEFAULTS)
        with self.assertRaises(ValueError):
            device.launch_video("file:///etc/passwd")

    def test_launch_video_reports_failure_without_claiming_success(self):
        from unittest.mock import Mock
        from adbtv.device import TVDevice
        from adbtv.config import DEFAULTS
        adb = Mock()
        adb.shell.return_value = CmdResult([], 1, "", "Error: Activity not started")
        result = TVDevice(adb, DEFAULTS).launch_video("http://x/y.mp4")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()


class MediaAuthTests(unittest.TestCase):
    """V3.4.2: no anonymous access, sessions bound to one file, Range works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lib"
        (self.root / "剧" / "a (2026)").mkdir(parents=True)
        self.file = self.root / "剧" / "a (2026)" / "a S01E001.mp4"
        self.file.write_bytes(b"0123456789" * 100)
        self.state = Path(self.tmp.name) / "state"
        self.server = MediaServer([str(self.root)], host="127.0.0.1", port=0,
                                  token="permanent-secret", state_dir=self.state).start()
        self.addCleanup(self.server.stop)

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def signed_path(self, server=None):
        server = server or self.server
        url = server.url_for(str(self.file), host="127.0.0.1")
        return url[url.index("/", 7):]

    def test_anonymous_access_is_denied_even_without_token_configured(self):
        from urllib.parse import quote
        server = MediaServer([str(self.root)], host="127.0.0.1", port=0,
                             state_dir=self.state)
        server.start()
        self.addCleanup(server.stop)
        relative = server.relative_of(str(self.file))
        response, body = self.get_for(server, "/" + quote(relative))
        self.assertEqual(response.status, 401, "未配置 token 也不能匿名放行")
        self.assertEqual(body[:1], b"{")

    def get_for(self, server, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_signed_session_link_plays_without_long_term_token(self):
        path = self.signed_path()
        self.assertNotIn("permanent-secret", path)
        response, body = self.get(path)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(body), self.file.stat().st_size)

    def test_signed_link_supports_range_for_tv_player(self):
        path = self.signed_path()
        response, body = self.get(path, {"Range": "bytes=100-199"})
        self.assertEqual(response.status, 206)
        self.assertEqual(body, (b"0123456789" * 100)[100:200])

    def test_explicit_long_term_token_still_works(self):
        from urllib.parse import quote
        relative = self.server.relative_of(str(self.file))
        response, _ = self.get("/" + quote(relative),
                               {"X-Auth-Token": "permanent-secret"})
        self.assertEqual(response.status, 200)
        response, _ = self.get("/" + quote(relative), {"X-Auth-Token": "wrong"})
        self.assertEqual(response.status, 401)

    def test_tampered_signature_is_rejected(self):
        path = self.signed_path().replace("sig=", "sig=0")
        self.assertEqual(self.get(path)[0].status, 401)

    def test_session_is_bound_to_a_single_file(self):
        relative = self.server.relative_of(str(self.file))
        sid, signature = self.server.auth.open_session(relative)
        self.assertTrue(self.server.auth.touch(relative, sid, signature))
        self.assertFalse(self.server.auth.touch("剧/a (2026)/b.mp4", sid, signature))

    def test_link_expires_after_idle_window(self):
        relative = self.server.relative_of(str(self.file))
        sid, signature = self.server.auth.open_session(relative)
        # 模拟空闲超过 session_idle
        sessions = self.server.auth._load_sessions()
        stale = time.time() - self.server.session_idle - 10
        sessions[sid] = {"path": relative, "created": stale, "last": stale}
        self.server.auth._save_sessions(sessions)
        self.assertFalse(self.server.auth.touch(relative, sid, signature))
        from urllib.parse import quote
        response, _ = self.get(f"/{quote(relative)}?sid={sid}&sig={signature}")
        self.assertEqual(response.status, 401)

    def test_activity_keeps_session_alive_for_long_playback(self):
        """模拟看 5 分钟后继续拖动：每次请求都应刷新空闲窗口。"""
        relative = self.server.relative_of(str(self.file))
        sid, signature = self.server.auth.open_session(relative)
        sessions = self.server.auth._load_sessions()
        started = time.time() - 600          # 已播放 10 分钟
        sessions[sid] = {"path": relative, "created": started,
                         "last": time.time() - 60}
        self.server.auth._save_sessions(sessions)
        self.assertTrue(self.server.auth.touch(relative, sid, signature),
                        "持续访问应使会话保持有效")
        path = f"/{relative}?sid={sid}&sig={signature}"
        from urllib.parse import quote
        response, _ = self.get("/" + quote(relative) + f"?sid={sid}&sig={signature}")
        self.assertEqual(response.status, 200, "活跃会话可以继续播放")
        response, _ = self.get("/" + quote(relative) + f"?sid={sid}&sig={signature}",
                              {"Range": "bytes=300-399"})
        self.assertEqual(response.status, 206, "长视频仍可拖动进度")

    def test_path_traversal_rejected(self):
        for path in ("/../etc/passwd", "/%2e%2e/etc/passwd"):
            with self.subTest(path=path):
                self.assertIn(self.get(path)[0].status, (400, 404))

    def test_health_endpoint_is_public(self):
        response, body = self.get("/health")
        self.assertEqual(response.status, 200)
        self.assertIn(b'"service": "media"', body)


class MediaBindTests(unittest.TestCase):
    """Blank/wildcard bind addresses must resolve to a concrete LAN address."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "x.mp4").write_bytes(b"x")

    def server(self, host):
        server = MediaServer([str(self.root)], host=host, port=0,
                             state_dir=Path(self.tmp.name) / "state",
                             cfg={"adb": {"host": "192.0.2.20"}})
        return server

    def test_wildcard_is_not_used_verbatim(self):
        for host in ("", "0.0.0.0", "::", "*"):
            with self.subTest(host=host):
                self.assertNotIn(self.server(host).host, ("0.0.0.0", "::", "*"))

    def test_explicit_lan_address_is_kept(self):
        self.assertEqual(self.server("192.0.2.10").host, "192.0.2.10")


class MultiProcessMediaTests(unittest.TestCase):
    """Every worker mints links for the same single media service."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lib"
        self.root.mkdir()
        self.file = self.root / "剧集 S01E001.mp4"
        self.file.write_bytes(b"Z" * 2048)
        self.state = Path(self.tmp.name) / "state"
        self.server = MediaServer([str(self.root)], host="127.0.0.1", port=0,
                                  state_dir=self.state).start()
        self.addCleanup(self.server.stop)
        self.cfg = {"media": {"roots": [str(self.root)], "serve_port": self.server.port,
                              "advertise_host": "127.0.0.1", "serve": True,
                              "state_dir": str(self.state)},
                    "execution": {"state_dir": str(self.state)}}

    def test_workers_are_clients_not_socket_owners(self):
        from adbtv.media_server import MediaLinks
        links = MediaLinks(self.cfg, allow_loopback=True)
        self.assertTrue(links.probe(), "应能探测到中央媒体服务")
        url = links.url_for(str(self.file))
        self.assertIsNone(getattr(links, "httpd", None), "worker 不应持有监听 socket")

    def test_many_workers_share_one_service(self):
        import concurrent.futures
        from adbtv.media_server import MediaLinks

        def play(index):
            links = MediaLinks(self.cfg, allow_loopback=True)
            path = links.url_for(str(self.file))
            path = path[path.index("/", 7):]
            conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
            conn.request("GET", path, headers={"Range": "bytes=0-99"})
            response = conn.getresponse()
            body = response.read()
            conn.close()
            return response.status, len(body)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(play, range(12)))
        self.assertEqual({status for status, _ in results}, {206})
        self.assertEqual({length for _, length in results}, {100})

    def test_worker_without_service_fails_clearly(self):
        from adbtv.media_server import MediaLinks
        cfg = dict(self.cfg)
        cfg["media"] = dict(cfg["media"], serve_port=1)   # 无服务监听
        links = MediaLinks(cfg, allow_loopback=True)
        self.assertFalse(links.probe())
class InProcessSimulatorTests(unittest.TestCase):
    """Fast in-process device simulation for volume tests (no subprocess)."""

    def cfg(self):
        import copy
        from adbtv.config import DEFAULTS
        cfg = copy.deepcopy(DEFAULTS)
        cfg["adb"].update(serial="fake-tv:5555", simulator="inprocess",
                          fake_state=str(Path(tempfile.mkdtemp()) / "tv.json"))
        cfg["execution"]["state_dir"] = str(Path(cfg["adb"]["fake_state"]).parent)
        return cfg

    def test_inprocess_device_reads(self):
        from adbtv.adb import AdbClient
        from adbtv.device import TVDevice
        cfg = self.cfg()
        device = TVDevice(AdbClient(cfg), cfg)
        self.assertIn("Xiaomi", device.info()["model"])
        self.assertEqual(device.current_package(), "com.xiaodianshi.tv.yst")
        self.assertTrue(device.ui_dump()["texts"])

    def test_inprocess_matches_subprocess_semantics(self):
        """Launch/search flow must behave the same in both backends."""
        import copy
        from adbtv.adb import AdbClient
        from adbtv.device import TVDevice
        from adbtv.tools import auto_play
        inproc = self.cfg()
        dev = TVDevice(AdbClient(inproc), inproc)
        result = auto_play(dev, "com.xiaodianshi.tv.yst", "科技", inproc)
        self.assertTrue(result["ok"], result.get("summary"))
        self.assertIn("tap_first_result", [s["step"] for s in result["log"]])

    def test_inprocess_is_much_faster_than_subprocess(self):
        import time, copy, sys, shlex
        from adbtv.adb import AdbClient
        from adbtv.config import DEFAULTS
        fast_cfg = self.cfg()
        slow_cfg = copy.deepcopy(DEFAULTS)
        root = Path(__file__).resolve().parents[1]
        slow_cfg["adb"].update(serial="fake-tv:5555",
                               binary=shlex.join([sys.executable, str(root / "tests" / "fake_adb.py")]),
                               fake_state=fast_cfg["adb"]["fake_state"])
        fast, slow = AdbClient(fast_cfg), AdbClient(slow_cfg)

        def timed(client, count=6):
            start = time.monotonic()
            for _ in range(count):
                client.shell(["getprop", "ro.product.model"])
            return time.monotonic() - start

        fast_time, slow_time = timed(fast), timed(slow)
        self.assertLess(fast_time, slow_time)
        self.assertLess(fast_time, 0.5, f"进程内应足够快: {fast_time:.2f}s")


class WakeTests(unittest.TestCase):
    """A sleeping TV must be woken before playback; never toggled while awake."""

    def device(self):
        import copy
        from adbtv.config import DEFAULTS
        from adbtv.adb import AdbClient
        from adbtv.device import TVDevice
        cfg = copy.deepcopy(DEFAULTS)
        cfg["adb"].update(serial="fake:5555", simulator="inprocess",
                          fake_state=str(Path(tempfile.mkdtemp()) / "tv.json"))
        return TVDevice(AdbClient(cfg), cfg)

    def test_wake_is_noop_when_awake(self):
        device = self.device()
        result = device.wake()
        self.assertTrue(result["ok"])
        self.assertFalse(result["woke"], "清醒时不应发送电源键")
        self.assertFalse(device.power_state()["asleep"])

    def test_wake_brings_sleeping_tv_back(self):
        device = self.device()
        device.key("power")                     # 让模拟电视进入睡眠
        self.assertTrue(device.power_state()["asleep"])
        result = device.wake()
        self.assertTrue(result["woke"])
        self.assertFalse(device.power_state()["asleep"])

    def test_unknown_power_state_never_sends_power_key(self):
        from unittest.mock import Mock
        from adbtv.device import TVDevice
        from adbtv.config import DEFAULTS
        from adbtv.adb import AdbError
        adb = Mock()
        adb.shell.side_effect = AdbError("timeout")
        device = TVDevice(adb, DEFAULTS)
        result = device.wake()
        self.assertTrue(result["ok"])
        self.assertFalse(result["woke"])
        adb.shell.assert_called_once()          # 只查询，不发键
