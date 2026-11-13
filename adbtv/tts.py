"""Piper Chinese TTS with a deliberately narrow, non-blocking adapter.

Measured TV limits (2026-11-11, MiTV-ASTP0): generic WAV VIEW intents only reach
Bluetooth OPP, and ``tinyplay`` cannot open the PCM device. Playing audio through
MiVideoPlayer would take over the video surface. Therefore this module produces
audio for browser/phone delivery; it never injects audio into the TV playback.

Safety properties:

* synthesis runs as an argv list (no shell), with a hard timeout;
* text is bounded and never used as a filename (hashed cache key);
* cache writes are atomic, private (0600) and bounded in size;
* failure returns an explicit error and never a fake "spoken" result.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TEXT = 200
DEFAULT_TIMEOUT = 15.0
DEFAULT_CACHE = 64


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class Synthesis:
    ok: bool
    text: str
    path: str = ""
    duration_ms: int = 0
    cached: bool = False
    elapsed_ms: float = 0.0
    error: str = ""

    def audio_url(self, base: str) -> str:
        if not self.ok or not self.path:
            return ""
        return f"{base.rstrip('/')}/api/voice/audio/{Path(self.path).name}"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "duration_ms": self.duration_ms,
                "cached": self.cached, "elapsed_ms": round(self.elapsed_ms, 1),
                "chars": len(self.text), "error": self.error}


def _bounded_text(text: Any) -> str:
    if not isinstance(text, str):
        raise TTSError("待播报文本必须是字符串")
    clean = " ".join(text.split())
    if not clean or len(clean) > MAX_TEXT:
        raise TTSError("待播报文本为空或过长")
    return clean


class PiperTTS:
    """Synthesize Chinese speech to a private cache directory."""

    def __init__(self, cfg: dict, *, binary: str | None = None,
                 model: str | None = None, cache_dir: str | None = None):
        voice = (cfg.get("voice") or {})
        options = (voice.get("tts") or {})
        self.binary = binary or options.get("binary") or self._default_binary()
        self.model = model or options.get("model") or ""
        self.timeout = max(1.0, float(options.get("timeout_seconds", DEFAULT_TIMEOUT)))
        self.cache_limit = max(4, int(options.get("cache_files", DEFAULT_CACHE)))
        if cache_dir:
            self.cache = Path(cache_dir)
        elif options.get("cache_dir"):
            self.cache = Path(options["cache_dir"]).expanduser()
        else:
            from .execution import runtime_dir
            self.cache = runtime_dir(cfg).parent / "voice_tts"
        self._lock = threading.Lock()

    @staticmethod
    def _default_binary() -> str:
        candidates = (Path.home() / ".local/bin/piper",
                      Path("/usr/local/bin/piper"), Path("/usr/bin/piper"))
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which("piper") or ""

    def available(self) -> tuple[bool, str]:
        if not self.binary or not Path(self.binary).is_file():
            return False, "piper 不可执行"
        if not self.model or not Path(self.model).is_file():
            return False, "缺少中文语音模型"
        return True, ""

    # ---------- 合成 ----------
    def synthesize(self, text: str) -> Synthesis:
        started = time.monotonic()
        try:
            clean = _bounded_text(text)
        except TTSError as exc:
            return Synthesis(False, "", error=str(exc))
        ok, reason = self.available()
        if not ok:
            return Synthesis(False, clean, error=reason)
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:32]
        target = self.cache / f"{digest}.wav"
        if target.is_file() and target.stat().st_size > 44:
            return Synthesis(True, clean, str(target), self._duration_ms(target),
                             True, (time.monotonic() - started) * 1000)
        try:
            self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            return Synthesis(False, clean, error=f"无法创建语音缓存目录: {exc}")
        temp = target.with_suffix(".tmp.wav")
        command = [self.binary, "-m", self.model, "-f", str(temp)]
        try:
            result = subprocess.run(command, input=clean.encode("utf-8"),
                                    capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            temp.unlink(missing_ok=True)
            return Synthesis(False, clean, error=f"语音合成失败: {exc}")
        if result.returncode != 0 or not temp.is_file() or temp.stat().st_size <= 44:
            temp.unlink(missing_ok=True)
            detail = (result.stderr or b"").decode("utf-8", "ignore")[:200]
            return Synthesis(False, clean, error=f"语音合成失败: {detail or result.returncode}")
        os.chmod(temp, 0o600)
        temp.replace(target)
        self._trim_cache()
        return Synthesis(True, clean, str(target), self._duration_ms(target), False,
                         (time.monotonic() - started) * 1000)

    @staticmethod
    def _duration_ms(path: Path) -> int:
        """WAV header duration; avoids spawning ffprobe on the hot path."""
        try:
            with path.open("rb") as handle:
                header = handle.read(44)
            if len(header) < 44 or header[:4] != b"RIFF":
                return 0
            import struct
            byte_rate = struct.unpack("<I", header[28:32])[0]
            data = struct.unpack("<I", header[40:44])[0]
            return int(data / byte_rate * 1000) if byte_rate else 0
        except OSError:
            return 0

    def _trim_cache(self) -> None:
        with self._lock:
            try:
                files = sorted(self.cache.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            except OSError:
                return
            for stale in files[:-self.cache_limit]:
                stale.unlink(missing_ok=True)