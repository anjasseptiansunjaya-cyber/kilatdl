import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kilatdl as k  # noqa: E402
from helpers import Site, blob, run_to_end  # noqa: E402


class SortTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual([k.category_for(n) for n in ("a.MP4", "b.pdf", "c.iso", "d.deb", "e.mp3", "f.png", "g.xyz", "h")],
                         ["Video", "Dokumen", "Arsip", "Program", "Musik", "Gambar", None, None])

    def test_download_goes_to_sub_folder_only_when_enabled(self):
        site = Site({"/clip.mp4": blob(5000), "/misc.xyz": blob(5000, 3)})
        self.addCleanup(site.close)
        with tempfile.TemporaryDirectory() as tmp:
            a = run_to_end(k.Download(site.url("/clip.mp4"), tmp, sort=True))
            b = run_to_end(k.Download(site.url("/misc.xyz"), tmp, sort=True))
            c = run_to_end(k.Download(site.url("/clip.mp4"), tmp, name="plain.mp4"))
            self.assertEqual(os.path.dirname(a.path), os.path.join(tmp, "Video"))
            self.assertEqual(os.path.dirname(b.path), tmp, "unknown types stay in the main folder")
            self.assertEqual(os.path.dirname(c.path), tmp, "sorting is off by default")
            self.assertTrue(k.Download.from_dict(a.to_dict()).sort)


class ScanTest(unittest.TestCase):
    def fake_clam(self, tmp, code, out=""):
        path = os.path.join(tmp, "clamscan")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\necho '{out}'\nexit {code}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return mock.patch.dict(os.environ, {"PATH": tmp})

    def test_clean_infected_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.fake_clam(tmp, 0):
                self.assertEqual(k.scan_file("/x"), ("clean", ""))
            with self.fake_clam(tmp, 1, "/x: Win.Test.EICAR_HDB-1 FOUND"):
                self.assertEqual(k.scan_file("/x"), ("infected", "Win.Test.EICAR_HDB-1"))
            with self.fake_clam(tmp, 2, "ERROR: cannot open file"):
                state, detail = k.scan_file("/x")
                self.assertEqual(state, "error")
                self.assertIn("cannot open", detail)
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(k.scan_file("/x")[0], "error")

    def test_finished_download_is_scanned_when_enabled(self):
        site = Site({"/f.bin": blob(5000)})
        self.addCleanup(site.close)
        self.addCleanup(k.SCAN.update, on=False)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bin_dir:
            k.SCAN["on"] = True
            with self.fake_clam(bin_dir, 1, "/f: Eicar-Test-Signature FOUND"):
                dl = run_to_end(k.Download(site.url("/f.bin"), tmp))
                for _ in range(100):
                    if dl.scan != "scanning":
                        break
                    import time
                    time.sleep(0.05)
            self.assertEqual((dl.status, dl.scan, dl.scan_detail), ("done", "infected", "Eicar-Test-Signature"))
            self.assertIn("VIRUS", k.status_text(dl))
            self.assertTrue(os.path.exists(dl.path), "an infected file is flagged, never deleted behind the user's back")


if __name__ == "__main__":
    unittest.main()
