"""Page-aware Feiniu navigation without fixed startup sleeps or unconditional relaunches."""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from .intent import Intent

PACKAGE = "com.trim.tv"


def normalized(text: str) -> str:
    return re.sub(r"[\s《》，,：:·。]", "", text).lower()


class FeiniuPlayer:
    def __init__(self, device, cfg):
        self.dev = device
        self.cfg = cfg
        self.log: list[dict[str, Any]] = []
        self.timings: dict[str, float] = {}
        # Content identity proven by the labels actually clicked during
        # navigation. The Feiniu player cannot be dumped while rendering, so this
        # is the authoritative evidence for "which episode is playing".
        self.confirmed: dict[str, Any] = {}
        self.timeout = float(cfg.get("ui", {}).get("wait_timeout", 20))
        self.poll = float(cfg.get("ui", {}).get("fast_poll_interval", 0.2))
        self.deadline = 0.0

    def measured(self, name: str, fn: Callable):
        start = time.monotonic()
        try:
            return fn()
        finally:
            self.timings[name] = self.timings.get(name, 0) + round(time.monotonic() - start, 3)

    def focus(self):
        return self.measured("state", self.dev.current_focus)

    def page(self, focus: str) -> str:
        if PACKAGE not in focus:
            return "other"
        for name, marker in (("player", "PlayerActivity"), ("file", "MovieDetailActivity"), ("season", "SeasonDetailActivity"),
                             ("detail", "EpisodeDetailActivity"), ("list", "MovieListActivity"),
                             ("home", "MainActivity")):
            if marker in focus:
                return name
        return "loading"

    def wait_page(self, pages: set[str]):
        end = min(self.deadline, time.monotonic() + self.timeout)
        while time.monotonic() < end:
            page = self.page(self.focus())
            if page in pages:
                return page
            time.sleep(self.poll)
        raise TimeoutError("等待飞牛页面超时")

    def back_to(self, pages: set[str]):
        # Player may consume the first BACK to hide its controls. Retry only
        # after observing that the foreground page has not changed.
        before = self.page(self.focus())
        for _ in range(3):
            self.dev.key("back")
            end = min(self.deadline, time.monotonic() + 0.7)
            while time.monotonic() < end:
                page = self.page(self.focus())
                if page in pages and page != before:
                    return page
                time.sleep(self.poll)
        raise RuntimeError("飞牛未退出当前页面，停止重复按键")

    def snapshot(self):
        return self.measured("ui", self.dev.elements)

    @staticmethod
    def actionable(element):
        return (element.focusable or element.clickable) and not any(
            kind in element.cls for kind in ("GridView", "ScrollView", "RecyclerView", "ViewPager"))

    @staticmethod
    def control(element, elements):
        """Map a label to its focusable card, retaining anonymous parent containers."""
        by_id = {e.node_id: e for e in elements if e.node_id}
        cur = element
        visited = set()
        while cur:
            if FeiniuPlayer.actionable(cur):
                return cur
            if not cur.parent_id or cur.parent_id in visited:
                break
            visited.add(cur.parent_id)
            cur = by_id.get(cur.parent_id)
        return element

    def activate(self, element, elements, page: str, *, settle: float = 1.2):
        """Select a card and confirm the page actually changed.

        A tap on Android TV normally moves focus; CENTER then activates. Dumping
        the tree after every tap costs seconds, so this verifies via the page
        (a cheap `dumpsys window` read) and only re-dumps when nothing happened.
        """
        target = self.control(element, elements)
        label = element.text or element.desc
        self.log.append({"step": "select", "text": label, "page": page})
        self._note_content(label, page)
        if target.focused:
            self.dev.key("center")
        else:
            self.dev.tap(target.x, target.y)
            if self.page(self.focus()) != page:
                return  # tap itself activated the card
            self.dev.key("center")
        # Cheap page check first; dump only if the screen did not change.
        end = min(self.deadline, time.monotonic() + settle)
        while time.monotonic() < end:
            if self.page(self.focus()) != page:
                return
            time.sleep(self.poll)
        fresh = self.snapshot()
        if self._page_of_elements(fresh) != page:
            return
        # Still on the same page: the target may need explicit focus movement.
        focused = next((e for e in fresh if e.focused), None)
        match = next((e for e in fresh if label and (e.text or e.desc) == label), None)
        if match is not None:
            target = self.control(match, fresh)
        if focused is not None and target is not None and \
                abs(target.x - focused.x) < 40 and abs(target.y - focused.y) < 100:
            self.dev.key("center")
            return
        raise RuntimeError("目标卡片未响应点击，停止盲目按键")

    def _note_content(self, label: str, page: str):
        """Record a verified title/season/episode taken from a clicked label."""
        import re as _re
        if not label:
            return
        episode = _re.search(r"[Ss](\d{1,3})[\s._-]*[Ee](\d{1,4})", label)
        if episode:
            self.confirmed.update(season=int(episode.group(1)),
                                  episode=int(episode.group(2)),
                                  episode_label=label, page=page)
            title = label.split(" S0")[0].split(" S1")[0].strip(" 0123456789.")
            title = _re.sub(r"^\d+[.、]\s*", "", title)
            if title:
                self.confirmed["title"] = title
            return
        only_episode = _re.fullmatch(r"第\s*(\d{1,4})\s*集", label.strip())
        if only_episode:
            self.confirmed.update(episode=int(only_episode.group(1)), page=page)
            return
        season_only = _re.fullmatch(r"第\s*(\d{1,3})\s*季", label.strip())
        if season_only:
            self.confirmed["season"] = int(season_only.group(1))
            return
        # A list card that exactly matches the requested show establishes title.
        if page in ("list", "home") and len(label) >= 2:
            self.confirmed.setdefault("list_label", label)

    @staticmethod
    def _page_of_elements(elements) -> str:
        """Best-effort page guess from element resource ids (no extra ADB read)."""
        for element in elements:
            rid = (element.resource_id or "").lower()
            if "splash" in rid:
                return "splash"
        return ""

    @staticmethod
    def _content_loading(els):
        """True while Feiniu shows skeleton/loading placeholders (content not ready)."""
        for e in els:
            rid = (e.resource_id or "").lower()
            cls = (e.cls or "").lower()
            if "skeleton" in rid or "skeleton" in cls or "loading" in rid:
                return True
        return False

    def wait_elements(self, predicate):
        end = min(self.deadline, time.monotonic() + self.timeout)
        while time.monotonic() < end:
            els = self.snapshot()
            found = next((e for e in els if predicate(e)), None)
            if found:
                return found, els
            # Content still loading (skeleton placeholders): grant the full
            # navigation budget instead of the short per-control timeout, so a
            # slow media-library load is not mistaken for a missing control.
            if self._content_loading(els):
                end = max(end, self.deadline)
            time.sleep(self.poll)
        raise TimeoutError("等待目标控件超时（飞牛内容可能仍在加载或媒体服务未响应）")

    # A card label may carry a season suffix and the show name may be spoken in
    # short form ("野神" for "穿成野神我靠香火斩诸神"), so matching accepts a
    # minimum-length prefix/containment rather than exact equality only.
    MIN_SHORT_TITLE = 2

    def title_matches(self, actual: str, intent: Intent):
        aliases = self.cfg.get("apps", {}).get("feiniu_tv", {}).get("title_aliases", {})
        title = aliases.get(intent.title, intent.title)
        if not title:
            return False
        want = normalized(title)
        have = normalized(actual)
        if not have:
            return False
        if intent.season not in (None, 1):
            forms = {normalized(f"{title}第{intent.season}季"),
                     normalized(f"{title}{intent.season}"),
                     normalized(f"{title}第{intent.season}部")}
            if have in forms:
                return True
        if have == want:
            return True
        # Short spoken name ("野神") may match the longer catalog title, but a
        # sequel/season marker makes it a DIFFERENT work ("山海归途" must not
        # match "山海归途续集"), so those are rejected explicitly.
        sequel = re.compile(r"^(?:续集|续|前传|外传|特别篇|剧场版|番外|第|\d|[一二三四五六七八九十]+(?:季|部))")
        if len(want) >= 3 and have.startswith(want):
            return not sequel.match(have[len(want):])
        if len(have) >= 3 and want.startswith(have):
            return not sequel.match(want[len(have):])
        return False

    def find_title(self, intent: Intent):
        """Scan the visible grid, scrolling a whole page at a time.

        Stepping one card per dump is far too slow on a large library, so each
        iteration scrolls roughly one screen and re-reads once.
        """
        seen = set()
        max_pages = int(self.cfg.get("ui", {}).get("max_search_pages", 12))
        for page_no in range(max_pages):
            els = self.snapshot()
            target = next((e for e in els if e.text and self.title_matches(e.text, intent)), None)
            if target:
                self.confirmed.setdefault("title", target.text)
                self.confirmed["list_label"] = target.text
                self.activate(target, els, "list")
                return
            signature = tuple((e.text, e.bounds) for e in els if e.text)
            if signature in seen:
                break
            seen.add(signature)
            if time.monotonic() >= self.deadline:
                break
            self._scroll_page(els)
        raise RuntimeError(f"当前短剧库未找到「{intent.title}」，没有改播其他内容")

    def _scroll_page(self, elements):
        """Scroll about one screen down using a swipe over the grid area."""
        grid = [e for e in elements if e.text and e.y > 150]
        if grid and callable(getattr(self.dev, "swipe", None)):
            xs = [e.x for e in grid]
            ys = [e.y for e in grid]
            x = sorted(xs)[len(xs) // 2]
            top = max(200, min(ys))
            self.dev.swipe(x, min(950, top + 300), x, max(200, top - 300), 260)
        else:
            for _ in range(4):
                self.dev.key("down")
        # No fixed sleep: the next snapshot's own device read provides the
        # settling delay, and the caller re-checks the deadline.

    def run(self, intent: Intent):
        start = time.monotonic()
        self.deadline = start + float(self.cfg.get("ui", {}).get("navigation_timeout", 60))
        try:
            page = self.page(self.focus())
            self.log.append({"step": "current_page", "page": page})
            if page == "other":
                result = self.measured("launch", lambda: self.dev.launch_app(PACKAGE))
                if not result.get("ok"):
                    raise RuntimeError("飞牛启动失败")
                page = self.wait_page({"home", "list", "detail", "season", "file", "player"})
            if intent.action == "open":
                return self.result(True, "已打开飞牛TV", start)
            # A player resumed from background may be stopped (track released).
            # Confirm real playback before treating it as a successful resume.
            if page == "player" and not intent.title:
                st = self.measured("verify", lambda: self.dev.playback_status(
                    include_ui=False, expected_package=PACKAGE))
                if st.get("playing"):
                    return self.result(True, "飞牛TV正在播放", start, st)
                if intent.drama:
                    # Wanted a fresh drama but landed on a stopped player:
                    # back out and navigate the 短剧 library normally.
                    page = self.back_to({"home", "list", "detail", "season", "file"})
                else:
                    # Explicit resume: Feiniu toggles with DPAD center, not the
                    # media-play key (which it ignores). Verify after.
                    self.dev.key("center")
                    return self.verify(start)
            elif page == "player" and intent.title:
                # BACK is deterministic and avoids toggling playback to expose controls.
                page = self.back_to({"detail", "season", "file", "list", "home"})
            if page == "loading":
                page = self.wait_page({"home", "list", "detail", "season", "file", "player"})
            if intent.title and page in ("detail", "season", "file"):
                els = self.snapshot()
                header = next((e.text for e in els if e.text and self.title_matches(e.text, intent)), None)
                if header:
                    self.confirmed.setdefault("title", header)
                aliases = self.cfg.get("apps", {}).get("feiniu_tv", {}).get("title_aliases", {})
                requested = normalized(aliases.get(intent.title, intent.title))
                same = any(e.text and (self.title_matches(e.text, intent) or (
                    page in ("season", "file") and requested in normalized(e.text) and
                    re.search(rf"S0*{intent.season or 1}E\d+", e.text, re.IGNORECASE))) for e in els)
                if not same:
                    for _ in range(4):
                        page = self.back_to({"home", "list", "detail", "season", "file"})
                        if page in ("home", "list"):
                            break
                    if page not in ("home", "list"):
                        raise RuntimeError("无法返回短剧库")
            # Generic resume can use the current detail directly.
            if page == "home":
                if intent.drama or intent.title:
                    target, els = self.wait_elements(lambda e: e.text == "短剧")
                    self.activate(target, els, "home")
                    page = self.wait_page({"list"})
                else:
                    label, els = self.wait_elements(lambda e: e.text == "继续观看")
                    cards = [e for e in els if self.actionable(e)
                             and e.y > label.y and e.x > 80]
                    cards.sort(key=lambda e: (e.y, e.x))
                    if not cards:
                        raise RuntimeError("继续观看列表为空")
                    self.activate(cards[0], els, "home")
                    page = self.wait_page({"player", "detail"})
            if page == "list":
                if intent.title:
                    self.find_title(intent)
                else:
                    target, els = self.wait_elements(lambda e: e.focused and e.focusable and e.y > 150)
                    self.activate(target, els, "list")
                page = self.wait_page({"detail", "player"})
            if page == "detail":
                # Explicit episode selection enters season view; otherwise resume detail button.
                if intent.episode is not None or intent.season is not None:
                    season = intent.season or 1
                    target, els = self.wait_elements(lambda e: e.text == f"第 {season} 季")
                    self.activate(target, els, "detail")
                    page = self.wait_page({"season"})
                else:
                    target, els = self.wait_elements(lambda e: bool(re.search(r"第\s*\d+\s*季\s*第\s*\d+\s*集", e.text)))
                    self.activate(target, els, "detail")
                    page = self.wait_page({"player"})
            if page == "season":
                episode = intent.episode or 1
                # Episode labels load asynchronously. Wait for identity, rather
                # than selecting an anonymous grid or sorting zoomed card centers.
                token = rf"S0*{intent.season or 1}E0*{episode}(?!\d)"
                def is_episode(e):
                    return bool(re.fullmatch(rf"第\s*{episode}\s*集", e.text)
                                or re.search(token, e.text, re.IGNORECASE))
                # The episode row starts below the viewport on this TV. Move
                # into it before waiting for labels which are not rendered yet.
                target = None
                for _ in range(3):
                    els = self.snapshot()
                    target = next((e for e in els if is_episode(e)), None)
                    if target:
                        break
                    self.dev.key("down")
                if target is None:
                    target, els = self.wait_elements(is_episode)
                self.activate(target, els, "season")
                page = self.wait_page({"player", "file"})
            if page == "file":
                els = self.snapshot()
                if intent.title:
                    aliases = self.cfg.get("apps", {}).get("feiniu_tv", {}).get("title_aliases", {})
                    requested = normalized(aliases.get(intent.title, intent.title))
                    if not any(requested in normalized(e.text) for e in els):
                        raise RuntimeError("文件详情片名与请求不符，未播放")
                    if intent.episode is not None:
                        token = rf"S0*{intent.season or 1}E0*{intent.episode}(?!\d)"
                        if not any(re.search(token, e.text, re.IGNORECASE) for e in els):
                            raise RuntimeError("文件详情季集与请求不符，未播放")
                target = next((e for e in els if e.text == "播放"), None)
                if target is None:
                    target, els = self.wait_elements(lambda e: e.text == "播放")
                self.activate(target, els, "file")
                self.wait_page({"player"})
            return self.verify(start)
        except (RuntimeError, TimeoutError) as e:
            return self.result(False, str(e), start)

    def verify(self, start):
        end = min(self.deadline, time.monotonic() + self.timeout)
        status = {}
        while time.monotonic() < end:
            status = self.measured("verify", lambda: self.dev.playback_status(
                include_ui=False, expected_package=PACKAGE))
            if status.get("playing"):
                self.log.append({"step": "verify_playback", "playing": True,
                                 "evidence": status.get("evidence")})
                return self.result(True, "飞牛TV已开始播放", start, status)
            time.sleep(self.poll)
        return self.result(False, "已进入播放器，暂未确认播放", start, status)

    def result(self, ok, summary, start, status=None):
        return {"ok": ok, "summary": summary, "log": self.log, "playback": status or {},
                "content_confirmed": dict(self.confirmed),
                "elapsed": round(time.monotonic() - start, 3),
                "timings": {k: round(v, 3) for k, v in self.timings.items()}}
