"""HTTP regressions for a local, CSP-compatible console bootstrap."""
import copy
import http.client
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from unittest.mock import Mock, patch

from adbtv.config import DEFAULTS
from adbtv.server import make_server


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["execution"]["state_dir"] = self.tmp.name
        cfg["memory"]["enabled"] = False
        cfg["server"].update(host="127.0.0.1", port=0, token="isolated-console-test-token")
        cfg["llm"]["provider"] = "none"
        with patch("adbtv.server.build_context", return_value=tuple(Mock() for _ in range(5))):
            self.server = make_server(cfg)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01))
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def get(self, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_script_policy_for_html_static_and_api_denies_inline_and_eval(self):
        for path in ("/", "/static/console.js", "/api/config"):
            with self.subTest(path=path):
                _, headers, _ = self.get(path)
                directives = dict((d.strip().split(None, 1) + [""])[:2]
                                  for d in headers["Content-Security-Policy"].split(";") if d.strip())
                self.assertEqual(directives["script-src"], "'self'")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_bootstrap_is_external_and_registers_before_pinned_alpine(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        parser = Scripts()
        parser.feed(body.decode())
        self.assertEqual([s.get("src") for s in parser.scripts],
                         ["/static/console.js", "/vendor/alpine-csp-3.14.9.min.js"])
        self.assertTrue(all("defer" in s and "async" not in s for s in parser.scripts))
        status, headers, script = self.get("/static/console.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"alpine:init", script)
        self.assertIn(b"getReader()", script)
        self.assertNotIn(b"localStorage", script)
        self.assertNotIn(b"sessionStorage", script)
        self.assertEqual(self.get("/vendor/alpine-csp-3.14.9.min.js")[0], 200)

    def test_static_route_cannot_expose_other_files_or_bypass_api_auth(self):
        for path in ("/static/index.html", "/static/server.py", "/static/missing.js",
                     "/static/../server.py", "/static/%2e%2e%2fserver.py",
                     "/static/console.js/extra", "/static/console.js%2f..%2fserver.py"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertIn(status, (400, 401, 404))
                self.assertNotIn(b"def make_server", body)
        self.assertEqual(self.get("/api/config")[0], 401)
        self.assertEqual(self.get("/api/runs")[0], 401)
        self.assertEqual(self.get("/api/voice/status")[0], 401)


if __name__ == "__main__":
    unittest.main()
