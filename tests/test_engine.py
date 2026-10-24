import hashlib
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import kilatdl as k  # noqa: E402
from helpers import Site, blob, run_to_end  # noqa: E402


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.data = blob(2_500_000)
        self.site = Site({"/f.bin": self.data})
        self.old = (k.MIN_SEGMENT, k.LIMITER.rate)
        k.MIN_SEGMENT = 200_000

    def tearDown(self):
        k.MIN_SEGMENT, k.LIMITER.rate = self.old
        self.site.close()
        self.tmp.cleanup()

    def read(self, dl):
        with open(dl.path, "rb") as f:
            return f.read()

    def test_segmented_download_is_identical(self):
        dl = run_to_end(k.Download(self.site.url("/f.bin"), self.dir, segments=5))
        self.assertEqual(dl.status, "done", dl.error)
        self.assertEqual(self.read(dl), self.data)
        self.assertGreater(len({r for _, r in self.site.hits if r}), 1, "expected several ranged requests")

    def test_server_without_range_falls_back_to_stream(self):
        self.site.ranges = False
        dl = run_to_end(k.Download(self.site.url("/f.bin"), self.dir, segments=5))
        self.assertEqual(dl.status, "done", dl.error)
        self.assertEqual(self.read(dl), self.data)
        self.assertFalse(dl.resumable)

    def test_pause_then_resume_from_disk(self):
        k.LIMITER.rate = 600_000
        dl = k.Download(self.site.url("/f.bin"), self.dir, segments=4)
        dl.start()
        while dl.fraction < 0.25 and dl.status == "downloading":
            time.sleep(0.02)
        dl.pause()
        dl.join()
        time.sleep(0.3)
        self.assertEqual(dl.status, "paused")
        self.assertTrue(os.path.exists(dl.meta_path))
        k.LIMITER.rate = 0
        again = k.Download.from_dict(dl.to_dict())  # what a restarted app does
        self.assertGreater(again.fraction, 0.2)
        run_to_end(again)
        self.assertEqual(again.status, "done", again.error)
        self.assertEqual(self.read(again), self.data)

    def test_speed_limit_is_respected(self):
        k.LIMITER.rate = 1_000_000
        start = time.monotonic()
        dl = run_to_end(k.Download(self.site.url("/f.bin"), self.dir, segments=4))
        self.assertEqual(dl.status, "done", dl.error)
        self.assertGreater(time.monotonic() - start, 1.8)  # 2.5 MB at 1 MB/s

    def test_checksum_ok_and_bad(self):
        good = hashlib.sha256(self.data).hexdigest()
        dl = k.Download(self.site.url("/f.bin"), self.dir, segments=3)
        dl.set_checksum("sha256:" + good)
        self.assertEqual(run_to_end(dl).verify, "ok")
        bad = k.Download(self.site.url("/f.bin"), self.dir, segments=3)
        bad.set_checksum("0" * 64)
        self.assertEqual(run_to_end(bad).verify, "bad")

    def test_checksum_can_be_added_after_finishing(self):
        dl = run_to_end(k.Download(self.site.url("/f.bin"), self.dir))
        dl.set_checksum(hashlib.md5(self.data).hexdigest())
        deadline = time.time() + 10
        while dl.verify != "ok" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(dl.verify, "ok")

    def test_parse_checksum_validation(self):
        self.assertEqual(k.parse_checksum("SHA256:" + "A" * 64), ("sha256", "a" * 64))
        self.assertEqual(k.parse_checksum("b" * 40)[0], "sha1")
        for bad in ("", "xyz", "abc", "md5:" + "a" * 64, "g" * 64):
            with self.assertRaises(ValueError, msg=bad):
                k.parse_checksum(bad)

    def slow_big(self):
        self.big = blob(6_000_000, 5)
        self.site.files["/big.bin"] = self.big
        self.site.delay = 0.05

    def test_expired_link_fails_fast_with_hint(self):
        self.slow_big()
        k.LIMITER.rate = 1_000_000
        dl = k.Download(self.site.url("/big.bin"), self.dir, segments=3)
        dl.start()
        while dl.fraction < 0.1:
            time.sleep(0.02)
        self.site.expired.add("/big.bin")
        started = time.monotonic()
        dl.join()
        self.assertEqual(dl.status, "error")
        self.assertIn("Ganti link", dl.error)
        self.assertLess(time.monotonic() - started, 10, "403 must not be retried with long backoff")

    def test_replace_url_keeps_progress_and_finishes(self):
        self.slow_big()
        k.LIMITER.rate = 1_000_000
        self.site.files["/new.bin"] = self.big
        dl = k.Download(self.site.url("/big.bin"), self.dir, segments=3)
        dl.start()
        while dl.fraction < 0.15:
            time.sleep(0.02)
        self.site.expired.add("/big.bin")
        dl.join()
        self.assertEqual(dl.status, "error")
        before = dl.downloaded
        dl.replace_url(self.site.url("/new.bin"))
        self.assertEqual(dl.status, "paused")
        self.assertGreaterEqual(dl.downloaded, before)
        k.LIMITER.rate = 0
        self.site.delay = 0
        dl.status = "queued"
        run_to_end(dl)
        self.assertEqual(dl.status, "done", dl.error)
        self.assertEqual(self.read(dl), self.big)

    def test_replace_url_rejects_other_file_and_dead_link(self):
        k.LIMITER.rate = 300_000
        self.site.files["/other.bin"] = blob(900_000, 7)
        dl = k.Download(self.site.url("/f.bin"), self.dir, segments=2)
        dl.start()
        while dl.fraction < 0.05:
            time.sleep(0.02)
        dl.pause()
        dl.join()
        with self.assertRaisesRegex(ValueError, "bukan file yang sama"):
            dl.replace_url(self.site.url("/other.bin"))
        with self.assertRaisesRegex(ValueError, "tidak bisa dibuka"):
            dl.replace_url(self.site.url("/missing.bin"))
        self.assertIn("/f.bin", dl.url)


class HlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.segs = [blob(50_000, i + 2) for i in range(6)]
        files = {f"/s{i}.ts": s for i, s in enumerate(self.segs)}
        body = "#EXTM3U\n" + "".join(f"#EXTINF:4,\ns{i}.ts\n" for i in range(6)) + "#EXT-X-ENDLIST\n"
        files["/high.m3u8"] = body.encode()
        files["/low.m3u8"] = b"#EXTM3U\n#EXTINF:4,\ns0.ts\n#EXT-X-ENDLIST\n"
        files["/master.m3u8"] = (b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nlow.m3u8\n"
                                 b"#EXT-X-STREAM-INF:BANDWIDTH=2500000\nhigh.m3u8\n")
        files["/enc.m3u8"] = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4,\ns0.ts\n#EXT-X-ENDLIST\n'
        files["/live.m3u8"] = b"#EXTM3U\n#EXTINF:4,\ns0.ts\n"
        self.site = Site(files)

    def tearDown(self):
        self.site.close()
        self.tmp.cleanup()

    def test_master_playlist_joins_best_variant_in_order(self):
        dl = run_to_end(k.Download(self.site.url("/master.m3u8"), self.tmp.name, name="Film 2.0 final"))
        self.assertEqual(dl.status, "done", dl.error)
        self.assertEqual(os.path.basename(dl.path), "Film 2.0 final.ts")
        with open(dl.path, "rb") as f:
            self.assertEqual(f.read(), b"".join(self.segs))
        self.assertFalse(os.path.exists(dl.path + ".hls"), "work directory must be cleaned up")

    def test_encrypted_and_live_streams_are_refused(self):
        enc = run_to_end(k.Download(self.site.url("/enc.m3u8"), self.tmp.name))
        self.assertEqual(enc.status, "error")
        self.assertIn("terenkripsi", enc.error)
        live = run_to_end(k.Download(self.site.url("/live.m3u8"), self.tmp.name))
        self.assertEqual(live.status, "error")
        self.assertIn("live", live.error)


if __name__ == "__main__":
    unittest.main()
