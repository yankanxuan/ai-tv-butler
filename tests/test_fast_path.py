#!/usr/bin/env python3
"""Fast-path regression tests; no ADB, television, network, or real sleeps.

Run with ``python3 -m unittest discover -s tests -p test_fast_path.py -v``.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adbtv.agent import Agent, parse_instruction
from adbtv.adb import CmdResult
from adbtv.device import TVDevice, UIElement
from adbtv.feiniu import FeiniuPlayer, PACKAGE
from adbtv.intent import Intent, parse_fast
from adbtv.tools import run_flow


CFG = {
    "agent": {"fast_path": True, "use_llm": True},
    "ui": {"wait_timeout": 0.03, "navigation_timeout": 0.05,
           "fast_poll_interval": 0.001, "max_search_pages": 3},
    "apps": {"feiniu_tv": {"package": PACKAGE}},
}


class MemoryDevice:
    """TV cards have anonymous focusable parents and non-clickable labels.

    A tap normally focuses a card; CENTER activates it. Optionally taps activate
    directly, as some TV apps do. Only a selected card can change the page.
    """

    activities = {"home": "MainActivity", "list": "MovieListActivity",
                  "detail": "EpisodeDetailActivity", "season": "SeasonDetailActivity",
                  "player": "PlayerActivity", "other": "Launcher"}

    def __init__(self, page="home", titles=None, current_title="旧片", tap_activates=False):
        self.page = page
        self.titles = list(titles if titles is not None else ["旧片", "山海归途"])
        self.current_title = current_title
        self.tap_activates = tap_activates
        self.calls = []
        self.pages = [page]
        self.played = []
        self.focused = None
        self.playing = page == "player"

    def change_page(self, page):
        self.page = page
        self.pages.append(page)
        self.focused = None

    def current_focus(self):
        self.calls.append(("focus",))
        package = "com.example.launcher" if self.page == "other" else PACKAGE
        return f"{package}/.{self.activities[self.page]}"

    def launch_app(self, package):
        self.calls.append(("launch", package))
        self.change_page("home")
        return {"ok": True, "package": package}

    def cards(self):
        if self.page == "home":
            return [("短剧", 200, 100)]
        if self.page == "list":
            return [(title, 300 + i * 220, 350) for i, title in enumerate(self.titles)]
        if self.page == "detail":
            return [("第 1 季 第 1 集", 400, 400), ("第 2 季", 700, 400)]
        if self.page == "season":
            return [("第 1 集", 400, 400), ("第 3 集", 700, 400)]
        return []

    def elements(self):
        self.calls.append(("dump", self.page))
        cards = self.cards()
        if self.focused is None and cards:
            self.focused = cards[0][0]
        elements = []
        if self.page in ("detail", "season"):
            elements.append(UIElement(text=self.current_title, node_id="heading"))
        for index, (label, x, y) in enumerate(cards):
            node_id = f"card-{index}"
            elements.append(UIElement(node_id=node_id, focusable=True,
                                      focused=self.focused == label,
                                      bounds=f"[{x-80},{y-40}][{x+80},{y+40}]", x=x, y=y))
            elements.append(UIElement(text=label, node_id=f"label-{index}",
                                      parent_id=node_id, x=x+9, y=y+7))
        return elements

    def activate(self, label):
        self.calls.append(("activate", self.page, label))
        if self.page == "home" and label == "短剧":
            self.change_page("list")
        elif self.page == "list" and label in self.titles:
            self.current_title = label
            self.change_page("detail")
        elif self.page == "detail" and label == "第 2 季":
            self.change_page("season")
        elif self.page in ("detail", "season") and label.startswith("第 "):
            self.played.append(self.current_title)
            self.playing = True
            self.change_page("player")

    def swipe(self, x1, y1, x2, y2, duration=300):
        # Scrolling reveals further rows in the fake library.
        self.calls.append(("swipe", y1, y2))
        return ""

    def tap(self, x, y):
        self.calls.append(("tap", x, y))
        # Child-label centers deliberately differ from the actionable parent.
        for label, cx, cy in self.cards():
            if (x, y) == (cx, cy):
                self.focused = label
                if self.tap_activates:
                    self.activate(label)
                return ""
        raise AssertionError(f"tap did not target an actionable card: {(x, y)}")

    def key(self, key):
        self.calls.append(("key", key))
        if key == "center":
            if self.page == "player":
                self.playing = not self.playing  # Feiniu player toggles on center
            else:
                self.activate(self.focused)
        elif key == "back":
            self.change_page({"player": "detail", "season": "detail",
                              "detail": "list", "list": "home"}.get(self.page, "home"))
        elif key == "play":
            self.playing = self.page == "player"
        return ""

    def playback_status(self, include_ui=True, expected_package=""):
        self.calls.append(("verify", include_ui, expected_package))
        return {"playing": self.playing, "package": PACKAGE,
                "evidence": {"media_session_playing": self.playing}}


class FastPathTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(CFG)
        # Do not patch monotonic: timeout/error paths must still terminate.
        self.sleep = patch("time.sleep", side_effect=AssertionError("unexpected fixed sleep"))
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def agent(self, device):
        llm = Mock()
        llm.enabled = True
        llm.chat.side_effect = AssertionError("fast path must not call a model")
        toolbox = Mock()
        toolbox.call.side_effect = AssertionError("fast path must not call LLM tools")
        return Agent(device, toolbox, llm, self.cfg)

    def assert_no_calls(self, device, *operations):
        self.assertFalse([call for call in device.calls if call[0] in operations], device.calls)

    def test_common_keys_have_no_model_or_ui_calls(self):
        cases = {"暂停": "pause", "暂停播放": "pause", "继续": "play",
                 "继续播放": "play", "恢复播放": "play", "返回": "back",
                 "返回上一级": "back", "返回桌面": "home", "回到桌面": "home",
                 "音量加": "volume_up", "调大音量": "volume_up", "声音大一点": "volume_up",
                 "音量减": "volume_down", "调小音量": "volume_down", "声音小一点": "volume_down"}
        for instruction, key in cases.items():
            with self.subTest(instruction=instruction):
                intent = parse_fast(instruction)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.key), ("key", key))
                device = MemoryDevice("player")
                agent = self.agent(device)
                result = agent.run(instruction)
                self.assertTrue(result["ok"], result)
                self.assertEqual(device.calls, [("key", key)])
                agent.llm.chat.assert_not_called()
                agent.toolbox.call.assert_not_called()

    def test_open_only_never_starts_or_verifies_playback(self):
        for page in ("other", "home", "list", "detail", "player"):
            with self.subTest(page=page):
                device = MemoryDevice(page)
                result = self.agent(device).run("打开飞牛")
                self.assertTrue(result["ok"], result)
                self.assertFalse(device.played)
                self.assert_no_calls(device, "key", "tap", "dump", "verify")
                if page != "other":
                    self.assert_no_calls(device, "launch")

    def test_resume_current_player_has_no_launch_or_dump(self):
        for instruction in ("飞牛继续播放", "打开飞牛播放短剧"):
            with self.subTest(instruction=instruction):
                device = MemoryDevice("player")  # already playing
                result = self.agent(device).run(instruction)
                self.assertTrue(result["ok"], result)
                # Already playing: resume must be a no-op, never toggle-pause it.
                self.assert_no_calls(device, "launch", "dump", "tap", "key")
                self.assertIn(("verify", False, PACKAGE), device.calls)

    def test_resume_paused_player_toggles_with_center(self):
        device = MemoryDevice("player")
        device.playing = False  # paused, content still loaded
        result = self.agent(device).run("飞牛继续播放")
        self.assertTrue(result["ok"], result)
        self.assertIn(("key", "center"), device.calls)
        self.assertTrue(device.playing)
        self.assert_no_calls(device, "launch", "dump", "tap")

    def test_generic_drama_uses_current_page(self):
        for page, expected in (("home", ["home", "list", "detail", "player"]),
                               ("list", ["list", "detail", "player"]),
                               ("detail", ["detail", "player"]),
                               ("player", ["player"])):
            with self.subTest(page=page):
                device = MemoryDevice(page)
                result = self.agent(device).run("打开飞牛播放短剧")
                self.assertTrue(result["ok"], result)
                self.assertEqual(device.pages, expected)
                self.assert_no_calls(device, "launch")
                self.assertTrue(device.playing)

    def test_requested_title_and_episode_survive_parsing(self):
        intent = parse_fast("打开飞牛播放《山海归途》第2季第3集")
        self.assertIsNotNone(intent)
        self.assertEqual((intent.title, intent.season, intent.episode), ("山海归途", 2, 3))

    def test_requested_title_never_resumes_another_title(self):
        for page in ("home", "list", "detail", "player"):
            for tap_activates in (False, True):
                with self.subTest(page=page, tap_activates=tap_activates):
                    device = MemoryDevice(page, tap_activates=tap_activates)
                    result = self.agent(device).run("打开飞牛播放《山海归途》")
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(device.played, ["山海归途"])
                    self.assertEqual(device.current_title, "山海归途")
                    self.assert_no_calls(device, "launch")

    def test_requested_season_and_episode_are_selected(self):
        device = MemoryDevice("list", titles=["旧片", "山海归途第2季"])
        result = self.agent(device).run("打开飞牛播放《山海归途》第2季第3集")
        self.assertTrue(result["ok"], result)
        self.assertIn(("activate", "detail", "第 2 季"), device.calls)
        self.assertIn(("activate", "season", "第 3 集"), device.calls)
        self.assertEqual(device.played, ["山海归途第2季"])

    def test_missing_title_fails_without_playing_a_substitute(self):
        device = MemoryDevice("list", titles=["别的剧", "山海归途续集"])
        result = self.agent(device).run("打开飞牛播放《山海归途》")
        self.assertFalse(result["ok"], result)
        self.assertIn("山海归途", result["summary"])
        self.assertFalse(device.played)
        self.assert_no_calls(device, "activate", "verify", "launch")

    def test_explicit_hongguo_app_does_not_route_to_feiniu(self):
        for instruction in ("打开红果播放飞牛里的短剧", "打开飞牛播放红果应用中的短剧"):
            with self.subTest(instruction=instruction):
                self.assertIsNone(parse_fast(instruction))
                self.assertEqual(parse_instruction(instruction, self.cfg)[0], "hongguo")
                device = MemoryDevice()
                self.assertIsNone(self.agent(device).fast_run(instruction))
                self.assertEqual(device.calls, [])

    def test_complex_requests_preserve_llm_fallback(self):
        for instruction in ("暂停十分钟后继续播放", "飞牛播放短剧然后返回桌面",
                            "打开飞牛推荐一个短剧", "打开飞牛播放山海归途并且调大音量"):
            with self.subTest(instruction=instruction):
                self.assertIsNone(parse_fast(instruction))
                device = MemoryDevice()
                agent = self.agent(device)
                self.assertIsNone(agent.fast_run(instruction))
                self.assertEqual(device.calls, [])
                expected = {"ok": True, "mode": "llm"}
                with patch.object(agent, "_llm_run", return_value=expected) as fallback:
                    self.assertIs(agent.run(instruction), expected)
                    fallback.assert_called_once_with(instruction)

    def test_ui_parser_retains_anonymous_focusable_parent(self):
        xml = '''<hierarchy><node text="" focusable="true" focused="true"
            bounds="[100,200][500,600]"><node text="山海归途"
            bounds="[120,240][400,300]"/></node></hierarchy>'''
        elements = TVDevice._parse_ui(xml)
        parent, label = elements
        self.assertFalse(parent.text)
        self.assertTrue(parent.focusable)
        self.assertEqual(label.parent_id, parent.node_id)
        self.assertIs(FeiniuPlayer.control(label, elements), parent)
        self.assertEqual(parent.to_dict()["focusable"], True)

    def test_control_walks_multiple_anonymous_parent_levels(self):
        parent = UIElement(node_id="card", focusable=True)
        container = UIElement(node_id="container", parent_id="card")
        label = UIElement(text="短剧", node_id="label", parent_id="container")
        self.assertIs(FeiniuPlayer.control(label, [parent, container, label]), parent)

    def test_episode_labels_are_revealed_before_waiting(self):
        class OffscreenDevice(MemoryDevice):
            revealed = False

            def cards(self):
                if self.page == "detail":
                    return [("第 1 季", 400, 400)]
                if self.page == "season":
                    return [("1.山海归途 S01E001", 400, 750)] if self.revealed else []
                return super().cards()

            def activate(self, label):
                if self.page == "detail" and label == "第 1 季":
                    self.change_page("season")
                elif self.page == "season" and label == "1.山海归途 S01E001":
                    self.played.append(self.current_title)
                    self.playing = True
                    self.change_page("player")
                else:
                    super().activate(label)

            def key(self, key):
                if key == "down" and self.page == "season":
                    self.revealed = True
                return super().key(key)

        device = OffscreenDevice("detail", current_title="山海归途")
        result = self.agent(device).run("打开飞牛播放山海归途第一部第一集")
        self.assertTrue(result["ok"], result)
        self.assertTrue(device.revealed)
        self.assertEqual(device.played, ["山海归途"])

    def test_file_detail_requires_play_button(self):
        class FileDevice(MemoryDevice):
            activities = dict(MemoryDevice.activities, file="MovieDetailActivity")

            def elements(self):
                if self.page != "file":
                    return super().elements()
                return [UIElement(text="山海归途 S01E001"),
                        UIElement(text="播放", focusable=True, focused=True)]

            def key(self, key):
                if key == "center" and self.page == "file":
                    self.played.append(self.current_title)
                    self.playing = True
                    self.change_page("player")
                    return ""
                return super().key(key)

        device = FileDevice("file", current_title="山海归途")
        result = self.agent(device).run("打开飞牛播放山海归途第一部第一集")
        self.assertTrue(result["ok"], result)
        self.assertEqual(device.played, ["山海归途"])

    def test_zoomed_focus_rebinds_card_by_label(self):
        class ZoomDevice(MemoryDevice):
            def elements(self):
                els = super().elements()
                for e in els:
                    if e.focused:
                        e.bounds = f"[{e.x-100},{e.y-60}][{e.x+100},{e.y+60}]"
                return els
        device = ZoomDevice("list")
        result = self.agent(device).run("打开飞牛播放山海归途")
        self.assertTrue(result["ok"], result)
        self.assertEqual(device.played, ["山海归途"])

    def test_known_media_state_avoids_ui_and_duplicate_queries(self):
        adb = Mock()
        def shell(args):
            if args == ["dumpsys", "window"]:
                return CmdResult(args, 0, f"mCurrentFocus={PACKAGE}/.PlayerActivity", "")
            if args == ["dumpsys", "media_session"]:
                return CmdResult(args, 0, f"package={PACKAGE}\\nstate=PlaybackState {{state=3}}", "")
            raise AssertionError(f"redundant query: {args}")
        adb.shell.side_effect = shell
        result = TVDevice(adb, self.cfg).playback_status(expected_package=PACKAGE)
        self.assertTrue(result["playing"])
        self.assertFalse(result["evidence"]["ui_checked"])
        self.assertEqual(adb.shell.call_count, 2)

    def test_content_loading_detects_skeleton(self):
        skel = [UIElement(resource_id="com.trim.tv:id/skeleton_view")]
        self.assertTrue(FeiniuPlayer._content_loading(skel))
        loading = [UIElement(resource_id="com.trim.tv:id/loading_bar")]
        self.assertTrue(FeiniuPlayer._content_loading(loading))
        content = [UIElement(text="短剧"), UIElement(text="继续观看")]
        self.assertFalse(FeiniuPlayer._content_loading(content))

    def test_legacy_wait_play_reuses_confirmed_evidence(self):
        self.cfg["agent"]["smart_navigation"] = False
        self.cfg["apps"]["feiniu_tv"]["flow"] = [
            {"action": "key", "key": "play"}, {"action": "wait_play", "timeout": 1}]
        device = MemoryDevice("player")
        confirmed = {"playing": True, "evidence": {"media_session_playing": True}}
        # A second probe would lose the known-good observation; do not do one.
        device.playback_status = Mock(side_effect=[confirmed, AssertionError("redundant verification")])
        result = run_flow(device, "feiniu_tv", self.cfg)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["playback"], confirmed)
        device.playback_status.assert_called_once_with()
        self.assert_no_calls(device, "focus", "dump", "launch")


if __name__ == "__main__":
    unittest.main()


class TitleMatchingSafetyTests(unittest.TestCase):
    """Short spoken names may match, sequels must never be substituted."""

    def setUp(self):
        # 显式注入别名，不依赖操作者本地的 config.yaml（公开包也能通过）。
        import copy
        from adbtv.config import DEFAULTS
        cfg = copy.deepcopy(DEFAULTS)
        cfg.setdefault("apps", {}).setdefault("feiniu_tv", {})["title_aliases"] = {
            "野神": "穿成野神我靠香火斩诸神",
            "穿成野神": "穿成野神我靠香火斩诸神",
        }
        self.player = FeiniuPlayer.__new__(FeiniuPlayer)
        self.player.cfg = cfg

    def match(self, actual, spoken):
        return self.player.title_matches(actual, Intent("play", app="feiniu_tv",
                                                        title=spoken, drama=True))

    def test_short_name_matches_full_catalog_title(self):
        self.assertTrue(self.match("穿成野神我靠香火斩诸神", "穿成野神"))
        self.assertTrue(self.match("穿成野神我靠香火斩诸神", "野神"))

    def test_sequel_is_never_treated_as_the_same_work(self):
        for actual in ("山海归途续集", "山海归途第二季", "山海归途前传"):
            with self.subTest(actual=actual):
                self.assertFalse(self.match(actual, "山海归途"))

    def test_exact_title_still_matches(self):
        self.assertTrue(self.match("山海归途", "山海归途"))
