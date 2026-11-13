#!/usr/bin/env python3
"""Minimal HTTPS front for the local AI-TV-Butler console (V4.4 phone entry).

Why this exists
---------------
Android/iOS browsers only expose the microphone (Web Speech / getUserMedia) in a
*secure context*, so the HTTP-only console on the LAN cannot be used from a
phone. This gateway terminates TLS with a self-signed certificate and forwards
requests verbatim to the console, which keeps doing its own token auth, CSRF,
rate limiting and payload checks. It adds no privileges of its own.

What it is NOT
--------------
* It does not store, inspect or log request bodies (audio, tokens, transcripts).
* It does not listen on the public internet; bind it to the LAN address only.
* It is a development/LAN helper, not a replacement for a real reverse proxy.

Usage
-----
    python3 scripts/v44_https_gateway.py \
        --listen 0.0.0.0 --port 8443 \
        --upstream http://<NAS_LAN_IP>:8771 \
        --cert deploy/data/v44-portal/certs/cert.pem \
        --key  deploy/data/v44-portal/certs/key.pem
"""
from __future__ import annotations

import argparse
import collections
import http.server
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY = 8 * 1024 * 1024          # hard cap for uploads (audio/JSON)
UPSTREAM_TIMEOUT = 120              # one turn may include a slow model call
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


MAX_AUDIO = 2 * 1024 * 1024         # 2 MB ≈ 60 s of 16 kHz mono 16-bit
AUDIO_RATE_LIMIT = (10, 60.0)       # 10 uploads per minute per client
MAX_TEXT = 500


