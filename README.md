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
- Antarmuka libadwaita (mengikuti tema terang/gelap sistem): kartu per unduhan, filter Semua/Aktif/Selesai, tempel link dengan Ctrl+V atau tarik ke jendela
- Ekstensi Firefox: menangkap unduhan browser (beserta cookie dan referer), mendeteksi video, dan menampilkan tombol **Unduh video** di atas pemutar (seperti IDM)
- Berjalan otomatis di latar belakang saat login

## Pasang

```bash
./install.sh
```

Memasang `kilatdl` dan `kilatbrowser` ke `~/.local/bin`, entri menu aplikasi, dan autostart
(`~/.config/autostart/kilatdl.desktop`; hapus file itu untuk mematikannya). Tidak memerlukan sudo.

Kebutuhan: Python 3.10+, PyGObject dengan GTK 4 dan libadwaita 1 (jendela), WebKit2GTK 4.1 (hanya KilatBrowser).

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

## Tray, jadwal, dan aksi akhir

- **Tray**: ikon petir di bilah atas (butuh ekstensi AppIndicator, sudah ada di Ubuntu). Klik untuk membuka
  jendela; menu berisi jeda/lanjutkan semua, ringkasan status, dan Keluar.
- **Jadwal** (menu ☰ → Jadwal & aksi akhir): unduhan antri hanya berjalan antara jam mulai dan berhenti
  (boleh melewati tengah malam). Tanpa jam berhenti, antrian dilepas saat jam mulai tiba dan berjalan sampai habis.
- **Aksi akhir**: setelah semua unduhan selesai, KilatDL bisa keluar, menidurkan, atau mematikan komputer.
  Tidur/mati selalu menampilkan hitung mundur 30 detik yang bisa dibatalkan, dan tidak pernah berjalan
  bila ada unduhan yang Anda jeda sendiri.

## Folder otomatis dan pemindaian virus

Keduanya dinyalakan di menu ☰ (atau `--sort` / `--scan` di mode terminal).

- **Folder per jenis**: file masuk ke sub-folder `Video`, `Musik`, `Gambar`, `Dokumen`, `Arsip`, atau `Program`
  di dalam folder tujuan. Jenis yang tidak dikenal tetap di folder utama. Bila Anda memilih folder sendiri
  saat menambah unduhan, pilihan itu dihormati dan tidak dipisah.
- **ClamAV**: setelah selesai, file dipindai dengan `clamdscan` (bila daemon jalan) atau `clamscan`.
  Pasang dengan `sudo apt install clamav`. File yang terdeteksi hanya ditandai dan diberi notifikasi;
  KilatDL tidak pernah menghapusnya sendiri.

## Riwayat dan pengambil file dari halaman

- **Riwayat** (ikon jam di header, atau Ctrl+H): semua unduhan yang selesai atau gagal tetap tercatat meski
  sudah dihapus dari daftar. Bisa dicari, dibuka lagi, diunduh ulang, diekspor ke CSV/JSON, atau dikosongkan
  (file aslinya tidak disentuh). Alamat disimpan tanpa query string dan kredensial karena link bertanda tangan
  memuat token di sana; jadi "Unduh lagi" bisa gagal untuk link yang butuh token.
- **Ambil file dari halaman** (menu ☰): tempel alamat halaman, KilatDL mencari tautan file (PDF, arsip, gambar,
  video, audio, dst.) di halaman itu, Anda memilih mana yang diunduh, bisa disaring per jenis atau ekstensi.
  Hanya halaman itu yang dibaca; tidak menelusuri halaman lain, dan halaman yang butuh login belum didukung.

## Data

`~/.local/share/kilatdl/` berisi `queue.json` (dapat memuat cookie sesi, izin 0600) dan `settings.json`.
Variabel lingkungan `KILATDL_APP_ID`, `KILATDL_PORT`, dan `XDG_DATA_HOME` memungkinkan menjalankan
salinan terpisah (dipakai untuk pengujian).

## Batasan

- Video terenkripsi (AES/DRM) dan stream live belum didukung
- Server yang sengaja memblokir klien non-browser akan menolak KilatDL (HTTP 403); unduh lewat
  browser dengan ekstensi supaya permintaan datang dari browser Anda sendiri
- DASH (`.mpd`) statis didukung; bila audio dan video terpisah, KilatDL memakai ffmpeg untuk menggabungnya
  (`sudo apt install ffmpeg`). Satu Period saja, tanpa DRM. File audio+video terpisah yang bukan DASH belum didukung

## Uji

```bash
python3 -m unittest discover -s tests -t . -v
python3 tests/e2e_firefox.py          # ujung-ke-ujung di Firefox Developer Edition (tombol di video)
```

`tests/e2e_firefox.py` memakai salinan profil dan instance KilatDL terpisah (port 9715), jadi data asli tidak tersentuh.
Untuk tangkapan layar jendela: `KILATDL_SCREENSHOT=out.png KILATDL_SCHEME=dark kilatdl --gui`.

`har_sanitize.py` membuat salinan file HAR yang aman dibagikan (cookie, token, dan isi respons dihapus).
