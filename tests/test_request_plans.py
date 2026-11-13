"""Finite request planning regressions: no device or model access."""
import unittest

from adbtv.intent import Intent, number, parse_fast, parse_request
from adbtv.verifier import GoalSpec

CFG = {"apps": {
    "feiniu_tv": {"package": "com.trim.tv", "label": "飞牛TV"},
    "bilibili": {"package": "tv.bilibili", "label": "哔哩哔哩TV/云视听小电视"},
    "my_app": {"package": "my.app", "label": "自定义电视"},
    "iqiyi": {"package": "tv.iqiyi", "label": "银河奇异果"},
}}


class RequestPlanTests(unittest.TestCase):
    def parse(self, instruction):
        return parse_request(instruction, CFG)

    def assert_unknown(self, instruction):
        goal = self.parse(instruction)
        self.assertEqual(goal.action, "unknown", (instruction, goal))
        self.assertTrue(goal.reason)
        self.assertEqual(goal.steps, ())

    def test_preserves_title_case_season_episode(self):
        goal = self.parse("请在飞牛播放《NASA 与 AI》第二部第一百零三集")
        self.assertEqual((goal.action, goal.package, goal.title, goal.season, goal.episode),
                         ("play", "com.trim.tv", "NASA 与 AI", 2, 103))

    def test_polite_app_prefix(self):
        for text in ("请在飞牛播放《Dune》", "帮我用飞牛播放《Dune》", "麻烦你打开飞牛播放《Dune》"):
            with self.subTest(text=text):
                goal = self.parse(text)
                self.assertEqual((goal.action, goal.title, goal.package), ("play", "Dune", "com.trim.tv"))

    def test_app_names_alias_labels_known_names(self):
        for name, package in (("飞牛", "com.trim.tv"), ("FEINIU", "com.trim.tv"),
                              ("B站", "tv.bilibili"), ("bilibili", "tv.bilibili"),
                              ("云视听小电视", "tv.bilibili"), ("my_app", "my.app"),
                              ("自定义电视", "my.app")):
            with self.subTest(name=name):
                self.assertEqual(self.parse("打开" + name), GoalSpec("open", package=package))

    def test_no_default_app_invented(self):
        self.assertEqual(self.parse("播放《Dune》").package, "")
        self.assertEqual(parse_request("播放《Dune》", {}).package, "")
        self.assertEqual(parse_request("打开飞牛", {}).action, "unknown")

    def test_search_only_negation(self):
        for text in ("在飞牛搜索《Dune》，不要播放", "只搜索《Dune》", "播放《Dune》但不要播放",
                     "不要播放《Dune》，只搜索", "搜索《Dune》不播放",
                     "搜索《Dune》，然后不要播放", "搜索《Dune》然后不要播放"):
            with self.subTest(text=text):
                goal = self.parse(text)
                self.assertEqual((goal.action, goal.title, goal.query), ("search", "Dune", "Dune"))

    def test_search_only_unquoted(self):
        goal = self.parse("在飞牛搜索野神，不要播放")
        self.assertEqual((goal.action, goal.title, goal.query), ("search", "野神", "野神"))

    def test_queries_are_read_only(self):
        for text, action in (("现在播放的是什么？", "query_media"), ("当前在哪个应用？", "query_app"),
                             ("当前应用是什么", "query_app"), ("正在播放什么", "query_media")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, action)

    def test_quoted_connectors_do_not_split(self):
        goal = self.parse("播放《然后，并且再暂停，返回 NASA》第二季第三集然后音量加")
        self.assertEqual(goal.action, "sequence")
        self.assertEqual(len(goal.steps), 2)
        self.assertEqual(goal.steps[0].title, "然后，并且再暂停，返回 NASA")
        self.assertEqual((goal.steps[0].season, goal.steps[0].episode), (2, 3))
        self.assertEqual(goal.steps[1].action, "volume_up")

    def test_multiple_steps_preserve_order_and_inherit_app(self):
        goal = self.parse("打开飞牛，然后播放《Dune》，再调大一点声音，并且暂停")
        self.assertEqual(goal.action, "sequence")
        self.assertEqual([g.action for g in goal.steps], ["open", "play", "volume_up", "pause"])
        self.assertTrue(all(g.package == "com.trim.tv" for g in goal.steps))

    def test_application_inheritance_updates(self):
        goal = self.parse("打开飞牛然后播放《A》然后打开B站然后播放《B》然后暂停")
        self.assertEqual(goal.action, "sequence")
        self.assertEqual([g.package for g in goal.steps],
                         ["com.trim.tv", "com.trim.tv", "tv.bilibili", "tv.bilibili", "tv.bilibili"])

    def test_unknown_suffix_invalidates_entire_plan(self):
        for text in ("打开飞牛然后帮我订披萨", "播放《Dune》然后下载字幕", "播放《Dune》并且定时关机",
                     "播放Dune然后做未知事情", "播放《Dune》以后不知道", "播放Dune十分钟", "暂停十分钟"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_no_swallowed_unquoted_action_or_timer(self):
        for text in ("播放野神调大音量", "播放野神关机", "播放野神每天十点", "播放野神五分钟",
                     "播放野神后暂停", "播放野神但是不要切台", "播放野神直到我回来"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_delay_never_executes_child_immediately(self):
        goal = self.parse("十分钟后暂停")
        self.assertEqual((goal.action, goal.seconds), ("delay", 600))
        self.assertEqual(goal.steps, (GoalSpec("pause"),))
        self.assertEqual(self.parse("等十分钟后暂停"), goal)

    def test_implicit_play_then_delay(self):
        goal = self.parse("在飞牛播放《X》十分钟后暂停")
        self.assertEqual(goal.action, "sequence")
        self.assertEqual([g.action for g in goal.steps], ["play", "delay"])
        self.assertEqual(goal.steps[0].title, "X")
        self.assertEqual(goal.steps[1].seconds, 600)
        self.assertEqual(goal.steps[1].steps, (GoalSpec("pause", package="com.trim.tv"),))

    def test_explicit_delay_inherits_app(self):
        goal = self.parse("打开飞牛然后两分钟后暂停")
        self.assertEqual(goal.steps[1].steps[0].package, "com.trim.tv")
        self.assertEqual(goal.steps[1].seconds, 120)

    def test_timed_words_inside_title_are_opaque(self):
        goal = self.parse("播放《十分钟后暂停》")
        self.assertEqual((goal.action, goal.title), ("play", "十分钟后暂停"))

    def test_invalid_delays_are_unknown(self):
        for text in ("零分钟后暂停", "十十分钟后暂停", "十分钟后定时关机", "明天十点暂停", "每天十点播放《X》"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_basic_controls(self):
        for text, action in (("暂停", "pause"), ("继续", "resume"), ("回首页", "home"),
                             ("返回", "back"), ("下一集", "next"), ("上一集", "previous")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, action)

    def test_volume_delta(self):
        self.assertEqual((self.parse("音量加三格").action, self.parse("音量加三格").delta), ("volume_up", 3))
        self.assertEqual((self.parse("声音减2级").action, self.parse("声音减2级").delta), ("volume_down", 2))
        self.assert_unknown("音量加零格")

    def test_relative_and_absolute_seek(self):
        for text, action, seconds in (("快进30秒", "seek", 30), ("后退十秒", "seek", -10),
                                      ("快退两分钟", "seek", -120), ("跳到第5分钟", "seek_to", 300),
                                      ("跳转到一小时", "seek_to", 3600)):
            with self.subTest(text=text):
                self.assertEqual((self.parse(text).action, self.parse(text).seconds), (action, seconds))

    def test_episode_up_to_999(self):
        for text, expected in (("第十集", 10), ("播放第一百零一集", 101), ("第九百九十九集", 999),
                               ("第999集", 999)):
            with self.subTest(text=text):
                self.assertEqual((self.parse(text).action, self.parse(text).episode), ("episode", expected))
        for text in ("第0集", "第一千集", "第1000集", "第十十集"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_restart_and_resume_policy(self):
        for text in ("从头播放", "从头播放《Dune》", "播放《Dune》从头播放"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).policy, "restart")
        self.assertEqual(self.parse("继续刚才那部").action, "resume")
        self.assertEqual(self.parse("继续播放《Dune》").policy, "resume")

    def test_genre_is_not_exact_title(self):
        for text in ("找个科技视频", "播放一个短剧", "播放科技视频"):
            with self.subTest(text=text):
                goal = self.parse(text)
                self.assertEqual(goal.action, "play")
                self.assertEqual(goal.title, "")
                self.assertTrue(goal.query)
        goal = self.parse("找个《明确片名》")
        self.assertEqual((goal.action, goal.title), ("play", "明确片名"))

    def test_baseline_demo_genre_play_suffix(self):
        goal = self.parse("打开哔哩哔哩，找个科技视频播放")
        self.assertEqual(goal.action, "sequence")
        self.assertEqual(goal.steps[0], GoalSpec("open", package="tv.bilibili"))
        self.assertEqual((goal.steps[1].action, goal.steps[1].package, goal.steps[1].title, goal.steps[1].query),
                         ("play", "tv.bilibili", "", "科技"))

    def test_quoted_find_play_suffix_preserves_identity(self):
        goal = self.parse("在哔哩哔哩找个《明确片名》播放")
        self.assertEqual((goal.action, goal.package, goal.title), ("play", "tv.bilibili", "明确片名"))
        goal = self.parse("在爱奇艺播放美食节目")
        self.assertEqual((goal.action, goal.package, goal.title), ("play", "tv.iqiyi", "美食节目"))

    def test_invalid_quote_or_trailing_requirements(self):
        for text in ("播放《X", "播放《》", "播放《X》《Y》", "播放《X》但是音量小一点", "播放《X》一些额外要求"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_season_episode_not_silently_dropped(self):
        self.assertEqual((self.parse("播放野神第一部第二集").season,
                          self.parse("播放野神第一部第二集").episode), (1, 2))
        for text in ("播放《X》第二季第三季", "播放《X》第零集", "播放《X》第二集第三集"):
            with self.subTest(text=text):
                self.assert_unknown(text)

    def test_number_validation_and_hundreds(self):
        for text, expected in (("零", 0), ("两", 2), ("十", 10), ("十一", 11), ("二十", 20),
                               ("一百", 100), ("一百零二", 102), ("一百二十", 120), ("两百三十一", 231),
                               ("九百九十九", 999), ("999", 999)):
            with self.subTest(text=text):
                self.assertEqual(number(text), expected)
        for text in ("", "垃圾", "十十", "一百二", "零十", "1.5", "-1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                number(text)

    def test_legacy_intent_remains_available(self):
        self.assertIsInstance(parse_fast("打开飞牛"), Intent)
        self.assertEqual(parse_fast("暂停").key, "pause")
        goal = parse_fast("打开飞牛播放《野神》第二季第一百零一集")
        self.assertEqual((goal.title, goal.season, goal.episode), ("野神", 2, 101))
        self.assertIsNone(parse_fast("飞牛播放《野神》第十十集"))
        self.assertEqual(parse_fast("打开FEINIU播放《NASA》").title, "NASA")

    def test_empty_or_unsupported_instructions(self):
        for text in ("", " ", "未知", "打开不存在的应用", "不要暂停", "播放《X》然后", "然后暂停"):
            with self.subTest(text=text):
                self.assert_unknown(text)


if __name__ == "__main__":
    unittest.main()


class CommaTitleTests(unittest.TestCase):
    """Titles may contain commas; only a following action makes it a split."""

    def setUp(self):
        from adbtv.config import DEFAULTS
        import copy
        self.cfg = copy.deepcopy(DEFAULTS)

    def test_comma_inside_title_is_preserved(self):
        from adbtv.intent import parse_request
        goal = parse_request("播放我，史前霸王龙第一集", self.cfg)
        self.assertEqual(goal.action, "play")
        self.assertEqual(goal.title, "我，史前霸王龙")
        self.assertEqual(goal.episode, 1)

    def test_comma_followed_by_action_still_splits(self):
        from adbtv.intent import parse_request
        goal = parse_request("暂停，然后播放野神第一集", self.cfg)
        self.assertEqual(goal.action, "sequence")
        self.assertEqual([s.action for s in goal.steps], ["pause", "play"])

    def test_demo_phrase_still_opens_then_plays(self):
        from adbtv.intent import parse_request
        goal = parse_request("打开哔哩哔哩，找个科技视频播放", self.cfg)
        self.assertEqual(goal.action, "sequence")
        self.assertEqual([s.action for s in goal.steps], ["open", "play"])


class SpeechFillerTests(unittest.TestCase):
    """Spoken input carries fillers and soft verbs; they must not break parsing."""

    def setUp(self):
        import copy
        from adbtv.config import DEFAULTS
        self.cfg = copy.deepcopy(DEFAULTS)

    def parse(self, text):
        from adbtv.intent import parse_request
        return parse_request(text, self.cfg)

    def test_trailing_fillers_are_ignored(self):
        for text, action in (("打开飞牛吧", "open"), ("暂停吧", "pause"),
                             ("音量调大呀", "volume_up"), ("打开B站呢", "open")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, action)

    def test_filler_never_becomes_a_title(self):
        goal = self.parse("B站播放吧")
        self.assertEqual(goal.action, "play")
        self.assertEqual(goal.title, "")

    def test_soft_verbs_are_normalized(self):
        self.assertEqual(self.parse("小电视播放").action, "play")
        self.assertEqual(self.parse("搜索一下飞牛").action, "search")
        goal = self.parse("播放穿成野神第一部第一集吧")
        self.assertEqual(goal.title, "穿成野神")
        self.assertEqual((goal.season, goal.episode), (1, 1))

    def test_open_with_bare_browse_verb_is_open(self):
        for text in ("打开飞牛看看", "打开B站瞧瞧", "打开飞牛瞅瞅"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).action, "open")

    def test_open_plus_real_action_still_plays(self):
        goal = self.parse("打开飞牛播放短剧")
        self.assertEqual(goal.action, "play")
        self.assertEqual(goal.package, "com.trim.tv")
