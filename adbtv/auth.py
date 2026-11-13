"""Local token provisioning and constant-time authentication for the console."""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from .execution import runtime_dir


def load_token(cfg):
    configured = os.environ.get("AI_TV_SERVER_TOKEN") or cfg.get("server", {}).get("token")
    if configured:
        return str(configured)
    path = Path(cfg.get("server", {}).get("token_file") or runtime_dir(cfg) / "console.token").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token = path.read_text().strip()
    if len(token) < 16:
        raise ValueError("控制台 token 文件过短，请设置至少16字符的凭证")
    return token


def authorized(headers, token):
    candidate = headers.get("X-Auth-Token", "")
    bearer = headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        candidate = bearer[7:]
    return bool(candidate) and hmac.compare_digest(candidate.encode(), token.encode())
