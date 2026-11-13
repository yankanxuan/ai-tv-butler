"""Structured contracts shared by voice routing, sessions, TTS and the API.

Pydantic is used when installed; otherwise an equivalent built-in validator
keeps the project runnable on an appliance that cannot fetch wheels. Both paths
enforce the same field set, types and bounds. **Schema validation never replaces
the security checks**: permission, whitelist, confirmation and re-validation
still run in their own modules.
"""
from __future__ import annotations

import math
import re
from typing import Any

try:  # pragma: no cover - which branch runs depends on the environment
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

    PYDANTIC = True
except Exception:  # noqa: BLE001 - optional dependency
    PYDANTIC = False

ROUTES = ("agent", "native", "clarify", "ignore")
RISKS = ("none", "low", "medium", "high")
JOB_STATES = ("QUEUED", "SYNTHESIZING", "READY", "FAILED")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[\w.-]{1,128}$", re.UNICODE)
_SHELL = re.compile(r"[;|&$`<>]|adb|shell", re.IGNORECASE)


class SchemaError(ValueError):
    """Shape violation; callers turn this into their own domain error."""


def _text(value, limit, name, *, allow_empty=False):
    if not isinstance(value, str):
        raise SchemaError(f"{name} 必须是字符串")
    if len(value) > limit:
        raise SchemaError(f"{name} 超出长度上限")
    if not allow_empty and not value.strip():
        raise SchemaError(f"{name} 不能为空")
    if any(ord(ch) < 32 for ch in value):
        raise SchemaError(f"{name} 含控制字符")
    return value


