import json
import os
import socket
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kilatdl as k  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        k.BRIDGE_PORT = free_port()
        cls.server = k.start_bridge(lambda url, headers, name: cls.received.append((url, headers, name)))
        cls.base = f"http://127.0.0.1:{k.BRIDGE_PORT}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, headers=None, path="/add"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    GOOD = {"X-KilatDL": "1", "Origin": "moz-extension://abc"}

    def test_ping(self):
        with urllib.request.urlopen(self.base + "/ping", timeout=5) as r:
            self.assertEqual(json.load(r), {"app": "kilatdl"})

    def test_rejects_requests_a_web_page_could_make(self):
        n = len(self.received)
        url = {"url": "http://example.com/x.zip"}
        self.assertEqual(self.post(url), 403)                                             # no header, no origin
        self.assertEqual(self.post(url, {"X-KilatDL": "1"}), 403)                         # no origin
        self.assertEqual(self.post(url, {"X-KilatDL": "1", "Origin": "https://evil.com"}), 403)
        self.assertEqual(self.post(url, {"Origin": "chrome-extension://abc"}), 403)       # no custom header
        self.assertEqual(len(self.received), n)

    def test_accepts_extension_and_cleans_input(self):
        status = self.post({"url": "http://example.com/a.mp4", "name": "../../etc/pa:ss*wd?",
                            "headers": {"Cookie": "a=b", "Referer": "http://x", "Evil": "1", "X-Other": "1"}},
                           self.GOOD)
        self.assertEqual(status, 200)
        url, headers, name = self.received[-1]
        self.assertEqual(headers, {"Cookie": "a=b", "Referer": "http://x"})
        self.assertEqual(os.path.basename(name), name, "a file name must never contain a directory part")
        self.assertNotIn(name, ("", ".", ".."))

    def test_rejects_bad_payloads(self):
        self.assertEqual(self.post({"url": "file:///etc/passwd"}, self.GOOD), 400)
        self.assertEqual(self.post({"url": "ftp://example.com/x"}, self.GOOD), 400)
        self.assertEqual(self.post({"nope": 1}, self.GOOD), 400)
        self.assertEqual(self.post({"url": "http://example.com/x"}, self.GOOD, path="/other"), 403)

    def test_header_injection_is_dropped(self):
        self.post({"url": "http://example.com/a.bin", "headers": {"Cookie": "a=b\r\nX-Evil: 1"}}, self.GOOD)
        self.assertEqual(self.received[-1][1], {})


if __name__ == "__main__":
    unittest.main()
