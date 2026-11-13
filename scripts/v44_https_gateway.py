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
import http.server
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_BODY = 8 * 1024 * 1024          # hard cap for uploads (audio/JSON)
UPSTREAM_TIMEOUT = 120              # one turn may include a slow model call
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def make_handler(upstream: str, upstream_host: str):
    base = upstream.rstrip("/")

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
            self._forward("GET")

        def do_POST(self) -> None:     # noqa: N802
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
    args = parser.parse_args(argv)

    parts = urlsplit(args.upstream)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        parser.error("--upstream must be an http(s) URL")
    upstream_host = parts.hostname
    handler = make_handler(args.upstream, upstream_host)
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
