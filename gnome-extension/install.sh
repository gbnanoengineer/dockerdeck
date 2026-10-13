#!/usr/bin/env bash
# Installs the DockerDeck GNOME Shell extension and the systemd user service it controls.
#   ./install.sh              install both
#   ./install.sh --autostart  also start DockerDeck on every login
set -euo pipefail
cd "$(dirname "$0")"
UUID="dockerdeck@gbnanoengineer.github.io"
ROOT="$(cd .. && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

command -v gnome-extensions >/dev/null || { echo "gnome-extensions not found. Install gnome-shell-extension-prefs."; exit 1; }
[ -f "$ROOT/app.py" ] || { echo "Cannot find $ROOT/app.py"; exit 1; }

# 1. the service. The extension only runs: systemctl --user start|stop|restart dockerdeck.service
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/dockerdeck.service" <<UNIT
[Unit]
Description=DockerDeck

[Service]
ExecStart=/usr/bin/python3 $ROOT/app.py
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
if [ "${1:-}" = "--autostart" ]; then systemctl --user enable dockerdeck.service; fi

# 2. the extension
mkdir -p dist
(cd "$UUID" && gnome-extensions pack --force --out-dir=../dist \
  --extra-source=api.js --extra-source=prefs.js --extra-source=icons --extra-source=stylesheet.css .)
gnome-extensions install --force "dist/$UUID.shell-extension.zip"

echo
echo "Installed the service ($UNIT_DIR/dockerdeck.service) and the extension."
echo "Next:"
echo "  1. Log out and in again (needed on Wayland the first time)."
echo "  2. gnome-extensions enable $UUID"
echo "Then use the DockerDeck menu in the top bar to start, stop and open it."
