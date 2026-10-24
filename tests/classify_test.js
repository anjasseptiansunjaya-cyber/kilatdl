const noop = () => {};
globalThis.chrome = { webRequest: { onResponseStarted: { addListener: noop } }, tabs: { onUpdated: { addListener: noop }, onRemoved: { addListener: noop } },
  downloads: { onCreated: { addListener: noop } }, runtime: { onInstalled: { addListener: noop }, onMessage: { addListener: noop } },
  contextMenus: { onClicked: { addListener: noop } }, action: {}, storage: { session: { get: async () => ({}), set: async () => {} }, local: {} } };
require(process.argv[2]);
const c = globalThis.__classify;
const t = (name, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); console.log(ok ? "ok  " : "GAGAL", name, ok ? "" : JSON.stringify(got)); if (!ok) process.exitCode = 1; };
t("mp4 besar", c("https://a.com/v/clip.mp4?x=1", "video/mp4", 5e6), { kind: "file", ext: ".mp4" });
t("mp4 kecil (iklan)", c("https://a.com/ad.mp4", "video/mp4", 20e3), null);
t("tanpa ekstensi, type video/webm", c("https://a.com/stream/123", "video/webm", 9e6), { kind: "file", ext: ".webm" });
t("m3u8 master", c("https://a.com/hls/master.m3u8?t=9", "application/vnd.apple.mpegurl", 400), { kind: "hls", ext: "" });
t("m3u8 type x-mpegURL", c("https://a.com/p", "application/x-mpegURL", 400), { kind: "hls", ext: "" });
t("segmen .ts diabaikan", c("https://a.com/seg1.ts", "video/mp2t", 2e6), null);
t("segmen .m4s diabaikan", c("https://a.com/seg1.m4s", "video/iso.segment", 2e6), null);
t("html bukan media", c("https://a.com/index.html", "text/html", 90e3), null);
t("mp3", c("https://a.com/lagu.mp3", "audio/mpeg", 4e6), { kind: "file", ext: ".mp3" });
t("manifest DASH dikenali (tapi tidak bisa diunduh)", c("https://a.com/v/manifest.mpd", "application/dash+xml", 5000), { kind: "dash", ext: "" });
t("url rusak", c("bukan url", "video/mp4", 5e6), null);
