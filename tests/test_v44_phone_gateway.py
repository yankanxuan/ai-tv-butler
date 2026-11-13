"""V4.4 phone entry: HTTPS gateway endpoints in front of the offline ASR lab.

The gateway is the only place a phone talks to: it terminates TLS, checks the
console token by asking the console itself, then forwards audio to the offline
ASR lab and text to ``/api/voice`` (the V4.4 dialogue). These tests use two tiny
local servers instead of the real console/ASR so they run offline.
"""
import http.server
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "scripts" / "v44_https_gateway.py"
TOKEN = "secret-token"   # allow-listed test fixture value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Mimics the console (token check + /api/voice) and the ASR lab."""

    protocol_version = "HTTP/1.1"
    calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).calls.append(("GET", self.path))
        if self.path == "/api/config":
            if self.headers.get("X-Auth-Token") != TOKEN:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"voice": {"enabled": True, "intelligence": {"enabled": True}}})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).calls.append(("POST", self.path, len(body)))
        if self.path == "/transcribe-upload":
            if self.headers.get("Authorization") != "Bearer lab-token":
                return self._send(401, {"error": "bad_lab_token"})
            if self.headers.get("X-ASR-Consent") != "true":
                return self._send(403, {"error": "explicit_consent_required"})
            return self._send(200, {"asr": {"hypothesis": "推荐三部好看的",
                                            "audio_seconds": 1.2, "wall_seconds": 0.4}})
        if self.path == "/api/voice":
            if self.headers.get("X-Auth-Token") != TOKEN:
                return self._send(401, {"error": "unauthorized"})
            payload = json.loads(body.decode() or "{}")
            return self._send(200, {"status": "SUCCESS",
                                    "reply": "共 35 部作品（收到：" + payload.get("text", "") + "）",
                                    "candidates": [{"id": "a1", "title": "宝星行", "episodes": 60}]})
        return self._send(404, {"error": "not_found"})


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.upstream_port = _free_port()
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", cls.upstream_port), _Upstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

        cls.port = _free_port()
        cert = tmp / "cert.pem"
        key = tmp / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=localhost"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        asr_token = tmp / "asr.token"
        asr_token.write_text("lab-token")
        cls.proc = subprocess.Popen(
            [sys.executable, str(GATEWAY), "--listen", "127.0.0.1", "--port", str(cls.port),
             "--upstream", f"http://127.0.0.1:{cls.upstream_port}",
             "--cert", str(cert), "--key", str(key),
             "--page", str(ROOT / "poc" / "v44_phone" / "index.html"),
             "--asr-url", f"http://127.0.0.1:{cls.upstream_port}",
             "--asr-token-file", str(asr_token)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.base = f"https://127.0.0.1:{cls.port}"
        cls.ctx = ssl._create_unverified_context()
        for _ in range(60):
            try:
                urllib.request.urlopen(cls.base + "/phone/", timeout=1, context=cls.ctx)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise RuntimeError("gateway did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.upstream.shutdown()
        cls.tmp.cleanup()

    def _post(self, path, data, headers):
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20, context=self.ctx) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_phone_page_is_served_over_tls(self):
        with urllib.request.urlopen(self.base + "/phone/", timeout=10, context=self.ctx) as response:
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("AI 电视管家", body)
        self.assertIn("/phone/transcribe", body)
        self.assertIn("getUserMedia", body)

    def test_console_requests_still_proxy_through(self):
        # Without a token the console answers 401; the gateway must relay that
        # decision unchanged instead of inventing its own auth.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/config", timeout=10, context=self.ctx)
        self.assertEqual(caught.exception.code, 401)

    def test_transcribe_requires_the_console_token(self):
        code, body = self._post("/phone/transcribe", b"RIFF0000WAVE",
                                {"Content-Type": "audio/wav"})
        self.assertEqual(code, 401)
        self.assertIn("令牌", body["error"])

    def test_transcribe_forwards_audio_and_returns_text(self):
        code, body = self._post("/phone/transcribe", b"RIFF0000WAVEfake",
                                {"Content-Type": "audio/wav", "X-Auth-Token": TOKEN})
        self.assertEqual(code, 200)
        self.assertEqual(body["text"], "推荐三部好看的")

    def test_transcribe_rejects_oversized_audio(self):
        code, body = self._post("/phone/transcribe", b"x" * (2 * 1024 * 1024 + 10),
                                {"Content-Type": "audio/wav", "X-Auth-Token": TOKEN})
        self.assertEqual(code, 413)

    def test_voice_returns_dialogue_reply_and_candidates(self):
        payload = json.dumps({"text": "现在资源库有什么？", "source": "web_microphone"}).encode()
        code, body = self._post("/phone/voice", payload,
                                {"Content-Type": "application/json", "X-Auth-Token": TOKEN})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "SUCCESS")
        self.assertIn("共 35 部作品", body["reply"])
        self.assertEqual(body["candidates"][0]["title"], "宝星行")

    def test_voice_validates_text_and_source(self):
        code, body = self._post("/phone/voice", json.dumps({"text": ""}).encode(),
                                {"Content-Type": "application/json", "X-Auth-Token": TOKEN})
        self.assertEqual(code, 400)
        code, body = self._post("/phone/voice",
                                json.dumps({"text": "hi", "source": "xiaomi_logcat"}).encode(),
                                {"Content-Type": "application/json", "X-Auth-Token": TOKEN})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
