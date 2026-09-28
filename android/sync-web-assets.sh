#!/usr/bin/env sh
set -eu
ANDROID_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$ANDROID_DIR/.." && pwd)"
DEST="$ANDROID_DIR/app/src/main/assets"
mkdir -p "$DEST/assets"
cp "$ROOT_DIR/index.html" "$ROOT_DIR/app.js" "$ROOT_DIR/styles.css" "$ROOT_DIR/manifest.webmanifest" "$ROOT_DIR/sw.js" "$DEST/"
cp "$ROOT_DIR/assets/cinematic-landscape.jpg" "$ROOT_DIR/assets/deisa-mark.svg" "$DEST/assets/"
printf 'Synced Deisa web UI to Android assets.\n'
