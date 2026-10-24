import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kilatdl as k  # noqa: E402
from helpers import Site  # noqa: E402


class Fake:
    def __init__(self, name, url, status="done", path="/x/a", size=10, error=""):
        self.name, self.url, self.status, self.path, self.size, self.error = name, url, status, path, size, error


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "h.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_safe_url_drops_secrets(self):
        self.assertEqual(k.safe_url("https://u:p@h.com/a/b.iso?token=SECRET&x=1#f"), "https://h.com/a/b.iso")

    def test_add_search_persist_and_dedupe(self):
        h = k.History(self.path)
        h.add(Fake("Film.mp4", "https://a.com/film.mp4?sig=abc", path="/d/Film.mp4"))
        h.add(Fake("Lagu.mp3", "https://b.com/lagu.mp3", path="/d/Lagu.mp3"))
        h.add(Fake("Film.mp4", "https://a.com/film.mp4?sig=new", path="/d/Film.mp4"))  # same file again
        self.assertEqual([e["name"] for e in h.entries], ["Film.mp4", "Lagu.mp3"])
        self.assertEqual([e["name"] for e in h.search("lagu b.com")], ["Lagu.mp3"])
        self.assertEqual(h.search("zzz"), [])
        self.assertNotIn("sig", open(self.path).read())
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertEqual(len(k.History(self.path).entries), 2)

    def test_limit_clear_and_failed_entries(self):
        h = k.History(self.path, limit=3)
        for i in range(5):
            h.add(Fake(f"f{i}", f"https://a.com/{i}", path=f"/d/{i}"))
        self.assertEqual([e["name"] for e in h.entries], ["f4", "f3", "f2"])
        h.add(Fake("bad", "https://a.com/bad", status="error", error="HTTP 404", path=None))
        self.assertEqual(h.entries[0]["note"], "HTTP 404")
        h.clear()
        self.assertEqual(k.History(self.path).entries, [])

    def test_export_csv_and_json(self):
        h = k.History(self.path)
        h.add(Fake("a, \"b\".zip", "https://a.com/a.zip", path="/d/a.zip", size=123))
        csv_path, json_path = os.path.join(self.tmp.name, "o.csv"), os.path.join(self.tmp.name, "o.json")
        h.export(csv_path)
        h.export(json_path)
        rows = list(csv.reader(open(csv_path, encoding="utf-8")))
        self.assertEqual(rows[0][1:4], ["nama", "ukuran_byte", "status"])
        self.assertEqual(rows[1][1:4], ['a, "b".zip', "123", "done"])
        self.assertEqual(json.load(open(json_path))[0]["size"], 123)

    def test_corrupt_file_starts_empty(self):
        open(self.path, "w").write("{not json")
        self.assertEqual(k.History(self.path).entries, [])


PAGE = """<html><head><base href="/files/"></head><body>
<a href="doc.pdf">d</a> <a href="/abs/arc.zip?x=1#top">a</a> <a href="page.html">p</a> <a href="about">p2</a>
<a href="mailto:x@y.z">m</a> <a href="javascript:void(0)">j</a> <a href="doc.pdf">dup</a>
<img src="pic.JPG"> <img srcset="s1.png 1x, s2.png 2x"> <video src="v.mp4"></video>
<source src="//cdn.example.com/m.webm"><a href="song.mp3">s</a></body></html>"""


class GrabTest(unittest.TestCase):
    def test_extracts_files_not_pages(self):
        links = k.grab_links(PAGE, "https://site.com/index.html")
        urls = [l["url"] for l in links]
        self.assertEqual(urls, ["https://site.com/files/doc.pdf", "https://site.com/abs/arc.zip?x=1",
                                "https://site.com/files/pic.JPG", "https://site.com/files/s1.png", "https://site.com/files/s2.png",
                                "https://site.com/files/v.mp4", "https://cdn.example.com/m.webm", "https://site.com/files/song.mp3"])
        self.assertEqual({l["name"]: l["kind"] for l in links}["arc.zip"], "Arsip")

    def test_filters(self):
        links = k.grab_links(PAGE, "https://site.com/")
        self.assertEqual([l["name"] for l in k.filter_links(links, "Gambar")], ["pic.JPG", "s1.png", "s2.png"])
        self.assertEqual([l["name"] for l in k.filter_links(links, None, "pdf, .zip")], ["doc.pdf", "arc.zip"])
        self.assertEqual(len(k.filter_links(links)), len(links))

    def test_fetch_page_and_refuse_non_html(self):
        site = Site({"/p.html": PAGE.encode(), "/f.zip": b"PK"})
        site.types = {"/p.html": "text/html; charset=utf-8", "/f.zip": "application/zip"}
        self.addCleanup(site.close)
        text, final = k.fetch_page(site.url("/p.html"))
        self.assertIn("doc.pdf", text)
        self.assertEqual(final, site.url("/p.html"))
        with self.assertRaises(ValueError):
            k.fetch_page(site.url("/f.zip"))


if __name__ == "__main__":
    unittest.main()
