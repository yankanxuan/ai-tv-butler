"""应用包名相关：别名解析、已安装列表、启动 Activity 解析。"""
from __future__ import annotations

import re
from typing import Any

from .adb import AdbClient

# 常见中文名/别名 -> 候选包名（按优先级）
KNOWN_PACKAGES: dict[str, list[str]] = {
    "哔哩哔哩": ["com.xiaodianshi.tv.yst", "tv.danmaku.bilianime", "com.bilibili.tv"],
    "bilibili": ["com.xiaodianshi.tv.yst", "tv.danmaku.bilianime", "com.bilibili.tv"],
    "b站": ["com.xiaodianshi.tv.yst", "tv.danmaku.bilianime"],
    "云视听小电视": ["com.xiaodianshi.tv.yst"],
    "爱奇艺": ["com.gitvdemo.video", "com.qiyi.video"],
    "奇异果": ["com.gitvdemo.video"],
    "腾讯视频": ["com.ktcp.video"],
    "云视听极光": ["com.ktcp.video"],
    "优酷": ["com.tudou.tvshadow", "com.youku.tv"],
    "酷喵": ["com.tudou.tvshadow"],
    "芒果": ["com.hunantv.market", "com.hunantv.www"],
    "芒果tv": ["com.hunantv.market", "com.hunantv.www"],
    "抖音": ["com.ss.android.ugc.aweme.tv", "com.ss.android.ugc.aweme"],
    "鲜时光": ["com.ss.android.ugc.aweme.tv"],
    "央视频": ["com.cctv.ott"],
    "netflix": ["com.netflix.ninja"],
    "youtube": ["com.google.android.youtube.tv"],
    "飞牛": ["com.trim.tv"],
    "飞牛tv": ["com.trim.tv"],
    "飞牛影视": ["com.trim.tv"],
    "红果": ["cn.hongguo.tv"],
    "红果短剧": ["cn.hongguo.tv"],
}


def list_packages(adb: AdbClient) -> dict[str, list[str]]:
    """返回 {'third': [...], 'system': [...], 'all': [...]}。"""
    third = [l.split("package:", 1)[1].strip()
             for l in adb.shell(["pm", "list", "packages", "-3"]).stdout.splitlines()
             if l.startswith("package:")]
    system = [l.split("package:", 1)[1].strip()
              for l in adb.shell(["pm", "list", "packages", "-s"]).stdout.splitlines()
              if l.startswith("package:")]
    return {"third": sorted(third), "system": sorted(system),
            "all": sorted(set(third) | set(system))}


def resolve_package(adb: AdbClient, name: str, cfg: dict[str, Any]) -> str:
    """把用户说的应用名解析成已安装的包名。找不到抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("应用名为空")
    # 1) 直接就是包名且已安装
    installed = list_packages(adb)
    allp = set(installed["all"])
    if name in allp:
        return name
    # 2) config.apps 别名
    aliases = cfg.get("apps", {})
    key = name.lower()
    if key in aliases and aliases[key].get("package") in allp:
        return aliases[key]["package"]
    # 2b) config.apps 的 label / 别名值匹配（如 "飞牛TV" -> com.trim.tv）
    for _k, v in aliases.items():
        if not isinstance(v, dict):
            continue
        label = str(v.get("label", ""))
        pkg = v.get("package")
        if pkg in allp and label and (name == label or name in label or label in name):
            return pkg
    # 3) 内置已知表
    candidates = KNOWN_PACKAGES.get(key) or KNOWN_PACKAGES.get(name)
    if candidates:
        for p in candidates:
            if p in allp:
                return p
    # 4) 包名包含匹配（按名字做模糊）
    lowered = name.lower()
    for p in installed["third"]:
        if lowered in p.lower():
            return p
    raise ValueError(
        f"没找到应用「{name}」对应的已安装包。已安装第三方应用：{', '.join(installed['third']) or '(空)'}"
    )


def resolve_launcher_activity(adb: AdbClient, package: str) -> str | None:
    """解析应用的启动 Activity，返回 'pkg/Activity' 或 None。"""
    r = adb.shell(["cmd", "package", "resolve-activity", "--brief",
                   "-c", "android.intent.category.LAUNCHER", package])
    txt = (r.stdout + "\n" + r.stderr).strip()
    # 典型输出：
    # priority=0 ...\n name=com.xx/.MainActivity  或  com.xx/com.xx.MainActivity
    m = re.search(r"([\w.]+)/([\w.$]+)", txt)
    if m:
        pkg, act = m.group(1), m.group(2)
        if not act.startswith("."):
            act = act
        elif not act.startswith(pkg):
            act = "." + act.lstrip(".")
        return f"{pkg}/{act}"
    # 退路：dumpsys package
    r2 = adb.shell(["dumpsys", "package", package])
    m2 = re.search(r"android\.intent\.action\.MAIN.*?\n.*?([\w.]+)/([\w.$]+)", r2.stdout, re.S)
    if m2:
        return f"{m2.group(1)}/{m2.group(2)}"
    return None
