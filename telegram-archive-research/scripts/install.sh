#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$SKILL_DIR/assets/engine"
TARGET="${TELEGRAM_ARCHIVE_HOME:-$HOME/telegram-chat-exporter}"
WITH_TRANSCRIPTION=0
INSTALL_BACKGROUND=0

usage() {
  echo "Usage: $0 [--target PATH] [--with-transcription] [--background]"
}

while (($#)); do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --with-transcription) WITH_TRANSCRIPTION=1; shift ;;
    --background) INSTALL_BACKGROUND=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
mkdir -p "$TARGET/scripts" "$TARGET/data/checkpoints" "$TARGET/output"
cp "$SOURCE/requirements.txt" "$SOURCE/requirements-transcribe.txt" "$SOURCE/.env.example" "$SOURCE/.gitignore" "$TARGET/"
cp "$SOURCE/scripts/"*.py "$TARGET/scripts/"

if [[ ! -f "$TARGET/.env" ]]; then
  cp "$TARGET/.env.example" "$TARGET/.env"
  chmod 600 "$TARGET/.env"
fi

python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python3" -m pip install --upgrade pip
"$TARGET/.venv/bin/python3" -m pip install -r "$TARGET/requirements.txt"
if ((WITH_TRANSCRIPTION)); then
  "$TARGET/.venv/bin/python3" -m pip install -r "$TARGET/requirements-transcribe.txt"
fi

if ((INSTALL_BACKGROUND)); then
  [[ "$(uname -s)" == Darwin ]] || { echo "--background is currently supported only on macOS" >&2; exit 1; }
  AGENTS="$HOME/Library/LaunchAgents"
  mkdir -p "$AGENTS"
  cat >"$AGENTS/com.local.telegram-archive.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.local.telegram-archive</string>
<key>ProgramArguments</key><array><string>$TARGET/.venv/bin/python3</string><string>$TARGET/scripts/personal_text_archive.py</string><string>sync</string></array>
<key>WorkingDirectory</key><string>$TARGET</string>
<key>StartInterval</key><integer>21600</integer>
<key>StandardOutPath</key><string>$TARGET/output/personal_text_archive.log</string>
<key>StandardErrorPath</key><string>$TARGET/output/personal_text_archive.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)" "$AGENTS/com.local.telegram-archive.plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.local.telegram-archive.plist"

  if ((WITH_TRANSCRIPTION)); then
    cat >"$AGENTS/com.local.telegram-transcribe.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.local.telegram-transcribe</string>
<key>ProgramArguments</key><array><string>$TARGET/.venv/bin/python3</string><string>$TARGET/scripts/idle_transcribe.py</string></array>
<key>WorkingDirectory</key><string>$TARGET</string>
<key>StartInterval</key><integer>300</integer>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>$TARGET/output/idle_transcribe.log</string>
<key>StandardErrorPath</key><string>$TARGET/output/idle_transcribe.log</string>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)" "$AGENTS/com.local.telegram-transcribe.plist" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.local.telegram-transcribe.plist"
  fi
fi

echo
echo "Installed to: $TARGET"
echo "Next: $SKILL_DIR/scripts/configure.py --target '$TARGET'"
