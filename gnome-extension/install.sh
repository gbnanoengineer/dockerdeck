#!/usr/bin/env bash
# Builds and installs the DockerDeck GNOME Shell extension for your user.
set -euo pipefail
cd "$(dirname "$0")"
UUID="dockerdeck@gbnanoengineer.github.io"

command -v gnome-extensions >/dev/null || { echo "gnome-extensions not found. Install gnome-shell-extension-prefs."; exit 1; }

mkdir -p dist
(cd "$UUID" && gnome-extensions pack --force --out-dir=../dist \
  --extra-source=api.js --extra-source=prefs.js --extra-source=icons --extra-source=stylesheet.css .)
gnome-extensions install --force "dist/$UUID.shell-extension.zip"

echo
echo "Installed. Now:"
echo "  1. Log out and log in again (needed on Wayland the first time)."
echo "  2. Run:  gnome-extensions enable $UUID"
echo "  3. Make sure DockerDeck is running:  ../run.sh"
