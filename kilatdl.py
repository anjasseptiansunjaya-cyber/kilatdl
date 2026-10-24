#!/usr/bin/env python3
"""KilatDL - multi-connection download manager (GTK4 GUI + CLI).

Usage:
    kilatdl                     open the GUI
    kilatdl URL [URL ...]       download in the terminal
    kilatdl --gui URL           open the GUI with URL queued
"""
import argparse
import hashlib
import http.server
import json
import os
import re
import math
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urljoin, urlsplit

APP_ID = os.environ.get("KILATDL_APP_ID", "id.anjas.KilatDL")
ICON_NAME = "id.anjas.KilatDL"  # installed by install.sh (assets/kilatdl.svg)
UA = "Mozilla/5.0 (X11; Linux x86_64) KilatDL/1.0"
MIN_SEGMENT = 1 << 20
CHUNK = 1 << 16
RETRIES = 6
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "kilatdl")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BRIDGE_PORT = int(os.environ.get("KILATDL_PORT", "9614"))
FATAL_HTTP = (401, 403, 404, 410)  # retrying will not help; usually an expired or forbidden link
HLS_RE = re.compile(r"\.m3u8$", re.I)
DASH_RE = re.compile(r"\.mpd$", re.I)


def default_dir():
    d = os.path.expanduser("~/Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


class Limiter:
    """Global bandwidth cap shared by every connection (bytes/s, 0 = unlimited)."""

    def __init__(self):
        self.rate = 0
        self._lock = threading.Lock()
        self._next = 0.0

    def consume(self, n):
        rate = self.rate
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(self._next, now)
            self._next = start + n / rate
        if start > now:
            time.sleep(start - now)


LIMITER = Limiter()


CATEGORIES = (
    ("Video", "mp4 mkv webm mov m4v ts avi flv wmv mpg mpeg 3gp"),
    ("Musik", "mp3 m4a aac ogg opus flac wav wma"),
    ("Gambar", "jpg jpeg png gif webp svg bmp tiff heic"),
    ("Dokumen", "pdf doc docx xls xlsx ppt pptx odt ods odp txt epub csv rtf"),
    ("Arsip", "zip rar 7z tar gz xz bz2 zst tgz iso img dmg"),
    ("Program", "exe msi deb rpm appimage apk sh run flatpak snap"),
)
SCAN = {"on": False}  # set from settings by the GUI: scan finished files with ClamAV


def category_for(name):
    """Sub-folder for a file name, or None when the type is unknown (stays in the main folder)."""
    ext = os.path.splitext(name or "")[1].lstrip(".").lower()
    return next((cat for cat, exts in CATEGORIES if ext in exts.split()), None)


def scan_file(path):
    """Scan one file with ClamAV. Returns ("clean" | "infected" | "error", detail)."""
    cmds = []
    if shutil.which("clamdscan"):  # fast when the daemon runs; exits 2 when it does not
        cmds.append(["clamdscan", "--no-summary", "--fdpass", path])
    if shutil.which("clamscan"):
        cmds.append(["clamscan", "--no-summary", path])
    if not cmds:
        return "error", "ClamAV tidak terpasang (sudo apt install clamav)"
    detail = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as e:
            detail = str(e)
            continue
        if r.returncode == 0:
            return "clean", ""
        if r.returncode == 1:
            m = re.search(r": (.+) FOUND", r.stdout)
            return "infected", m[1] if m else "?"
        out = (r.stderr.strip() or r.stdout.strip()).splitlines()
        detail = out[-1] if out else ""
    return "error", detail or "pemindaian gagal"


AFTER_ACTIONS = {"none": "Tidak ada", "quit": "Keluar dari KilatDL", "suspend": "Tidurkan komputer",
                 "poweroff": "Matikan komputer"}
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class Scheduler:
    """Decides when queued downloads may start, and what to do once everything has finished.

    With a start and stop time the window is [start, stop) and may wrap past midnight. With only a
    start time the queue is released when that time is reached and stays released until it is empty.
    """

    def __init__(self, settings):
        self.settings = settings
        self.released = False
        self.last = None        # previous "HH:MM" seen, to detect the start time being crossed
        self.busy = False
        self.done_before = 0

    def is_open(self, now, unfinished):
        sc = self.settings["schedule"]
        if not sc["enabled"] or not HHMM.fullmatch(sc["start"]):
            self.last = now
            return True
        start, stop = sc["start"], sc["stop"]
        if HHMM.fullmatch(stop or ""):
            self.last = now
            return start <= now < stop if start < stop else (now >= start or now < stop)
        last, self.last = self.last, now
        if last is not None and (last < start <= now or (now < last and (start > last or start <= now))):
            self.released = True
        if not unfinished:
            self.released = False
        return self.released

    def finished_action(self, items):
        """Returns the configured after-action once, when a batch of downloads has just completed."""
        busy = any(d.status in ("downloading", "queued") for d in items)
        done = sum(d.status == "done" for d in items)
        if busy:
            if not self.busy:
                self.busy, self.done_before = True, done
            return None
        if not self.busy:
            return None
        self.busy = False
        # a manual pause must never power the machine off
        if done > self.done_before and not any(d.status == "paused" for d in items) and self.settings["after"] != "none":
            return self.settings["after"]
        return None


def load_settings():
    s = {"directory": default_dir(), "limit_kbps": 0, "parallel": 3, "after": "none", "sort": False, "scan": False,
         "schedule": {"enabled": False, "start": "23:00", "stop": ""}}
    try:
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not os.path.isdir(s["directory"]):
        s["directory"] = default_dir()
    s["schedule"] = {"enabled": False, "start": "23:00", "stop": "", **(s["schedule"] if isinstance(s["schedule"], dict) else {})}
    if s["after"] not in AFTER_ACTIONS:
        s["after"] = "none"
    return s


def save_settings(s):
    write_private(SETTINGS_FILE, s)


def write_private(path, obj):
    """Write JSON readable only by the owner (the queue can hold cookies)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


HASH_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def parse_checksum(text):
    """'sha256:ab12..' or a bare hex digest -> (algorithm, lowercase hex). Raises ValueError."""
    text = (text or "").strip().lower()
    algo, _, digest = text.rpartition(":")
    digest = digest.split()[0] if digest.split() else ""
    if not re.fullmatch(r"[0-9a-f]+", digest) or len(digest) not in HASH_BY_LEN:
        raise ValueError("checksum harus heksadesimal 32/40/64/128 karakter (MD5/SHA-1/SHA-256/SHA-512)")
    algo = algo or HASH_BY_LEN[len(digest)]
    if algo not in hashlib.algorithms_available or hashlib.new(algo).digest_size * 2 != len(digest):
        raise ValueError(f"algoritma {algo} tidak cocok dengan panjang checksum")
    return algo, digest


def human(n, suffix=""):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}{suffix}" if unit == "B" else f"{n:.1f} {unit}{suffix}"
        n /= 1024


def filename_from(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip())
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        name = m.group(1).strip() if m else ""
    if not name:
        name = unquote(os.path.basename(urlsplit(url).path))
    name = os.path.basename(name.replace("\\", "/")).strip().lstrip(".")
    return name or "download"


def unique_path(directory, name):
    path = os.path.join(directory, name)
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(path) or os.path.exists(path + ".part"):
        path = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return path


def _iso_seconds(text):
    """ISO 8601 duration such as PT1H2M3.5S -> seconds (0 when absent or unparseable)."""
    m = re.fullmatch(r"P(?:\d+Y)?(?:\d+M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", text or "")
    if not m:
        return 0.0
    d, h, mi, sec = (float(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def parse_mpd(text, base_url):
    """DASH manifest -> {"video": track, "audio": track}; track = {"bandwidth", "id", "urls"}.

    Picks the highest-bandwidth representation of each kind from the first Period. urls lists the
    initialization segment (if any) followed by the media segments, in order. Raises RuntimeError
    for live streams and DRM, which cannot be downloaded.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"manifest DASH rusak: {e}") from e
    local = lambda e: e.tag.rsplit("}", 1)[-1]
    kids = lambda e, name: [c for c in e if local(c) == name]
    first = lambda e, name: next(iter(kids(e, name)), None)
    if root.get("type") == "dynamic":
        raise RuntimeError("stream live tidak didukung")
    period = first(root, "Period")
    if period is None:
        raise RuntimeError("manifest DASH tanpa Period")
    duration = _iso_seconds(period.get("duration")) or _iso_seconds(root.get("mediaPresentationDuration"))
    tracks = {}
    for aset in kids(period, "AdaptationSet"):
        for rep in kids(aset, "Representation"):
            mime = rep.get("mimeType") or aset.get("mimeType") or ""
            kind = (rep.get("contentType") or aset.get("contentType") or mime.split("/")[0]).lower()
            if kind not in ("video", "audio"):
                continue
            if kids(aset, "ContentProtection") or kids(rep, "ContentProtection"):
                raise RuntimeError("video ber-DRM tidak bisa diunduh")
            bw = int(rep.get("bandwidth") or 0)
            if kind in tracks and tracks[kind]["bandwidth"] >= bw:
                continue
            base = base_url
            for node in (root, period, aset, rep):
                b = first(node, "BaseURL")
                if b is not None and (b.text or "").strip():
                    base = urljoin(base, b.text.strip())
            tmpl = next((t for t in (first(rep, "SegmentTemplate"), first(aset, "SegmentTemplate"),
                                     first(period, "SegmentTemplate")) if t is not None), None)
            ids = {"RepresentationID": rep.get("id") or "", "Bandwidth": str(bw)}
            if tmpl is not None:
                urls = _template_urls(tmpl, ids, base, duration, first)
            else:
                lst = first(rep, "SegmentList") or first(aset, "SegmentList")
                if lst is not None:
                    init = first(lst, "Initialization")
                    urls = ([urljoin(base, init.get("sourceURL"))] if init is not None and init.get("sourceURL") else []) + \
                           [urljoin(base, u.get("media")) for u in kids(lst, "SegmentURL") if u.get("media")]
                else:
                    urls = [base]  # a single file (BaseURL / SegmentBase)
            if urls:
                tracks[kind] = {"bandwidth": bw, "id": rep.get("id"), "urls": urls}
    if not tracks:
        raise RuntimeError("manifest DASH tidak berisi video atau audio")
    return tracks


