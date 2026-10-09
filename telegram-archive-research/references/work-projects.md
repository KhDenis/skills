# Work Chat to Project Archive

Use this workflow when the user wants to discover likely work chats, verify them, collect project media, and reconstruct project histories. The stages are deliberately separate so a broad scan cannot silently trigger a bulk media download.

Set the engine once:

```bash
ENGINE="${TELEGRAM_ARCHIVE_HOME:-$HOME/telegram-chat-exporter}"
PY="$ENGINE/.venv/bin/python3"
APP="$ENGINE/scripts/work_project_archive.py"
```

## 1. Discover

Translate the user's description into a concise brief and optional keywords. Recent previews are read, but no media is downloaded.

```bash
"$PY" "$APP" discover \
  --brief "Go decor: design projects, renders from designers, client and installation discussions" \
  --keywords "го декор визуализация интерьер фасад мебель" \
  --max-dialogs 300 \
  --preview-messages 20
```

Review `output/work_projects/candidates.md` with the user. Scores are triage signals, not conclusions.

## 2. Approve or Reject

Use numeric dialog IDs from the candidate report. Only approved dialogs can enter the archive or image downloader.

```bash
"$PY" "$APP" approve --chat 123456 --chat -1001234567890
"$PY" "$APP" reject --chat 987654
```

## 3. Synchronize Text and Image Metadata

Full history:

```bash
"$PY" "$APP" sync
```

For a bounded first pass, use `--limit-per-chat 1000`; repeat to continue older history. This stores text, captions, provenance, and image metadata but does not download images.

Export for agent analysis:

```bash
"$PY" "$APP" export-analysis
```

The JSONL is `output/work_projects/approved_messages.jsonl`.

## 4. Plan and Download Images

Always show the plan first:

```bash
"$PY" "$APP" plan-images
```

After the user accepts the count and known size:

```bash
"$PY" "$APP" download-images --confirm
```

Use `--max-items` or `--max-bytes` for a bounded run. Images are content-addressed by SHA-256 and downloaded only from currently approved dialogs.

## 5. Review Images and Renders

Build manageable labeled grids:

```bash
"$PY" "$APP" contact-sheets
```

Pages and `manifest.jsonl` are written to `output/work_projects/contact_sheets/`. Inspect the pages with the image-viewing tool. Every cell is labeled with `dialog_id:message_id`, date, chat, and caption. Record exact `message_refs` for designer renders and other project assets; do not classify an image from its filename alone.

## 6. Build the Project Manifest

Analyze the approved JSONL and draft a reviewed UTF-8 JSON manifest. Do not assume every message in a work chat belongs to the same project. Use explicit message references for ambiguous chats.

```json
{
  "projects": [
    {
      "id": "go-decor-northern-house",
      "name": "Go decor — Northern House",
      "description": "Interior design and implementation chronology.",
      "dialog_ids": [-1001234567890],
      "keywords": ["northern house", "северный дом"],
      "message_refs": ["123456:789"]
    }
  ]
}
```

Selectors are additive for explicit `message_refs`; `dialog_ids` constrain keyword matching. Review names, aliases, and ambiguous assignments before materializing.

## 7. Materialize Project Folders

```bash
"$PY" "$APP" materialize --manifest projects.json
```

Each folder under `output/work_projects/projects/` contains:

- `timeline.md` with chronological, source-linked events;
- `messages.jsonl` for further analysis;
- `images/` containing hard links where possible, so the media store is not duplicated.

The agent may add a source-grounded `summary.md`, project facts, participant roles, milestones, open questions, and render selections. Clearly distinguish direct evidence from inference.

## Status

```bash
"$PY" "$APP" status
```

The SQLite database is cumulative and idempotent. Re-running discovery refreshes scores without losing approval decisions; synchronization upserts messages; downloaded files and project memberships are reused.
