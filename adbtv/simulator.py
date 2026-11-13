#!/usr/bin/env python3
"""内置模拟电视：模拟 `adb` 命令行行为，供离线演示 / 测试使用。

用法（由 adbtv 自动调用，也可手动）：
    FAKE_ADB_STATE=/path/state.json python3 -m adbtv.simulator -s fake-tv:5555 shell getprop ro.product.model

它维护一个 JSON 状态文件，模拟：桌面 -> 搜索 -> 结果列表 -> 播放器 的界面流转，
使 AI 电视管家可以在没有真机的情况下跑通完整链路。
"""
import base64
import json
import os
import shlex
import sys
from xml.sax.saxutils import escape

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

APPS = {
    "com.xiaodianshi.tv.yst": ("哔哩哔哩TV", "云视听小电视"),
    "com.gitvdemo.video": ("银河奇异果", "爱奇艺"),
    "com.ktcp.video": ("云视听极光", "腾讯视频"),
    "com.tudou.tvshadow": ("CIBN酷喵", "优酷"),
    "com.hunantv.market": ("芒果TV", "芒果"),
    "com.ss.android.ugc.aweme.tv": ("鲜时光", "抖音"),
    "com.trim.tv": ("飞牛TV", "飞牛TV"),
    "cn.hongguo.tv": ("红果免费短剧", "红果"),
}
SYSTEM_PKGS = ["android", "com.android.systemui", "com.android.settings",
               "com.android.tv.settings", "com.google.android.leanbacklauncher",
               "com.gathin.tvhome"]


def state_path():
    from pathlib import Path
    base = Path(os.environ.get("AI_TV_DATA_DIR") or
                str(Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "ai-tv-butler"))
    return os.environ.get("FAKE_ADB_STATE") or str(base / "fake_tv_state.json")


def load_state():
    p = state_path()
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "serial": "fake-tv:5555",
        "model": "Xiaomi TV S Pro (模拟)",
        "brand": "Xiaomi",
        "sdk": "30",
        "android": "11",
        "screen": "home",
        "app": "com.xiaodianshi.tv.yst",
        "query": "",
        "playing": False,
        "title": "",
        "last_xml": "",
        "default_input_method": "com.sohu.inputmethod.sogou.tv/.SogouIME",
        "volume": 7,
    }


def save_state(st):
    p = state_path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def app_title(pkg):
    return APPS.get(pkg, ("应用", "应用"))[0]


def build_xml(st):
    screen = st["screen"]
    app = st["app"]
    title = app_title(app)
    nodes = []

    def node(text="", desc="", rid="", cls="android.widget.FrameLayout",
             clickable=False, bounds="[0,0][1920,1080]"):
        text, desc, rid = (escape(value, {'"': '&quot;'}) for value in (text, desc, rid))
        nodes.append(
            f'<node text="{text}" content-desc="{desc}" resource-id="{rid}" '
            f'class="{cls}" clickable="{str(clickable).lower()}" bounds="{bounds}" />'
        )

    if screen == "home":
        node(text=title, cls="android.widget.TextView", bounds="[80,40][900,140]")
        node(desc="搜索", rid="com.app:id/search", cls="android.widget.ImageView",
             clickable=True, bounds="[1560,80][1720,200]")
        node(text="热门推荐", cls="android.widget.TextView", bounds="[80,300][400,360]")
        node(text="今日必看 精选合集", rid="com.app:id/card1", cls="android.widget.LinearLayout",
             clickable=True, bounds="[80,420][600,760]")
        node(text="热门综艺 第1期", rid="com.app:id/card2", cls="android.widget.LinearLayout",
             clickable=True, bounds="[660,420][1180,760]")
    elif screen == "search":
        node(text=st.get("query", ""), desc="搜索视频", rid="com.app:id/search_box", cls="android.widget.EditText",
             clickable=True, bounds="[300,120][1520,240]")
        node(text="取消", cls="android.widget.Button", clickable=True, bounds="[1560,120][1780,240]")
        node(text="搜索历史：科技 数码 电影", cls="android.widget.TextView", bounds="[300,320][1200,380]")
    elif screen == "results":
        node(text=st["query"], rid="com.app:id/search_box", cls="android.widget.EditText",
             clickable=True, bounds="[300,120][1520,240]")
        node(text="取消", cls="android.widget.Button", clickable=True, bounds="[1560,120][1780,240]")
        items = [f"{st['query']}博览 第1集", f"{st['query']}前沿 第2集", f"{st['query']}测评 第3集"]
        y = 300
        for i, it in enumerate(items, 1):
            node(text=it, rid=f"com.app:id/result_{i}", cls="android.widget.LinearLayout",
                 clickable=True, bounds=f"[80,{y}][1840,{y+140}]")
            y += 160
    elif screen == "player":
        node(text=f"正在播放：{st['title']}", cls="android.widget.TextView", bounds="[80,60][1400,160]")
        node(text="暂停", desc="暂停", rid="com.app:id/play_pause", cls="android.widget.ImageView",
             clickable=True, bounds="[900,900][1020,1020]")
        node(text="进度 00:12 / 10:00", cls="android.widget.TextView", bounds="[80,980][900,1030]")

    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<hierarchy rotation="0"><node class="android.widget.FrameLayout" bounds="[0,0][1920,1080]" '
           'package="' + app + '">' + "".join(nodes) + "</node></hierarchy>")
    return xml


