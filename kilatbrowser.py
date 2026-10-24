#!/usr/bin/env python3
"""KilatBrowser - small tabbed browser (WebKitGTK) that hands downloads to KilatDL.

Usage: kilatbrowser [URL ...]
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import quote_plus

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

BRIDGE = "http://127.0.0.1:9614"
HOME = "https://duckduckgo.com/"
DATA = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "kilatbrowser")


def to_url(text):
    text = text.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://", text) or text.startswith(("about:", "file:")):
        return text
    if " " not in text and ("." in text or text.startswith("localhost")):
        return "https://" + text
    return HOME + "?q=" + quote_plus(text)


def bridge_up():
    try:
        with urllib.request.urlopen(BRIDGE + "/ping", timeout=2) as r:
            return json.load(r).get("app") == "kilatdl"
    except (OSError, ValueError):
        return False


def send_to_kilatdl(url, headers):
    """POST a link to the local KilatDL bridge, starting KilatDL if it is not running."""
    if not bridge_up():
        subprocess.Popen([os.path.expanduser("~/.local/bin/kilatdl"), "--background"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(0.5)
            if bridge_up():
                break
        else:
            return False
    # The bridge only accepts extension-style origins; this is our own trusted local client.
    req = urllib.request.Request(BRIDGE + "/add", data=json.dumps({"url": url, "headers": headers}).encode(),
                                 headers={"Content-Type": "application/json", "X-KilatDL": "1",
                                          "Origin": "moz-extension://kilatbrowser"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except OSError:
        return False


class Browser(Gtk.Window):
    def __init__(self, urls):
        super().__init__(title="KilatBrowser", default_width=1100, default_height=750)
        os.makedirs(DATA, exist_ok=True)
        wdm = WebKit2.WebsiteDataManager(base_data_directory=DATA, base_cache_directory=os.path.join(DATA, "cache"))
        self.ctx = WebKit2.WebContext.new_with_website_data_manager(wdm)
        self.cookies = self.ctx.get_cookie_manager()
        self.cookies.set_persistent_storage(os.path.join(DATA, "cookies.sqlite"),
                                            WebKit2.CookiePersistentStorage.SQLITE)
        self.ctx.connect("download-started", self.on_download_started)

        self.back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.fwd = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        self.reload = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        newtab = Gtk.Button.new_from_icon_name("tab-new-symbolic", Gtk.IconSize.BUTTON)
        self.entry = Gtk.Entry(hexpand=True)
        self.entry.connect("activate", lambda e: self.current().load_uri(to_url(e.get_text())))
        self.back.connect("clicked", lambda *_: self.current().go_back())
        self.fwd.connect("clicked", lambda *_: self.current().go_forward())
        self.reload.connect("clicked", lambda *_: self.current().reload())
        newtab.connect("clicked", lambda *_: self.new_tab(HOME))

        bar = Gtk.Box(spacing=4, margin=4)
        for w in (self.back, self.fwd, self.reload, self.entry, newtab):
            bar.pack_start(w, w is self.entry, True, 0)
        self.tabs = Gtk.Notebook(scrollable=True)
        self.tabs.connect("switch-page", self.on_switch)
        self.status = Gtk.Label(xalign=0, margin_start=6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(bar, False, False, 0)
        box.pack_start(self.tabs, True, True, 0)
        box.pack_start(self.status, False, False, 2)
        self.add(box)
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", Gtk.main_quit)

        for u in urls or [HOME]:
            self.new_tab(u)
        self.show_all()

    def current(self):
        return self.tabs.get_nth_page(self.tabs.get_current_page())

    def new_tab(self, uri=None, related=None):
        view = (WebKit2.WebView.new_with_related_view(related) if related
                else WebKit2.WebView(web_context=self.ctx))
        label = Gtk.Label(label="Tab baru", max_width_chars=18, ellipsize=3)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        head = Gtk.Box(spacing=4)
        head.pack_start(label, True, True, 0)
        head.pack_start(close, False, False, 0)
        head.show_all()
        close.connect("clicked", lambda *_: self.close_tab(view))
        view.connect("notify::title", lambda v, _: label.set_text(v.get_title() or v.get_uri() or "Tab baru"))
        view.connect("notify::uri", lambda v, _: v is self.current() and self.entry.set_text(v.get_uri() or ""))
        view.connect("notify::estimated-load-progress", self.on_progress)
        view.connect("decide-policy", self.on_policy)
        view.connect("create", lambda v, nav: self.new_tab(related=v))
        view.connect("mouse-target-changed", lambda v, hit, _m: self.status.set_text(
            hit.get_link_uri() or "" if hit.context_is_link() else ""))
        page = self.tabs.append_page(view, head)
        self.tabs.set_tab_reorderable(view, True)
        view.show()
        self.tabs.set_current_page(page)
        if uri:
            view.load_uri(to_url(uri))
        return view

    def close_tab(self, view):
        self.tabs.remove_page(self.tabs.page_num(view))
        if self.tabs.get_n_pages() == 0:
            self.destroy()

    def on_switch(self, _nb, page, _n):
        self.entry.set_text(page.get_uri() or "")

    def on_progress(self, view, _p):
        if view is self.current():
            p = view.get_estimated_load_progress()
            self.entry.set_progress_fraction(p if p < 1 else 0)

    def on_key(self, _w, ev):
        ctrl = ev.state & Gtk.accelerator_get_default_mod_mask() == 0x4  # Control
        alt = ev.state & 0x8
        key = ev.keyval
        if ctrl and key == Gdk.KEY_t:
            self.new_tab(HOME)
        elif ctrl and key == Gdk.KEY_w:
            self.close_tab(self.current())
        elif ctrl and key == Gdk.KEY_l:
            self.entry.grab_focus()
        elif key == Gdk.KEY_F5 or (ctrl and key == Gdk.KEY_r):
            self.current().reload()
        elif alt and key == Gdk.KEY_Left:
            self.current().go_back()
        elif alt and key == Gdk.KEY_Right:
            self.current().go_forward()
        else:
            return False
        return True

    # -- downloads -> KilatDL ---------------------------------------------
    def on_policy(self, view, decision, dtype):
        if dtype != WebKit2.PolicyDecisionType.RESPONSE:
            return False
        resp = decision.get_response()
        headers = resp.get_http_headers()
        disp = headers.get_one("Content-Disposition") if headers else None
        attachment = bool(disp and disp.lower().lstrip().startswith("attachment"))
        if attachment or not decision.is_mime_type_supported():
            decision.ignore()
            self.hand_off(resp.get_uri(), view.get_uri())
            return True
        return False

    def on_download_started(self, _ctx, download):
        download.connect("decide-destination", lambda d, _name: (self._cancel(d), True)[1])

    def _cancel(self, download):
        uri = download.get_request().get_uri()
        download.cancel()
        self.hand_off(uri, self.current().get_uri())

    def hand_off(self, url, referrer):
        if not url or not re.match(r"https?://", url):
            return
        self.status.set_text("Mengirim ke KilatDL: " + url)

        def got_cookies(mgr, res):
            headers = {"Referer": referrer} if referrer else {}
            try:
                jar = mgr.get_cookies_finish(res)
                if jar:
                    headers["Cookie"] = "; ".join(f"{c.get_name()}={c.get_value()}" for c in jar)
            except GLib.Error:
                pass

            def work():
                ok = send_to_kilatdl(url, headers)
                GLib.idle_add(self.status.set_text, "Dikirim ke KilatDL" if ok else "Gagal menghubungi KilatDL")

            threading.Thread(target=work, daemon=True).start()

        self.cookies.get_cookies(url, None, got_cookies)


if __name__ == "__main__":
    Browser(sys.argv[1:])
    Gtk.main()