def _template_urls(tmpl, ids, base, duration, first):
    scale = int(tmpl.get("timescale") or 1)
    number = int(tmpl.get("startNumber") or 1)

    def fill(pattern, **extra):
        def sub(m):
            name, fmt = m.group(1), m.group(2)
            if name == "":
                return "$"
            val = {**ids, **extra}[name]
            return (f"%{fmt[1:]}" % int(val)) if fmt else str(val)
        return urljoin(base, re.sub(r"\$(\w*)(%0\d+d)?\$", sub, pattern))

    urls = []
    if tmpl.get("initialization"):
        urls.append(fill(tmpl.get("initialization")))
    media = tmpl.get("media")
    if not media:
        return urls
    timeline = first(tmpl, "SegmentTimeline")
    if timeline is not None:
        t = 0
        for s in (c for c in timeline if c.tag.rsplit("}", 1)[-1] == "S"):
            t = int(s.get("t") or t)
            d, r = int(s.get("d")), int(s.get("r") or 0)
            if r < 0:  # repeat until the end of the Period
                r = max(0, math.ceil((duration * scale - t) / d) - 1)
            for _ in range(r + 1):
                urls.append(fill(media, Number=number, Time=t))
                number, t = number + 1, t + d
    elif tmpl.get("duration"):
        if not duration:
            raise RuntimeError("manifest DASH tanpa durasi")
        count = math.ceil(duration * scale / int(tmpl.get("duration")))
        urls += [fill(media, Number=number + i, Time=i * int(tmpl.get("duration"))) for i in range(count)]
    return urls


