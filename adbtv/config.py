"""配置加载：config.yaml + 环境变量覆盖。

查找顺序（从高到低）：
  1. 环境变量 AI_TV_CONFIG 指定的文件
  2. 项目根目录 ./config.yaml
  3. ~/.config/ai-tv-butler/config.yaml
  4. 内置默认值
"""
from __future__ import annotations

import os
import copy
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML 系统里一般都有
except Exception:  # pragma: no cover
    yaml = None

DEFAULTS: dict[str, Any] = {
    "adb": {
        # 真机填 host/port（如中兴盒子 192.0.2.20:6000 若已开通标准 adb），
        # 或直接给 serial（如 192.0.2.10:5555）。
        "host": "",
        "port": 5555,
        "serial": "",
        "binary": "adb",
        "auto_connect": True,
        "connect_timeout": 15,
        "command_timeout": 20,
        # 离线模拟器：binary = "python3 -m adbtv.simulator"（wheel 内置）
        "fake_state": "",
    },
    "llm": {
        "provider": "anthropic",          # anthropic | openai | none
        "base_url": "http://127.0.0.1:7866",
        "api_key": "",
        "model": "cn:deepseek-v4.1-flash",
        "vision_model": "",               # 留空表示不做截图视觉分析
        "max_tokens": 1200,
        "temperature": 0.2,
        "timeout": 90,
    },
    "agent": {
        "max_steps": 18,
        "log_dir": "logs",
        "use_llm": True,
        "fast_path": True,       # 常用指令本地执行，复杂请求才交给模型
        "smart_navigation": True,
    },
    "ui": {
        "wait_timeout": 20,
        # 直连播放需要播放器冷启动 + 网络缓冲；首次播放实测可达 30s+
        "direct_play_timeout": 45,
        "poll_interval": 1.2,
        "fast_poll_interval": 0.2,
        "navigation_timeout": 60,
        "max_search_pages": 12,
    },
    "scheduler": {
        "store": "state/tasks.json",
        "poll_seconds": 20,
    },
    "execution": {
        "enabled": True,
        "timeout": 180,
        "max_actions": 100,
        "detect_manual_control": True,
        "verify_progress": False,
    },
    "perception": {"cache_ttl": 0.3, "vision_cache_size": 16},
    "recovery": {"max_attempts": 4, "per_failure": 2},
    "memory": {"enabled": False, "path": "", "record_history": True},  # 显式启用后写入 state/memory.sqlite3
    # 本机飞牛影视库（文件系统级索引，只读扫描）
    # 本机飞牛影视库（文件系统级索引，只读扫描）。默认关闭：需显式配置
    # roots 才启用，避免在没有媒体库的机器上依赖固定路径。
    "media": {"enabled": False, "roots": [], "cache": "",
              # serve_host 留空=只监听电视可达的网卡地址；private_only=仅内网可访问
              "serve": False, "serve_host": "", "serve_port": 8899,
              "serve_token": "", "link_ttl": 300, "session_idle": 1800,
              "private_only": True, "state_dir": "", "advertise_host": ""},
    # 语音：enabled 控制浏览器 Web Speech；listener 控制电视 ASR 旁听
    "voice": {
        "enabled": False,
        # V4 phase 1/2 is opt-in; legacy routing remains available.
        "intelligence": {"enabled": False, "llm_timeout_seconds": 4,
                         "confidence_threshold": 0.8, "native_settle_seconds": 0.25},
        # V4.2 自然语言助理循环：只读工具 + 多步决策 + 事实对齐。
        # remember_preferences 默认关闭，需显式授权才持久化偏好（不含对话原文）。
        "assistant": {"enabled": False, "max_steps": 4, "budget_seconds": 14,
                      "remember_preferences": False, "preferences_path": ""},
        "conversation": {"ttl_seconds": 180, "max_turns": 8,
                         "print_replies": False, "persist_agent_history": False,
                         # 共享会话文件目录；留空=单进程内存会话
                         "shared_dir": ""},
        # 第三阶段：语音回执。默认关闭；电视端音频实测不可用，出声走浏览器/手机。
        "tts": {"enabled": False, "reply_tts": False,
                "binary": "", "model": "", "cache_dir": "",
                "timeout_seconds": 15, "cache_files": 64,
                "ack_on_start": False, "speak_during_playback": False,
                "include_url": True, "audio_base_url": "",
                # Piper 每次重新加载模型（实测约 9 秒），默认放后台合成。
                "async": True},
        "listener": {
            "enabled": False,
            "dedup_seconds": 8,
            # 比这更旧的识别结果视为过期（同时用于忽略 logcat 历史重放）
            "stale_seconds": 10,
            "backoff_min": 2,
            "backoff_max": 30,
            # 有界执行队列：语音指令排队执行，不阻塞 logcat 读取
            "queue_size": 4,
            # 持久化去重状态（只存哈希，不存语音原文）
            "state_file": "",
            # 白名单/黑名单留空时使用 adbtv/voice.py 的内置默认表
            "allow": [],
            "deny": [],
            "prefix_only": [],
            "log_full_text": False,     # 默认不把语音原文写进终端/日志
            "reply_tts": False,         # 预留：语音回执（未实现）
        },
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8770,
        "token": "",
    },
    "apps": {
        # 常用应用：别名 -> 包名（真实包名以 tv_list_apps 探测为准）
        "bilibili": {"package": "com.xiaodianshi.tv.yst", "label": "哔哩哔哩TV/云视听小电视"},
        "bilibili_hd": {"package": "tv.danmaku.bilianime", "label": "哔哩哔哩TV(新版)"},
        "iqiyi": {"package": "com.gitvdemo.video", "label": "银河奇异果(爱奇艺TV)"},
        "tencent": {"package": "com.ktcp.video", "label": "云视听极光(腾讯视频TV)"},
        "youku": {"package": "com.tudou.tvshadow", "label": "CIBN酷喵(优酷TV)"},
        "mgtv": {"package": "com.hunantv.market", "label": "芒果TV"},
        "douyin_tv": {"package": "com.ss.android.ugc.aweme.tv", "label": "抖音TV(鲜时光)"},
        # 飞牛TV：smart_navigation=true 时由 FeiniuPlayer 按页面导航；
        # 下面的 flow 仅作为 smart_navigation=false 时的旧版兜底路径。
        "feiniu_tv": {"package": "com.trim.tv", "label": "飞牛TV",
                      "flow": [{"action": "launch"}, {"action": "sleep", "seconds": 5},
                               {"action": "key", "key": "down"}, {"action": "key", "key": "center"},
                               {"action": "wait_play", "timeout": 18}]},
        "feiniu_drama": {"package": "com.trim.tv", "label": "飞牛TV短剧",
                         "flow": [{"action": "launch"}, {"action": "sleep", "seconds": 5},
                                  {"action": "key", "key": "right"},
                                  {"action": "key", "key": "center"},
                                  {"action": "sleep", "seconds": 4},
                                  {"action": "key", "key": "center"},
                                  {"action": "sleep", "seconds": 4},
                                  {"action": "key", "key": "center"},
                                  {"action": "wait_play", "timeout": 18}]},
        "hongguo": {"package": "cn.hongguo.tv", "label": "红果免费短剧"},
    },
}

