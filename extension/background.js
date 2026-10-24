const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_PORT = 9614;
// the port can be changed with storage.local.bridgePort (used by the automated tests)
const bridge = async () => `http://127.0.0.1:${(await api.storage.local.get("bridgePort")).bridgePort || DEFAULT_PORT}`;
const MIN_BYTES = 1 << 20;      // leave tiny files to the browser
const MIN_MEDIA_BYTES = 300e3;  // ignore ad stings and tiny probe requests
const MEDIA_EXT = /\.(mp4|webm|mkv|mov|m4v|mp3|m4a|aac|ogg|opus|flac|wav)$/i;
const SEGMENT_EXT = /\.(ts|m4s|vtt|srt|key|cmfv|cmfa)$/i;
const TYPE_EXT = { "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                   "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/aac": ".aac", "video/x-matroska": ".mkv" };

// tabId -> Map(url -> {url, kind, size, ext})
const found = new Map();
// what we saw but could not offer, per tab: {segments: n} (tiny stream pieces with no playlist)
const diag = new Map();
const trace = new Map();      // tabId -> last media-looking responses with the verdict we gave them
const videos = new Map();     // tabId -> {frameId: info reported by the content script}
const MEDIAISH = /video|audio|mpegurl|dash|mp4|ogg|webm|octet-stream|matroska/i;
function record(tabId, entry) {
  const list = trace.get(tabId) || [];
  list.push(entry);
  if (list.length > 30) list.shift();
  trace.set(tabId, list);
}

// The background page can be suspended when idle; keep detections in session storage.
const store = api.storage.session || api.storage.local;
const ready = store.get(["found", "diag"]).then(({ found: saved = {}, diag: savedDiag = {} }) => {
  for (const [tab, items] of Object.entries(saved)) {
    if (!found.has(+tab)) found.set(+tab, new Map(items.map(i => [i.url, i])));
  }
  for (const [tab, d] of Object.entries(savedDiag)) if (!diag.has(+tab)) diag.set(+tab, d);
}).catch(() => {});
const persist = () => store.set({
  found: Object.fromEntries([...found].map(([t, m]) => [t, [...m.values()]])),
  diag: Object.fromEntries(diag),
}).catch(() => {});

// Decide whether a response is a downloadable media file or an HLS playlist.
function classify(url, contentType, size, requestType) {
  let path;
  try { path = new URL(url).pathname; } catch (_) { return null; }
  const type = (contentType || "").split(";")[0].trim().toLowerCase();
  if (SEGMENT_EXT.test(path)) return null;
  if (/\.m3u8$/i.test(path) || type === "application/vnd.apple.mpegurl" || type === "application/x-mpegurl") {
    return { kind: "hls", ext: "" };
  }
  if (/\.mpd$/i.test(path) || type === "application/dash+xml") return { kind: "dash", ext: "" };
  // requestType "media" = the page itself asked for it as <video>/<audio>, whatever the server calls it
  const isMedia = MEDIA_EXT.test(path) || type.startsWith("video/") || type.startsWith("audio/") ||
                  requestType === "media" || /^application\/(mp4|ogg|x-matroska)$/.test(type);
  if (!isMedia || type === "video/mp2t") return null;
  if (size && size < MIN_MEDIA_BYTES) return null;
  const ext = (path.match(MEDIA_EXT) || [])[0] || TYPE_EXT[type] || (type === "application/mp4" ? ".mp4" : "");
  return { kind: "file", ext };
}
globalThis.__classify = classify; // exposed for tests

function header(h, name) {
  const x = (h || []).find(e => e.name.toLowerCase() === name);
  return x ? x.value : "";
}

function badge(tabId) {
  const n = (found.get(tabId) || new Map()).size;
  api.action.setBadgeText({ tabId, text: n ? String(n) : "" }).catch?.(() => {});
}

api.webRequest.onResponseStarted.addListener(async (d) => {
  await ready;
  if (d.tabId < 0 || d.statusCode >= 400) return;
  const range = header(d.responseHeaders, "content-range").match(/\/(\d+)$/);
  const size = range ? +range[1] : +header(d.responseHeaders, "content-length") || 0;
  const ctype = header(d.responseHeaders, "content-type");
  const info = classify(d.url, ctype, size, d.type);
  let pathname = "";
  try { pathname = new URL(d.url).pathname; } catch (_) { /* not a URL */ }
  if (d.type === "media" || MEDIAISH.test(ctype) || MEDIA_EXT.test(pathname) || SEGMENT_EXT.test(pathname) || /\.(m3u8|mpd)$/i.test(pathname)) {
    let host = "";
    try { host = new URL(d.url).host; } catch (_) { /* keep empty */ }
    record(d.tabId, { type: d.type, status: d.statusCode, ctype: ctype.split(";")[0], size, host,
                      path: pathname.slice(-60), verdict: info ? info.kind : (SEGMENT_EXT.test(pathname) ? "segmen" : "diabaikan") });
  }
  if (!info) {
    try {
      if (SEGMENT_EXT.test(new URL(d.url).pathname)) {
        const x = diag.get(d.tabId) || { segments: 0 };
        x.segments++;
        diag.set(d.tabId, x);
        if (x.segments === 1 || x.segments % 20 === 0) persist();
      }
    } catch (_) { /* not a URL */ }
    return;
  }
  const list = found.get(d.tabId) || new Map();
  const key = d.url.split("#")[0];
  if (!list.has(key)) list.set(key, { url: key, size, ...info });
  found.set(d.tabId, list);
  persist();
  badge(d.tabId);
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);

api.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading" && change.url) { found.delete(tabId); diag.delete(tabId); trace.delete(tabId); videos.delete(tabId); persist(); badge(tabId); }
});
api.tabs.onRemoved.addListener((tabId) => { found.delete(tabId); diag.delete(tabId); trace.delete(tabId); videos.delete(tabId); persist(); });

