# Work Chat to Project Archive

Use this workflow when the user wants to discover likely work chats, verify them, collect project media, and reconstruct project histories. The stages are deliberately separate so a broad scan cannot silently trigger a bulk media download.

Set the engine once:

```bash
ENGINE="${TELEGRAM_ARCHIVE_HOME:-$HOME/telegram-chat-exporter}"
PY="$ENGINE/.venv/bin/python3"
APP="$ENGINE/scripts/work_project_archive.py"
```

## 1. Search the Full History by Text

Start here. Each phrase is searched separately through Telegram's global search, so old chats are found too; only text and attachment metadata are stored, no media is downloaded.

```bash
"$PY" "$APP" search \
  --keywords "го декор, рендер, визуализация, смета, монтаж" \
  --since 2019-01-01 \
  --max-hits-per-keyword 500
```

- `--keyword-file PATH` adds phrases from a UTF-8 file, one per line (`#` starts a comment); commas also separate phrases.
- `--chat ID` (repeatable) searches only inside the given dialogs instead of globally.
- `--since` / `--until` are inclusive dates; `--pause` sets the seconds between phrases (default 2). Flood waits from Telegram are waited out and the phrase is retried.
- Bots and broadcast channels are skipped unless `--include-broadcasts` is given.

Hits go to the `search_hits` table; matched chats are added to `dialogs` as `pending`, and existing `approved`/`rejected` decisions are kept. Re-running is idempotent. Review `output/work_projects/search_hits.md` with the user: chats by number of matching messages, matches per phrase, first and last date, attachments by kind and up to five `dialog_id:message_id` examples. `search-report` rewrites the report from the database.

### Optional: Discover Recent Activity

`discover` ranks every dialog by its last `--preview-messages` messages. It is a quick overview of current chats and misses older projects, so use it after `search`, not instead of it.

```bash
"$PY" "$APP" discover \
  --brief "Go decor: design projects, renders from designers, client and installation discussions" \
  --keywords "го декор визуализация интерьер фасад мебель" \
  --max-dialogs 300 \
  --preview-messages 20
```

Its report is `output/work_projects/candidates.md`. Scores are triage signals, not conclusions.

## 2. Approve or Reject

Use numeric dialog IDs from `search_hits.md` or `candidates.md`. Only approved dialogs can enter the archive or image downloader.

```bash
"$PY" "$APP" approve --chat 123456 --chat -1001234567890
"$PY" "$APP" reject --chat 987654
```

## 3. Synchronize Text and Attachment Metadata

Full history:

```bash
"$PY" "$APP" sync
```

For a bounded first pass, use `--limit-per-chat 1000`; repeat to continue older history. This stores text, captions, provenance, and the kind, name and size of every attachment (`photo`, `image_document`, `video`, `video_note`, `voice`, `audio`, `document`) but downloads no files.

Export for agent analysis:

```bash
"$PY" "$APP" export-analysis
```

The JSONL is `output/work_projects/approved_messages.jsonl`.

## 4. Plan and Download Media for Projects

Media comes after the project manifest, not before it: build the manifest from text (section 6), run `materialize` once without media so that project membership is recorded (section 7), then download only what the projects need. There is no default selection; a command without one refuses to run.

Choose messages in any combination:

- `--project ID`: messages assigned to the project by the last `materialize`; repeatable.
- `--refs dialog_id:message_id ...` or `--refs-file PATH` (one or more references per line, `#` starts a comment).
- `--around N`: also take attachments from up to N synchronized messages before and after each chosen message in the same chat. Designers often send renders as a series around the message that mentions the project.

Always show the plan first. It prints the pending count and known size in total, by kind and by project:

```bash
"$PY" "$APP" plan-media --project go-decor-northern-house --around 5
```

After the user accepts the count and size:

```bash
"$PY" "$APP" download-media --project go-decor-northern-house --around 5 --max-items 20 --confirm
```

`--kinds photo,image_document,video` narrows the selection (default: all kinds). Use `--max-items` or `--max-bytes` for a bounded run. Files are content-addressed by SHA-256, taken only from currently approved dialogs, and a repeated run downloads nothing twice. Sizes of attachments synchronized by an older version may be unknown until downloaded. A flood wait of up to 15 minutes is waited out; a longer one ends the run cleanly, a file that fails is reported and skipped, and the next run continues with what is still pending.

`plan-images` and `download-images --confirm` remain for the old order of work: they take every photo and image document of all approved dialogs. Prefer the targeted commands for large accounts.

## 5. Review and Tag Images

Tagging happens after media is downloaded for the projects (sections 4 and 7). Each attachment gets one tag: `render`, `drawing`, `photo` (taken with a camera, stage not decided), `site_photo`, `finished` or `other`. Renders and drawings are the "before" of a portfolio entry, `finished` is the "after".

First apply the cheap rules. They use only the file type (PDF, DWG and similar are drawings), a real file name (`render…`, `IMG_1234…`, `планировка…`), camera EXIF data and the caption; whatever no rule recognises stays untagged. Compressed Telegram photos carry no EXIF and no real name, so most of them need a look.

