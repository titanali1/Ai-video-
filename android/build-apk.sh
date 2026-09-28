#!/usr/bin/env sh
set -eu
ANDROID_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if ! command -v java >/dev/null 2>&1; then
  echo "ERROR: JDK 17 is required to compile the APK. Open the project in Android Studio or install JDK 17." >&2
  exit 1
fi
if ! command -v gradle >/dev/null 2>&1; then
  echo "ERROR: Gradle 9.6.0+ is required. Android Studio can download the configured Android Gradle Plugin and SDK." >&2
  exit 1
fi

"$ANDROID_DIR/sync-web-assets.sh"
cd "$ANDROID_DIR"
gradle --no-daemon --stacktrace :app:assembleDebug
printf '\nDebug APK: %s\n' "$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