def handle_shell(st, args):
    if not args:
        return ""
    sub = args[0]

    # 电源状态：KEYCODE_POWER(26) 在睡眠时唤醒、清醒时睡眠（与真机一致）
    if args[:2] == ["dumpsys", "power"]:
        asleep = bool(st.get("asleep"))
        wake = "Asleep" if asleep else "Awake"
        screen = "OFF" if asleep else "ON"
        return ("mWakefulness=" + wake + "\n"
                "mWakefulnessChanging=false\n"
                "Display Power: state=" + screen)

    if args[:4] == ["settings", "get", "secure", "default_input_method"]:
        return st.get("default_input_method", "com.sohu.inputmethod.sogou.tv/.SogouIME")
    if args[:2] == ["ime", "list"]:
        return "com.sohu.inputmethod.sogou.tv/.SogouIME\ncom.android.adbkeyboard/.AdbIME"
    if args[:2] == ["ime", "enable"]:
        return "Input method enabled"
    if args[:2] == ["ime", "set"] and len(args) == 3:
        st["default_input_method"] = args[2]
        save_state(st)
        return "Input method selected"
    if args[:2] == ["media", "volume"]:
        return f"[v] volume is {st.get('volume', 7)} in range [0..15]"

    # getprop
    if sub == "getprop":
        key = args[1] if len(args) > 1 else ""
        return {
            "ro.product.model": st["model"],
            "ro.product.brand": st["brand"],
            "ro.product.device": "fake",
            "ro.build.version.sdk": st["sdk"],
            "ro.build.version.release": st["android"],
        }.get(key, "")
    if sub == "wm" and len(args) > 1 and args[1] == "size":
        return "Physical size: 1920x1080"

    # 输入
    if sub == "input":
        if len(args) >= 2 and args[1] == "keyevent":
            key = args[2] if len(args) > 2 else ""
            if key in ("26", "KEYCODE_POWER"):
                st["asleep"] = not bool(st.get("asleep"))
                save_state(st)
                return ""
            return handle_key(st, key)
        if len(args) >= 4 and args[1] == "tap":
            return handle_tap(st, int(args[2]), int(args[3]))
        if len(args) >= 2 and args[1] == "text":
            txt = args[2].replace("%s", " ") if len(args) > 2 else ""
            st["query"] = txt
            st["screen"] = "results"
            save_state(st)
            return ""

    # UI dump
    if sub == "uiautomator" and len(args) >= 3 and args[1] == "dump":
        xml = build_xml(st)
        st["last_xml"] = xml
        save_state(st)
        return "UI hierchary dumped to: " + args[2]
    if sub == "cat" and len(args) > 1:
        return st.get("last_xml", "")

    # dumpsys
    if sub == "dumpsys":
        what = args[1] if len(args) > 1 else ""
        if what == "window":
            comp = {
                "home": f"{st['app']}/.MainActivity",
                "search": f"{st['app']}/.SearchActivity",
                "results": f"{st['app']}/.SearchResultActivity",
                "player": f"{st['app']}/.PlayerActivity",
            }[st["screen"]]
            return ("  mCurrentFocus=Window{abc u0 " + comp + "}\n"
                    "  mFocusedApp=AppWindowToken{def token=Token{ghi ActivityRecord{xyz "
                    + comp + "}}}")
        if what == "media_session":
            if st.get("playing"):
                return ("Media session Service: SessionRecord{state=PlaybackState "
                        "{state=3, position=12000, speed=1.0}}\n"
                        "active=true")
            return "Media session Service: SessionRecord{state=PlaybackState {state=2}}\nactive=false"
        if what == "package":
            pkg = args[2] if len(args) > 2 else st["app"]
            return (f"Package [{pkg}] (abc):\n  android.intent.action.MAIN:\n"
                    f"    {pkg}/.MainActivity filter x\n")
        return ""

    # 包管理
    if sub == "pm" and len(args) >= 3 and args[1] == "list" and args[2] == "packages":
        if "-3" in args:
            return "\n".join("package:" + p for p in APPS)
        return "\n".join("package:" + p for p in SYSTEM_PKGS)
    if sub == "cmd" and len(args) >= 2 and args[1] == "package":
        # cmd package resolve-activity --brief [-a MAIN -c HOME] [pkg]
        if "android.intent.category.HOME" in args:
            return "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1\n  name=com.gathin.tvhome/.MainActivity"
        pkg = args[-1]
        return f"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1\n  name={pkg}/.MainActivity"

    # 启动应用
    if sub == "am":
        if len(args) >= 2 and args[1] == "start":
            pkg = ""
            for i, a in enumerate(args):
                if a == "-n" and i + 1 < len(args):
                    pkg = args[i + 1].split("/")[0]
            st["app"] = pkg or st["app"]
            st["screen"] = "home"
            st["query"] = ""
            st["playing"] = False
            save_state(st)
            return "Starting: Intent { cmp=" + (pkg or st["app"]) + "/.MainActivity }"
        if len(args) >= 2 and args[1] == "broadcast":
            # ADB_INPUT_B64 --es msg <b64>
            if ("--es" in args and "msg" in args and
                    st.get("default_input_method") in ("com.android.adbkeyboard/.AdbIME",
                                                       "com.android.adbkeyboard/com.android.adbkeyboard.AdbIME")):
                idx = args.index("msg")
                b64 = args[idx + 1] if idx + 1 < len(args) else ""
                try:
                    txt = base64.b64decode(b64).decode("utf-8")
                except Exception:
                    txt = ""
                st["query"] = txt
                st["screen"] = "results"
                save_state(st)
            return "Broadcast completed: result=0"
    if sub == "monkey":
        pkg = ""
        if "-p" in args:
            pkg = args[args.index("-p") + 1]
        st["app"] = pkg or st["app"]
        st["screen"] = "home"
        st["playing"] = False
        save_state(st)
        return "Events injected: 1"
    return ""


