"""End-to-end without network: fake gallery-dl + fake Telegram bot."""
import asyncio
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings  # noqa: E402
from downloader import DownloadError, Downloader, ErrorKind, classify_error, post_info  # noqa: E402
from linkparser import parse_message  # noqa: E402
from bot import deliver_stream  # noqa: E402
from downloader import Media  # noqa: E402
from sender import ChatActionKeeper, _input_media, build_post_caption, send_items  # noqa: E402
from telegram import InputFile  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

FAKE = """#!{py}
import json, sys
from pathlib import Path
a = sys.argv[1:]
d = Path(a[a.index("-D") + 1]); url = a[-1]
mode = "{mode}"
import time
if mode == "fail":
    print("[instagram][error] AuthRequired: 'authenticated cookies' needed", file=sys.stderr); sys.exit(4)
if mode == "empty":
    sys.exit(0)
if mode in ("many", "slow"):
    for i in range(1, 24 if mode == "many" else 400):
        f = d / f"twitter_{{i}}_1.{{'mp4' if i == 1 else 'jpg'}}"
        f.write_bytes(b"x" * 100)
        time.sleep(0.02 if mode == "many" else 0.1)
        Path(str(f) + ".json").write_text(json.dumps({{"tweet_id": i, "author": {{"name": "jack"}}, "content": ""}}))
    sys.exit(0)
n = 3 if "media" in url or "instagram.com/user" in url else 2
for i in range(1, n + 1):
    ext = "mp4" if i == 1 else "jpg"
    f = d / f"twitter_111_{{i}}.{{ext}}"
    f.write_bytes(b"x" * 100)
    meta = {{"tweet_id": 111, "num": i, "author": {{"name": "jack"}}, "content": "hello & <world> https://t.co/abc",
             "width": 640, "height": 480}}
    Path(str(f) + ".json").write_text(json.dumps(meta))
"""


class FakeBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def rec(**kw):
            self.calls.append((name, kw))
        return rec