def make_handler(upstream: str, upstream_host: str, *, page: str = "",
                 asr_url: str = "", asr_token: str = ""):
    base = upstream.rstrip("/")
    page_bytes = Path(page).read_bytes() if page else b""
    asr_base = asr_url.rstrip("/")
    hits: dict[str, collections.deque] = {}
    hits_lock = threading.Lock()

    def rate_ok(client: str) -> bool:
        limit, window = AUDIO_RATE_LIMIT
        now = time.monotonic()
        with hits_lock:
            queue = hits.setdefault(client, collections.deque())
            while queue and now - queue[0] > window:
                queue.popleft()
            if len(queue) >= limit:
                return False
            queue.append(now)
            return True

    def console_ok(token: str) -> bool:
        """The console owns identity: only it decides whether a token is valid."""
        if not token:
            return False
        request = urllib.request.Request(base + "/api/config",
                                         headers={"X-Auth-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status == 200
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ai-tv-butler-gateway"
        sys_version = ""

        # ---- helpers -------------------------------------------------
        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                raise ValueError("payload too large")
            return self.rfile.read(length) if length else b""

        # ---- V4.4 phone entry (offline ASR + console dialogue) --------
        def _token(self) -> str:
            return (self.headers.get("X-Auth-Token") or "").strip()

        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _phone_transcribe(self) -> None:
            token = self._token()
            if not console_ok(token):
                return self._json(401, {"error": "令牌无效，请重新登录"})
            if not asr_base or not asr_token:
                return self._json(503, {"error": "离线识别服务未启用"})
            if not rate_ok(self.client_address[0]):
                return self._json(429, {"error": "识别太频繁，请稍后再试"})
            try:
                body = self._read_body()
            except ValueError:
                return self._json(413, {"error": "音频太大"})
            if not body or len(body) > MAX_AUDIO:
                return self._json(413, {"error": "音频为空或超过 2 MB"})
            request = urllib.request.Request(
                asr_base + "/transcribe-upload", data=body, method="POST",
                headers={"Authorization": "Bearer " + asr_token,
                         "X-ASR-Consent": "true", "Content-Type": "audio/wav"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return self._json(502, {"error": "识别服务返回 %s" % exc.code})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                return self._json(502, {"error": "识别服务不可用"})
            asr = payload.get("asr") or {}
            return self._json(200, {"text": (asr.get("hypothesis") or "").strip(),
                                    "audio_seconds": asr.get("audio_seconds"),
                                    "wall_seconds": asr.get("wall_seconds")})

        def _phone_voice(self) -> None:
            token = self._token()
            if not console_ok(token):
                return self._json(401, {"error": "令牌无效，请重新登录"})
            try:
                body = json.loads(self._read_body().decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "请求格式无效"})
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                return self._json(400, {"error": "文本须为 1..%d 字符" % MAX_TEXT})
            source = body.get("source") or "web_microphone"
            if source not in ("web_microphone", "external_asr"):
                return self._json(400, {"error": "source 不合法"})
            request = urllib.request.Request(
                base + "/api/voice", method="POST",
                data=json.dumps({"text": text, "source": source}).encode(),
                headers={"Content-Type": "application/json", "X-Auth-Token": token})
            try:
                with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                try:
                    return self._json(exc.code, json.loads(payload.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    return self._json(exc.code, {"error": "控制台返回错误"})
            except (urllib.error.URLError, TimeoutError, OSError):
                return self._json(502, {"error": "控制台不可用"})
            try:
                return self._json(200, json.loads(payload.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                return self._json(502, {"error": "控制台返回格式无效"})

        def _phone_page(self) -> None:
            if not page_bytes:
                return self._plain(404, "phone page not configured")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; "
                             "base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(page_bytes)

        def _forward(self, method: str) -> None:
            try:
                body = self._read_body()
            except ValueError:
                return self._plain(413, "payload too large")
            target = base + self.path
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in HOP_BY_HOP}
            request = urllib.request.Request(target, data=body if body else None,
                                             headers=headers, method=method)
            try:
                response = urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)
            except urllib.error.HTTPError as exc:
                response = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                return self._plain(502, f"upstream unavailable: {exc}")
            try:
                self._relay(response)
            finally:
                response.close()

        def _relay(self, response) -> None:
            self.send_response(response.status)
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            for key, value in response.headers.items():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-type":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            streaming = content_type.startswith("text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if streaming:
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                return
            payload = response.read()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _plain(self, code: int, message: str) -> None:
            payload = (message + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # ---- verbs ---------------------------------------------------
        def do_GET(self) -> None:      # noqa: N802 - stdlib naming
            path = urlsplit(self.path).path
            if path in ("/phone", "/phone/", "/phone/index.html"):
                return self._phone_page()
            self._forward("GET")

        def do_POST(self) -> None:     # noqa: N802
            path = urlsplit(self.path).path
            if path == "/phone/transcribe":
                return self._phone_transcribe()
            if path == "/phone/voice":
                return self._phone_voice()
            self._forward("POST")

        def do_PUT(self) -> None:      # noqa: N802
            self._forward("PUT")

        def do_DELETE(self) -> None:   # noqa: N802
            self._forward("DELETE")

        def log_message(self, fmt: str, *args) -> None:
            # Never log paths or headers: they can carry tokens and audio.
            sys.stderr.write("[gateway] %s\n" % (fmt % args,))

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--upstream", default="http://127.0.0.1:8771")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--page", default="poc/v44_phone/index.html",
                        help="phone recorder page (served at /phone/)")
    parser.add_argument("--asr-url", default="http://127.0.0.1:8765",
                        help="offline ASR lab base URL")
    parser.add_argument("--asr-token-file", default="",
                        help="file holding the ASR lab token (not the console token)")
    args = parser.parse_args(argv)

    parts = urlsplit(args.upstream)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        parser.error("--upstream must be an http(s) URL")
    upstream_host = parts.hostname
    asr_token = ""
    if args.asr_token_file:
        asr_token = Path(args.asr_token_file).expanduser().read_text().strip()
    handler = make_handler(args.upstream, upstream_host, page=args.page,
                           asr_url=args.asr_url, asr_token=asr_token)
    httpd = http.server.ThreadingHTTPServer((args.listen, args.port), handler)
    httpd.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    print(f"gateway ready: https://{args.listen}:{args.port}/ -> {args.upstream}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
