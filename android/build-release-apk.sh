#!/usr/bin/env sh
set -eu
ANDROID_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if ! command -v java >/dev/null 2>&1; then
  echo "ERROR: JDK 17 is required to build the release variant." >&2
  exit 1
fi
if ! command -v gradle >/dev/null 2>&1; then
  echo "ERROR: Gradle 9.6.0+ is required to build the release variant." >&2
  exit 1
fi

"$ANDROID_DIR/sync-web-assets.sh"
cd "$ANDROID_DIR"
gradle --no-daemon --stacktrace :app:assembleRelease
printf '\nUnsigned release APK: %s\n' "$ANDROID_DIR/app/build/outputs/apk/release/app-release-unsigned.apk"
printf 'Sign it with your own protected release keystore before distribution.\n'