class Download:
    """One file. States: queued, downloading, paused, done, error, cancelled."""

    def __init__(self, url, directory=None, segments=8, name=None, headers=None, sort=False):
        self.url = url
        self.sort = sort        # put the file in a sub-folder by type (Video, Arsip, ...)
        self.scan = ""          # "", "scanning", "clean", "infected" or "error"
        self.scan_detail = ""
        self.headers = headers or {}   # extra request headers (Cookie, Referer) from the browser
        self.directory = directory or default_dir()
        self.max_segments = segments
        self.name = name
        self.path = None
        self.size = 0
        self.segs = []          # [start, end, pos]; end inclusive
        self.streamed = 0       # bytes received when size/ranges are unknown
        self.hls = False        # HLS playlist: segments are fetched, then joined
        self.seg_total = self.seg_done = self.hls_bytes = 0
        self.checksum = None    # (algorithm, hex digest) the finished file must match
        self.verify = ""        # "", "checking", "ok" or "bad"
        self.notified = False   # GUI: completion notification already shown
        self.resumable = False
        self.status = "queued"
        self.error = ""
        self.speed = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._last = (time.monotonic(), 0)

    # -- persistence ------------------------------------------------------
    def to_dict(self):
        return {"url": self.url, "directory": self.directory, "segments": self.max_segments,
                "name": self.name, "path": self.path, "status": self.status, "headers": self.headers,
                "checksum": ":".join(self.checksum) if self.checksum else None, "verify": self.verify,
                "sort": self.sort, "scan": self.scan if self.scan != "scanning" else "", "scan_detail": self.scan_detail}

    @classmethod
    def from_dict(cls, d):
        dl = cls(d["url"], d.get("directory"), d.get("segments", 8), d.get("name"), d.get("headers"), d.get("sort", False))
        dl.path = d.get("path")
        dl.scan, dl.scan_detail = d.get("scan", ""), d.get("scan_detail", "")
        if d.get("checksum"):
            dl.checksum, dl.verify = parse_checksum(d["checksum"]), d.get("verify", "")
        # a download still marked "downloading" means the app died mid-transfer: resume it
        saved = d.get("status")
        dl.status = "done" if saved == "done" else "queued" if saved in ("downloading", "queued") else "paused"
        if dl.status == "done" and dl.path and os.path.exists(dl.path):
            dl.size = os.path.getsize(dl.path)
            dl.segs = [[0, dl.size - 1, dl.size]]
        elif dl.status == "done":
            dl.status = "paused"
        dl._load_meta()
        dl.notified = dl.status == "done"
        if dl.status == "done" and dl.checksum and dl.verify not in ("ok", "bad"):
            dl.start_verify()
        return dl

    @property
    def meta_path(self):
        return self.path + ".kdl" if self.path else None

    def _save_meta(self):
        if self.path and self.resumable:
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"url": self.url, "size": self.size, "segs": self.segs}, f)
            os.replace(tmp, self.meta_path)

    def _load_meta(self):
        if self.status == "done" or not self.path or not os.path.exists(self.meta_path):
            return
        try:
            with open(self.meta_path) as f:
                m = json.load(f)
            if m["url"] == self.url and os.path.exists(self.path + ".part"):
                self.size, self.segs, self.resumable = m["size"], m["segs"], True
        except (OSError, ValueError, KeyError):
            pass

    # -- progress ---------------------------------------------------------
    @property
    def downloaded(self):
        if self.hls:
            return self.hls_bytes
        if self.resumable:
            return sum(min(p, e + 1) - s for s, e, p in self.segs)
        return self.streamed

    @property
    def fraction(self):
        if self.hls:
            return 1.0 if self.status == "done" else self.seg_done / self.seg_total if self.seg_total else 0.0
        return min(1.0, self.downloaded / self.size) if self.size else 0.0

    def tick(self):
        now, done = time.monotonic(), self.downloaded
        t0, d0 = self._last
        if now - t0 >= 0.5:
            inst = max(0, done - d0) / (now - t0)
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            self._last = (now, done)
        if self.status != "downloading":
            self.speed = 0.0

    @property
    def eta(self):
        if self.status == "downloading" and self.speed > 1 and self.size:
            return int((self.size - self.downloaded) / self.speed)
        return None

    # -- control ----------------------------------------------------------
    def start(self):
        if self.status in ("downloading", "done"):
            return
        self._stop.clear()
        self.status, self.error = "downloading", ""
        self._last = (time.monotonic(), self.downloaded)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        if self.status == "downloading":
            self._stop.set()
            self.status = "paused"

    def cancel(self, delete_files=True):
        self._stop.set()
        self.status = "cancelled"
        if delete_files and self.path and not os.path.exists(self.path):
            for p in (self.path + ".part", self.meta_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            shutil.rmtree(self.path + ".hls", ignore_errors=True)

    def join(self):
        if self._thread:
            self._thread.join()

    # -- checksum ---------------------------------------------------------
    def set_checksum(self, text):
        """Set (or clear, with empty text) the expected digest; verifies at once if already finished."""
        self.checksum = parse_checksum(text) if text and text.strip() else None
        self.verify = ""
        if self.checksum and self.status == "done":
            self.start_verify()

    def finish(self):
        self.start_verify()
        if SCAN["on"]:
            self.scan, self.scan_detail = "scanning", ""
            threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        self.scan_detail = ""
        state, detail = scan_file(self.path)
        self.scan_detail = detail
        self.scan = state

    def start_verify(self):
        if not (self.checksum and self.path and os.path.exists(self.path)):
            return
        self.verify = "checking"
        threading.Thread(target=self._verify, daemon=True).start()

    def _verify(self):
        algo, want = self.checksum
        try:
            h = hashlib.new(algo)
            with open(self.path, "rb") as f:
                while block := f.read(1 << 20):
                    h.update(block)
            self.verify = "ok" if h.hexdigest() == want else "bad"
        except OSError:
            self.verify = ""

    # -- expired links ----------------------------------------------------
    def replace_url(self, new_url, headers=None):
        """Point an unfinished download at a fresh link for the same file; progress is kept."""
        if self.status == "downloading":
            raise ValueError("jeda unduhannya dulu")
        if self.status == "done":
            raise ValueError("unduhan sudah selesai")
        if self.hls:
            raise ValueError("ganti link belum didukung untuk HLS")
        old = self.headers
        if headers is not None:
            self.headers = headers
        try:
            with self._open(new_url, 0, 0) as r:
                final = r.geturl()
                m = re.match(r"bytes\s+0-0/(\d+)", r.headers.get("Content-Range", ""))
                ranges = r.status == 206 and bool(m)
                size = int(m[1]) if ranges else int(r.headers.get("Content-Length") or 0)
        except (OSError, ValueError) as e:
            self.headers = old
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            raise ValueError(f"link baru tidak bisa dibuka: {e}") from e
        if self.size and size != self.size:
            self.headers = old
            raise ValueError(f"ukuran berbeda ({human(size)} vs {human(self.size)}); itu bukan file yang sama")
        if self.resumable and not ranges:
            self.headers = old
            raise ValueError("server link baru tidak mendukung lanjut (Range)")
        self.url, self.error = final, ""
        if self.status == "error":
            self.status = "paused"
        self._save_meta()

    # -- engine -----------------------------------------------------------
    def _open(self, url, start=None, end=None):
        hdrs = {"User-Agent": UA, "Accept-Encoding": "identity", **self.headers}
        req = urllib.request.Request(url, headers=hdrs)
        if start is not None:
            req.add_header("Range", f"bytes={start}-{end if end is not None else ''}")
        return urllib.request.urlopen(req, timeout=30)

    def _probe(self):
        with self._open(self.url, 0, 0) as r:
            self.url = r.geturl()
            if not self.name:
                self.name = filename_from(r, self.url)
            cr = r.headers.get("Content-Range", "")
            m = re.match(r"bytes\s+0-0/(\d+)", cr)
            if r.status == 206 and m:
                self.size, self.resumable = int(m.group(1)), True
            else:
                self.size = int(r.headers.get("Content-Length") or 0)
                self.resumable = False

    def _run(self):
        try:
            path = urlsplit(self.url).path
            if self.hls or HLS_RE.search(path) or DASH_RE.search(path):
                self.hls = True  # segment-based progress
                (self._run_dash if DASH_RE.search(path) else self._run_hls)()
                return
            if not self.segs:
                self._probe()
                self.path = self.path or self._choose_path(self.name)
                self.name = os.path.basename(self.path)
                if self.resumable and self.size:
                    n = max(1, min(self.max_segments, self.size // MIN_SEGMENT))
                    step = -(-self.size // n)
                    self.segs = [[i * step, min(self.size, (i + 1) * step) - 1, i * step] for i in range(n)]
                else:
                    self.resumable = False
                    self.segs = []
            self.streamed = 0
            if self.resumable:
                self._run_segments()
            else:
                self._run_stream()
            if self._stop.is_set():
                return
            os.replace(self.path + ".part", self.path)
            if self.meta_path and os.path.exists(self.meta_path):
                os.remove(self.meta_path)
            self.status = "done"
            self.finish()
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            if not self._stop.is_set():
                self.status, self.error = "error", str(e) or e.__class__.__name__
                if isinstance(e, urllib.error.HTTPError) and e.code in FATAL_HTTP:
                    self.error = f"HTTP {e.code} - link mungkin kedaluwarsa, pakai 'Ganti link'"

    def _fetch_text(self, url):
        with self._open(url) as r:
            return r.read(8 << 20).decode("utf-8", "replace")

    def _run_hls(self):
        """Download an unencrypted VOD HLS stream and join its segments into one file."""
        base, text = self.url, self._fetch_text(self.url)
        for _ in range(3):  # master playlist -> best variant
            lines = text.splitlines()
            variants = [(int((re.search(r"BANDWIDTH=(\d+)", l) or [0, 0])[1]), nxt)
                        for l, nxt in zip(lines, lines[1:] + [""]) if l.startswith("#EXT-X-STREAM-INF")]
            if not variants:
                break
            uri = next(x for x in lines[lines.index(max(variants)[1]):] if x and not x.startswith("#"))
            base = urljoin(base, uri)
            text = self._fetch_text(base)
        if re.search(r"#EXT-X-KEY:(?!.*METHOD=NONE)", text):
            raise RuntimeError("HLS terenkripsi (AES/DRM) belum didukung")
        if "#EXT-X-ENDLIST" not in text:
            raise RuntimeError("stream live tidak didukung")
        init = re.search(r'#EXT-X-MAP:.*URI="([^"]+)"', text)
        urls = ([urljoin(base, init.group(1))] if init else []) + \
               [urljoin(base, l.strip()) for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not urls:
            raise RuntimeError("playlist kosong")
        if not self.path:
            self.path = self._choose_path(self._stem() + (".mp4" if init else ".ts"))
            self.name = os.path.basename(self.path)
        work = self.path + ".hls"
        files = self._fetch_parts(urls, work)
        if files is None:
            return
        with open(self.path + ".part", "wb") as out:
            for f in files:
                with open(f, "rb") as src:
                    shutil.copyfileobj(src, out, 1 << 20)
        os.replace(self.path + ".part", self.path)
        shutil.rmtree(work, ignore_errors=True)
        self.size = os.path.getsize(self.path)
        self.status = "done"
        self.finish()

    def _fetch_parts(self, urls, work):
        """Download every URL into work/NNNNNN, skipping finished ones. Returns the file list, or None if stopped."""
        os.makedirs(work, exist_ok=True)
        files = [os.path.join(work, f"{i:06d}") for i in range(len(urls))]
        self.seg_total = len(urls)
        self.seg_done = sum(os.path.exists(f) for f in files)
        self.hls_bytes = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        self.size = int(self.hls_bytes / self.seg_done * self.seg_total) if self.seg_done else 0
        todo, lock, errors = [i for i, f in enumerate(files) if not os.path.exists(f)], threading.Lock(), []

        def worker():
            while not self._stop.is_set() and not errors:
                with lock:
                    if not todo:
                        return
                    i = todo.pop(0)
                for attempt in range(RETRIES + 1):
                    if self._stop.is_set():
                        return
                    try:
                        got = 0
                        with self._open(urls[i]) as r, open(files[i] + ".tmp", "wb") as out:
                            while data := r.read(CHUNK):
                                LIMITER.consume(len(data))
                                out.write(data)
                                got += len(data)
                                if self._stop.is_set():
                                    return
                        os.replace(files[i] + ".tmp", files[i])
                        break
                    except (OSError, urllib.error.URLError) as e:
                        if attempt == RETRIES:
                            errors.append(e)
                            return
                        self._stop.wait(min(2 ** attempt, 20))
                with lock:
                    self.seg_done += 1
                    self.hls_bytes += got
                    self.size = int(self.hls_bytes / self.seg_done * self.seg_total)

        workers = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, self.max_segments))]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        if self._stop.is_set():
            return None
        if errors:
            raise errors[0]
        return files

    def _run_dash(self):
        """Download a static DASH stream; join the segments per track and mux audio+video with ffmpeg."""
        tracks = parse_mpd(self._fetch_text(self.url), self.url)
        order = [k for k in ("video", "audio") if k in tracks]
        if len(order) == 2 and not shutil.which("ffmpeg"):
            raise RuntimeError("butuh ffmpeg untuk menggabung audio+video: sudo apt install ffmpeg")
        if not self.path:
            self.path = self._choose_path(self._stem() + ".mp4")
            self.name = os.path.basename(self.path)
        work = self.path + ".hls"
        urls = [u for k in order for u in tracks[k]["urls"]]
        files = self._fetch_parts(urls, work)
        if files is None:
            return
        joined, i = [], 0
        for k in order:
            n = len(tracks[k]["urls"])
            dest = os.path.join(work, k + ".mp4")
            with open(dest, "wb") as out:
                for f in files[i:i + n]:
                    with open(f, "rb") as src:
                        shutil.copyfileobj(src, out, 1 << 20)
            joined.append(dest)
            i += n
        part = self.path + ".part"
        if len(joined) == 1:
            os.replace(joined[0], part)
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", joined[0], "-i", joined[1],
                   "-c", "copy", "-f", "mp4", "-movflags", "+faststart", part]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError("ffmpeg gagal: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        os.replace(part, self.path)
        shutil.rmtree(work, ignore_errors=True)
        self.size = os.path.getsize(self.path)
        self.status = "done"
        self.finish()

    def _choose_path(self, name):
        target = self.directory
        if self.sort and category_for(name):
            target = os.path.join(target, category_for(name))
        os.makedirs(target, exist_ok=True)
        return unique_path(target, name)

    def _stem(self):
        strip_ext = lambda n: re.sub(r"\.(m3u8|mpd|ts|mp4)$", "", n, flags=re.I)  # keep dots in titles like "Film 2.0"
        return strip_ext(self.name or "") or strip_ext(unquote(os.path.basename(urlsplit(self.url).path))) or "video"

    def _run_segments(self):
        fd = os.open(self.path + ".part", os.O_RDWR | os.O_CREAT)
        errors = []
        try:
            os.ftruncate(fd, self.size)
            self._save_meta()
            workers = [threading.Thread(target=self._segment, args=(fd, s, errors), daemon=True)
                       for s in self.segs if s[2] <= s[1]]
            for w in workers:
                w.start()
            while any(w.is_alive() for w in workers):
                time.sleep(1)
                self._save_meta()
            for w in workers:
                w.join()
            self._save_meta()
        finally:
            os.close(fd)
        if errors and not self._stop.is_set():
            raise errors[0]

    def _segment(self, fd, seg, errors):
        failures = 0
        while seg[2] <= seg[1] and not self._stop.is_set() and not errors:
            try:
                with self._open(self.url, seg[2], seg[1]) as r:
                    if r.status != 206:
                        raise OSError(f"server ignored Range request (HTTP {r.status})")
                    while not self._stop.is_set():
                        data = r.read(min(CHUNK, seg[1] + 1 - seg[2]))
                        if not data:
                            break
                        LIMITER.consume(len(data))
                        os.pwrite(fd, data, seg[2])
                        seg[2] += len(data)
                        failures = 0
                        if seg[2] > seg[1]:
                            break
                if seg[2] <= seg[1] and not self._stop.is_set():
                    raise OSError("connection closed early")
            except (OSError, urllib.error.URLError) as e:
                failures += 1
                if failures > RETRIES or (isinstance(e, urllib.error.HTTPError) and e.code in FATAL_HTTP):
                    errors.append(e)
                    return
                self._stop.wait(min(2 ** failures, 20))

    def _run_stream(self):
        with self._open(self.url) as r, open(self.path + ".part", "wb") as f:
            while not self._stop.is_set():
                data = r.read(CHUNK)
                if not data:
                    break
                LIMITER.consume(len(data))
                f.write(data)
                self.streamed += len(data)
        if not self.size:
            self.size = self.streamed


class Manager:
    """Runs at most `parallel` downloads at once; the rest wait in the queue."""

    def __init__(self, parallel=3):
        self.parallel = parallel
        self.items = []
        self.hold = False   # True outside the scheduled window: nothing new may start

    def add(self, dl, start=True):
        self.items.append(dl)
        if start:
            dl.status = "queued"
        self.schedule()
        return dl

    def remove(self, dl):
        dl.cancel(delete_files=dl.status != "done")
        if dl in self.items:
            self.items.remove(dl)
        self.schedule()

    def schedule(self):
        if self.hold:
            return
        running = sum(d.status == "downloading" for d in self.items)
        for d in self.items:
            if running >= self.parallel:
                break
            if d.status == "queued":
                d.start()
                running += 1

    def save(self):
        write_private(QUEUE_FILE, [d.to_dict() for d in self.items if d.status != "cancelled"])

    def load(self):
        try:
            with open(QUEUE_FILE) as f:
                self.items = [Download.from_dict(d) for d in json.load(f)]
        except (OSError, ValueError, KeyError):
            self.items = []


def fmt_eta(s):
    if s is None:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}j {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def status_text(d):
    if d.status == "error":
        return f"Gagal: {d.error}"
    if d.status == "done":
        mark = {"checking": " - memverifikasi checksum...", "ok": " - \u2713 checksum cocok",
                "bad": " - \u2717 CHECKSUM TIDAK COCOK, file mungkin rusak"}.get(d.verify, "")
        mark += {"scanning": " - memindai virus...", "clean": " - \u2713 bebas virus",
                 "infected": f" - \u2717 VIRUS: {d.scan_detail}", "error": f" - pemindaian virus gagal ({d.scan_detail})"}.get(d.scan, "")
        return f"Selesai - {human(d.size)}{mark}"
    total = human(d.size) if d.size else "?"
    base = f"{human(d.downloaded)} / {total}"
    if d.status == "downloading":
        eta = fmt_eta(d.eta)
        return f"{base} - {human(d.speed, '/s')}" + (f" - sisa {eta}" if eta else "")
    if d.status == "queued" and getattr(d, "held", False):
        return f"Menunggu jadwal - {base}"
    return {"queued": "Antri", "paused": "Jeda", "cancelled": "Dibatalkan"}[d.status] + f" - {base}"


# ---------------------------------------------------------------- CLI
def run_cli(urls, directory, segments, checksum=None, sort=False, scan=False):
    SCAN["on"] = scan
    items = [Download(u, directory, segments, sort=sort) for u in urls]
    if checksum:
        if len(items) != 1:
            print("--checksum hanya untuk satu URL", file=sys.stderr)
            return 2
        try:
            items[0].set_checksum(checksum)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    mgr = Manager(parallel=len(items))
    for d in items:
        mgr.add(d)
    try:
        while any(d.status in ("queued", "downloading") for d in items):
            for d in items:
                d.tick()
            line = " | ".join(f"{(d.name or d.url)[:28]} {d.fraction * 100:5.1f}% {human(d.speed, '/s')}" for d in items)
            print("\r" + line[:shutil_width()].ljust(shutil_width()), end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        for d in items:
            d.pause()
        print("\nDijeda. Jalankan perintah yang sama untuk melanjutkan.")
        return 130
    while any(d.verify == "checking" or d.scan == "scanning" for d in items):
        time.sleep(0.2)
    print()
    ok = True
    for d in items:
        extra = {"ok": " [checksum cocok]", "bad": " [CHECKSUM TIDAK COCOK]"}.get(d.verify, "")
        extra += {"clean": " [bebas virus]", "infected": f" [VIRUS: {d.scan_detail}]", "error": f" [scan gagal: {d.scan_detail}]"}.get(d.scan, "")
        print(f"{'OK  ' if d.status == 'done' else 'GAGAL'} {d.path or d.url} {d.error}{extra}")
        ok &= d.status == "done" and d.verify != "bad" and d.scan != "infected"
    return 0 if ok else 1


def shutil_width():
    import shutil
    return shutil.get_terminal_size((100, 20)).columns - 1


# ---------------------------------------------------------------- GUI
def start_bridge(on_add):
    """Local endpoint the browser extension posts links to (127.0.0.1 only)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"app": "kilatdl"}) if self.path == "/ping" else self._send(404, {})

        def do_POST(self):
            # Web pages cannot forge these: the custom header forces a CORS preflight
            # that we never approve, and only extensions send an extension Origin.
            origin = self.headers.get("Origin", "")
            if (self.path != "/add" or self.headers.get("X-KilatDL") != "1"
                    or not origin.startswith(("chrome-extension://", "moz-extension://"))):
                return self._send(403, {"error": "forbidden"})
            try:
                size = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(size, 1 << 16)))
                url = body["url"]
                if not isinstance(url, str) or not re.match(r"https?://", url):
                    raise ValueError("bad url")
                headers = {k: v for k, v in (body.get("headers") or {}).items()
                           if k in ("Cookie", "Referer") and isinstance(v, str) and "\n" not in v and "\r" not in v}
                name = body.get("name")
                name = os.path.basename(re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name)).strip(". ")[:120] if isinstance(name, str) else None
                on_add(url, headers, name or None)
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad request"})
            self._send(200, {"ok": True})

        def log_message(self, *args):
            pass

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_gui(urls, background=False):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

    CSS = b"""
    .dl-card { padding: 14px 16px; border-radius: 16px; background: @card_bg_color;
               box-shadow: 0 0 0 1px alpha(@borders, .6), 0 1px 3px alpha(black, .08); }
    .dl-icon { background: alpha(@accent_bg_color, .14); color: @accent_color; border-radius: 12px;
               min-width: 46px; min-height: 46px; }
    .dl-done .dl-icon { background: alpha(@success_bg_color, .16); color: @success_color; }
    .dl-error .dl-icon { background: alpha(@error_bg_color, .16); color: @error_color; }
    .dl-title { font-weight: 700; font-size: 1.02em; }
    .dl-info { font-size: 0.88em; opacity: .72; }
    .dl-error .dl-info { color: @error_color; opacity: 1; }
    .dl-card progressbar > trough, .dl-card progressbar > trough > progress { min-height: 6px; border-radius: 3px; }
    .dl-done progressbar > trough > progress { background: @success_bg_color; }
    .dl-error progressbar > trough > progress { background: @error_bg_color; }
    .statusbar { padding: 8px 18px; font-size: 0.9em; opacity: .8; }
    """
    ICONS = (
        (("mp4", "mkv", "webm", "mov", "m4v", "ts", "avi"), "video-x-generic-symbolic"),
        (("mp3", "m4a", "aac", "ogg", "opus", "flac", "wav"), "audio-x-generic-symbolic"),
        (("zip", "rar", "7z", "tar", "gz", "xz", "bz2", "iso", "img", "dmg"), "package-x-generic-symbolic"),
        (("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "txt", "epub"), "x-office-document-symbolic"),
        (("exe", "msi", "deb", "rpm", "appimage", "apk", "sh"), "application-x-executable-symbolic"),
        (("jpg", "jpeg", "png", "gif", "webp", "svg"), "image-x-generic-symbolic"),
    )

    def icon_for(name):
        ext = os.path.splitext(name or "")[1].lstrip(".").lower()
        return next((icon for exts, icon in ICONS if ext in exts), "folder-download-symbolic")

    def open_path(path):
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path), None)
        except GLib.Error:
            pass  # no handler for this file type

    def icon_button(icon, tip, callback, *css):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
        for c in ("flat", "circular") + css:
            b.add_css_class(c)
        b.connect("clicked", callback)
        return b

    settings = load_settings()
    mgr = Manager(settings["parallel"])
    scheduler = Scheduler(settings)
    SCAN["on"] = bool(settings["scan"]) and bool(shutil.which("clamscan") or shutil.which("clamdscan"))
    mgr.load()
    LIMITER.rate = settings["limit_kbps"] * 1024

    class Row(Gtk.Box):
        def __init__(self, dl, win):
            super().__init__(spacing=14)
            self.dl, self.win = dl, win
            self.add_css_class("dl-card")
            self.icon = Gtk.Image(icon_name=icon_for(dl.name or dl.url), pixel_size=22, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            frame = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.START, hexpand=False)
            frame.set_size_request(46, 46)
            frame.add_css_class("dl-icon")
            frame.append(self.icon)
            self.icon.set_hexpand(True)
            self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            self.title.add_css_class("dl-title")
            self.bar = Gtk.ProgressBar()
            self.info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.info.add_css_class("dl-info")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
            for w in (self.title, self.bar, self.info):
                text.append(w)
            self.toggle = icon_button("media-playback-pause-symbolic", "Jeda", self.on_toggle)
            self.open_btn = icon_button("document-open-symbolic", "Buka file", lambda *_: open_path(self.dl.path))
            self.folder_btn = icon_button("folder-open-symbolic", "Buka folder", self.on_folder)
            self.link_btn = icon_button("insert-link-symbolic", "Ganti link (kalau link kedaluwarsa)", self.on_link)
            self.sum_btn = icon_button("object-select-symbolic", "Verifikasi checksum", self.on_checksum)
            self.rm_btn = icon_button("user-trash-symbolic", "Hapus dari daftar", self.on_remove)
            actions = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            for b in (self.toggle, self.open_btn, self.folder_btn, self.link_btn, self.sum_btn, self.rm_btn):
                actions.append(b)
            for w in (frame, text, actions):
                self.append(w)
            self.refresh()

        def on_toggle(self, _):
            d = self.dl
            if d.status in ("downloading", "queued"):
                d.pause()
            elif d.status in ("paused", "error", "cancelled"):
                d.status, d.notified = "queued", False
            mgr.schedule()

        def on_link(self, _):
            d = self.dl
            if d.status == "downloading":
                d.pause()
                d.join()

            def apply(text, done):
                def work():
                    try:
                        d.replace_url(text.strip())
                        GLib.idle_add(done, None)
                    except ValueError as e:
                        GLib.idle_add(done, str(e))
                threading.Thread(target=work, daemon=True).start()

            self.win.prompt("Ganti link", "Alamat baru (URL)", "Ukuran file harus sama dengan yang lama. Progres dilanjutkan.", apply)

        def on_checksum(self, _):
            def apply(text, done):
                try:
                    self.dl.set_checksum(text)
                    done(None)
                except ValueError as e:
                    done(str(e))

            self.win.prompt("Verifikasi checksum", "sha256:... (MD5, SHA-1, SHA-256 atau SHA-512)",
                            "Tempel hash dari situs unduhan. Kosongkan untuk menghapus.", apply)

        def on_folder(self, _):
            open_path(self.dl.directory)

        def on_remove(self, _):
            mgr.remove(self.dl)
            self.win.rebuild()

        def refresh(self):
            d = self.dl
            d.tick()
            self.title.set_text(d.name or d.url)
            self.icon.set_from_icon_name(icon_for(d.name or d.url))
            self.bar.set_fraction(d.fraction if d.status != "done" else 1.0)
            self.info.set_text(status_text(d))
            for state in ("dl-done", "dl-error"):
                self.remove_css_class(state)
            if d.status == "done" and d.verify != "bad":
                self.add_css_class("dl-done")
            elif d.status == "error" or d.verify == "bad":
                self.add_css_class("dl-error")
            icon, tip = {"downloading": ("media-playback-pause-symbolic", "Jeda"),
                         "queued": ("media-playback-stop-symbolic", "Batal antri")}.get(
                d.status, ("media-playback-start-symbolic", "Lanjut"))
            self.toggle.set_icon_name(icon)
            self.toggle.set_tooltip_text(tip)
            self.toggle.set_visible(d.status != "done")
            self.open_btn.set_visible(d.status == "done")
            self.link_btn.set_visible(d.status in ("paused", "error", "cancelled", "queued") and not d.hls)
            self.sum_btn.set_visible(d.status in ("done", "downloading") or d.checksum is not None)

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="KilatDL", default_width=760, default_height=640)
            self.filter = "all"
            self.rows = {}
            if os.environ.get("KILATDL_SCHEME") == "dark":  # test hook for screenshots
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

            add = Gtk.Button(child=Adw.ButtonContent(label="Tambah", icon_name="list-add-symbolic"))
            add.add_css_class("suggested-action")
            add.set_tooltip_text("Tambah unduhan (Ctrl+N)")
            add.connect("clicked", lambda *_: self.ask_url())

            tabs = Gtk.Box(css_classes=["linked"])
            first = None
            for key, label in (("all", "Semua"), ("active", "Aktif"), ("done", "Selesai")):
                b = Gtk.ToggleButton(label=label, active=key == "all")
                if first:
                    b.set_group(first)
                first = first or b
                b.connect("toggled", self.on_filter, key)
                tabs.append(b)

            header = Adw.HeaderBar(title_widget=tabs)
            header.pack_start(add)
            header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", popover=self.build_menu(app), tooltip_text="Pengaturan"))

            self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                                margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
            scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroll.set_child(Adw.Clamp(maximum_size=820, tightening_threshold=620, child=self.list))

            has_logo = Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).has_icon(ICON_NAME)
            empty = Adw.StatusPage(icon_name=ICON_NAME if has_logo else "folder-download-symbolic", title="Belum ada unduhan",
                                   description="Tempel link dengan Ctrl+V, tarik tautan ke jendela ini,\natau klik Tambah.")
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            self.stack.add_named(empty, "empty")
            self.stack.add_named(scroll, "list")

            self.stats = Gtk.Label(xalign=0, hexpand=True)
            self.stats.add_css_class("statusbar")

            view = Adw.ToolbarView()
            view.add_top_bar(header)
            view.set_content(self.stack)
            view.add_bottom_bar(self.stats)
            self.toasts = Adw.ToastOverlay(child=view)
            self.set_content(self.toasts)

            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
            drop.connect("drop", lambda _t, value, _x, _y: self.add_text(value) > 0)
            self.add_controller(drop)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.on_key)
            self.add_controller(keys)

            self.connect("close-request", lambda *_: (self.set_visible(False), True)[1])
            self.rebuild()
            GLib.timeout_add(500, self.tick)
            if os.environ.get("KILATDL_TEST_DIALOG") == "schedule":  # test hook
                GLib.timeout_add(800, lambda: self.ask_schedule() and False)
            if os.environ.get("KILATDL_SCREENSHOT"):  # test hook: render the window to a PNG, then quit
                GLib.timeout_add(2500, self.snapshot)

        # -- settings menu ---------------------------------------------------
        def build_menu(self, app):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
            box.set_size_request(280, -1)
            limit = Gtk.SpinButton.new_with_range(0, 1_000_000, 100)
            limit.set_value(settings["limit_kbps"])
            limit.connect("value-changed", self.on_limit)
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label="Batas kecepatan (KB/s)", xalign=0, hexpand=True))
            row.append(limit)
            hint = Gtk.Label(label="0 = tanpa batas", xalign=0)
            hint.add_css_class("dim-label")
            self.folder_btn = Gtk.Button()
            self.folder_btn.connect("clicked", lambda *_: self.pick_folder(self.set_default_dir))
            self.show_default_dir()
            def switch_row(label, key, on_change=None):
                sw = Gtk.Switch(active=bool(settings[key]), valign=Gtk.Align.CENTER)

                def changed(_sw, state):
                    if on_change and not on_change(state):
                        GLib.idle_add(lambda: sw.set_active(False) and False)  # refused: put the switch back
                        return True
                    settings[key] = state
                    save_settings(settings)
                    sw.set_state(state)
                    return True

                sw.connect("state-set", changed)
                row = Gtk.Box(spacing=8)
                row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
                row.append(sw)
                return row

            def toggle_scan(state):
                if state and not (shutil.which("clamscan") or shutil.which("clamdscan")):
                    self.toasts.add_toast(Adw.Toast.new("ClamAV belum terpasang: sudo apt install clamav"))
                    return False
                SCAN["on"] = state
                return True

            sort_row = switch_row("Folder otomatis per jenis file", "sort")
            scan_row = switch_row("Pindai virus (ClamAV)", "scan", toggle_scan)
            sched_btn = Gtk.Button(label="Jadwal & aksi akhir...")
            sched_btn.connect("clicked", lambda *_: (self.menu_pop.popdown(), self.ask_schedule()))
            quit_btn = Gtk.Button(label="Keluar dari KilatDL")
            quit_btn.add_css_class("destructive-action")
            quit_btn.connect("clicked", lambda *_: app.quit())
            for w in (row, hint, Gtk.Separator(), Gtk.Label(label="Folder tujuan", xalign=0), self.folder_btn,
                      sort_row, scan_row, Gtk.Separator(), sched_btn, Gtk.Separator(), quit_btn):
                box.append(w)
            self.menu_pop = Gtk.Popover(child=box)
            return self.menu_pop

        def show_default_dir(self):
            self.folder_btn.set_label(settings["directory"])
            self.folder_btn.set_tooltip_text("Ubah folder tujuan bawaan")

        def set_default_dir(self, path):
            settings["directory"] = path
            save_settings(settings)
            self.show_default_dir()

        def on_limit(self, spin):
            settings["limit_kbps"] = int(spin.get_value())
            LIMITER.rate = settings["limit_kbps"] * 1024
            save_settings(settings)

        def pick_folder(self, callback, initial=None):
            dialog = Gtk.FileDialog(title="Pilih folder tujuan")
            dialog.set_initial_folder(Gio.File.new_for_path(initial or settings["directory"]))

            def done(dlg, res):
                try:
                    callback(dlg.select_folder_finish(res).get_path())
                except GLib.Error:
                    pass  # dialog cancelled

            dialog.select_folder(self, None, done)

        # -- list ------------------------------------------------------------
        def on_filter(self, button, key):
            if button.get_active():
                self.filter = key
                self.rebuild()

        def visible_items(self):
            if self.filter == "done":
                return [d for d in mgr.items if d.status == "done"]
            if self.filter == "active":
                return [d for d in mgr.items if d.status != "done"]
            return list(mgr.items)

        def rebuild(self):
            self.rows.clear()
            child = self.list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.list.remove(child)
                child = nxt
            for d in reversed(self.visible_items()):  # newest first
                row = Row(d, self)
                self.rows[d] = row
                self.list.append(row)
            self.stack.set_visible_child_name("list" if self.rows else "empty")
            mgr.save()

        def tick(self):
            self.notify_finished()
            unfinished = any(d.status in ("downloading", "queued") for d in mgr.items)
            mgr.hold = not scheduler.is_open(time.strftime("%H:%M"), unfinished)
            for d in mgr.items:
                d.held = mgr.hold
                if mgr.hold and d.status == "downloading":
                    d.pause()
                    d.status = "queued"  # picked up again when the next window opens
            mgr.schedule()
            action = scheduler.finished_action(mgr.items)
            if action:
                self.run_after(action)
            for r in self.rows.values():
                r.refresh()
            self.stats.set_text(self.summary())
            if self.get_application().tray:
                self.get_application().tray.refresh()
            if int(time.time()) % 5 == 0:
                mgr.save()
            return True

        def summary(self):
            active = [d for d in mgr.items if d.status == "downloading"]
            queued = sum(d.status == "queued" for d in mgr.items)
            done = sum(d.status == "done" for d in mgr.items)
            parts = [f"{len(active)} aktif"] + ([f"{queued} antri"] if queued else []) + [f"{done} selesai"]
            if active:
                parts.append("↓ " + human(sum(d.speed for d in active), "/s"))
            if settings["limit_kbps"]:
                parts.append(f"batas {settings['limit_kbps']} KB/s")
            return "  •  ".join(parts)

        def run_after(self, action):
            app = self.get_application()
            if action == "quit":
                app.quit()
                return
            command = ["systemctl", action]
            label = AFTER_ACTIONS[action].lower()
            dlg = Adw.AlertDialog(heading="Semua unduhan selesai", body="")
            dlg.add_response("cancel", "Batal")
            dlg.add_response("now", "Sekarang")
            dlg.set_default_response("cancel")
            left, cancelled = [30], [False]

            def count():
                if cancelled[0]:
                    return False
                if left[0] <= 0:
                    dlg.close()
                    subprocess.Popen(command, start_new_session=True)
                    return False
                dlg.set_body(f"Akan {label} dalam {left[0]} detik.")
                left[0] -= 1
                return True

            GLib.timeout_add(1000, count)
            count()

            def answered(_d, response):
                if response == "now" and not cancelled[0]:
                    subprocess.Popen(command, start_new_session=True)
                cancelled[0] = True

            dlg.connect("response", answered)
            dlg.present(self)
            self.present()

        def ask_schedule(self):
            dlg = Adw.Dialog(title="Jadwal & aksi akhir", content_width=440)
            sc = settings["schedule"]
            group = Adw.PreferencesGroup(description="Unduhan antri hanya berjalan di dalam jendela waktu ini. "
                                                     "Kosongkan 'Berhenti' agar berjalan sampai antrian habis.")
            enabled = Adw.SwitchRow(title="Aktifkan jadwal", active=sc["enabled"])
            start = Adw.EntryRow(title="Mulai (JJ:MM, 24 jam)", text=sc["start"])
            stop = Adw.EntryRow(title="Berhenti (JJ:MM, opsional)", text=sc["stop"])
            after = Adw.ComboRow(title="Setelah semua unduhan selesai", model=Gtk.StringList.new(list(AFTER_ACTIONS.values())))
            after.set_selected(list(AFTER_ACTIONS).index(settings["after"]))
            for row in (enabled, start, stop, after):
                group.add(row)
            msg = Gtk.Label(xalign=0, wrap=True)
            msg.add_css_class("error")
            ok = Gtk.Button(label="Simpan", halign=Gtk.Align.END)
            ok.add_css_class("suggested-action")
            ok.add_css_class("pill")

            def save(*_):
                st, sp = start.get_text().strip(), stop.get_text().strip()
                if enabled.get_active() and not HHMM.fullmatch(st):
                    msg.set_text("Jam mulai harus berformat JJ:MM, mis. 23:00")
                    return
                if sp and not HHMM.fullmatch(sp):
                    msg.set_text("Jam berhenti harus berformat JJ:MM atau dikosongkan")
                    return
                settings["schedule"] = {"enabled": enabled.get_active(), "start": st or "23:00", "stop": sp}
                settings["after"] = list(AFTER_ACTIONS)[after.get_selected()]
                save_settings(settings)
                scheduler.released = False
                dlg.close()
                self.toasts.add_toast(Adw.Toast.new("Jadwal disimpan"))

            ok.connect("clicked", save)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16,
                           margin_start=16, margin_end=16)
            for w in (group, msg, ok):
                body.append(w)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            view.set_content(body)
            dlg.set_child(view)
            dlg.present(self)

        def pause_all(self):
            for d in mgr.items:
                if d.status == "downloading":
                    d.pause()
                elif d.status == "queued":
                    d.status = "paused"

        def resume_all(self):
            for d in mgr.items:
                if d.status in ("paused", "error"):
                    d.status, d.notified = "queued", False
            mgr.schedule()

        # -- adding ----------------------------------------------------------
        def add_url(self, url, directory=None, headers=None, name=None, checksum=None):
            url = url.strip()
            if not re.match(r"https?://", url):
                return False
            sort = settings["sort"] and directory in (None, settings["directory"])  # a folder picked by hand is respected
            dl = Download(url, directory or settings["directory"], name=name, headers=headers, sort=sort)
            if checksum:
                dl.set_checksum(checksum)
            mgr.add(dl)
            self.rebuild()
            self.toasts.add_toast(Adw.Toast.new("Unduhan ditambahkan"))
            return True

        def add_text(self, text):
            found = re.findall(r"https?://[^\s\"'<>]+", text or "")
            return sum(self.add_url(u) for u in found)

        def on_key(self, _ctl, keyval, _code, state):
            ctrl = state & Gdk.ModifierType.CONTROL_MASK
            if ctrl and keyval == Gdk.KEY_v:
                def got(clip, res):
                    try:
                        text = clip.read_text_finish(res) or ""
                    except GLib.Error:
                        return
                    if not self.add_text(text):
                        self.toasts.add_toast(Adw.Toast.new("Tidak ada link di clipboard"))
                self.get_clipboard().read_text_async(None, got)
                return True
            if ctrl and keyval == Gdk.KEY_n:
                self.ask_url()
                return True
            return False

        def ask_url(self):
            dlg = Adw.Dialog(title="Tambah unduhan", content_width=480)
            group = Adw.PreferencesGroup()
            url = Adw.EntryRow(title="Alamat file (URL)")
            checksum = Adw.EntryRow(title="Checksum (opsional) sha256:... / md5 / sha1")
            chosen = [settings["directory"]]
            where = Adw.ActionRow(title="Simpan ke", subtitle=chosen[0])
            change = Gtk.Button(label="Ubah", valign=Gtk.Align.CENTER)
            where.add_suffix(change)

            def picked(path):
                chosen[0] = path
                where.set_subtitle(path)

            change.connect("clicked", lambda *_: self.pick_folder(picked, chosen[0]))
            for row in (url, where, checksum):
                group.add(row)
            msg = Gtk.Label(xalign=0, wrap=True)
            msg.add_css_class("error")
            ok = Gtk.Button(label="Unduh", halign=Gtk.Align.END)
            ok.add_css_class("suggested-action")
            ok.add_css_class("pill")

            def submit(*_):
                try:
                    if checksum.get_text().strip():
                        parse_checksum(checksum.get_text())
                except ValueError as e:
                    msg.set_text(str(e))
                    return
                if self.add_url(url.get_text(), chosen[0], checksum=checksum.get_text()):
                    dlg.close()
                else:
                    msg.set_text("URL harus diawali http:// atau https://")

            url.connect("entry-activated", submit)
            checksum.connect("entry-activated", submit)
            ok.connect("clicked", submit)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16,
                           margin_start=16, margin_end=16)
            for w in (group, msg, ok):
                body.append(w)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            view.set_content(body)
            dlg.set_child(view)

            def got(clip, res):
                try:
                    text = (clip.read_text_finish(res) or "").strip()
                    if re.match(r"https?://", text) and not url.get_text():
                        url.set_text(text)
                except GLib.Error:
                    pass

            self.get_clipboard().read_text_async(None, got)
            dlg.present(self)
            url.grab_focus()

        def prompt(self, title, label, hint, apply):
            """Single-field dialog. apply(text, done) must call done(None) on success or done(message)."""
            dlg = Adw.Dialog(title=title, content_width=460)
            entry = Adw.EntryRow(title=label)
            group = Adw.PreferencesGroup(description=hint)
            group.add(entry)
            msg = Gtk.Label(xalign=0, wrap=True)
            msg.add_css_class("error")
            ok = Gtk.Button(label="OK", halign=Gtk.Align.END)
            ok.add_css_class("suggested-action")
            ok.add_css_class("pill")

            def done(error):
                ok.set_sensitive(True)
                dlg.close() if error is None else msg.set_text(error)

            def submit(*_):
                ok.set_sensitive(False)
                msg.set_text("")
                apply(entry.get_text(), done)

            entry.connect("entry-activated", submit)
            ok.connect("clicked", submit)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16,
                           margin_start=16, margin_end=16)
            for w in (group, msg, ok):
                body.append(w)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            view.set_content(body)
            dlg.set_child(view)
            dlg.present(self)
            entry.grab_focus()

        # -- notifications ---------------------------------------------------
        def notify_finished(self):
            app = self.get_application()
            for d in mgr.items:
                if d.notified:
                    continue
                finished = d.status == "done" and (not d.checksum or d.verify in ("ok", "bad")) and d.scan != "scanning"
                if not (finished or d.status == "error"):
                    continue
                d.notified = True
                if d.status == "error":
                    note = Gio.Notification.new("Unduhan gagal")
                    note.set_body(f"{d.name or d.url}\n{d.error}")
                    note.set_icon(Gio.ThemedIcon.new("dialog-error-symbolic"))
                else:
                    virus = d.scan == "infected"
                    bad = d.verify == "bad" or virus
                    note = Gio.Notification.new("Virus terdeteksi!" if virus else "Checksum tidak cocok!" if bad else "Unduhan selesai")
                    note.set_body(f"{d.name}\n" + (f"{d.scan_detail} - jangan dibuka" if virus else human(d.size) + (" - file mungkin rusak" if bad else "")))
                    note.set_icon(Gio.ThemedIcon.new("dialog-warning-symbolic" if bad else ICON_NAME))
                    note.add_button_with_target("Buka file", "app.open-file", GLib.Variant("s", d.path))
                    note.add_button_with_target("Buka folder", "app.open-folder", GLib.Variant("s", os.path.dirname(d.path)))
                    note.set_default_action_and_target("app.open-folder", GLib.Variant("s", os.path.dirname(d.path)))
                app.send_notification(f"kilatdl-{id(d)}", note)

        def snapshot(self):
            """Test hook (KILATDL_SCREENSHOT=file.png): save what the window shows, then quit."""
            gi.require_version("Graphene", "1.0")
            from gi.repository import Graphene
            content = self.get_content()
            w, h = content.get_width(), content.get_height()
            inner = Gtk.Snapshot()
            Gtk.WidgetPaintable.new(content).snapshot(inner, w, h)
            node = inner.to_node()
            self.snap_tries = getattr(self, "snap_tries", 0) + 1
            if node is None:  # first frame not drawn yet
                if self.snap_tries < 8:
                    self.present()
                    content.queue_draw()
                    return True
                print("snapshot: nothing rendered", file=sys.stderr)
            else:
                dark = Adw.StyleManager.get_default().get_dark()  # the window background is not part of the content
                outer = Gtk.Snapshot()
                outer.append_color(Gdk.RGBA(*((0.14, 0.14, 0.14, 1) if dark else (0.98, 0.98, 0.98, 1))),
                                   Graphene.Rect().init(0, 0, w, h))
                outer.append_node(node)
                self.get_native().get_renderer().render_texture(outer.to_node(), None).save_to_png(os.environ["KILATDL_SCREENSHOT"])
            self.get_application().quit()
            return False

    SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
      <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
      <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
      <property name="IconName" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
      <property name="Menu" type="o" access="read"/><property name="WindowId" type="i" access="read"/>
      <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
      <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
      <signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal></interface></node>"""
    MENU_XML = """<node><interface name="com.canonical.dbusmenu">
      <property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
      <property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
      <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
        <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
      <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
        <arg type="a(ia{sv})" direction="out"/></method>
      <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
      <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
      <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
      <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
      <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
      <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
      <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ia{sv}as)"/></signal></interface></node>"""

    class Tray:
        """Status icon in the top bar (StatusNotifierItem + dbusmenu, shown by GNOME's AppIndicator extension).

        Quietly does nothing when no watcher is running. `actions` maps menu ids to callbacks:
        1 open window, 2 pause all, 3 resume all, 9 quit. Item 5 is a read-only status line.
        """

        def __init__(self, actions, status_text):
            self.actions, self.status_text, self.rev, self.conn = actions, status_text, 1, None
            self.label = status_text()
            self.menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            Gio.bus_own_name(Gio.BusType.SESSION, f"org.kde.StatusNotifierItem-{os.getpid()}-1",
                             Gio.BusNameOwnerFlags.NONE, self.on_bus, None, None)

        def on_bus(self, conn, name):
            self.conn = conn
            conn.register_object("/StatusNotifierItem", Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0],
                                 self.item_call, self.item_prop, None)
            conn.register_object("/MenuBar", self.menu_info, self.menu_call, self.menu_prop, None)
            conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                      "RegisterStatusNotifierItem", GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 2000, None,
                      lambda c, r: self._ignore(c, r))

        @staticmethod
        def _ignore(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error:
                pass  # no tray host on this desktop

        def item_prop(self, _c, _s, _p, _i, prop):
            tip = self.label
            return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", "kilatdl"),
                    "Title": GLib.Variant("s", "KilatDL"), "Status": GLib.Variant("s", "Active"),
                    "IconName": GLib.Variant("s", ICON_NAME), "ItemIsMenu": GLib.Variant("b", False),
                    "Menu": GLib.Variant("o", "/MenuBar"), "WindowId": GLib.Variant("i", 0),
                    "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], "KilatDL", tip))}.get(prop)

        def item_call(self, _c, _s, _p, _i, method, _params, inv):
            if method == "Activate":
                self.actions[1]()
            inv.return_value(None)

        def layout(self):
            item = lambda i, label, **kw: (i, {"label": GLib.Variant("s", label), **{k: GLib.Variant(*v) for k, v in kw.items()}}, [])
            sep = (4, {"type": GLib.Variant("s", "separator")}, [])
            sep2 = (6, {"type": GLib.Variant("s", "separator")}, [])
            kids = [item(1, "Buka KilatDL"), item(2, "Jeda semua"), item(3, "Lanjutkan semua"), sep,
                    item(5, self.label, enabled=("b", False)), sep2, item(9, "Keluar")]
            return (0, {"children-display": GLib.Variant("s", "submenu")},
                    [GLib.Variant("(ia{sv}av)", k) for k in kids])

        def menu_prop(self, _c, _s, _p, _i, prop):
            return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                    "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

        def menu_call(self, _c, _s, _p, _i, method, params, inv):
            if method == "GetLayout":
                inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.rev, self.layout())))
            elif method == "GetGroupProperties":
                inv.return_value(GLib.Variant("(a(ia{sv}))", ([],)))
            elif method == "AboutToShow":
                inv.return_value(GLib.Variant("(b)", (False,)))
            elif method == "Event":
                item_id, kind = params.unpack()[:2]
                if kind == "clicked" and item_id in self.actions:
                    self.actions[item_id]()
                inv.return_value(None)
            else:
                inv.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, "tidak didukung")

        def refresh(self):
            text = self.status_text()
            if text == self.label or not self.conn:
                return
            self.label, self.rev = text, self.rev + 1
            self.conn.emit_signal(None, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated",
                                  GLib.Variant("(ui)", (self.rev, 0)))
            self.conn.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", None)

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.win = None
            self.tray = None
            for name in ("open-file", "open-folder"):
                act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
                act.connect("activate", lambda _a, param: open_path(param.get_string()))
                self.add_action(act)

        def do_startup(self):
            Adw.Application.do_startup(self)
            Gtk.Window.set_default_icon_name(ICON_NAME)
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_command_line(self, cmd):
            args = cmd.get_arguments()[1:]
            if not self.win:
                self.win = Window(self)
                self.hold()  # keep running (downloads + browser bridge) when the window is closed
                self.tray = Tray({1: self.win.present, 2: self.win.pause_all, 3: self.win.resume_all, 9: self.quit},
                                 self.win.summary)
                start_bridge(lambda url, headers, name: GLib.idle_add(lambda: self.win.add_url(url, None, headers, name) and False))
            quiet = "--background" in args
            for a in args:
                if a != "--background":
                    self.win.add_url(a)
            if not quiet or any(a != "--background" for a in args):
                self.win.present()
            return 0

        def do_shutdown(self):
            for d in mgr.items:
                d.pause()
            mgr.save()
            Adw.Application.do_shutdown(self)

    app = App()
    return app.run([sys.argv[0]] + (["--background"] if background else []) + urls)


def main():
    p = argparse.ArgumentParser(prog="kilatdl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*")
    p.add_argument("-o", "--output", default=default_dir(), help="folder tujuan (mode terminal)")
    p.add_argument("-n", "--segments", type=int, default=8, help="jumlah koneksi per file (default 8)")
    p.add_argument("--checksum", help="hash yang harus cocok, mis. sha256:ab12... (satu URL, mode terminal)")
    p.add_argument("--sort", action="store_true", help="simpan di sub-folder per jenis file (mode terminal)")
    p.add_argument("--scan", action="store_true", help="pindai dengan ClamAV setelah selesai (mode terminal)")
    p.add_argument("--gui", action="store_true", help="paksa buka GUI")
    p.add_argument("--background", action="store_true", help="jalan tanpa membuka jendela (untuk autostart)")
    a = p.parse_args()
    if a.gui or a.background or not a.urls:
        return run_gui(a.urls, a.background)
    return run_cli(a.urls, a.output, max(1, a.segments), a.checksum, a.sort, a.scan)


if __name__ == "__main__":
    sys.exit(main())
