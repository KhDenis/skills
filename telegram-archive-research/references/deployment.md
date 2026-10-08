# Portable Deployment

The skill contains a sanitized engine snapshot in `assets/engine`. It contains no `.env`, Telegram sessions, databases, media, messages, or logs.

## Install

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
