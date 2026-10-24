"""Local HTTP test server with switchable behaviours (Range support, expiring tokens, content types)."""
import http.server
import os
import re
import threading
import time


class Site:
    def __init__(self, files):
        self.files = files            # path -> bytes
        self.ranges = True            # answer Range with 206
        self.expired = set()          # paths that now answer 403
        self.types = {}               # path -> content type
        self.hits = []                # (path, range header)
        self.delay = 0                # seconds between 32 KB pieces (simulates a slow origin)
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                site.hits.append((path, self.headers.get("Range")))
                if path in site.expired:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path not in site.files:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = site.files[path]
                n = len(data)
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if m and site.ranges:
                    a, b = int(m[1]), int(m[2]) if m[2] else n - 1
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{n}")
                    data = data[a:b + 1]
                else:
                    self.send_response(200)
                self.send_header("Content-Type", site.types.get(path, "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:  # send in pieces so a token can expire mid-transfer, like a real CDN
                    for i in range(0, len(data), 32768):
                        if path in site.expired:
                            self.close_connection = True
                            return
                        self.wfile.write(data[i:i + 32768])
                        if site.delay:
                            time.sleep(site.delay)
                except OSError:
                    pass  # client paused / disconnected

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def blob(n, seed=1):
    return bytes((i * seed + (i >> 8)) & 0xFF for i in range(n))


def run_to_end(dl, timeout=60):
    import time
    dl.start()
    dl.join()
    deadline = time.time() + timeout
    while dl.verify == "checking" and time.time() < deadline:
        time.sleep(0.05)
    return dl
