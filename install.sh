#!/usr/bin/env bash
# Install KilatDL for the current user (no sudo needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.local/share/applications
install -m 755 "$here/kilatdl.py" ~/.local/bin/kilatdl
install -m 755 "$here/kilatbrowser.py" ~/.local/bin/kilatbrowser
cat > ~/.local/share/applications/kilatbrowser.desktop <<DESK2
[Desktop Entry]
Type=Application
Name=KilatBrowser
Comment=Browser ringan dengan unduhan lewat KilatDL
Exec=$HOME/.local/bin/kilatbrowser %U
Icon=web-browser
Terminal=false
Categories=Network;WebBrowser;
DESK2
rm -f ~/.local/share/applications/kilatdl.desktop
cat > ~/.local/share/applications/id.anjas.KilatDL.desktop <<DESK
[Desktop Entry]
Type=Application
Name=KilatDL
Comment=Download manager multi-koneksi
Exec=$HOME/.local/bin/kilatdl %U
Icon=folder-download
Terminal=false
Categories=Network;FileTransfer;
MimeType=x-scheme-handler/http;x-scheme-handler/https;
StartupNotify=true
DESK
sed -i '/^MimeType=/d' ~/.local/share/applications/id.anjas.KilatDL.desktop  # do not hijack browser links
mkdir -p ~/.config/autostart
sed -e "s|^Exec=.*|Exec=$HOME/.local/bin/kilatdl --background|" -e '/^StartupNotify=/d' \
    ~/.local/share/applications/id.anjas.KilatDL.desktop > ~/.config/autostart/kilatdl.desktop
echo "X-GNOME-Autostart-enabled=true" >> ~/.config/autostart/kilatdl.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Autostart: ~/.config/autostart/kilatdl.desktop (hapus file ini untuk mematikan)"
echo "Terpasang: ~/.local/bin/kilatdl dan menu aplikasi 'KilatDL'"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Catatan: tambahkan ~/.local/bin ke PATH";; esac