```bash
TAGS="$ENGINE/scripts/image_tags.py"
"$PY" "$TAGS" auto
"$PY" "$TAGS" report --list-untagged
```

Then build labeled grids, for one project at a time:

```bash
"$PY" "$APP" contact-sheets --project go-decor-northern-house
```

Pages and `manifest.jsonl` are written to `output/work_projects/contact_sheets/`. Inspect the pages with the image-viewing tool. Every cell is labeled with `dialog_id:message_id`, date, current tag, chat and caption; only images are shown. Decide from what the image shows, not from its file name, and record the decision:

```bash
"$PY" "$TAGS" set --tag finished --refs 123456:789 123456:790 --note "готовая гостиная"
"$PY" "$TAGS" set --tag render --refs-file renders.txt
```

Manual tags are never changed by `auto`; `clear --refs ...` removes a tag. `report` prints counts per project (by tag, untagged, before, after) without names or text.

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

Check the manifest before using it. The command writes nothing; it prints, per project, the number of messages, the first and last date and the attachments (downloaded or not), and exits with 1 when it finds a problem: a reference that is not synchronized or belongs to a chat that is not approved, a project that selects nothing, or two projects with the same id or folder.

```bash
"$PY" "$APP" check-manifest --manifest projects.json
```

## 7. Materialize Project Folders

```bash
"$PY" "$APP" materialize --manifest projects.json
```

Run it once before downloading media (it records which messages belong to which project, which `plan-media --project` needs) and again after `download-media` to place the files.

Each folder under `output/work_projects/projects/` contains:

- `timeline.md` with chronological, source-linked events; a message with an attachment shows its kind and a relative link to the file, or `(not downloaded)`;
- `messages.jsonl` for further analysis, with `media_file` set to the relative path of a downloaded attachment;
- `media/` with every downloaded attachment of the project, named `<date>_<dialog_id>_<message_id>_<name>`;
- `images/` with the photos and image documents again, for visual review.

Files are hard links where possible, so the media store is not duplicated.

The agent may add a source-grounded `summary.md`, project facts, participant roles, milestones, open questions, and render selections. Clearly distinguish direct evidence from inference.

## 8. Combined Chronology and State Report

After `materialize` has placed the project folders (with `--output-dir` inside the chosen local or synchronized folder), write one chronology across all projects into the same directory:

```bash
"$PY" "$ENGINE/scripts/chronology.py" timeline --output-dir "<output>/Projects"
```

`chronology.md` starts with a table of projects (first and last date, messages, attachments, folder link) and then lists every project message by day with relative links to the project folder and the attachment file. `chronology.jsonl` holds the same records for tools. Links are relative, so the folder can be copied as a whole. The script opens the database read-only and never contacts Telegram.

```bash
"$PY" "$ENGINE/scripts/chronology.py" report
```

prints counts as JSON (dialogs by status, messages, attachments by kind and how many are downloaded, projects, last synchronization, dialogs whose history is incomplete) without chat titles or message text; it exits with 1 if the database cannot be read.

## 9. Portfolio Skeleton

When images are tagged (section 5) and the project folders hold the files (section 7), write `portfolio.json` into every project folder:

```bash
"$PY" "$ENGINE/scripts/portfolio.py" --output-dir "<output>/Projects" build
```

The file has two parts. `generated` is rewritten on every run: project dates, message count, partners (who wrote, how often), chats, and the tagged files that exist in the folder, grouped as `before` (renders, drawings), `after` (finished object) and `progress` (site photos, untriaged camera photos), each with its reference, date, relative file path and caption. `editorial` is written by the agent with the owner and is never overwritten: `title`, `date`, `event`, `summary`, `collaborators`, and the references chosen for `before` and `after`. Base the editorial text on the project's `timeline.md`; state only what the messages show.

```bash
"$PY" "$ENGINE/scripts/portfolio.py" --output-dir "<output>/Projects" check
```

lists per project what is still missing and marks a project `ready` when the texts are filled and both `before` and `after` have chosen images. It exits with 1 when a chosen reference is not a tagged file of the project or its file is gone. A broken `portfolio.json` is never overwritten by `build`.

## 10. Printable Portfolio (trial)

One candidate for the designer stage, to be compared with the others on real projects before it is chosen: an HTML page built from the ready `portfolio.json` files, printable to PDF.

```bash
"$PY" "$ENGINE/scripts/portfolio_html.py" --output-dir "<output>/Projects" --pdf
```

It writes `portfolio.html` into every ready project folder and a combined `portfolio.html` (cover, contents, one page per project, newest first) next to the folders; `--pdf` prints the combined page with headless Chrome when it is installed. Projects that are not ready are skipped with the reason; `--include-incomplete` renders them as drafts. Only the images chosen in `editorial.before` and `editorial.after` are shown. `--theme theme.json` sets `company`, `tagline`, `contacts`, `logo` (path relative to the output directory), the colours `accent`, `ink`, `paper`, `muted`, `font` and the section labels. The page loads nothing from the network and links files relatively.

## Status

```bash
"$PY" "$APP" status
```

The SQLite database is cumulative and idempotent. Re-running discovery refreshes scores without losing approval decisions; synchronization upserts messages; downloaded files and project memberships are reused.
