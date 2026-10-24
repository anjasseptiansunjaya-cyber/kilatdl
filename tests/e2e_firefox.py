#!/usr/bin/env python3
"""End-to-end check of the extension overlay in a real Firefox (run by hand, not part of unittest discovery).

Needs: Firefox Developer Edition at ~/.local/opt/firefox-dev, geckodriver, and the profile made by the
setup (~/.local/share/firefox-dev-profile). Everything runs on copies and an isolated KilatDL instance
(own data dir, bridge port 9715, app id ...Test), so the real KilatDL and profile are never touched.

    python3 tests/e2e_firefox.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from helpers import Site, blob  # noqa: E402

FIREFOX = os.path.expanduser("~/.local/opt/firefox-dev/firefox")
PROFILE_SRC = os.path.expanduser("~/.local/share/firefox-dev-profile")
G = "http://127.0.0.1:4444"
PORT = "9715"


def call(method, path, body=None):
    req = urllib.request.Request(G + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["value"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def wait_for(fn, seconds, label):
    end = time.time() + seconds
    while time.time() < end:
        try:
            got = fn()
            if got:
                return got
        except Exception:  # noqa: BLE001 - polling
            pass
        time.sleep(0.3)
    raise SystemExit(f"TIMEOUT: {label}")


def main():
    tmp = tempfile.mkdtemp(prefix="kilatdl-e2e-")
    procs = []
    try:
        out_dir, data = os.path.join(tmp, "out"), os.path.join(tmp, "data")
        os.makedirs(out_dir)
        os.makedirs(os.path.join(data, "kilatdl"))
        with open(os.path.join(data, "kilatdl", "settings.json"), "w") as f:
            json.dump({"directory": out_dir, "limit_kbps": 0, "parallel": 3}, f)

        clip = blob(700_000, 3)
        page = video_page = (b'<!doctype html><title>Kuliah Hidrolika Uji</title><body style="margin:0;background:#222">'
                b'<video id="v" src="clip.mp4" width="640" height="360" controls muted preload="auto"></video>'
                b'<script>fetch("master.m3u8").then(()=>fetch("s0.ts"))</script>')
        site = Site({"/page.html": page, "/clip.mp4": clip, "/s0.ts": blob(60_000, 9),
                     "/master.m3u8": b"#EXTM3U\n#EXTINF:4,\ns0.ts\n#EXT-X-ENDLIST\n",
                     "/dash.html": video_page.replace(b'fetch("master.m3u8").then(()=>fetch("s0.ts"))', b'fetch("manifest.mpd").then(()=>fetch("a1.m4s"))').replace(b' src="clip.mp4"', b""),
                     "/manifest.mpd": b"<MPD/>", "/a1.m4s": blob(50_000, 4),
                     "/seg.html": video_page.replace(b'fetch("master.m3u8").then(()=>fetch("s0.ts"))', b'fetch("s0.ts").then(()=>fetch("s1.ts"))').replace(b' src="clip.mp4"', b""),
                     "/s1.ts": blob(60_000, 8),
                     "/octet.html": video_page.replace(b'fetch("master.m3u8").then(()=>fetch("s0.ts"))', b'0').replace(b"clip.mp4", b"stream"),
                     "/stream": blob(700_000, 6),
                     "/empty.html": video_page.replace(b'fetch("master.m3u8").then(()=>fetch("s0.ts"))', b'0').replace(b' src="clip.mp4"', b"")})
        site.types.update({"/page.html": "text/html", "/clip.mp4": "video/mp4", "/s0.ts": "video/mp2t",
                           "/master.m3u8": "application/vnd.apple.mpegurl", "/dash.html": "text/html",
                           "/seg.html": "text/html", "/manifest.mpd": "application/dash+xml",
                           "/a1.m4s": "video/iso.segment", "/s1.ts": "video/mp2t",
                           "/octet.html": "text/html", "/empty.html": "text/html", "/stream": "application/octet-stream"})

        env = dict(os.environ, XDG_DATA_HOME=data, KILATDL_APP_ID="id.anjas.KilatDLTest", KILATDL_PORT=PORT)
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "kilatdl.py"), "--background"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        wait_for(lambda: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=1).status == 200, 15, "isolated KilatDL")

        # profile copy carrying the current extension build
        profile = os.path.join(tmp, "profile")
        shutil.copytree(PROFILE_SRC, profile, ignore=shutil.ignore_patterns("cache2", "storage", "lock", ".parentlock"))
        xpi = os.path.join(profile, "extensions", "kilatdl@anjas.local.xpi")
        import zipfile
        with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("manifest.json", "background.js", "content.js", "popup.html", "popup.js", *[f"icons/icon-{n}.png" for n in (16, 32, 48, 96, 128)]):
                z.write(os.path.join(ROOT, "extension", name), name)

        fx_env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1")
        procs.append(subprocess.Popen([FIREFOX, "-headless", "-marionette", "--no-remote", "-profile", profile],
                                      env=fx_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_for(lambda: subprocess.run(["bash", "-c", "ss -ltn | grep -q 2828"]).returncode == 0, 40, "marionette")
        time.sleep(4)
        procs.append(subprocess.Popen(["geckodriver", "--port", "4444", "--connect-existing", "--marionette-port", "2828"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(2)
        sid = wait_for(lambda: call("POST", "/session", {"capabilities": {"alwaysMatch": {}}})["sessionId"], 20, "webdriver session")
        base = f"/session/{sid}"
        try:
            page_handle = call("GET", base + "/window")
            call("POST", base + "/url", {"url": site.url("/page.html")})
            time.sleep(3)

            prefs = open(os.path.join(profile, "prefs.js")).read()
            import re
            uuids = json.loads(re.search(r'extensions\.webextensions\.uuids", "(.*)"\);', prefs).group(1).replace('\\"', '"').replace("\\\\", "\\"))
            uid = uuids["kilatdl@anjas.local"]

            # point the extension at the isolated KilatDL via an extension page (it has the browser.* API)
            before = set(call("GET", base + "/window/handles"))
            call("POST", base + "/moz/context", {"context": "chrome"})
            call("POST", base + "/execute/sync", {"script": "gBrowser.selectedTab = gBrowser.addTab(arguments[0], {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});",
                                                    "args": [f"moz-extension://{uid}/popup.html"]})
            call("POST", base + "/moz/context", {"context": "content"})
            time.sleep(2)
            popup = next(h for h in call("GET", base + "/window/handles") if h not in before)
            call("POST", base + "/window", {"handle": popup})
            call("POST", base + "/execute/async", {"script": f"const done = arguments[arguments.length-1]; browser.storage.local.set({{bridgePort: {PORT}}}).then(() => done(true));", "args": []})

            # back to the page: reload so the extension sees the media requests, then hover the video
            call("POST", base + "/window", {"handle": page_handle})
            call("POST", base + "/url", {"url": site.url("/page.html")})
            time.sleep(3)
            rect = call("POST", base + "/execute/sync", {"script": "const r = document.getElementById('v').getBoundingClientRect(); return [r.left + r.width/2, r.top + r.height/2];", "args": []})
            move = {"actions": [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"},
                                 "actions": [{"type": "pointerMove", "x": int(rect[0]), "y": int(rect[1]), "origin": "viewport"},
                                             {"type": "pointerMove", "x": int(rect[0]) + 20, "y": int(rect[1]) + 10, "origin": "viewport"}]}]}
            call("POST", base + "/actions", move)

            def overlay_button():
                host = call("POST", base + "/element", {"using": "css selector", "value": "kilatdl-overlay"})
                shadow = call("GET", base + f"/element/{list(host.values())[0]}/shadow")
                btn = call("POST", base + f"/shadow/{list(shadow.values())[0]}/element", {"using": "css selector", "value": "#btn"})
                return shadow, btn

            shadow, btn = wait_for(overlay_button, 15, "overlay button to appear on hover")
            shadow_id = list(shadow.values())[0]
            print("PASS  overlay button appears when the pointer is over the video")

            call("POST", base + f"/element/{list(btn.values())[0]}/click", {})
            rows = wait_for(lambda: call("POST", base + f"/shadow/{shadow_id}/elements", {"using": "css selector", "value": ".item"}) or None, 10, "media list")
            names = [call("POST", base + f"/shadow/{shadow_id}/element", {"using": "css selector", "value": ".name"}) for _ in rows[:1]]
            print(f"PASS  menu lists {len(rows)} detected item(s)")
            texts = call("POST", base + "/execute/sync", {"script": "const s = document.querySelector('kilatdl-overlay').shadowRoot; return [...s.querySelectorAll('.item')].map(i => i.innerText.replace(/\\n/g,' | '));", "args": []})
            print("      ", texts)
            if os.environ.get("KILATDL_E2E_SHOT"):  # optional: keep a picture of the overlay
                import base64
                with open(os.environ["KILATDL_E2E_SHOT"], "wb") as f:
                    f.write(base64.b64decode(call("GET", base + "/screenshot")))
            assert any("clip.mp4" in t for t in texts), texts
            assert any("HLS" in t for t in texts), texts

            # untrusted (page-script) clicks must not trigger anything
            call("POST", base + "/execute/sync", {"script": "document.querySelector('kilatdl-overlay').shadowRoot.querySelector('.go').click();", "args": []})
            time.sleep(1.5)
            assert not os.listdir(out_dir), "a script-made click must NOT start a download"
            print("PASS  a click made by the page's own script is ignored")

            go = call("POST", base + f"/shadow/{shadow_id}/elements", {"using": "css selector", "value": ".go"})
            for item_row in call("POST", base + f"/shadow/{shadow_id}/elements", {"using": "css selector", "value": ".item"}):
                name = call("POST", base + f"/shadow/{shadow_id}/elements", {"using": "css selector", "value": ".name"})
            mp4_index = next(i for i, t in enumerate(texts) if "clip.mp4" in t)
            call("POST", base + f"/element/{list(go[mp4_index].values())[0]}/click", {})
            got = wait_for(lambda: [f for f in os.listdir(out_dir) if f.endswith(".mp4")], 20, "download to land in the folder")
            path = os.path.join(out_dir, got[0])
            assert open(path, "rb").read() == clip, "downloaded bytes differ"
            print(f"PASS  one click on Unduh delivered the video to KilatDL ({got[0]}, {os.path.getsize(path)} bytes, identical)")

            def probe(path):
                """Open a page, hover its video, open the overlay menu and return the explanation it shows."""
                call("POST", base + "/url", {"url": site.url(path)})
                time.sleep(3)
                call("POST", base + "/actions", move)
                _, button = wait_for(overlay_button, 15, f"overlay button on {path}")
                call("POST", base + f"/element/{list(button.values())[0]}/click", {})
                time.sleep(1.5)
                return call("POST", base + "/execute/sync", {"script": "const n = document.querySelector('kilatdl-overlay').shadowRoot.querySelector('.note'); return n ? n.innerText : null;", "args": []})

            note = probe("/dash.html")
            assert note and "DASH" in note, note
            print("PASS  a DASH page explains that DASH is not supported")
            note = probe("/seg.html")
            assert note and "potongan kecil" in note, note
            print("PASS  a page with bare stream pieces explains why nothing can be downloaded")

            note = probe("/empty.html")
            assert note and "Belum ada permintaan media sama sekali" in note, note
            print("PASS  a page that never requested media says so")

            # a video the server labels application/octet-stream and gives no extension is still found,
            # because the browser requested it as <video>; read the background state via the report command
            call("POST", base + "/url", {"url": site.url("/octet.html")})
            time.sleep(4)
            call("POST", base + "/window", {"handle": popup})
            report = call("POST", base + "/execute/async", {"script": """
                const done = arguments[arguments.length - 1];
                (async () => {
                  const tabs = await browser.tabs.query({});
                  const t = tabs.find(x => x.url.includes('octet.html'));
                  const rep = await new Promise(r => browser.runtime.sendMessage({cmd: 'report', tabId: t.id}, r));
                  done(rep);
                })();""", "args": []})
            call("POST", base + "/window", {"handle": page_handle})
            assert report["version"], report
            assert any(i["kind"] == "file" for i in report["found"]), report
            assert any(t["type"] == "media" and t["verdict"] == "file" and t["ctype"] == "application/octet-stream" for t in report["trace"]), report
            assert any(v["src"] == "http" and not v["drm"] for v in report["videos"].values()), report
            print(f"PASS  octet-stream media without extension is detected (report v{report['version']}: {len(report['trace'])} trace, {len(report['videos'])} video)")
        finally:
            try:
                call("DELETE", base)
            except Exception:  # noqa: BLE001
                pass
        print("E2E OK")
    finally:
        for p in procs:
            p.terminate()
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
