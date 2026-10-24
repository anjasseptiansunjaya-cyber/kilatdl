# KilatDL

Download manager multi-koneksi untuk Linux, dengan jendela GTK4, mode terminal, ekstensi Firefox,
dan browser kecil bernama KilatBrowser. Dibuat sebagai pengganti Internet Download Manager.

## Fitur

- Satu file dibagi menjadi beberapa koneksi (default 8), jeda dan lanjut, termasuk setelah aplikasi ditutup
- Antrian (3 unduhan sekaligus) dan batas kecepatan total
- Video HLS (`.m3u8`) tanpa enkripsi: pilih kualitas tertinggi, gabung segmen menjadi `.ts`/`.mp4`
- Verifikasi checksum (MD5, SHA-1, SHA-256, SHA-512) setelah unduhan selesai
- **Ganti link**: kalau link bertoken kedaluwarsa, tempel link baru dan unduhan dilanjutkan dari titik terakhir
- Notifikasi saat selesai atau gagal, dengan tombol Buka file / Buka folder
- Ekstensi Firefox: menangkap unduhan browser (beserta cookie dan referer) dan mendeteksi video di halaman
- Berjalan otomatis di latar belakang saat login

## Pasang

```bash
./install.sh
```

Memasang `kilatdl` dan `kilatbrowser` ke `~/.local/bin`, entri menu aplikasi, dan autostart
(`~/.config/autostart/kilatdl.desktop`; hapus file itu untuk mematikannya). Tidak memerlukan sudo.

Kebutuhan: Python 3.10+, PyGObject dengan GTK 4 (jendela), WebKit2GTK 4.1 (hanya KilatBrowser).

## Pakai

```bash
kilatdl                                   # buka jendela
kilatdl https://situs/file.zip -n 8       # mode terminal
kilatdl URL --checksum sha256:ab12...     # terminal, dengan verifikasi
kilatdl --background                      # tanpa jendela (dipakai autostart)
```

### Ekstensi browser

Folder `extension/`. Di Firefox rilis, ekstensi harus ditandatangani Mozilla (AMO, saluran unlisted).
Firefox Developer Edition/Nightly dapat memuatnya langsung dengan `xpinstall.signatures.required = false`.
Ekstensi bicara ke KilatDL lewat `127.0.0.1:9614`. Jalur itu hanya menerima permintaan dengan header
`X-KilatDL` dan origin ekstensi, sehingga halaman web biasa tidak bisa menambah unduhan.

## Data

`~/.local/share/kilatdl/` berisi `queue.json` (dapat memuat cookie sesi, izin 0600) dan `settings.json`.
Variabel lingkungan `KILATDL_APP_ID`, `KILATDL_PORT`, dan `XDG_DATA_HOME` memungkinkan menjalankan
salinan terpisah (dipakai untuk pengujian).

## Batasan

- Video terenkripsi (AES/DRM), DASH (`.mpd`), dan stream live belum didukung
- Server yang sengaja memblokir klien non-browser akan menolak KilatDL (HTTP 403); unduh lewat
  browser dengan ekstensi supaya permintaan datang dari browser Anda sendiri
- Tidak menggabungkan audio dan video yang terpisah (butuh ffmpeg)

## Uji

```bash
python3 -m unittest discover -s tests -t . -v
```

`har_sanitize.py` membuat salinan file HAR yang aman dibagikan (cookie, token, dan isi respons dihapus).
