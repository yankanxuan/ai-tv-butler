"""Read-only media HTTP server with HTTP Range support for TV playback.

Android TV video players require byte-range requests (206 Partial Content) for
progressive playback and seeking. Python's stdlib ``http.server`` does not
implement Range, so a player that receives a plain 200 with the whole file may
report duration 0 and give up.

Security model (V3.4.2):

* Access is **never anonymous**. A request must present either a valid playback
  session link (``?sid=..&sig=..``) or an explicitly configured long-term token.
  Leaving ``token`` empty does not open the library.
* Playback links are bound to one file and carry a session id. Sessions live in
  a shared state file, so several worker processes can validate the same link,
  and each authorized request slides the idle window forward. A long movie keeps
  playing (and can still seek) without re-issuing links, while an idle link stops
  working after ``session_idle`` seconds.
* The signing key is persisted under the runtime directory and shared, so URLs
  minted by any process are accepted by the single media service.
* Only files under the configured roots, only GET/HEAD, no listings, no writes.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import http.server
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import socketserver
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from .media_address import resolve_media_host
from . import __version__

CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class MediaAuth:
    """Shared signing/session logic used by both the server and URL minting."""

    def __init__(self, key_path: str | Path, sessions_path: str | Path,
                 *, link_ttl: int = 300, session_idle: int = 1800):
        self.key_path = Path(key_path).expanduser()
        self.sessions_path = Path(sessions_path).expanduser()
        self.link_ttl = max(30, int(link_ttl))
        self.session_idle = max(self.link_ttl, int(session_idle))
        self._lock = threading.Lock()
        self._secret = self._load_key()
        self._lock_path = self.sessions_path.with_suffix(".lock")

    @contextlib.contextmanager
    def _persistent_lock(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _file_lock(self):
        return self._persistent_lock(self._lock_path)

    @staticmethod
    def _atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            # Only the mkstemp path in this exact parent is eligible for cleanup.
            target = Path(temp).resolve()
            if target.parent != path.parent.resolve() or not target.name.startswith(path.name + "."):
                raise RuntimeError("Unexpected temporary path")
            target.unlink(missing_ok=True)

    def _load_key(self) -> bytes:
        with self._persistent_lock(self.key_path.with_name(self.key_path.name + ".lock")):
            try:
                raw = self.key_path.read_bytes()
            except FileNotFoundError:
                raw = secrets.token_bytes(32)
                if len(raw) != 32:
                    raise ValueError("Generated media key must contain exactly 32 bytes")
                self._atomic_write(self.key_path, raw)
            if len(raw) != 32:
                raise ValueError("Media key must contain exactly 32 bytes; restore the key backup")
            return raw

    # ---------- 会话状态 ----------
    def _load_sessions(self) -> dict:
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("Media sessions must be an object")
        return data

    def _save_sessions(self, sessions: dict) -> None:
        now = time.time()
        kept = {sid: value for sid, value in sessions.items()
                if isinstance(value, dict)
                and now - float(value.get("last", 0)) <= self.session_idle * 2}
        self._atomic_write(self.sessions_path, json.dumps(kept, ensure_ascii=False).encode("utf-8"))

    # ---------- 签发与校验 ----------
    def signature(self, sid: str, relative: str) -> str:
        message = f"{sid}\n{relative}".encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def open_session(self, relative: str) -> tuple[str, str]:
        """Create a playback session for one file; returns (sid, signature)."""
        sid = secrets.token_urlsafe(12)
        with self._lock, self._file_lock():
            sessions = self._load_sessions()
            now = time.time()
            sessions[sid] = {"path": relative, "created": now, "last": now}
            self._save_sessions(sessions)
        return sid, self.signature(sid, relative)

    def touch(self, relative: str, sid: str | None, signature: str | None) -> bool:
        """Validate a link and slide its idle window; False means deny."""
        if not sid or not signature:
            return False
        expected = self.signature(sid, relative)
        if not hmac.compare_digest(expected, str(signature)):
            return False
        now = time.time()
        with self._lock, self._file_lock():
            sessions = self._load_sessions()
            entry = sessions.get(sid)
            if not isinstance(entry, dict) or entry.get("path") != relative:
                return False
            last = float(entry.get("last", 0))
            created = float(entry.get("created", last))
            # 首次访问必须发生在 link_ttl 内，之后按空闲窗口续期。
            if last == created and now - created > self.link_ttl:
                return False
            if now - last > self.session_idle:
                return False
            entry["last"] = now
            sessions[sid] = entry
            self._save_sessions(sessions)
        return True


class MediaServer:
    DEFAULT_TTL = 300
    DEFAULT_IDLE = 1800

    def __init__(self, roots: list[str], host: str = "", port: int = 0,
                 token: str = "", *, link_ttl: int = DEFAULT_TTL,
                 session_idle: int = DEFAULT_IDLE, private_only: bool = True,
                 state_dir: str | Path | None = None, cfg=None, advertise_host=""):
        self.roots = [Path(r).expanduser().resolve() for r in roots if r]
        if not self.roots:
            raise ValueError("媒体服务至少需要一个根目录")
        self.token = token or ""
        self.private_only = bool(private_only)
        state = Path(state_dir).expanduser() if state_dir else Path("state")
        self.auth = MediaAuth(state / "media_key", state / "media_sessions.json",
                              link_ttl=link_ttl, session_idle=session_idle)
        self.link_ttl = self.auth.link_ttl
        self.session_idle = self.auth.session_idle
        # 空白或通配地址不再被接受：解析为电视可达的内网 IP。
        self.host = resolve_media_host(cfg, host=host, advertise_host=advertise_host,
                                       allow_loopback=True)
        self.port = int(port)
        self.httpd: socketserver.TCPServer | None = None
        self.thread: threading.Thread | None = None

    # ---------- 路径安全 ----------
    def resolve(self, url_path: str) -> Path | None:
        rel = unquote(urlsplit(url_path).path).lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return None
        for root in self.roots:
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
        return None

    def relative_of(self, file_path: str) -> str:
        target = Path(file_path).expanduser().resolve()
        for root in self.roots:
            try:
                return str(target.relative_to(root))
            except ValueError:
                continue
        raise ValueError(f"文件不在媒体根目录内: {file_path}")

    def url_for(self, file_path: str, host: str | None = None,
                ttl: int | None = None) -> str:
        relative = self.relative_of(file_path)
        sid, signature = self.auth.open_session(relative)
        host = resolve_media_host(host=host or self.host, allow_loopback=True)
        return (f"http://{host}:{self.port}/" + quote(relative)
                + f"?sid={sid}&sig={signature}")

    def advertise_host(self) -> str:
        """The resolved server address, shared by binding and links."""
        return self.host

    # ---------- 授权 ----------
    def authorize(self, relative: str, query: dict, headers) -> bool:
        sid = (query.get("sid") or [None])[0]
        signature = (query.get("sig") or [None])[0]
        if self.auth.touch(relative, sid, signature):
            return True
        # 显式配置的长期令牌：仅在确实配置并校验通过时放行。
        if not self.token:
            return False
        supplied = (headers.get("X-Auth-Token") or (query.get("token") or [""])[0])
        return bool(supplied) and hmac.compare_digest(supplied.encode(),
                                                      self.token.encode())

    def readiness(self):
        failures = []
        for root in self.roots:
            try:
                # fnOS may grant access through ACLs while Unix mode bits are 000.
                # Check effective access, then perform the actual directory operation.
                if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                    raise OSError("unreadable media root")
                with os.scandir(root) as entries:
                    next(entries, None)
            except OSError:
                failures.append("media_root_unavailable")
        try:
            key = self.auth.key_path.read_bytes()
            if len(key) != 32 or not hmac.compare_digest(key, self.auth._secret):
                raise ValueError("key changed")
        except (OSError, ValueError):
            failures.append("media_key_unavailable")
        return {"ok": not failures, "service": "media", "version": __version__,
                "checks": sorted(set(failures))}

    # ---------- 生命周期 ----------
    def start(self, blocking: bool = False) -> "MediaServer":
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # keep the console quiet
                return

            def _client_allowed(self) -> bool:
                if not server.private_only:
                    return True
                host = (self.client_address or ("", 0))[0]
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    return False
                return address.is_loopback or address.is_private

            def _serve(self, with_body: bool):
                if not self._client_allowed():
                    return self._send_error(403, "client address not allowed")
                endpoint = unquote(urlsplit(self.path).path).rstrip("/")
                if endpoint in ("/health", "/readyz", "/livez"):
                    return self._send_health(endpoint)
                path = server.resolve(self.path)
                if path is None:
                    return self._send_error(404, "not found")
                query = parse_qs(urlsplit(self.path).query)
                relative = server.relative_of(str(path))
                try:
                    authorized = server.authorize(relative, query, self.headers)
                except (OSError, ValueError):
                    return self._send_error(503, "media authorization unavailable")
                if not authorized:
                    return self._send_error(401, "unauthorized")
                self._stream(path, with_body)

            def _send_health(self, endpoint):
                payload = ({"ok": True, "service": "media", "version": __version__} if endpoint == "/livez"
                           else server.readiness())
                body = json.dumps(payload).encode()
                self.send_response(200 if payload["ok"] else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _stream(self, path: Path, with_body: bool):
                size = path.stat().st_size
                content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                start, end, partial = 0, size - 1, False
                header = self.headers.get("Range", "")
                if header:
                    match = _RANGE.fullmatch(header.strip())
                    if not match:
                        return self._send_error(400, "bad range")
                    first, second = match.groups()
                    if not first and not second:
                        return self._send_error(400, "bad range")
                    if first:
                        start = int(first)
                        end = int(second) if second else size - 1
                    else:  # suffix range: last N bytes
                        length = int(second)
                        start = max(0, size - length)
                        end = size - 1
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    partial = True
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not with_body:
                    return
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        remaining -= len(chunk)

            def _send_error(self, code, message):
                body = json.dumps({"ok": False, "error": message}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

            def _reject(self):
                return self._send_error(405, "method not allowed")

            do_POST = do_PUT = do_DELETE = do_PATCH = _reject

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.httpd = Server((self.host, self.port), Handler)
        self.port = self.httpd.server_address[1]
        if blocking:
            self.httpd.serve_forever()
        else:
            # 短轮询：shutdown() 立即返回，避免测试与重启多等 0.5s。
            self.thread = threading.Thread(
                target=lambda: self.httpd.serve_forever(poll_interval=0.05),
                name="tv-media-http", daemon=True)
            self.thread.start()
        return self

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except RuntimeError:
                pass
            try:
                self.httpd.server_close()
            except OSError:
                pass
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


class MediaLinks:
    """Mint playback URLs without binding a port (used by every Agent worker).

    Only one process may own the media socket; workers therefore sign URLs
    against the shared key/session store and point at the running service.
    """

    def __init__(self, cfg: dict, roots: list[str] | None = None, *, allow_loopback=False):
        media = (cfg.get("media", {}) or {})
        from .execution import runtime_dir
        state = media.get("state_dir") or str(runtime_dir(cfg).parent)
        self.auth = MediaAuth(Path(state) / "media_key",
                              Path(state) / "media_sessions.json",
                              link_ttl=int(media.get("link_ttl", MediaServer.DEFAULT_TTL)),
                              session_idle=int(media.get("session_idle",
                                                         MediaServer.DEFAULT_IDLE)))
        self.roots = [Path(r).expanduser().resolve()
                      for r in (roots if roots is not None else media.get("roots") or [])]
        self.host = resolve_media_host(cfg, host=media.get("serve_host", ""),
                                       advertise_host=media.get("advertise_host", ""),
                                       allow_loopback=allow_loopback)
        self._allow_loopback = allow_loopback
        self.port = int(media.get("serve_port", 8899))

    def probe(self, timeout: float = 1.5) -> bool:
        """True when media readiness answers at the shared configured address."""
        host = self.host
        try:
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://{host}:{self.port}/readyz", timeout=timeout) as response:
                payload = json.loads(response.read())
                return response.status == 200 and payload.get("ok") is True and payload.get("service") == "media"
        except (OSError, ValueError):
            return False

    def url_for(self, file_path: str, host: str | None = None) -> str:
        target = Path(file_path).expanduser().resolve()
        for root in self.roots:
            try:
                relative = str(target.relative_to(root))
            except ValueError:
                continue
            sid, signature = self.auth.open_session(relative)
            host = resolve_media_host(host=host or self.host, allow_loopback=self._allow_loopback)
            return (f"http://{host}:{self.port}/" + quote(relative)
                    + f"?sid={sid}&sig={signature}")
        raise ValueError(f"文件不在媒体根目录内: {file_path}")


def serve_from_config(cfg) -> MediaServer | None:
    """Start the media server when cfg['media'] opts in; returns None if off."""
    media = cfg.get("media", {}) or {}
    if not media.get("enabled", False) or not media.get("serve", False):
        return None
    from .execution import runtime_dir
    server = MediaServer(media.get("roots") or [],
                         host=media.get("serve_host", ""),
                         port=int(media.get("serve_port", 8899)),
                         token=media.get("serve_token", ""),
                         link_ttl=int(media.get("link_ttl", MediaServer.DEFAULT_TTL)),
                         session_idle=int(media.get("session_idle",
                                                    MediaServer.DEFAULT_IDLE)),
                         private_only=bool(media.get("private_only", True)),
                         state_dir=media.get("state_dir") or runtime_dir(cfg).parent,
                         cfg=cfg, advertise_host=media.get("advertise_host", ""))
    return server.start()