const api = globalThis.browser ?? globalThis.chrome;
const box = document.getElementById("on");
const state = document.getElementById("state");
const media = document.getElementById("media");

api.storage.local.get("enabled").then(({ enabled = true }) => { box.checked = enabled; });
box.addEventListener("change", () => api.storage.local.set({ enabled: box.checked }));

const overlay = document.getElementById("overlay");
api.storage.local.get("overlay").then(({ overlay: on = true }) => { overlay.checked = on !== false; });
overlay.addEventListener("change", () => api.storage.local.set({ overlay: overlay.checked }));

api.storage.local.get("bridgePort")
  .then(({ bridgePort = 9614 }) => fetch(`http://127.0.0.1:${bridgePort}/ping`))
  .then(r => r.json())
  .then(j => { state.textContent = j.app === "kilatdl" ? "KilatDL berjalan" : "Respon tidak dikenal"; })
  .catch(() => { state.textContent = "KilatDL tidak berjalan - unduhan tetap oleh browser"; });

const human = n => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n ? Math.round(n / 1e3) + " KB" : "";

api.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
  api.runtime.sendMessage({ cmd: "list", tabId: tab.id }, (items) => {
    if (!items || !items.length) return;
    media.textContent = "";
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "item";
      const label = document.createElement("span");
      label.title = it.url;
      label.textContent = (it.kind === "hls" ? "[HLS] " : "") + it.url.split("?")[0].split("/").pop();
      const size = document.createElement("small");
      size.textContent = human(it.size);
      const btn = document.createElement("button");
      btn.textContent = "Unduh";
      btn.addEventListener("click", () => {
        btn.disabled = true;
        api.runtime.sendMessage({ cmd: "download", tabId: tab.id, url: it.url }, (ok) => {
          btn.textContent = ok ? "Terkirim" : "Gagal";
          btn.disabled = !!ok;
        });
      });
      row.append(label, size, btn);
      media.append(row);
    }
  });
});
