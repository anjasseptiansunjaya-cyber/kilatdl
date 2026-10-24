// KilatDL overlay: a small "Unduh video" button that appears on top of the video being watched.
(() => {
  "use strict";
  const api = globalThis.browser ?? globalThis.chrome;
  if (window.__kilatdlOverlay) return;
  window.__kilatdlOverlay = true;

  let enabled = true;
  api.storage.local.get("overlay").then(({ overlay = true }) => { enabled = overlay !== false; tick(); });
  api.storage.onChanged.addListener((c) => { if (c.overlay) { enabled = c.overlay.newValue !== false; tick(); } });

  const FONT = '13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif';
  const host = document.createElement("kilatdl-overlay");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;display:none;top:0;left:0;";
  // open on purpose: pages can read it but cannot act through it, every handler checks isTrusted
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      * { box-sizing: border-box; }
      #btn { all: unset; font: ${FONT}; cursor: pointer; display: flex; align-items: center; gap: 8px; padding: 8px 14px 8px 10px;
             border-radius: 999px; color: #fff; background: rgba(24,24,28,.86); backdrop-filter: blur(6px);
             box-shadow: 0 2px 10px rgba(0,0,0,.35), inset 0 0 0 1px rgba(255,255,255,.14); font-weight: 600; }
      #btn:hover { background: rgba(224,82,30,.95); }
      #btn svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2.2;
                 stroke-linecap: round; stroke-linejoin: round; }
      #menu { font: ${FONT}; margin-top: 8px; min-width: 280px; max-width: 360px; border-radius: 14px; overflow: hidden; color: #f2f2f2;
              background: rgba(28,28,32,.96); box-shadow: 0 8px 28px rgba(0,0,0,.5), inset 0 0 0 1px rgba(255,255,255,.12); }
      #menu[hidden] { display: none; }
      .note { padding: 14px; color: #c9c9c9; }
      .item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-top: 1px solid rgba(255,255,255,.08); }
      .item:first-child { border-top: 0; }
      .meta { flex: 1; min-width: 0; }
      .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
      .sub { color: #a8a8a8; font-size: 12px; }
      .go { all: unset; font: ${FONT}; cursor: pointer; padding: 6px 12px; border-radius: 8px; background: #e0521e; color: #fff; font-weight: 600; }
      .go:hover { background: #f2682f; }
      .go[disabled] { background: #3a7d44; cursor: default; }
    </style>
    <button id="btn" type="button" aria-label="Unduh video dengan KilatDL">
      <svg viewBox="0 0 24 24"><path d="M12 3v12m0 0l-5-5m5 5l5-5M5 20h14"/></svg><span>Unduh video</span>
    </button>
    <div id="menu" hidden></div>`;
  const btn = root.getElementById("btn");
  const menu = root.getElementById("menu");

  let current = null;      // the <video> the button is attached to
  let menuOpen = false;
  let pointer = { x: -1, y: -1, t: 0 };

  addEventListener("mousemove", (e) => { pointer = { x: e.clientX, y: e.clientY, t: Date.now() }; tick(); },
                   { capture: true, passive: true });
  addEventListener("scroll", () => tick(), { capture: true, passive: true });
  addEventListener("resize", () => tick(), { passive: true });
  document.addEventListener("fullscreenchange", () => { host.remove(); tick(); });
  setInterval(tick, 500);

  function visibleVideos() {
    return [...document.querySelectorAll("video")].map(v => ({ v, r: v.getBoundingClientRect() }))
      .filter(({ v, r }) => r.width >= 240 && r.height >= 135 && r.bottom > 40 && r.right > 40 &&
                            r.top < innerHeight && r.left < innerWidth && getComputedStyle(v).visibility !== "hidden");
  }

  const reported = new WeakSet();
  function report(v) {
    if (reported.has(v)) return;
    reported.add(v);
    const src = v.currentSrc || v.src || "";
    ask({ cmd: "video-seen", info: {
      frame: window === window.top ? "halaman utama" : "iframe " + location.host,
      src: !src ? "kosong" : /^blob:/i.test(src) ? "blob (MSE)" : /^https?:/i.test(src) ? "http" : src.split(":")[0],
      sourceTags: v.querySelectorAll("source").length, readyState: v.readyState, drm: !!v.mediaKeys,
      size: Math.round(v.getBoundingClientRect().width) + "x" + Math.round(v.getBoundingClientRect().height) } });
  }

  function pickVideo() {
    const list = visibleVideos();
    list.sort((a, b) => (!b.v.paused - !a.v.paused) || (b.r.width * b.r.height - a.r.width * a.r.height));
    return list[0] || null;
  }

  const inside = (r, x, y) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;

  function tick() {
    if (!enabled) { host.style.display = "none"; return; }
    const pick = pickVideo();
    if (!pick) { hide(); return; }
    const { v, r } = pick;
    report(v);
    const recent = Date.now() - pointer.t < 2500;
    const hovering = (recent && inside(r, pointer.x, pointer.y)) || host.matches(":hover");
    if (!(hovering || menuOpen || !v.paused)) { hide(); return; }
    if (!host.isConnected) (document.fullscreenElement || document.documentElement).append(host);
    current = v;
    host.style.display = "block";
    const w = host.offsetWidth || 150;
    host.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.right - w - 12)) + "px";
    host.style.top = Math.max(8, r.top + 12) + "px";
  }

  function hide() {
    if (menuOpen) return;
    host.style.display = "none";
  }

  function ask(msg) {
    return new Promise((resolve) => {
      try { api.runtime.sendMessage(msg, (r) => resolve(r)); } catch (_) { resolve(null); }
    });
  }

  const human = n => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n ? Math.round(n / 1e3) + " KB" : "";
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };

  async function openMenu() {
    menuOpen = true;
    menu.hidden = false;
    menu.replaceChildren(el("div", "note", "Mencari media..."));
    const items = (await ask({ cmd: "list" })) || [];
    const direct = current && /^https?:/i.test(current.currentSrc) ? current.currentSrc : null;
    if (direct && !items.some(i => i.url === direct)) items.unshift({ url: direct, kind: "file", size: 0 });
    menu.replaceChildren();
    if (!items.some(i => i.kind !== "dash")) {
      const d = (await ask({ cmd: "diagnose" })) || { segments: 0, seen: 0 };
      const blob = current && /^blob:/i.test(current.currentSrc);
      let why = "Belum ada media terdeteksi. Putar videonya beberapa detik, lalu klik lagi.";
      if (items.length) why = "Situs ini memakai DASH (streaming adaptif dengan audio dan video terpisah). KilatDL belum mendukung jenis ini.";
      else if (d.segments > 0) why = `Video dikirim dalam ${d.segments}+ potongan kecil tanpa playlist yang bisa dibaca, jadi tidak ada satu file untuk diunduh. Belum didukung.`;
      else if (d.drm) why = "Video ini dilindungi DRM (terenkripsi), jadi tidak bisa diunduh.";
      else if (blob) why = "Video ini diputar lewat blob (MSE) dan belum ada permintaan media yang terbaca. Putar beberapa detik lalu klik lagi; kalau tetap kosong, situs ini belum didukung.";
      if (!items.length && !d.segments && !d.drm && !blob) {
        why += d.seen ? ` (${d.seen} permintaan media terlihat tapi tidak ada yang layak diunduh. Buka ikon ekstensi di toolbar, lalu klik "Salin diagnostik" dan kirim ke saya.)`
                      : " (Belum ada permintaan media sama sekali dari halaman ini.)";
      }
      menu.append(el("div", "note", why));
      return;
    }
    for (const it of items.filter(i => i.kind !== "dash")) {
      const row = el("div", "item");
      const meta = el("div", "meta");
      const file = it.url.split("?")[0].split("/").pop() || it.url;
      meta.append(el("div", "name", file), el("div", "sub", (it.kind === "hls" ? "Video HLS (streaming)" : "Berkas media" + (it.size ? " • " + human(it.size) : ""))));
      const go = el("button", "go", "Unduh");
      go.addEventListener("click", async (e) => {
        if (!e.isTrusted) return;
        go.disabled = true;
        const ok = await ask({ cmd: "download", url: it.url });
        go.textContent = ok ? "Terkirim" : "KilatDL mati";
        if (ok) setTimeout(closeMenu, 1200); else go.disabled = false;
      });
      row.append(meta, go);
      menu.append(row);
    }
  }

  function closeMenu() { menuOpen = false; menu.hidden = true; tick(); }

  btn.addEventListener("click", (e) => {
    if (!e.isTrusted) return; // a page script must not be able to trigger a download
    menuOpen ? closeMenu() : openMenu();
  });
  document.addEventListener("click", (e) => { if (menuOpen && e.isTrusted && !e.composedPath().includes(host)) closeMenu(); }, true);
})();