def handle_key(st, code):
    code = code.upper()
    if code in ("KEYCODE_HOME",):
        st["screen"] = "home"; st["playing"] = False
        st["app"] = "com.gathin.tvhome"
    elif code == "KEYCODE_BACK":
        if st["screen"] == "player":
            st["screen"] = "results"; st["playing"] = False
        elif st["screen"] == "results":
            st["screen"] = "search"
        elif st["screen"] == "search":
            st["screen"] = "home"
    elif code in ("KEYCODE_MEDIA_PLAY", "KEYCODE_MEDIA_PLAY_PAUSE"):
        if st["screen"] == "player":
            st["playing"] = True
    elif code == "KEYCODE_MEDIA_PAUSE":
        st["playing"] = False
    elif code == "KEYCODE_VOLUME_UP":
        st["volume"] = min(15, st.get("volume", 7) + 1)
    elif code == "KEYCODE_VOLUME_DOWN":
        st["volume"] = max(0, st.get("volume", 7) - 1)
    elif code == "KEYCODE_SEARCH":
        st["screen"] = "search"; st["playing"] = False
    save_state(st)
    return ""


def handle_tap(st, x, y):
    screen = st["screen"]
    if screen == "home":
        if x >= 1400 and y <= 260:
            st["screen"] = "search"
        elif 80 <= x <= 1840 and 400 <= y <= 780:
            # 点击推荐卡片直接播放
            st["title"] = "热门推荐 精选合集"
            st["screen"] = "player"; st["playing"] = True
    elif screen == "search":
        if x >= 1540 and y <= 260:
            st["screen"] = "home"
        # 点输入框：保持
    elif screen == "results":
        if y <= 260:
            pass  # 搜索框区域
        else:
            idx = 1 if y < 460 else (2 if y < 620 else 3)
            st["title"] = f"{st['query']}博览 第{idx}集" if idx == 1 else f"{st['query']}前沿 第{idx}集"
            st["screen"] = "player"; st["playing"] = True
    elif screen == "player":
        st["playing"] = not st.get("playing", False)
    save_state(st)
    return ""


