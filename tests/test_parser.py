import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linkparser import Kind, Platform, parse_message  # noqa: E402


class ParserTests(unittest.TestCase):
    def check(self, text, platform, kind, url, username):
        link = parse_message(text)
        self.assertIsNotNone(link, text)
        self.assertEqual((link.platform, link.kind, link.url, link.username),
                         (platform, kind, url, username), text)

    def test_twitter_posts(self):
        T, P = Platform.TWITTER, Kind.POST
        self.check("https://x.com/jack/status/20", T, P, "https://x.com/jack/status/20", "jack")
        self.check("https://twitter.com/jack/status/20?s=46&t=abc", T, P, "https://x.com/jack/status/20", "jack")
        self.check("https://mobile.twitter.com/jack/status/20/photo/1", T, P, "https://x.com/jack/status/20", "jack")
        self.check("look https://x.com/i/web/status/20 wow", T, P, "https://x.com/i/web/status/20", None)

    def test_twitter_profiles(self):
        T, F = Platform.TWITTER, Kind.PROFILE
        self.check("https://x.com/jack", T, F, "https://x.com/jack/media", "jack")
        self.check("https://twitter.com/jack/media", T, F, "https://x.com/jack/media", "jack")
        self.assertIsNone(parse_message("https://x.com/explore"))
        self.assertIsNone(parse_message("https://x.com/home"))

    def test_instagram_posts(self):
        I, P = Platform.INSTAGRAM, Kind.POST
        self.check("https://www.instagram.com/p/ABC123/?igsh=xyz", I, P, "https://www.instagram.com/p/ABC123/", None)
        self.check("https://instagram.com/reel/ABC123/", I, P, "https://www.instagram.com/reel/ABC123/", None)
        self.check("https://www.instagram.com/reels/ABC123/", I, P, "https://www.instagram.com/reel/ABC123/", None)
        self.check("https://www.instagram.com/someone/reel/ABC123/", I, P, "https://www.instagram.com/reel/ABC123/", "someone")
        self.check("https://www.instagram.com/stories/someone/3456/", I, P, "https://www.instagram.com/stories/someone/3456/", "someone")

    def test_instagram_other(self):
        I = Platform.INSTAGRAM
        self.check("https://www.instagram.com/someone/", I, Kind.PROFILE, "https://www.instagram.com/someone/", "someone")
        self.check("https://www.instagram.com/some.one_/reels/", I, Kind.PROFILE, "https://www.instagram.com/some.one_/reels/", "some.one_")
        self.check("https://www.instagram.com/stories/someone/", I, Kind.STORIES, "https://www.instagram.com/stories/someone/", "someone")
        self.check("https://www.instagram.com/stories/highlights/1789/", I, Kind.HIGHLIGHT, "https://www.instagram.com/stories/highlights/1789/", None)
        self.assertIsNone(parse_message("https://www.instagram.com/explore/"))
        self.assertIsNone(parse_message("https://www.instagram.com/accounts/login/"))

    def test_ignored(self):
        self.assertIsNone(parse_message("hello"))
        self.assertIsNone(parse_message("https://example.com/a/b"))


if __name__ == "__main__":
    unittest.main()