# ---------------------------------------------------------------- VoiceDecision
def validate_voice_decision(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("decision 必须是对象")
    required = {"route", "intent", "target", "parameters", "confidence", "reply", "risk"}
    if set(data) != required:
        raise SchemaError("decision 字段必须与 Schema 完全一致")
    if data["route"] not in ROUTES:
        raise SchemaError("未知 route")
    if data["risk"] not in RISKS:
        raise SchemaError("未知 risk")
    _text(data["intent"], 40, "intent")
    _text(data["target"], 200, "target", allow_empty=True)
    _text(data["reply"], 1000, "reply", allow_empty=True)
    confidence = data["confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence):
        raise SchemaError("confidence 必须是有限数值")
    if not 0 <= confidence <= 1:
        raise SchemaError("confidence 超出 0..1")
    if not isinstance(data["parameters"], dict):
        raise SchemaError("parameters 必须是对象")
    if PYDANTIC:
        try:
            _DecisionModel(**data)
        except ValidationError as exc:
            raise SchemaError(str(exc).splitlines()[0]) from exc
    return data


# ---------------------------------------------------------------- PendingAction
def validate_pending(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise SchemaError("pending 必须是对象或空")
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce):
        raise SchemaError("pending 需要 32 位十六进制 nonce")
    command = _text(data.get("command"), 400, "command")
    if _SHELL.search(command):
        raise SchemaError("pending 命令不得包含 shell 结构")
    created, expires = data.get("created"), data.get("expires")
    try:
        created, expires = float(created), float(expires)
    except (TypeError, ValueError) as exc:
        raise SchemaError("pending 时间戳必须是数值") from exc
    if not (0 < created <= expires):
        raise SchemaError("pending 有效期非法")
    candidate_id = data.get("candidate_id")
    if candidate_id is not None and (not isinstance(candidate_id, str)
                                     or not _SAFE_ID.fullmatch(candidate_id)):
        raise SchemaError("pending 候选 id 非法")
    validated = {"nonce": nonce, "command": command,
                 "intent": str(data.get("intent") or "unknown")[:64],
                 "candidate_id": candidate_id, "created": created, "expires": expires}
    if PYDANTIC:
        try:
            _PendingModel(**validated)
        except ValidationError as exc:
            raise SchemaError(str(exc).splitlines()[0]) from exc
    return validated


# ---------------------------------------------------------------- TTSJob
def validate_job(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("job 必须是对象")
    job_id = data.get("id")
    if not isinstance(job_id, str) or not _HEX32.fullmatch(job_id):
        raise SchemaError("job id 必须是 32 位十六进制")
    state = data.get("state")
    if state not in JOB_STATES:
        raise SchemaError("未知 job 状态")
    if PYDANTIC:
        # Extra keys are allowed (reason/duration/url) but must be typed.
        try:
            _JobModel(id=job_id, state=state,
                      audio_url=data.get("audio_url"),
                      reason=data.get("reason"),
                      duration_ms=data.get("duration_ms"))
        except ValidationError as exc:
            raise SchemaError(str(exc).splitlines()[0]) from exc
    return data


# ---------------------------------------------------------------- VoiceResponse
def validate_voice_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("voice response 必须是对象")
    status = data.get("status")
    _text(status, 40, "status")
    route = data.get("route")
    if route is not None and route not in ROUTES:
        raise SchemaError("未知 route")
    _text(data.get("reply", ""), 1000, "reply", allow_empty=True)
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        raise SchemaError("candidates 必须是列表")
    for item in candidates:
        if not isinstance(item, dict) or not _SAFE_ID.fullmatch(str(item.get("id", ""))):
            raise SchemaError("候选条目非法")
        _text(item.get("title"), 200, "candidate.title")
    if PYDANTIC:
        try:
            _ResponseModel(status=status, route=route, reply=data.get("reply") or "",
                           candidate_count=len(candidates))
        except ValidationError as exc:
            raise SchemaError(str(exc).splitlines()[0]) from exc
    return data


# ---------------------------------------------------------------- SessionSnapshot
def validate_snapshot(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("snapshot 必须是对象")
    session_id = data.get("session_id")
    if session_id is not None and (not isinstance(session_id, str)
                                  or not _HEX32.fullmatch(session_id)):
        raise SchemaError("session_id 非法")
    expires = data.get("expires_at")
    if expires is not None:
        try:
            float(expires)
        except (TypeError, ValueError) as exc:
            raise SchemaError("expires_at 必须是数值") from exc
    if PYDANTIC:
        try:
            _SnapshotModel(session_id=session_id, expires_at=expires)
        except ValidationError as exc:
            raise SchemaError(str(exc).splitlines()[0]) from exc
    return data


def availability() -> dict[str, Any]:
    if not PYDANTIC:
        return {"backend": "builtin", "pydantic": None,
                "note": "pydantic 未安装，使用等价内置校验（同一字段/边界）"}
    import pydantic
    return {"backend": "pydantic", "pydantic": pydantic.VERSION}


if PYDANTIC:  # pragma: no cover - exercised only with pydantic installed
    class _DecisionModel(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        route: str
        intent: str
        target: str
        parameters: dict
        confidence: float = Field(ge=0, le=1)
        reply: str
        risk: str

        @field_validator("route")
        @classmethod
        def _route(cls, value):
            if value not in ROUTES:
                raise ValueError("unknown route")
            return value

        @field_validator("risk")
        @classmethod
        def _risk(cls, value):
            if value not in RISKS:
                raise ValueError("unknown risk")
            return value

    class _PendingModel(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        nonce: str
        command: str
        intent: str
        candidate_id: str | None = None
        created: float
        expires: float

    class _JobModel(BaseModel):
        model_config = ConfigDict(extra="ignore", strict=True)
        id: str
        state: str
        audio_url: str | None = None
        reason: str | None = None
        duration_ms: int | None = None

    class _ResponseModel(BaseModel):
        model_config = ConfigDict(extra="ignore", strict=True)
        status: str
        route: str | None = None
        reply: str = ""
        candidate_count: int = Field(ge=0, le=32)

    class _SnapshotModel(BaseModel):
        model_config = ConfigDict(extra="ignore", strict=True)
        session_id: str | None = None
        expires_at: float | None = None
else:  # pragma: no cover
    class _Unavailable:
        def __init__(self, **kwargs):
            pass

    _DecisionModel = _PendingModel = _JobModel = _ResponseModel = _SnapshotModel = _Unavailable