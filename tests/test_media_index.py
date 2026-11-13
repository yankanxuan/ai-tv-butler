"""Local fnOS media-library index tests (temp tree + real-library smoke)."""
import os
import tempfile
import unittest
from pathlib import Path

from adbtv.media_index import (MediaIndex, parse_show_name, parse_episode,
                               normalize, cn_number)


def make_tree(root: Path):
    # category -> show -> Season NN -> SxxExx
    show = root / "短剧" / "穿成野神我靠香火斩诸神 (2026)" / "Season 01"
    show.mkdir(parents=True)
    (show / "穿成野神我靠香火斩诸神 S01E001.mp4").write_text("a")
    (show / "穿成野神我靠香火斩诸神 S01E002.mp4").write_text("a")
    s10 = root / "短剧" / "穿成野神我靠香火斩诸神第十季 (2026)" / "Season 10"
    s10.mkdir(parents=True)
    (s10 / "穿成野神我靠香火斩诸神第十季 S10E001.mp4").write_text("a")
    (s10 / "穿成野神我靠香火斩诸神第十季 S10E005.mp4").write_text("a")
    other = root / "电影" / "测试电影 (2025)"
    other.mkdir(parents=True)
    (other / "测试电影.mp4").write_text("a")


class MediaIndexTests(unittest.TestCase):
    def test_parse_helpers(self):
        self.assertEqual(parse_show_name("穿成野神我靠香火斩诸神第十季 (2026)")[0], "穿成野神我靠香火斩诸神")
        self.assertEqual(parse_show_name("穿成野神我靠香火斩诸神第十季 (2026)")[1], 10)
        self.assertEqual(parse_show_name("穿成野神我靠香火斩诸神7 (2026)")[1], 7)
        self.assertEqual(parse_show_name("我的右眼是神级计算机：第一季")[1], 1)
        self.assertEqual(parse_episode("剧名 S03E012.mkv"), (3, 12))
        self.assertEqual(parse_episode("第5集.mp4"), (None, 5))
        self.assertEqual(cn_number("十一"), 11)
        self.assertEqual(cn_number("一百零五"), 105)

    def test_scan_search_resolve_across_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            make_tree(tree)
            idx = MediaIndex([str(tree)]).scan()
            self.assertEqual(idx.stats()["series"], 2)  # two distinct shows
            res = idx.search("野神")
            self.assertTrue(res and res[0]["title"] == "穿成野神我靠香火斩诸神")
            s1 = idx.resolve("野神", 1, 1)
            self.assertEqual((s1["season"], s1["episode"]), (1, 1))
            self.assertTrue(s1["path"].endswith("S01E001.mp4"))
            s10 = idx.resolve("野神", 10, 5)
            self.assertEqual((s10["season"], s10["episode"]), (10, 5))
            self.assertTrue(s10["path"].endswith("S10E005.mp4"))
            self.assertIsNone(idx.resolve("不存在的剧"))
            self.assertEqual(idx.search("测试电影")[0]["title"], "测试电影")

    def test_missing_requested_season_or_episode_is_reported_not_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp); make_tree(tree)
            idx = MediaIndex([str(tree)]).scan()
            r = idx.resolve("野神", 5, 1)  # season 5 absent
            self.assertFalse(r["requested_season_found"])
            r2 = idx.resolve("野神", 1, 99)
            self.assertFalse(r2["requested_episode_found"])

    def test_alias_and_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "lib"; tree.mkdir(); make_tree(tree)
            cache = str(Path(tmp) / "idx.json")
            idx = MediaIndex([str(tree)], aliases={"野神": "穿成野神我靠香火斩诸神"}, cache_path=cache).scan()
            idx.save()
            loaded = MediaIndex.load(cache, aliases={"野神": "穿成野神我靠香火斩诸神"})
            self.assertEqual(loaded.stats()["episodes"], idx.stats()["episodes"])
            self.assertEqual(loaded.resolve("野神", 1, 1)["path"], idx.resolve("野神", 1, 1)["path"])

    def test_loose_match_tolerates_repeated_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp); make_tree(tree)
            idx = MediaIndex([str(tree)]).scan()
            # "野野神" repeated char still resolves the series
            self.assertTrue(idx.search("野野神"))

    @unittest.skipUnless(Path("/vol1/1000/影视").is_dir(), "本机无影视库")
    def test_real_library_smoke(self):
        idx = MediaIndex(["/vol1/1000/影视"]).scan()
        self.assertGreater(idx.stats()["series"], 0)
        hits = idx.search("穿成野神")
        if hits:
            r = idx.resolve("穿成野神", 1, 1)
            self.assertTrue(Path(r["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