def make_settings(tmp: Path, mode: str) -> Settings:
    fake = tmp / "fake-gallery-dl"
    fake.write_text(FAKE.format(py=sys.executable, mode=mode))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp / "tmp").mkdir()
    return Settings(
        bot_token="x", allowed_ids=(1,), download_timeout=20, profile_idle_timeout=20, max_concurrent=1, request_delay=0, max_upload_bytes=50 * 1024 * 1024,
        local_api_url=None, gallery_dl_bin=fake, gallery_dl_conf=tmp / "c.conf",
        cookies_dir=tmp / "cookies", tmp_dir=tmp / "tmp", log_dir=tmp / "logs",
    )


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_flow(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_settings(Path(t), "ok")
            dl = Downloader(s)
            link = parse_message("https://x.com/jack/status/111")
            job = dl.new_job(1)
            bot = FakeBot()
            async with dl.download(link, job) as res:
                self.assertEqual([m.kind for m in res.items], ["video", "photo"])
                user, cap, url = post_info(res.items[0].meta, link)
                self.assertEqual((user, cap, url), ("jack", "hello & <world>", "https://x.com/jack/status/111"))
                html_cap, overflow = build_post_caption(cap, user, "https://x.com/jack", url)
                self.assertIn("&amp; &lt;world&gt;", html_cap)
                self.assertIn('by: <a href="https://x.com/jack">jack</a>', html_cap)
                async with ChatActionKeeper(bot, 1) as keeper:
                    await asyncio.sleep(0.05)  # let the keeper loop fire once
                    st = await send_items(bot, 1, res.items, html_cap, "first", keeper, job, s.max_upload_bytes)
                workdir = res.items[0].path.parent
            self.assertEqual(st.sent, 2)
            names = [c[0] for c in bot.calls if c[0] != "send_chat_action"]
            self.assertEqual(names, ["send_media_group"])
            media = [c for c in bot.calls if c[0] == "send_media_group"][0][1]["media"]
            self.assertEqual(media[0].caption, html_cap)
            self.assertIsNone(media[1].caption)
            self.assertFalse(workdir.exists(), "tmp dir must be removed")
            self.assertTrue(any(c[0] == "send_chat_action" for c in bot.calls))

    async def test_profile_has_no_range(self):
        with tempfile.TemporaryDirectory() as t:
            dl = Downloader(make_settings(Path(t), "ok"))
            for url in ("https://x.com/jack", "https://www.instagram.com/someone/",
                        "https://www.instagram.com/stories/someone/", "https://x.com/jack/status/1"):
                cmd = dl._build_cmd(parse_message(url), Path("/w"))
                self.assertNotIn("--range", cmd, url)

    async def test_errors(self):
        for mode, kind in (("fail", ErrorKind.AUTH), ("empty", ErrorKind.EMPTY)):
            with tempfile.TemporaryDirectory() as t:
                dl = Downloader(make_settings(Path(t), mode))
                job = dl.new_job(1)
                with self.assertRaises(DownloadError) as cm:
                    async with dl.download(parse_message("https://x.com/jack/status/1"), job):
                        pass
                self.assertEqual(cm.exception.kind, kind)
                self.assertEqual(list((Path(t) / "tmp").iterdir()), [], "tmp must be clean after error")

    async def test_stream_batches_and_cleanup(self):
        Downloader.POLL = 0.1
        with tempfile.TemporaryDirectory() as t:
            dl = Downloader(make_settings(Path(t), "many"))
            job = dl.new_job(1)
            seen = []

            async def on_batch(batch):
                self.assertTrue(all(m.path.is_file() for m in batch))
                self.assertTrue(all(m.meta.get("tweet_id") for m in batch), "metadata sidecar must be loaded")
                seen.append(len(batch))

            res = await dl.stream(parse_message("https://x.com/jack"), job, on_batch)
            self.assertEqual(seen, [10, 10, 3])
            self.assertEqual(res.total, 23)
            self.assertEqual(list((Path(t) / "tmp").iterdir()), [], "tmp must be empty at the end")

    async def test_stream_username_only_on_first_album(self):
        Downloader.POLL = 0.1
        with tempfile.TemporaryDirectory() as t:
            s = make_settings(Path(t), "many")
            dl = Downloader(s)
            job = dl.new_job(1)
            bot = FakeBot()
            async with ChatActionKeeper(bot, 1) as keeper:
                await deliver_stream(bot, 1, parse_message("https://x.com/jack"), keeper, job, s, dl)
            groups = [c[1]["media"] for c in bot.calls if c[0] == "send_media_group"]
            self.assertEqual([len(g) for g in groups], [10, 10, 3])
            self.assertEqual(groups[0][0].caption, '<a href="https://x.com/jack">jack</a>')
            self.assertIsNone(groups[1][0].caption)
            self.assertIsNone(groups[2][0].caption)

    async def test_stream_cancel_while_paused_sending(self):
        Downloader.POLL = 0.1
        with tempfile.TemporaryDirectory() as t:
            dl = Downloader(make_settings(Path(t), "slow"))
            job = dl.new_job(1)
            procs = []

            async def on_batch(batch):
                procs.append(job.proc)           # gallery-dl is SIGSTOPped right now
                await asyncio.sleep(0.5)
                dl.cancel(1)                     # /cancel while paused: kill must still work
                await asyncio.sleep(0.2)

            with self.assertRaises(DownloadError) as cm:
                await asyncio.wait_for(dl.stream(parse_message("https://x.com/jack"), job, on_batch), 30)
            self.assertEqual(cm.exception.kind, ErrorKind.CANCELLED)
            self.assertIsNotNone(procs[0].returncode, "gallery-dl must be dead")
            self.assertEqual(list((Path(t) / "tmp").iterdir()), [])

    async def test_stream_empty_error(self):
        with tempfile.TemporaryDirectory() as t:
            dl = Downloader(make_settings(Path(t), "empty"))

            async def on_batch(batch):
                self.fail("no batch expected")

            with self.assertRaises(DownloadError) as cm:
                await dl.stream(parse_message("https://www.instagram.com/stories/someone/"), dl.new_job(1), on_batch)
            self.assertEqual(cm.exception.kind, ErrorKind.NO_STORIES)

    async def _send(self, bot, kinds, tmp):
        items = []
        for n, k in enumerate(kinds):
            f = tmp / f"f{n}.bin"
            f.write_bytes(b"x" * 10)
            items.append(Media(f, k, "1", {}))
        class J:  # minimal job stand-in
            cancelled = False
        async with ChatActionKeeper(bot, 1) as keeper:
            return await send_items(bot, 1, items, "cap", "first", keeper, J(), 50 * 1024 * 1024)

    @staticmethod
    def _calls(bot):
        return [c for c in bot.calls if c[0] != "send_chat_action"]

    async def test_everything_goes_in_albums(self):
        with tempfile.TemporaryDirectory() as t:
            bot = FakeBot()
            # 9 photos + 1 GIF + 2 documents: one visual album (GIF rides along) + one document album
            await self._send(bot, ["photo"] * 9 + ["animation"] + ["document"] * 2, Path(t))
            calls = self._calls(bot)
            self.assertEqual([c[0] for c in calls], ["send_media_group", "send_media_group"])
            self.assertEqual([len(c[1]["media"]) for c in calls], [10, 2])
            self.assertEqual(calls[0][1]["media"][0].caption, "cap")
            self.assertIsNone(calls[1][1]["media"][0].caption)

    async def test_albums_of_ten_then_remainder(self):
        with tempfile.TemporaryDirectory() as t:
            bot = FakeBot()
            await self._send(bot, ["photo"] * 12, Path(t))
            self.assertEqual([len(c[1]["media"]) for c in self._calls(bot)], [10, 2])
        with tempfile.TemporaryDirectory() as t:
            bot = FakeBot()
            await self._send(bot, ["photo"] * 11, Path(t))  # 10 + 1 (the 1 goes as a single message)
            self.assertEqual([c[0] for c in self._calls(bot)], ["send_media_group", "send_photo"])

    async def test_bad_file_falls_back_to_document_album(self):
        class Picky(FakeBot):
            async def send_media_group(self, **kw):
                self.calls.append(("send_media_group", kw))
                if type(kw["media"][0]).__name__ != "InputMediaDocument":
                    raise BadRequest("PHOTO_INVALID_DIMENSIONS")
        with tempfile.TemporaryDirectory() as t:
            bot = Picky()
            await self._send(bot, ["photo", "photo", "video"], Path(t))
            calls = self._calls(bot)
            self.assertEqual([c[0] for c in calls], ["send_media_group", "send_media_group"])  # never one-by-one
            self.assertEqual(type(calls[1][1]["media"][0]).__name__, "InputMediaDocument")

    def test_album_items_are_uploaded_not_file_uris(self):
        # regression: InputMedia(Path) becomes file:///... -> "unsupported url protocol" on the cloud API
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.jpg"
            f.write_bytes(b"x" * 10)
            for kind, as_doc in (("photo", False), ("video", False), ("animation", False), ("document", False), ("photo", True)):
                im = _input_media(Media(f, kind, "1", {}), "cap", as_document=as_doc)
                self.assertIsInstance(im.media, InputFile, (kind, as_doc))
                self.assertTrue(im.media.attach_uri, "must be attach://... multipart reference")

    def test_classify(self):
        self.assertEqual(classify_error("[twitter][error] NotFoundError: 404"), ErrorKind.NOT_FOUND)
        self.assertEqual(classify_error("[x][error] HttpError: 429 Too Many Requests"), ErrorKind.RATE_LIMIT)
        self.assertEqual(classify_error("[x][error] AuthorizationError: private"), ErrorKind.PRIVATE)

    def test_long_caption_truncates(self):
        html_cap, full = build_post_caption("a" * 3000, "u", "https://x.com/u", "https://x.com/u/status/1")
        self.assertLessEqual(len(html_cap), 1024)
        self.assertTrue(full)


if __name__ == "__main__":
    unittest.main()
