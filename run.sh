#!/usr/bin/env bash
# Starts DockerDeck on http://127.0.0.1:8787 (local only). No install needed.
cd "$(dirname "$0")"
exec python3 app.py
