# Portable Deployment

The skill contains a sanitized engine snapshot in `assets/engine`. It contains no `.env`, Telegram sessions, databases, media, messages, or logs.

## Install

The minimal installer has been checked with Python 3.12 on macOS Intel. It uses
`python3` from PATH; make sure that is the intended interpreter. For a reproducible
installation, check out the desired Git commit before running the installer.
Install into a **new profile** before upgrading a working archive.


Copy the entire `telegram-archive-research` directory to `~/.codex/skills/` on the destination machine, then run:

```bash
~/.codex/skills/telegram-archive-research/scripts/install.sh --with-transcription
~/.codex/skills/telegram-archive-research/scripts/configure.py
```

The default engine location is `~/telegram-chat-exporter`. Override it with `--target PATH` or `TELEGRAM_ARCHIVE_HOME`.

The configure command writes a private `.env`, then opens Telegram authorization. Login codes and 2FA passwords must be entered interactively and must never be placed in prompts, shell history, skill files, or reports.

For macOS periodic synchronization and idle transcription, install after authorization with:

```bash
~/.codex/skills/telegram-archive-research/scripts/install.sh --with-transcription --background
```

This refreshes bundled code while preserving `.env`, sessions, `data/`, and `output/`.

## Minimal Install

Use `install.sh` without `--with-transcription` when only dialog listing, text export, and text search are needed. Whisper dependencies are large and should not be installed unnecessarily.

## Multiple Accounts

Use one target directory per authorized Telegram account:

```bash
install.sh --target ~/telegram-profiles/alice
install.sh --target ~/telegram-profiles/bob
```

Set `TELEGRAM_ARCHIVE_HOME` to the intended profile before invoking the skill. Never copy a `.session` from one profile into another. If background jobs are needed for multiple profiles, generate unique launchd labels rather than reusing the default labels.

## Upgrade and Verification

Re-run `install.sh` from the newer skill bundle. It replaces only engine code and requirement manifests. Verify with:

```bash
~/telegram-chat-exporter/.venv/bin/python3 ~/telegram-chat-exporter/scripts/diagnose.py
~/telegram-chat-exporter/.venv/bin/python3 ~/telegram-chat-exporter/scripts/export_chat.py --list-chats
```

Back up the destination's `.env`, `*.session`, and `data/` independently. They are deliberately not included in the portable skill archive.

## Check a new installation before login

```bash
PROFILE="$HOME/telegram-profiles/test"
bash telegram-archive-research/scripts/install.sh --target "$PROFILE"
"$PROFILE/.venv/bin/python3" "$PROFILE/scripts/diagnose.py" --offline --json
"$PROFILE/.venv/bin/python3" "$PROFILE/scripts/work_project_archive.py" --help
```

`installation_ok: true` confirms the minimal local Python dependencies;
`telegram_configured: false` is expected before configuration. The offline check
makes no network calls, creates no Telegram session and does not print credential
or proxy values. `ffmpeg_available` is informational for the minimal install;
audio/video processing additionally needs ffmpeg and transcription dependencies.
Without `--offline`, the existing diagnostic checks network reachability.

For interactive authorization, run the configuration helper in your own terminal:

```bash
python3 telegram-archive-research/scripts/configure.py --target "$PROFILE"
```

API credentials are saved only to the profile's `.env` (mode 600). Enter the login
code and optional 2FA password locally when Telegram requests them; never pass them
as command arguments, Issue text or messages to an agent. `--no-auth` writes the
configuration interactively without starting login. With blank credentials,
`export_chat.py --list-chats` must stop with a missing-configuration error before
creating a session; this is expected, not proof of a successful login.

The 2026-10-15 clean-profile check verified a fresh venv, installation, offline
report, CLI help and safe refusal without credentials. A second installer run
preserved the existing `.env`. Telegram login, live media and model execution were
not part of that installation check.
