"""Perception/verifier regression tests: no television, ADB process, or network."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adbtv.device import TVDevice, UIElement
from adbtv.perception import Perception, ScreenState
from adbtv.verifier import GoalSpec, VerificationStatus as Status, Verifier, verify

PACKAGE = "com.trim.tv"
TITLE = "山海归途"


def card(text=TITLE, x=150):
    return [UIElement(node_id="0", clickable=True, focused=True,
                      bounds=f"[{x-50},100][{x+50},200]", x=x, y=150),
            UIElement(text=text, node_id="1", parent_id="0",
                      bounds=f"[{x-40},120][{x+40},180]", x=x, y=150)]


class MemoryDevice:
    def __init__(self):
        self.cfg = {}
        self.focus = PACKAGE + "/.PlayerActivity"
        self.nodes = card()
        self.capture_count = 0
        self.status_count = 0
        self.taps = []
        self.adb = Mock(spec=["screenshot_bytes"])
        self.adb.screenshot_bytes.return_value = b"not-decoded-png"
        self.status = {"package": PACKAGE, "playing": True, "state": "PLAYING",
                       "evidence": {"media_session_playing": True}}

    def current_focus(self):
        return self.focus

    def capture_ui(self):
        self.capture_count += 1
        if isinstance(self.nodes, Exception):
            raise self.nodes
        return copy.deepcopy(self.nodes)

    def playback_status(self, *, include_ui, expected_package):
        assert include_ui is False
        self.status_count += 1
        return copy.deepcopy(self.status)

    def tap(self, x, y):
        self.taps.append((x, y))
        return "command accepted"


def screen(*, title=None, activity=".PlayerActivity", position=None,
           timestamp=1, state="PLAYING", texts=None, evidence=None, **extra):
    playback = {"package": PACKAGE, "playing": state == "PLAYING", "state": state}
    if title is not None:
        playback["metadata"] = {"title": title}
    if position is not None:
        playback["position_ms"] = position
    if evidence is not None:
        playback["evidence"] = evidence
    playback.update(extra)
    return ScreenState(app=PACKAGE, activity=activity, playback=playback,
                       texts=texts or [], timestamp=timestamp)


class PerceptionTests(unittest.TestCase):
    def setUp(self):
        self.dev = MemoryDevice()
        self.p = Perception(self.dev)

    def test_xml_relationship_focus_and_no_default_screenshot(self):
        state = self.p.observe()
        self.assertEqual(state.app, PACKAGE)
        self.assertEqual(state.focused.node_id, "0")
        self.assertEqual(state.elements[1].parent_id, "0")
        self.assertEqual(state.texts, [TITLE])
        self.assertTrue(state.xml_available)
        self.assertEqual(state.screenshot_status, "not_requested")
        self.dev.adb.screenshot_bytes.assert_not_called()
        self.assertEqual(state.to_dict()["elements"][1]["parent_id"], "0")

    def test_real_xml_parser_parent_nodes_are_preserved(self):
        self.dev.nodes = TVDevice._parse_ui('<hierarchy><node text="" clickable="true" '
                                           'bounds="[0,0][100,100]"><node text="剧名" '
                                           'bounds="[0,0][100,100]"/></node></hierarchy>')
        state = self.p.snapshot(include_playback=False)
        self.assertEqual(state.elements[1].parent_id, state.elements[0].node_id)
        self.assertEqual(state.texts, ["剧名"])
        self.assertEqual(self.dev.status_count, 0)

    def test_short_ttl_and_action_invalidation(self):
        with patch("adbtv.perception.time.monotonic", return_value=10) as clock:
            self.p.observe(include_playback=False)
            self.p.observe(include_playback=False)
            self.assertEqual(self.dev.capture_count, 1)
            clock.return_value = 10.31
            self.p.observe(include_playback=False)
            self.assertEqual(self.dev.capture_count, 2)
            self.p.after_action()
            self.p.observe(include_playback=False)
            self.assertEqual(self.dev.capture_count, 3)

    def test_cache_isolation_and_separate_capabilities(self):
        state = self.p.observe(include_playback=False)
        state.elements[1].text = "mutated"
        self.assertEqual(self.p.observe(include_playback=False).texts, [TITLE])
        self.assertEqual(self.p.observe(include_playback=False).elements[1].text, TITLE)
        self.p.observe(include_playback=True)
        self.assertEqual(self.dev.status_count, 1)

    def test_fingerprint_changes_and_fresh_bypasses_cache(self):
        a = self.p.observe(include_playback=False)
        self.assertFalse(a.changed)
        b = self.p.observe(fresh=True, include_playback=False)
        self.assertFalse(b.changed)
        self.dev.nodes = card("另一部")
        c = self.p.observe(fresh=True, include_playback=False)
        self.assertTrue(c.changed)
        self.assertNotEqual(a.page_fingerprint, c.page_fingerprint)

    def test_xml_failure_preserves_system_fallback(self):
        self.dev.nodes = RuntimeError("dump failed")
        state = self.p.observe()
        self.assertEqual(state.app, PACKAGE)
        self.assertTrue(state.playback["playing"])
        self.assertFalse(state.xml_available)
        self.assertIn("xml: dump failed", state.observation_errors)

    def test_empty_and_malformed_screenshot_do_not_claim_black(self):
        state = self.p.observe(include_image=True)
        self.assertEqual(state.screenshot_status, "unknown")
        self.assertTrue(state.screenshot_hash)
        self.assertEqual(state.screenshot_path, "")
        self.dev.adb.screenshot_bytes.return_value = b""
        state = self.p.observe(fresh=True, include_image=True)
        self.assertEqual(state.screenshot_status, "unavailable")
        self.assertTrue(any("screenshot:" in error for error in state.errors))

    def test_vision_requires_missing_content_and_explicit_opt_in(self):
        llm = Mock(spec=["analyze_image_bytes"])
        llm.analyze_image_bytes.return_value = {"texts": ["视觉候选"]}
        p = Perception(self.dev, llm)
        p.observe(allow_vision=True)
        llm.analyze_image_bytes.assert_not_called()
        self.dev.nodes = []
        p.observe(fresh=True)
        llm.analyze_image_bytes.assert_not_called()
        state = p.observe(fresh=True, allow_vision=True)
        self.assertEqual(state.texts, [])
        self.assertFalse(state.vision["verified"])
        self.assertEqual(state.vision["candidates"]["texts"], ["视觉候选"])

    def test_path_client_private_temp_file_removed_on_error(self):
        self.dev.nodes = []
        paths = []

        class PathClient:
            def analyze_image(client, path, prompt):
                paths.append(path)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(Path(path).read_bytes(), b"not-decoded-png")
                raise RuntimeError("vision offline")

        p = Perception(self.dev, PathClient())
        state = p.observe(allow_vision=True)
        self.assertTrue(any("vision offline" in error for error in state.errors))
        self.assertFalse(os.path.exists(paths[0]))
        self.assertEqual(state.screenshot_path, "")
        self.assertEqual(state.app, PACKAGE)

    def test_vision_dedup_failure_and_bounded_lru(self):
        self.dev.nodes = []
        llm = Mock(spec=["analyze_image_bytes"])
        llm.analyze_image_bytes.side_effect = RuntimeError("unavailable")
        p = Perception(self.dev, llm, vision_cache_size=2)
        p.observe(fresh=True, allow_vision=True)
        p.invalidate()
        p.observe(fresh=True, allow_vision=True)
        self.assertEqual(llm.analyze_image_bytes.call_count, 1)
        for image in (b"second", b"third"):
            self.dev.adb.screenshot_bytes.return_value = image
            p.observe(fresh=True, allow_vision=True)
        self.assertEqual(len(p._vision_cache), 2)
        self.dev.adb.screenshot_bytes.return_value = b"not-decoded-png"
        p.observe(fresh=True, allow_vision=True)
        self.assertEqual(llm.analyze_image_bytes.call_count, 4)

    def test_locate_id_text_coords_and_ambiguous_negative(self):
        self.assertEqual(self.p.locate(text=TITLE).parent_id, "0")
        self.assertEqual(self.p.locate(coords=(150, 150)).node_id, "0")
        self.assertIsNone(self.p.locate(coords=(5000, 5000)))
        self.assertIsNone(self.p.locate(id="1"))  # snapshot-local IDs require state
        state = self.p.observe(include_playback=False)
        self.assertEqual(self.p.locate(id="1", state=state).text, TITLE)
        self.dev.nodes += card()
        self.assertIsNone(self.p.locate(text=TITLE))

    def test_click_fresh_rebinds_label_to_moved_parent(self):
        element = self.p.locate(text=TITLE)
        self.dev.nodes = card(x=450)
        self.p.click(element)
        self.assertEqual(self.dev.taps, [(450, 150)])
        count = self.dev.capture_count
        self.p.observe(include_playback=False)
        self.assertEqual(self.dev.capture_count, count + 1)

    def test_click_recycled_id_or_missing_element_never_uses_old_coords(self):
        self.dev.nodes[1].resource_id = "com.trim.tv:id/title"
        element = self.p.locate(text=TITLE)
        self.dev.nodes = card("别的剧")
        self.dev.nodes[1].resource_id = "com.trim.tv:id/title"
        with self.assertRaises(ValueError):
            self.p.click(element)
        self.assertEqual(self.dev.taps, [])

    def test_cross_page_stale_element_rejected(self):
        element = self.p.locate(text=TITLE)
        self.dev.focus = PACKAGE + "/.MovieListActivity"
        self.p.after_action()
        with self.assertRaises(ValueError):
            self.p.click(element)
        self.assertEqual(self.dev.taps, [])

    def test_direct_snapshot_element_remembers_scope_after_invalidation(self):
        element = self.p.observe(include_playback=False).elements[1]
        self.dev.focus = "com.other.app/.PlayerActivity"
        self.p.invalidate()
        with self.assertRaises(ValueError):
            self.p.click(element)
        self.assertEqual(self.dev.taps, [])

    def test_empty_xml_and_anonymous_element_fail_closed(self):
        self.dev.nodes = []
        with self.assertRaises(ValueError):
            self.p.click(coords=(150, 150))
        self.assertIsNone(self.p.validate(UIElement(node_id="0", x=150, y=150)))
        self.assertEqual(self.dev.taps, [])

    def test_wait_is_dynamic_and_cancel_checked_before_observation(self):
        count = []

        def predicate(state):
            count.append(state)
            if len(count) == 1:
                self.dev.nodes = card("ready")
            return "ready" in state.texts

        with patch("adbtv.perception.time.sleep"):
            state = self.p.wait_for(predicate, .1, include_playback=False)
        self.assertIn("ready", state.texts)
        self.assertEqual(len(count), 2)
        calls = self.dev.capture_count
        self.assertIsNone(self.p.wait_for(lambda state: True, 1, cancelcheck=lambda: True))
        self.assertEqual(self.dev.capture_count, calls)
        self.assertIsNone(self.p.wait_for(lambda state: False, 0, include_playback=False))

    def test_media_enrichment_uses_only_explicit_owned_session(self):
        self.dev.adb = Mock(spec=["shell", "screenshot_bytes"])
        raw = ('com.other.app/Session (userId=0)\n'
               ' state=PlaybackState {state=3, position=9999, speed=1.0}\n'
               ' android.media.metadata.TITLE=Other\n'
               'com.trim.tv/Session (userId=0)\n'
               ' state=PlaybackState {state=3, position=123, speed=1.0}\n'
               ' android.media.metadata.TITLE=山海归途\n')
        self.dev.adb.shell.return_value = Mock(returncode=0, stdout=raw, stderr="")
        state = self.p.observe()
        self.assertEqual(state.playback["position_ms"], 123)
        self.assertEqual(state.playback["metadata"]["title"], TITLE)
        self.dev.adb.shell.assert_called_once_with(["dumpsys", "media_session"])

    def test_ambiguous_sessions_do_not_provide_progress(self):
        self.dev.adb = Mock(spec=["shell", "screenshot_bytes"])
        raw = ('com.trim.tv/One\n state=PlaybackState {state=3, position=10}\n'
               'com.trim.tv/Two\n state=PlaybackState {state=3, position=999}\n')
        self.dev.adb.shell.return_value = Mock(returncode=0, stdout=raw, stderr="")
        self.assertNotIn("position_ms", self.p.observe().playback)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.v = Verifier()
        self.goal = GoalSpec("play", PACKAGE, TITLE)

    def test_application_and_media_content_success(self):
        after = screen(title=TITLE)
        result = verify(self.goal, None, after)
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.to_dict()["status"], "SUCCESS")

    def test_specified_content_unknown_is_partial_not_audio_success(self):
        after = screen(evidence={"audio_track_started": True})
        result = self.v.verify(self.goal, None, after)
        self.assertEqual(result.status, Status.PARTIAL)
        self.assertIsNone(result.conditions["title"])
        self.assertIsNone(result.conditions["playback"])

    def test_audio_alone_does_not_complete_generic_video(self):
        after = screen(evidence={"audio_track_started": True})
        self.assertNotEqual(self.v.verify(GoalSpec("play"), None, after).status, Status.SUCCESS)

    def test_wrong_content_fails_even_with_playing_session(self):
        after = screen(title="另一部片", evidence={"media_session_playing": True})
        self.assertEqual(self.v.verify(self.goal, None, after).status, Status.FAILED)

    def test_browse_and_detail_title_do_not_confirm_current_content(self):
        for activity in (".MovieListActivity", ".EpisodeDetailActivity"):
            after = screen(activity=activity, texts=[TITLE])
            self.assertEqual(self.v.verify(self.goal, None, after).status, Status.PARTIAL)

    def test_player_xml_exact_title_is_observed_evidence(self):
        after = screen(texts=[TITLE])
        result = self.v.verify(self.goal, None, after)
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertEqual(result.evidence["title_source"], "player_xml")
        after.texts = [TITLE + "第二部"]
        self.assertNotEqual(self.v.verify(self.goal, None, after).status, Status.SUCCESS)

    def test_vision_model_candidate_never_confirms_title(self):
        after = screen()
        after.vision = {"verified": False, "candidates": {"title": TITLE, "texts": [TITLE]}}
        after.playback["candidate_title"] = TITLE
        self.assertEqual(self.v.verify(self.goal, None, after).status, Status.PARTIAL)
        after.playback["metadata"] = {"title": TITLE, "source": "llm", "verified": False}
        self.assertEqual(self.v.verify(self.goal, None, after).status, Status.PARTIAL)

    def test_episode_season_unknown_or_wrong_cannot_pass(self):
        goal = GoalSpec("play", PACKAGE, TITLE, season=2, episode=1)
        after = screen(title=TITLE)
        self.assertEqual(self.v.verify(goal, None, after).status, Status.PARTIAL)
        after.texts = ["S02E10"]
        result = self.v.verify(goal, None, after)
        self.assertTrue(result.conditions["season"])
        self.assertIsNone(result.conditions["episode"])
        after.playback["metadata"].update(season=2, episode=10)
        self.assertEqual(self.v.verify(goal, None, after).status, Status.FAILED)
        after.playback["metadata"]["episode"] = 1
        self.assertEqual(self.v.verify(goal, None, after).status, Status.SUCCESS)

    def test_foreign_media_session_and_wrong_app_fail(self):
        after = screen(title=TITLE)
        after.playback["package"] = "com.other.app"
        self.assertEqual(self.v.verify(self.goal, None, after).status, Status.FAILED)
        after = screen(title=TITLE)
        after.app = "com.other.app"
        self.assertEqual(self.v.verify(self.goal, None, after).status, Status.FAILED)

    def test_progress_requires_real_numeric_increment(self):
        goal = GoalSpec("play", PACKAGE, TITLE, require_progress=True)
        before = screen(title=TITLE, position=100, timestamp=1)
        after = screen(title=TITLE, position=200, timestamp=2)
        result = self.v.verify(goal, before, after)
        self.assertEqual(result.status, Status.SUCCESS)
        self.assertEqual(result.evidence["delta"], 100)
        for value in (100, 99):
            after.playback["position_ms"] = value
            self.assertEqual(self.v.verify(goal, before, after).status, Status.FAILED)

    def test_elapsed_time_playing_or_true_is_not_progress(self):
        for value in (None, True, "200", float("nan"), float("inf"), -10):
            before = screen(position=100, timestamp=1)
            after = screen(position=value, timestamp=200)
            result = self.v.verify_progress(GoalSpec("play"), before, after)
            self.assertEqual(result.status, Status.UNKNOWN)

    def test_position_units_and_track_switch_cannot_fake_progress(self):
        before = screen(title=TITLE, position=100, timestamp=1)
        after = screen(title="另一部", position=10000, timestamp=2)
        self.assertEqual(self.v.verify_progress(self.goal, before, after).status, Status.UNKNOWN)
        after = screen(timestamp=2)
        after.playback["position_seconds"] = 200
        self.assertEqual(self.v.verify_progress(self.goal, before, after).status, Status.UNKNOWN)
        after = screen(position=200, timestamp=1)
        self.assertEqual(self.v.verify_progress(self.goal, before, after).status, Status.UNKNOWN)

    def test_key_input_and_search_command_acceptance_are_not_completion(self):
        after = screen(title=TITLE)
        after.playback.update(ok=True, input_text={"ok": True}, key_command_success=True)
        # Pure input/key/tap actions have no semantic outcome -> PARTIAL at best.
        for action in ("key", "input_text", "tap", "volume"):
            result = self.v.verify(GoalSpec(action, PACKAGE), None, after)
            self.assertEqual(result.status, Status.PARTIAL)
            self.assertIsNone(result.conditions["action_outcome"])
        # Home/volume/search are verified only from real system evidence; an
        # observation with none of it can never be reported as SUCCESS.
        for action in ("home", "volume_up", "search"):
            result = self.v.verify(GoalSpec(action, PACKAGE), None, after)
            self.assertNotEqual(result.status, Status.SUCCESS)

    def test_pause_and_resume_use_observed_state(self):
        after = screen(state="PAUSED")
        self.assertEqual(self.v.verify(GoalSpec("pause", PACKAGE), None, after).status, Status.SUCCESS)
        self.assertEqual(self.v.verify(GoalSpec("resume", PACKAGE), None, after).status, Status.FAILED)
        after = screen(state="PLAYING")
        self.assertEqual(self.v.verify(GoalSpec("pause", PACKAGE), None, after).status, Status.FAILED)

    def test_component_methods_and_unknown_observation(self):
        after = ScreenState(observation_errors=["xml: unavailable"])
        for method in (self.v.verify_application, self.v.verify_content,
                       self.v.verify_playback, self.v.verify_progress):
            result = method(self.goal, None, after)
            self.assertEqual(result.status, Status.UNKNOWN)
        result = self.v.verify(self.goal, None, after)
        self.assertEqual(result.status, Status.UNKNOWN)
        self.assertEqual(result.evidence["observation_errors"], ["xml: unavailable"])


if __name__ == "__main__":
    unittest.main()