_ENV_MAP = {
    "AI_TV_ADB_HOST": ("adb", "host"),
    "AI_TV_ADB_PORT": ("adb", "port"),
    "AI_TV_ADB_SERIAL": ("adb", "serial"),
    "AI_TV_ADB_BINARY": ("adb", "binary"),
    "AI_TV_LLM_PROVIDER": ("llm", "provider"),
    "AI_TV_LLM_BASE_URL": ("llm", "base_url"),
    "AI_TV_LLM_API_KEY": ("llm", "api_key"),
    "AI_TV_LLM_MODEL": ("llm", "model"),
    "AI_TV_LLM_VISION_MODEL": ("llm", "vision_model"),
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("AI_TV_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    proj = Path(__file__).resolve().parent.parent
    paths.append(proj / "config.yaml")
    paths.append(Path("~/.config/ai-tv-butler/config.yaml").expanduser())
    return paths


def load_config(path: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    explicit = path or os.environ.get("AI_TV_CONFIG")
    candidates = [Path(explicit).expanduser()] if explicit else _candidate_paths()
    if explicit and not candidates[0].is_file():
        raise FileNotFoundError(f"配置文件不存在: {candidates[0]}")
    used = None
    for p in candidates:
        if p and p.is_file():
            if yaml is None:
                raise RuntimeError("需要 PyYAML 才能读取 yaml 配置，请 pip install pyyaml")
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError("配置文件顶层必须是 mapping")
            used = str(p.resolve())
            break
    cfg = _deep_merge(DEFAULTS, data)
    cfg["_source"] = used or "<defaults>"

    # 环境变量覆盖
    for env, (sec, key) in _ENV_MAP.items():
        if env in os.environ and os.environ[env] != "":
            val: Any = os.environ[env]
            if key == "port":
                val = int(val)
            cfg[sec][key] = val

    # 推导 serial
    adb = cfg["adb"]
    if not adb.get("serial") and adb.get("host"):
        adb["serial"] = f"{adb['host']}:{adb['port']}"

    _resolve_paths(cfg, data)
    return cfg


def _resolve_paths(cfg: dict[str, Any], supplied: dict[str, Any]) -> None:
    """Resolve paths without creating files or requiring a writable package tree.

    Explicit per-feature paths win over defaults. Relative feature paths retain
    project-relative semantics for local source installs, and are data-relative
    with an external data directory. A relative data_dir is project-relative.
    """
    root = project_root()
    configured = (os.environ.get("AI_TV_DATA_DIR") or
                  cfg.get("paths", {}).get("data_dir") or cfg.get("data_dir"))
    source_install = (root / "pyproject.toml").is_file()
    writable = bool(root.stat().st_mode & 0o222) and os.access(root, os.W_OK)
    legacy = not configured and source_install and writable
    if configured:
        base = Path(configured).expanduser()
        if not base.is_absolute():
            base = root / base
    elif adb_state := cfg.get("adb", {}).get("fake_state"):
        base = Path(adb_state).expanduser().resolve().parent
        legacy = False
    elif legacy:
        base = root / "state"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "ai-tv-butler"
    base = base.resolve()
    cfg.setdefault("paths", {})["data_dir"] = str(base)
    cfg["data_dir"] = str(base)

    def resolve(value: str) -> str:
        p = Path(value).expanduser()
        return str((p if p.is_absolute() else (root if legacy else base) / p).resolve())

    runtime = cfg.get("execution", {}).get("state_dir")
    runtime = resolve(runtime) if runtime else str(base / "runtime")
    cfg.setdefault("execution", {})["state_dir"] = runtime
    defaults = {
        ("agent", "log_dir"): root / "logs" if legacy else base / "logs",
        ("scheduler", "store"): base / "tasks.json",
        ("memory", "path"): Path(runtime).parent / "memory.sqlite3",
        ("media", "cache"): Path(runtime).parent / "media_index.json",
        ("server", "token_file"): Path(runtime) / "console.token",
        ("adb", "key_home"): Path.home() / ".android" if legacy else base / ".android",
    }
    for (section, key), default in defaults.items():
        explicit = supplied.get(section, {}).get(key)
        cfg.setdefault(section, {})[key] = resolve(explicit) if explicit else str(default)
    if cfg.get("adb", {}).get("fake_state"):
        cfg["adb"]["fake_state"] = str(Path(cfg["adb"]["fake_state"]).expanduser().resolve())


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent
