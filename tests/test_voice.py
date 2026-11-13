"""Voice control tests: ASR parsing, dedup and whitelist routing."""
import json
import time
import unittest
from unittest.mock import Mock

from adbtv.voice import Utterance, VoiceListener, VoiceRouter, parse_log_line


def asr_line(text, is_final=True, request_id=None, extra=""):
    payload = {
        "header": {"name": "RecognizeResult", "namespace": "SpeechRecognizer"},
        "payload": {"is_final": is_final, "is_vad_begin": True,
                    "results": [{"origin_text": text, "text": text,
                                 "confidence": 0.0}]},
    }
    rid = f" requestId='{request_id}'" if request_id else ""
    return ("I =vc=en=aifw==xaf=pia=: FarFieldEngineprocessAsrResult, "
            f"instruction = {json.dumps(payload, ensure_ascii=False)}{rid}{extra}")


class ParseTests(unittest.TestCase):
    def test_final_result_is_extracted(self):
        utterance = parse_log_line(asr_line("今天天气怎么样"))
        self.assertIsNotNone(utterance)
        self.assertEqual(utterance.text, "今天天气怎么样")
        self.assertTrue(utterance.is_final)

    def test_interim_result_is_ignored(self):
        self.assertIsNone(parse_log_line(asr_line("今天天", is_final=False)))

    def test_failure_frames_are_ignored(self):
        for hint in ("state_result_type=fail", "CONVERSATION_TIMEOUT",
                     "no audio bytes"):
            with self.subTest(hint=hint):
                self.assertIsNone(parse_log_line(asr_line("播放", extra=" " + hint)))

    def test_non_asr_lines_are_ignored(self):
        self.assertIsNone(parse_log_line("I/ActivityManager: Start proc foo"))
        self.assertIsNone(parse_log_line(""))

    def test_malformed_json_does_not_raise(self):
        line = "I x: FarFieldEngineprocessAsrResult, instruction = {broken"
        self.assertIsNone(parse_log_line(line))

    def test_origin_detection(self):
        key = parse_log_line(asr_line("打开飞牛", extra=" wakeupOrigin=KEY"))
        self.assertEqual(key.origin, "KEY")
        voice = parse_log_line(asr_line("打开飞牛", extra=" wakeupOrigin=VOICE"))
        self.assertEqual(voice.origin, "VOICE")


class RouterTests(unittest.TestCase):
    def test_playback_commands_are_handled(self):
        router = VoiceRouter({})
        for text in ("播放穿成野神第一部第一集", "暂停", "下一集", "打开飞牛", "音量调大"):
            with self.subTest(text=text):
                handled, _ = router.route(text)
                self.assertTrue(handled)

    def test_native_assistant_domain_is_left_alone(self):
        router = VoiceRouter({})
        for text in ("今天天气怎么样", "现在几点", "讲个笑话", "开灯"):
            with self.subTest(text=text):
                handled, reason = router.route(text)
                self.assertFalse(handled)
                self.assertIn("denied", reason)

    def test_deny_wins_over_allow(self):
        # "打开" is allow-listed but "打开空调" belongs to the home assistant.
        router = VoiceRouter({})
        handled, _ = router.route("打开空调")
        self.assertFalse(handled)

    def test_config_override_lists(self):
        cfg = {"voice": {"listener": {"allow": ["播"], "deny": []}}}
        router = VoiceRouter(cfg)
        self.assertTrue(router.route("播一下")[0])
        self.assertFalse(router.route("暂停")[0])

    def test_prefix_only_mode_is_strict(self):
        cfg = {"voice": {"listener": {"prefix_only": ["电视管家"]}}}
        router = VoiceRouter(cfg)
        self.assertTrue(router.route("电视管家 播放野神")[0])
        self.assertFalse(router.route("播放野神")[0])


class ListenerTests(unittest.TestCase):
    def listener(self, dedup=8):
        cfg = {"adb": {"serial": "fake:5555", "binary": "adb"},
               "voice": {"listener": {"dedup_seconds": dedup}}}
        return VoiceListener(cfg, lambda u: None)

    def test_duplicate_utterance_is_dropped(self):
        listener = self.listener()
        first = Utterance(text="播放野神", request_id="abc12345")
        self.assertFalse(listener._is_duplicate(first))
        self.assertTrue(listener._is_duplicate(first))

    def test_same_text_different_request_is_new(self):
        listener = self.listener()
        listener._is_duplicate(Utterance(text="暂停", request_id="r1aaaaaa"))
        self.assertFalse(listener._is_duplicate(Utterance(text="暂停", request_id="r2bbbbbb")))

    def test_dedup_window_expires(self):
        listener = self.listener(dedup=0.01)
        listener._is_duplicate(Utterance(text="继续", request_id="x"))
        time.sleep(0.05)
        self.assertFalse(listener._is_duplicate(Utterance(text="继续", request_id="x")))


if __name__ == "__main__":
    unittest.main()


class ListenerStabilityTests(unittest.TestCase):
    """Queue bounds, staleness (history replay), persistent dedup, reconnect."""

    def make(self, **overrides):
        import tempfile, os
        self.tmp = tempfile.mkdtemp()
        listener_cfg = {"dedup_seconds": 8, "stale_seconds": 10, "queue_size": 2,
                        "state_file": os.path.join(self.tmp, "voice_state.json")}
        listener_cfg.update(overrides)
        cfg = {"adb": {"serial": "fake:5555", "binary": "adb"},
               "execution": {"state_dir": self.tmp},
               "voice": {"listener": listener_cfg}}
        self.handled = []
        return VoiceListener(cfg, lambda u: self.handled.append(u.text))

    def test_history_replay_is_rejected_as_stale(self):
        listener = self.make()
        listener._started_at = 1000.0
        old = Utterance(text="昨天的旧指令", source_at=900.0)
        fresh = Utterance(text="刚说的话", source_at=1001.0)
        self.assertTrue(listener.is_stale(old))
        self.assertFalse(listener.is_stale(fresh))

    def test_utterance_without_timestamp_is_not_treated_stale(self):
        listener = self.make()
        listener._started_at = 1000.0
        self.assertFalse(listener.is_stale(Utterance(text="无时间戳", source_at=0.0)))

    def test_dedup_survives_restart(self):
        import os
        listener = self.make()
        utterance = Utterance(text="播放穿成野神", request_id="req-000001")
        self.assertFalse(listener._is_duplicate(utterance))
        # 用同一个 state_file 重建监听器，模拟进程重启
        cfg = {"adb": {"serial": "fake:5555", "binary": "adb"},
               "execution": {"state_dir": self.tmp},
               "voice": {"listener": {"dedup_seconds": 8, "stale_seconds": 10,
                                      "state_file": listener._state_path}}}
        second = VoiceListener(cfg, lambda u: None)
        self.assertTrue(second._is_duplicate(utterance),
                        "重启后不应再次执行同一条指令")

    def test_dedup_state_never_contains_transcript(self):
        listener = self.make()
        listener._is_duplicate(Utterance(text="播放穿成野神第一部第一集"))
        raw = listener._state_path.read_text()
        self.assertNotIn("穿成野神", raw)
        self.assertNotIn("播放", raw)

    def test_queue_overflow_keeps_newest_and_counts_drops(self):
        listener = self.make(queue_size=2)
        listener.start()
        try:
            for index in range(5):
                listener._enqueue(Utterance(text=f"命令{index}", source_at=time.time()))
            time.sleep(0.25)
        finally:
            listener.stop()
        self.assertEqual(listener.stats()["dropped"], 3)
        self.assertEqual(self.handled, ["命令3", "命令4"])

    def test_expired_item_is_not_executed(self):
        listener = self.make(queue_size=4)
        listener._started_at = time.time()
        stale = Utterance(text="过期指令", source_at=listener._started_at - 600)
        listener.start()
        try:
            listener._enqueue(stale)
            time.sleep(0.2)
        finally:
            listener.stop()
        self.assertEqual(self.handled, [])
        self.assertEqual(listener.stats()["expired"], 1)

    def test_slow_handler_does_not_block_reader(self):
        """A long task runs on the worker; enqueue must stay non-blocking."""
        import time as _t
        started = _t.monotonic()
        listener = self.make(queue_size=4)
        listener.on_utterance = lambda u: _t.sleep(0.3)
        listener.start()
        try:
            for index in range(3):
                listener._enqueue(Utterance(text=f"慢{index}", source_at=_t.time()))
            elapsed = _t.monotonic() - started
        finally:
            listener.stop()
        self.assertLess(elapsed, 0.28, "入队不应等待任务执行")

    def test_logcat_args_use_epoch_and_start_after_now(self):
        listener = self.make()
        args = listener._logcat_args()
        joined = " ".join(args)
        self.assertIn("-v epoch", joined)
        self.assertIn("-T", joined)

    def test_redacted_view_hides_text(self):
        utterance = Utterance(text="打开飞牛播放新闻")
        view = utterance.redacted()
        self.assertEqual(view["text_len"], len("打开飞牛播放新闻"))
        self.assertNotIn("text", view)
        self.assertNotIn("飞牛", str(view))


class IntentCombinationTests(unittest.TestCase):
    """Routing must combine app + action + object instead of matching substrings."""

    def setUp(self):
        self.router = VoiceRouter({})

    def test_home_automation_is_never_taken_even_with_media_verb(self):
        for text in ("关闭客厅的灯", "打开空调", "把卧室灯调暗", "开窗帘", "关掉热水器"):
            with self.subTest(text=text):
                handled, reason = self.router.route(text)
                self.assertFalse(handled, reason)
                self.assertIn("home", reason)

    def test_media_playback_of_news_is_taken_despite_info_noun(self):
        for text in ("打开B站播放新闻", "播放新闻", "在飞牛搜索新闻"):
            with self.subTest(text=text):
                handled, _ = self.router.route(text)
                self.assertTrue(handled)

    def test_bare_title_playback_without_object_word_is_taken(self):
        for text in ("播放穿成野神第一部第一集", "放野神第一部", "看山海归途"):
            with self.subTest(text=text):
                self.assertTrue(self.router.route(text)[0])

    def test_open_verb_alone_needs_app_or_media_object(self):
        self.assertTrue(self.router.route("打开飞牛")[0])
        self.assertTrue(self.router.route("打开B站")[0])
        self.assertFalse(self.router.route("打开设置")[0])
        self.assertFalse(self.router.route("打开关于本机")[0])

    def test_directional_fang_is_not_playback(self):
        self.assertFalse(self.router.route("放在桌上的手机")[0])
        self.assertFalse(self.router.route("放到第3集")[0])
        self.assertTrue(self.router.route("放野神第一部")[0])

    def test_info_and_smalltalk_stay_native(self):
        for text in ("今天天气怎么样", "现在几点", "讲个笑话", "念首古诗"):
            with self.subTest(text=text):
                handled, reason = self.router.route(text)
                self.assertFalse(handled)
                self.assertIn("denied", reason)

    def test_classify_exposes_domain_for_diagnostics(self):
        intent = self.router.classify("打开B站播放新闻")
        self.assertEqual(intent.app, "b站")
        self.assertEqual(intent.obj, "新闻")
        self.assertEqual(intent.domain, "media")
        self.assertTrue(intent.handle)

    def test_conflict_prone_detection(self):
        self.assertTrue(VoiceRouter.conflict_prone("暂停"))
        self.assertTrue(VoiceRouter.conflict_prone("打开B站"))
        self.assertFalse(VoiceRouter.conflict_prone("现在播放的是什么"))


class ReconnectTests(unittest.TestCase):
    """The reader must survive logcat exits and reconnect with backoff."""

    def test_reconnects_after_logcat_exits(self):
        import os, tempfile, threading, time
        tmp = tempfile.mkdtemp()
        # 一个"假 adb"：每次被调用输出一行 ASR 后退出，用于验证重连。
        script = os.path.join(tmp, "fake_adb.sh")
        line = asr_line("播放穿成野神")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\n"
                     f"printf '%s\\n' '{line}'\n")
        os.chmod(script, 0o755)

        handled = []
        events = []
        cfg = {"adb": {"serial": "", "binary": script},
               "execution": {"state_dir": tmp},
               "voice": {"listener": {"dedup_seconds": 60, "stale_seconds": 60,
                                      "backoff_min": 0.05, "backoff_max": 0.2,
                                      "state_file": os.path.join(tmp, "v.json")}}}
        listener = VoiceListener(cfg, lambda u: handled.append(u.text),
                                 on_event=events.append)
        thread = threading.Thread(target=listener.run_forever, daemon=True)
        thread.start()
        time.sleep(0.9)
        listener.stop()
        thread.join(timeout=5)
        connects = [e for e in events if e.get("kind") == "listener_connected"]
        self.assertGreaterEqual(len(connects), 2, "应至少重连一次")
        # 同一内容只执行一次（去重），不会因重连重复触发
        self.assertLessEqual(len(handled), 1)


class SpeechFillerRoutingTests(unittest.TestCase):
    """Spoken fillers must not change routing decisions."""

    def setUp(self):
        self.router = VoiceRouter({})

    def test_fillers_do_not_break_control_commands(self):
        for text in ("继续吧", "暂停啊", "下一集呀", "音量调大呢"):
            with self.subTest(text=text):
                self.assertTrue(self.router.route(text)[0])

    def test_fillers_do_not_break_media_requests(self):
        for text in ("播放穿成野神吧", "打开B站吧", "小电视播放呀"):
            with self.subTest(text=text):
                self.assertTrue(self.router.route(text)[0])

    def test_pleasantries_stay_native(self):
        for text in ("谢谢", "好的", "嗯嗯"):
            with self.subTest(text=text):
                self.assertFalse(self.router.route(text)[0])


class QueueAccountingTests(unittest.TestCase):
    """Dropped/expired items must settle queue accounting, not leak counters."""

    def make(self, **overrides):
        import os, tempfile
        self.tmp = tempfile.mkdtemp()
        listener_cfg = {"dedup_seconds": 8, "stale_seconds": 10, "queue_size": 2,
                        "state_file": os.path.join(self.tmp, "voice_state.json")}
        listener_cfg.update(overrides)
        cfg = {"adb": {"serial": "fake:5555", "binary": "adb"},
               "execution": {"state_dir": self.tmp},
               "voice": {"listener": listener_cfg}}
        self.handled = []
        return VoiceListener(cfg, lambda u: self.handled.append(u.text))

    def test_overflow_does_not_leak_unfinished_tasks(self):
        listener = self.make(queue_size=1)
        listener.start()
        try:
            for index in range(6):
                listener._enqueue(Utterance(text=f"命令{index}", source_at=time.time()))
            time.sleep(0.3)
            listener.stop()
        finally:
            pass
        self.assertEqual(listener._queue.unfinished_tasks, 0,
                         "丢弃的任务必须调用 task_done")

    def test_drain_settles_all_branches(self):
        listener = self.make(queue_size=4)
        listener._started_at = time.time()
        fresh = Utterance(text="正常", source_at=time.time())
        stale = Utterance(text="过期", source_at=time.time() - 600)
        listener._enqueue(fresh)
        listener._enqueue(stale)
        listener._drain()
        self.assertEqual(listener._queue.unfinished_tasks, 0)
        self.assertEqual(listener._queue.qsize(), 0)
        self.assertEqual(self.handled, ["正常"])
        self.assertEqual(listener.stats()["expired"], 1)

    def test_worker_survives_handler_exception(self):
        listener = self.make(queue_size=4)

        def boom(utterance):
            raise RuntimeError("handler failed")

        listener.on_utterance = boom
        listener.start()
        try:
            listener._enqueue(Utterance(text="一", source_at=time.time()))
            listener._enqueue(Utterance(text="二", source_at=time.time()))
            time.sleep(0.3)
        finally:
            listener.stop()
        self.assertEqual(listener._queue.unfinished_tasks, 0)


class TimestampAnomalyTests(unittest.TestCase):
    def make(self):
        import os, tempfile
        tmp = tempfile.mkdtemp()
        cfg = {"adb": {"serial": "", "binary": "adb"},
               "execution": {"state_dir": tmp},
               "voice": {"listener": {"state_file": os.path.join(tmp, "v.json")}}}
        return VoiceListener(cfg, lambda u: None)

    def test_history_is_stale(self):
        listener = self.make()
        listener._started_at = 1000.0
        self.assertTrue(listener.is_stale(Utterance(text="旧", source_at=900.0)))

    def test_far_future_timestamp_is_not_trusted_but_not_dropped_as_stale(self):
        listener = self.make()
        listener._started_at = 1000.0
        self.assertFalse(listener.is_stale(Utterance(text="未来", source_at=99999.0)))

    def test_missing_timestamp_is_not_stale(self):
        listener = self.make()
        listener._started_at = 1000.0
        self.assertFalse(listener.is_stale(Utterance(text="无时间", source_at=0.0)))

    def test_parses_epoch_and_rejects_garbage(self):
        good = parse_log_line(asr_line("打开飞牛").replace(
            "I =vc=", "         1794378472.982  1    1 I =vc="))
        self.assertIsNotNone(good)
        self.assertAlmostEqual(good.source_at, 1794378472.982)
        garbage = parse_log_line(asr_line("打开飞牛").replace(
            "I =vc=", "         not-a-number  1    1 I =vc="))
        self.assertIsNotNone(garbage)
        self.assertEqual(garbage.source_at, 0.0)


class VoiceEventLogTests(unittest.TestCase):
    """Routing decisions must be auditable without reading container stdout."""

    def make(self):
        import os, tempfile
        tmp = tempfile.mkdtemp()
        cfg = {"adb": {"serial": "", "binary": "adb"},
               "execution": {"state_dir": tmp},
               "voice": {"listener": {"state_file": os.path.join(tmp, "v.json")}}}
        return VoiceListener(cfg, lambda u: None)

    def test_event_record_contains_no_transcript(self):
        import json
        listener = self.make()
        utterance = Utterance(text="打开飞牛播放新闻", request_id="abc")
        listener._record_event(utterance, "media:app", True, True)
        line = listener._events_path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        self.assertEqual(record["route"], "media:app")
        self.assertTrue(record["handled"])
        self.assertTrue(record["conflict_risk"])
        self.assertEqual(record["len"], len("打开飞牛播放新闻"))
        self.assertNotIn("飞牛", line)
        self.assertFalse(listener.worker_status(listener.cfg)["online"])
