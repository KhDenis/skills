---
name: telegram-archive-research
description: "Work with consented local Telegram archives: discover dialogs, export messages, transcribe voice/video notes, download linked files, inspect archive status, and search text or media. Use for Telegram archival, cross-chat research, or locating messages, links, files, and photos in authorized accounts."
---

# Telegram Archive Research

Resolve the engine in this order: `$TELEGRAM_ARCHIVE_HOME`, `~/telegram-chat-exporter`, then the legacy path `/Users/admin/agent/telegram-chat-exporter`. Reuse its virtual environment, Telegram session, proxy configuration, checkpoints, and databases instead of rebuilding one-off scripts in each conversation. If no engine exists, install the sanitized bundled engine by following [deployment.md](references/deployment.md).

## Boundaries

- Work only with the account owner's authorization and within the scope the user specifies. A logged-in session proves access, not consent for unrelated bulk analysis.
- Never print `.env`, bot tokens, API hashes, session contents, private URLs, or authentication codes.
- Keep each Telegram account/persona in a separate profile directory and database. Never merge identities merely because names match.
- Prefer read-only discovery first. Before bulk downloads, report the estimated item count, size, destination, and whether links are public or require login.
- Preserve provenance for every result: profile, dialog ID/title, message ID/date, sender, and source path or URL.
- Do not infer facts about a person from media or messages without identifying uncertainty and the evidence used.

## Workflow

1. Identify the requested profile, dialog scope, data types, and output. If the user refers to another person's Telegram account, confirm that the owner authorized access before importing it.
2. Inspect the current project and status rather than assuming old commands or ports remain valid. Use `rg`, SQLite queries, and existing scripts.
3. Reuse `telegram_export.session` and `scripts/export_chat.py` for the currently configured account. Use `scripts/personal_text_archive.py` for the cumulative personal archive.
4. For another account, create a distinct named profile and session before connecting. Do not overwrite the current `.env`, session, database, checkpoints, or outputs.
5. Store durable data in the archive/index layer; keep task-specific reports in a separate results directory. This is what creates cumulative value across Codex chats.
6. For text searches, query SQLite/JSONL structurally and return message references. For links, resolve and download with resumable transfers and verify sizes or hashes when available.
7. For photo searches, first check whether the visual index exists. If absent, explain the indexing cost and build the reusable pipeline described in [architecture.md](references/architecture.md), rather than manually inspecting every image for each request.
8. After mutations, verify observable counts/files and report where results were written. Avoid leaving foreground sessions running when a background job is appropriate.

## Current Capabilities

- `scripts/export_chat.py --list-chats`: discover dialogs.
- `scripts/export_chat.py --chat ...`: export one dialog and optionally transcribe voice messages and video notes.
- `scripts/personal_text_archive.py`: incrementally synchronize and export the cumulative text/transcript database.
- `scripts/idle_transcribe.py`: low-priority background transcription governed by charging, idle, power, and thermal checks.
- SQLite database: `data/personal_text_archive.sqlite3`.
- Existing cumulative output: `output/personal_text_export/`.

Read [architecture.md](references/architecture.md) when adding another Telegram account, cross-chat media search, OCR, embeddings, or a new durable index.

Read [deployment.md](references/deployment.md) when installing, moving, upgrading, or repairing the skill/engine on another machine.
