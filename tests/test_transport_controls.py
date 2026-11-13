"""Transport safety with mock ADB only: acknowledgements need later verification."""
import unittest
from unittest.mock import Mock

from adbtv.adb import CmdResult
from adbtv.device import TVDevice, UIElement
from adbtv.perception import ScreenState


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.adb = Mock()
        self.adb.shell.return_value = CmdResult([], 0, "", "")
        self.device = TVDevice(self.adb, {})
        self.device.key = Mock(return_value="")
        self.device.tap = Mock(return_value="")

    @staticmethod
    def status(package="com.trim.tv", state="PLAYING", activity="PlayerActivity", **extra):
        return {"package": package, "activity": package + "/." + activity,
                "state": state, "playing": state == "PLAYING", **extra}

    def test_home_and_unknown_state_do_not_control(self):
        for status in (self.status(activity="MainActivity"), self.status(state="UNKNOWN"),
                       self.status(state="STOPPED"), self.status(state="BUFFERING"),
                       self.status(package="com.other", activity="HomeActivity")):
            for action in ("pause", "resume"):
                self.assertFalse(self.device.transport(action, status)["ok"])
        self.device.key.assert_not_called()

    def test_foreground_and_media_owner_must_match(self):
        status = self.status()
        status["package"] = "com.other"
        self.assertFalse(self.device.transport("pause", status)["ok"])
        self.device.key.assert_not_called()

    def test_feiniu_center_and_other_player_explicit_media_keys(self):
        result = self.device.transport("pause", self.status())
        self.assertTrue(result["ok"])
        self.assertTrue(result["acknowledged"])
        self.assertEqual(result["status"], "ACKNOWLEDGED")
        self.device.key.assert_called_once_with("center")
        self.device.key.reset_mock()
        self.device.transport("resume", self.status(state="PAUSED"))
        self.device.key.assert_called_once_with("center")
        self.device.key.reset_mock()
        self.device.transport("pause", self.status(package="com.example.player"))
        self.device.key.assert_called_once_with("pause")
        self.device.key.reset_mock()
        self.device.transport("resume", self.status(package="com.example.player", state="PAUSED"))
        self.device.key.assert_called_once_with("play")

    def test_repeated_pause_and_resume_are_idempotent_with_new_observations(self):
        state = self.status()
        self.device.playback_status = Mock(side_effect=lambda **kwargs: dict(state))
        def change(key):
            state["state"] = "PAUSED" if state["state"] == "PLAYING" else "PLAYING"
            state["playing"] = state["state"] == "PLAYING"
        self.device.key.side_effect = change
        self.assertEqual(self.device.transport("pause")["status"], "ACKNOWLEDGED")
        self.assertEqual(self.device.transport("pause")["method"], "noop")
        self.assertEqual(self.device.transport("resume")["status"], "ACKNOWLEDGED")
        self.assertEqual(self.device.transport("resume")["method"], "noop")
        self.assertEqual(self.device.key.call_count, 2)

    def test_activity_package_names_and_home_classes_are_not_player_evidence(self):
        for activity in ("com.example.player.MainActivity", "com.trim.tv.PlayerSelectionActivity", "com.trim.tv/.PlayerHomeActivity"):
            observed = self.status()
            observed["activity"] = activity
            self.assertFalse(self.device.transport("pause", observed)["ok"])
        self.device.key.assert_not_called()

    def test_positive_evidence_fills_absent_state_but_not_explicit_unknown(self):
        observed = self.status(state="", evidence={"audio_track_paused": True})
        self.assertEqual(self.device.transport("pause", observed)["method"], "noop")
        self.device.key.assert_not_called()
        observed["state"] = "UNKNOWN"
        self.assertFalse(self.device.transport("resume", observed)["ok"])
        self.device.key.assert_not_called()

    def test_playback_status_reuses_owned_session_read_for_capabilities(self):
        self.device.current_focus = Mock(return_value="com.example/.PlayerActivity")
        raw = "com.example/Session (userId=0)\n state=PlaybackState {state=3, position=20000, actions=304}\n metadata: android.media.metadata.DURATION=137000"
        self.adb.shell.return_value = CmdResult([], 0, raw, "")
        result = self.device.playback_status(include_ui=False)
        self.assertEqual(result["state"], "PLAYING")
        self.assertEqual(result["media_actions"], 304)
        self.assertEqual(result["position_ms"], 20000)
        self.assertEqual(result["duration_ms"], 137000)
        self.adb.shell.assert_called_once_with(["dumpsys", "media_session"])

    def test_observation_is_reused_without_reading_device(self):
        self.device.playback_status = Mock(side_effect=AssertionError("unexpected query"))
        result = self.device.transport("pause", self.status(state="PAUSED"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "noop")
        self.device.key.assert_not_called()
        self.adb.shell.assert_not_called()

    def test_invalid_actions_are_rejected_before_device_reads(self):
        with self.assertRaises(ValueError):
            self.device.transport("toggle")
        with self.assertRaises(ValueError):
            self.device.skip_episode("random")
        self.adb.shell.assert_not_called()

    def test_episode_skip_requires_owned_session_capability(self):
        for status in (self.status(), self.status(activity="HomeActivity", media_actions=48, media_session_registered=True),
                       self.status(media_actions=0, media_session_registered=True),
                       self.status(media_actions=48, media_session_registered=False)):
            self.assertFalse(self.device.skip_episode("next", status)["ok"])
        self.device.key.assert_not_called()
        session = self.status(package="com.example.player", media_actions=48, media_session_registered=True)
        self.assertTrue(self.device.skip_episode("next", session)["ok"])
        self.device.key.assert_called_once_with("next")
        self.device.key.reset_mock()
        self.assertTrue(self.device.skip_episode("previous", session)["ok"])
        self.device.key.assert_called_once_with("prev")

    def test_session_parser_does_not_borrow_foreign_or_ambiguous_capabilities(self):
        raw = """com.other/Session (userId=0)
 state=PlaybackState {state=3, position=400, actions=48}
 metadata: duration=10000
com.example.player/Session (userId=0)
 state=PlaybackState {state=2, position=2000, actions=256}
 metadata: android.media.metadata.DURATION=137000
"""
        own = self.device._session_details(raw, "com.example.player")
        self.assertEqual(own["media_actions"], 256)
        self.assertEqual(own["position_ms"], 2000)
        self.assertEqual(own["duration_ms"], 137000)
        duplicate = raw + "com.example.player/Other (userId=0)\n state=PlaybackState {state=2, actions=48}\n"
        self.assertEqual(self.device._session_details(duplicate, "com.example.player"), {})
        self.assertEqual(self.device._session_details("state=PlaybackState {state=3, actions=48}", "com.example.player"), {})

    def seek_observation(self, *, duration=True, timeline=True):
        elements = [UIElement(text="00:20 / 02:17", node_id="time")]
        if timeline:
            elements.append(UIElement(resource_id="com.trim.tv:id/seek_bar", cls="android.widget.SeekBar", bounds="[100,200][1100,240]", x=600, y=220))
        state = ScreenState(app="com.trim.tv", activity=".PlayerActivity", playback=self.status(), elements=elements)
        if not duration:
            state.elements[0].text = "Playing"
        self.device.current_focus = Mock(return_value="com.trim.tv/.PlayerActivity")
        self.device.capture_ui = Mock(side_effect=lambda: list(state.elements))
        return state

    def test_seek_unsupported_does_not_press_fast_forward_or_tap(self):
        for state in (self.seek_observation(duration=False), self.seek_observation(timeline=False)):
            self.assertFalse(self.device.seek_to(30, state)["ok"])
        self.device.key.assert_not_called()
        self.device.tap.assert_not_called()

    def test_seek_maps_real_timeline_and_clamps_to_bounds(self):
        state = self.seek_observation()
        result = self.device.seek_to(68.5, state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration_seconds"], 137)
        self.assertEqual(result["target_seconds"], 68.5)
        self.device.tap.assert_called_once_with(600, 220)
        self.device.tap.reset_mock()
        self.device.seek_to(999, state)
        self.device.tap.assert_called_once_with(1099, 220)
        self.device.tap.reset_mock()
        self.device.seek_to(-30, state)
        self.device.tap.assert_called_once_with(100, 220)
        self.device.key.assert_not_called()

    def test_seek_accepts_serialized_screen_state(self):
        state = self.seek_observation()
        self.assertTrue(self.device.seek_to(30, state.to_dict())["ok"])

    def test_seek_uses_real_media_duration_when_ui_has_no_clock(self):
        state = self.seek_observation(duration=False)
        state.playback.update(position_ms=20000, duration_ms=100000)
        self.device.playback_status = Mock(return_value=dict(state.playback))
        result = self.device.seek_to(25, state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "media")
        self.assertEqual(result["duration_seconds"], 100)
        self.device.tap.assert_called_once_with(350, 220)
        self.device.playback_status.assert_called_once_with(include_ui=False, expected_package="com.trim.tv", focus="com.trim.tv/.PlayerActivity")

    def test_seek_rechecks_media_duration_and_rejects_wrong_scope(self):
        state = self.seek_observation(duration=False)
        state.playback.update(position_ms=20000, duration_ms=100000)
        self.device.playback_status = Mock(return_value={**state.playback, "duration_ms": 200000})
        self.assertFalse(self.device.seek_to(25, state)["ok"])
        self.device.tap.assert_not_called()
        self.device.playback_status.return_value = self.status(package="com.other", position_ms=20000, duration_ms=100000)
        self.assertFalse(self.device.seek_to(25, state)["ok"])
        self.device.tap.assert_not_called()

    def test_seek_rechecks_focus_immediately_before_tap(self):
        state = self.seek_observation()
        self.device.current_focus.side_effect = ["com.trim.tv/.PlayerActivity", "com.other/.PlayerActivity"]
        self.assertFalse(self.device.seek_to(25, state)["ok"])
        self.device.tap.assert_not_called()

    def test_seek_rejects_invalid_numbers_without_reading_device(self):
        for number in (True, None, "30", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.device.seek_to(number)
        self.adb.shell.assert_not_called()
        self.device.tap.assert_not_called()

    def test_seek_rejects_stale_scope_element_coordinates_and_duration(self):
        state = self.seek_observation()
        self.device.current_focus.return_value = "com.other/.PlayerActivity"
        self.assertFalse(self.device.seek_to(30, state)["ok"])
        self.device.tap.assert_not_called()
        state = self.seek_observation()
        moved = UIElement(resource_id="com.trim.tv:id/seek_bar", cls="android.widget.SeekBar", bounds="[200,200][1200,240]")
        self.device.capture_ui.return_value = [state.elements[0], moved]
        self.device.capture_ui.side_effect = None
        self.assertFalse(self.device.seek_to(30, state)["ok"])
        self.device.tap.assert_not_called()
        state = self.seek_observation()
        self.device.capture_ui.side_effect = None
        self.device.capture_ui.return_value = [UIElement(text="00:20 / 03:00"), state.elements[1]]
        self.assertFalse(self.device.seek_to(30, state)["ok"])
        self.device.tap.assert_not_called()

    def test_seek_rejects_ambiguous_or_volume_seekbars(self):
        state = self.seek_observation()
        state.elements.append(UIElement(resource_id="second_seek", cls="android.widget.SeekBar", bounds="[100,300][1100,340]"))
        self.assertFalse(self.device.seek_to(30, state)["ok"])
        state = self.seek_observation()
        state.elements[1].resource_id = "volume_seek"
        self.assertFalse(self.device.seek_to(30, state)["ok"])
        self.device.tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RegressionV342Tests(unittest.TestCase):
    """Bugs found by the 30x3 real-device statistics run."""

    def test_device_exposes_set_volume(self):
        """缺失 set_volume 会让所有"音量设到 N"指令 AttributeError。"""
        from adbtv.device import TVDevice
        self.assertTrue(hasattr(TVDevice, "set_volume"), "TVDevice.set_volume 必须存在")

    def test_set_volume_clamps_report(self):
        import copy, tempfile
        from adbtv.config import DEFAULTS
        from adbtv.adb import AdbClient
        from adbtv.device import TVDevice
        cfg = copy.deepcopy(DEFAULTS)
        cfg["adb"].update(serial="x", simulator="inprocess",
                          fake_state=tempfile.mktemp())
        device = TVDevice(AdbClient(cfg), cfg)
        result = device.set_volume(7)
        self.assertTrue(result["ok"])
        self.assertEqual(result["after"]["index"], 7)

    def test_set_volume_rejects_negative(self):
        from adbtv.device import TVDevice
        device = TVDevice.__new__(TVDevice)
        with self.assertRaises(ValueError):
            TVDevice.set_volume(device, -1)

    def test_query_summary_is_static(self):
        """曾因缺少 @staticmethod 导致所有查询指令报参数个数错误。"""
        from adbtv.agent import Agent
        from adbtv.perception import ScreenState
        from adbtv.verifier import GoalSpec
        state = ScreenState(app="com.mitv.mivideoplayer",
                            playback={"playing": True, "metadata": {"title": "片名"}})
        self.assertIn("片名", Agent._query_summary(GoalSpec("query_media"), state))
        self.assertIn("com.mitv.mivideoplayer",
                      Agent._query_summary(GoalSpec("query_app"), state))