function fileName(title, ext) {
  const base = (title || "video").replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_").trim().slice(0, 100) || "video";
  return base + ext;
}

async function send(url, referrer, name) {
  const headers = {};
  if (referrer) headers.Referer = referrer;
  try {
    const cookies = await api.cookies.getAll({ url });
    if (cookies.length) headers.Cookie = cookies.map(c => `${c.name}=${c.value}`).join("; ");
  } catch (_) { /* no cookie access for this URL */ }
  try {
    const r = await fetch(`${await bridge()}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KilatDL": "1" },
      body: JSON.stringify({ url, headers, name }),
    });
    return r.ok;
  } catch (_) {
    return false; // KilatDL is not running: let the browser keep the download
  }
}

async function enabled() {
  const { enabled = true } = await api.storage.local.get("enabled");
  return enabled;
}

api.downloads.onCreated.addListener(async (item) => {
  if (!(await enabled())) return;
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url)) return;
  if (item.fileSize > 0 && item.fileSize < MIN_BYTES) return;
  if (await send(url, item.referrer)) {
    await api.downloads.cancel(item.id);
    await api.downloads.erase({ id: item.id });
  }
});

api.runtime.onInstalled.addListener(() => {
  api.contextMenus.create({ id: "kilatdl-link", title: "Unduh dengan KilatDL", contexts: ["link"] });
});

api.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "kilatdl-link" && info.linkUrl) {
    if (!(await send(info.linkUrl, tab && tab.url))) console.warn("KilatDL tidak berjalan");
  }
});

// popup <-> background: list what was detected in a tab, or send one item to KilatDL
api.runtime.onMessage.addListener((msg, sender, reply) => {
  if (sender.id !== api.runtime.id) return false;
  const tabId = msg.tabId ?? (sender.tab && sender.tab.id); // content scripts do not know their tab id
  if (msg.cmd === "list") {
    ready.then(() => reply([...(found.get(tabId) || new Map()).values()]));
  } else if (msg.cmd === "diagnose") {
    ready.then(() => reply({ ...(diag.get(tabId) || { segments: 0 }), drm: Object.values(videos.get(tabId) || {}).some(v => v.drm),
                              seen: (trace.get(tabId) || []).length }));
  } else if (msg.cmd === "video-seen") {
    const frames = videos.get(tabId) || {};
    frames[sender.frameId] = msg.info;
    videos.set(tabId, frames);
    reply(true);
  } else if (msg.cmd === "report") {
    reply({ version: api.runtime.getManifest().version, trace: trace.get(tabId) || [], videos: videos.get(tabId) || {},
            found: [...(found.get(tabId) || new Map()).values()].map(i => ({ kind: i.kind, size: i.size, ext: i.ext })) });
  } else if (msg.cmd === "download") {
    ready.then(() => api.tabs.get(tabId)).then(tab => {
      const item = (found.get(tabId) || new Map()).get(msg.url);
      let ext = item ? item.ext : "";
      if (!item) { try { ext = (new URL(msg.url).pathname.match(MEDIA_EXT) || [""])[0]; } catch (_) { /* keep empty */ } }
      return send(msg.url, tab.url, fileName(tab.title, ext));
    }).then(ok => reply(ok), () => reply(false));
  } else {
    return false;
  }
  return true; // keep the channel open for the async reply
});