class InProcessSimulator:
    """Same adb semantics as the CLI entry, but without spawning a process.

    Tests use this to avoid paying a Python start-up (~40-80 ms) per device
    command; the on-disk CLI path stays available for manual demos and for
    validating that quoting/exit codes behave like real adb.
    """

    def __init__(self, state_path: str | None = None):
        self._state_path = state_path
        self._state = None

    # ---------- 状态 ----------
    def _load(self):
        if self._state is None:
            self._state = load_state()
        return self._state

    def reset(self) -> None:
        self._state = None
        path = self._state_path or state_path()
        try:
            os.unlink(path)
        except OSError:
            pass

    @property
    def state(self):
        return self._load()

    # ---------- 调用 ----------
    def invoke(self, argv: list[str]) -> tuple[int, bytes]:
        """Return (returncode, stdout bytes) for an adb argv (without `adb`)."""
        # handle_shell() persists through module-level save_state(), which reads
        # FAKE_ADB_STATE; scope it to this instance's path for the call.
        previous = os.environ.get("FAKE_ADB_STATE")
        if self._state_path:
            os.environ["FAKE_ADB_STATE"] = str(self._state_path)
        try:
            return self._invoke(argv)
        finally:
            if self._state_path:
                if previous is None:
                    os.environ.pop("FAKE_ADB_STATE", None)
                else:
                    os.environ["FAKE_ADB_STATE"] = previous

    def _invoke(self, argv: list[str]) -> tuple[int, bytes]:
        st = self._load()
        argv = list(argv)
        if "connect" in argv:
            index = argv.index("connect")
            target = argv[index + 1] if index + 1 < len(argv) else st["serial"]
            return 0, f"connected to {target}\n".encode()
        if "devices" in argv:
            return 0, f"List of devices attached\n{st['serial']}\tdevice\n\n".encode()
        if "exec-out" in argv:
            return 0, PNG_1x1
        if "shell" in argv:
            index = argv.index("shell")
            args = argv[index + 1:]
            if len(args) == 1:
                try:
                    args = shlex.split(args[0])
                except ValueError as exc:
                    return 2, f"invalid shell quoting: {exc}\n".encode()
            out = handle_shell(st, args)
            return 0, (out + "\n").encode() if out else b""
        return 0, b""

    # ---------- 写盘 ----------
    def flush(self) -> None:
        if self._state is not None:
            save_state(self._state)


def main():
    argv = sys.argv[1:]
    st = load_state()
    if "connect" in argv:
        target = argv[argv.index("connect") + 1] if argv.index("connect") + 1 < len(argv) else st["serial"]
        print(f"connected to {target}")
        return 0
    if "devices" in argv:
        print("List of devices attached")
        print(f"{st['serial']}\tdevice")
        return 0
    if "exec-out" in argv:
        # screencap
        sys.stdout.buffer.write(PNG_1x1)
        return 0
    if "shell" in argv:
        idx = argv.index("shell")
        args = argv[idx + 1:]
        if len(args) == 1:
            # Parse quoting only: never run a shell, expand variables, or execute
            # operators/substitutions present in untrusted text arguments.
            try:
                args = shlex.split(args[0])
            except ValueError as exc:
                print(f"invalid shell quoting: {exc}", file=sys.stderr)
                return 2
        out = handle_shell(st, args)
        if out:
            print(out)
        return 0
    # 兜底
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
