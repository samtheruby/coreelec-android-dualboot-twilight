#!/usr/bin/env bash
# Build the Reboot-to-CoreELEC APK under WSL. Installs JDK 17 + Android SDK 34
# on first run, then ./gradlew assembleDebug. Idempotent.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/RebootToCoreELEC" && pwd)"
SDK="$HOME/android-sdk"
CLT_ZIP="commandlinetools-linux-11076708_latest.zip"
CLT_URL="https://dl.google.com/android/repository/$CLT_ZIP"
OUT="$(cd "$(dirname "$0")/.." && pwd)/artifacts"

echo "== 1. JDK 17 =="
if ! command -v javac >/dev/null 2>&1; then
  sudo -n apt-get update -qq
  sudo -n apt-get install -y -qq openjdk-17-jdk-headless unzip wget
fi
export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
echo "   JAVA_HOME=$JAVA_HOME"

echo "== 2. Android cmdline-tools =="
mkdir -p "$SDK/cmdline-tools"
if [ ! -d "$SDK/cmdline-tools/latest" ]; then
  cd /tmp
  [ -f "$CLT_ZIP" ] || wget -q "$CLT_URL"
  rm -rf cmdline-tools; unzip -q "$CLT_ZIP"
  mv cmdline-tools "$SDK/cmdline-tools/latest"
fi
export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$PATH"

echo "== 3. SDK packages (licenses + platform 34 + build-tools) =="
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager --install "platform-tools" "platforms;android-34" "build-tools;34.0.0" >/dev/null

echo "== 4. gradle assembleDebug =="
cd "$PROJ"
sed -i 's/\r$//' gradlew 2>/dev/null || true
chmod +x gradlew
# local.properties so gradle finds the SDK
echo "sdk.dir=$SDK" > local.properties
./gradlew --no-daemon clean assembleDebug

APK=$(find "$PROJ/app/build/outputs/apk" -name '*.apk' | head -1)
echo "== built: $APK =="
mkdir -p "$OUT"
cp "$APK" "$OUT/RebootToCoreELEC.apk"
echo "== copied -> $OUT/RebootToCoreELEC.apk =="
ls -la "$OUT/RebootToCoreELEC.apk"
sha256sum "$OUT/RebootToCoreELEC.apk"
