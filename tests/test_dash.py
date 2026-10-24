import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kilatdl as k  # noqa: E402
from helpers import Site, blob, run_to_end  # noqa: E402

NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def mpd(body, attrs='type="static" mediaPresentationDuration="PT20S"'):
    return f'<?xml version="1.0"?><MPD {NS} {attrs}><Period>{body}</Period></MPD>'


TEMPLATE = mpd('''
<AdaptationSet contentType="video" mimeType="video/mp4">
 <SegmentTemplate timescale="1000" duration="5000" startNumber="1" initialization="v/$RepresentationID$/init.mp4" media="v/$RepresentationID$/seg-$Number%03d$.m4s"/>
 <Representation id="lo" bandwidth="500000"/><Representation id="hi" bandwidth="2000000"/>
</AdaptationSet>
<AdaptationSet contentType="audio" mimeType="audio/mp4">
 <Representation id="a1" bandwidth="64000"><SegmentTemplate timescale="1000" initialization="a/init.mp4" media="a/$Time$.m4s">
  <SegmentTimeline><S t="0" d="10000" r="-1"/></SegmentTimeline></SegmentTemplate></Representation>
</AdaptationSet>''')


class ParseMpdTest(unittest.TestCase):
    def test_template_number_timeline_and_best_bandwidth(self):
        t = k.parse_mpd(TEMPLATE, "http://h/x/m.mpd")
        self.assertEqual(t["video"]["id"], "hi")
        self.assertEqual(t["video"]["urls"][0], "http://h/x/v/hi/init.mp4")
        self.assertEqual([u.rsplit("/", 1)[1] for u in t["video"]["urls"][1:]],
                         ["seg-001.m4s", "seg-002.m4s", "seg-003.m4s", "seg-004.m4s"])
        self.assertEqual([u.rsplit("/", 1)[1] for u in t["audio"]["urls"]], ["init.mp4", "0.m4s", "10000.m4s"])

    def test_single_file_base_url(self):
        t = k.parse_mpd(mpd('<AdaptationSet mimeType="video/mp4"><Representation id="1" bandwidth="1"><BaseURL>v.mp4</BaseURL>'
                            '</Representation></AdaptationSet>'), "http://h/m.mpd")
        self.assertEqual(t["video"]["urls"], ["http://h/v.mp4"])

    def test_drm_live_and_empty_are_refused(self):
        drm = mpd('<AdaptationSet mimeType="video/mp4"><ContentProtection schemeIdUri="urn:x"/>'
                  '<Representation id="1" bandwidth="1"><BaseURL>v.mp4</BaseURL></Representation></AdaptationSet>')
        for text, word in ((drm, "DRM"), (mpd("", 'type="dynamic"'), "live"), (mpd(""), "tidak berisi"), ("<MPD", "rusak")):
            with self.assertRaises(RuntimeError) as cm:
                k.parse_mpd(text, "http://h/m.mpd")
            self.assertIn(word, str(cm.exception))


class DashDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = [blob(30_000, i + 2) for i in range(3)]   # init + 2 segments
        self.site = Site({"/v/init.mp4": self.v[0], "/v/1.m4s": self.v[1], "/v/2.m4s": self.v[2],
                          "/a.mp4": blob(10_000, 9)})
        self.video_only = mpd('<AdaptationSet contentType="video"><Representation id="1" bandwidth="9">'
                              '<SegmentTemplate timescale="1" duration="10" startNumber="1" initialization="v/init.mp4" media="v/$Number$.m4s"/>'
                              '</Representation></AdaptationSet>')
        self.both = self.video_only.replace("</Period>", '<AdaptationSet contentType="audio"><Representation id="a" bandwidth="1">'
                                            '<BaseURL>a.mp4</BaseURL></Representation></AdaptationSet></Period>')

    def tearDown(self):
        self.site.close()
        self.tmp.cleanup()

    def test_single_track_is_joined_in_order(self):
        self.site.files["/m.mpd"] = self.video_only.encode()
        dl = run_to_end(k.Download(self.site.url("/m.mpd"), self.tmp.name, name="Klip"))
        self.assertEqual(dl.status, "done", dl.error)
        self.assertEqual(os.path.basename(dl.path), "Klip.mp4")
        with open(dl.path, "rb") as f:
            self.assertEqual(f.read(), b"".join(self.v))
        self.assertFalse(os.path.exists(dl.path + ".hls"))

    def test_audio_plus_video_without_ffmpeg_explains_and_downloads_nothing(self):
        self.site.files["/m.mpd"] = self.both.encode()
        with mock.patch("shutil.which", return_value=None):
            dl = run_to_end(k.Download(self.site.url("/m.mpd"), self.tmp.name))
        self.assertEqual(dl.status, "error")
        self.assertIn("ffmpeg", dl.error)
        self.assertEqual([h for h in self.site.hits if h[0] != "/m.mpd"], [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg tidak terpasang")
    def test_audio_and_video_are_muxed_with_ffmpeg(self):
        d = tempfile.mkdtemp()
        v, a = os.path.join(d, "v.mp4"), os.path.join(d, "a.mp4")
        run = lambda *args: subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)
        run("-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", v)
        run("-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov+default_base_moof", a)
        with open(v, "rb") as f1, open(a, "rb") as f2:
            self.site.files["/v.mp4"], self.site.files["/a.mp4"] = f1.read(), f2.read()
        self.site.files["/m.mpd"] = mpd(
            '<AdaptationSet contentType="video"><Representation id="1" bandwidth="9"><BaseURL>v.mp4</BaseURL></Representation></AdaptationSet>'
            '<AdaptationSet contentType="audio"><Representation id="a" bandwidth="1"><BaseURL>a.mp4</BaseURL></Representation></AdaptationSet>').encode()
        dl = run_to_end(k.Download(self.site.url("/m.mpd"), self.tmp.name, name="Gabung"))
        self.assertEqual(dl.status, "done", dl.error)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", dl.path],
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(sorted(out), ["audio", "video"])
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
