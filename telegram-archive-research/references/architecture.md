# Cumulative Telegram Research Architecture

## Principle

Skills hold reusable operating knowledge. The engine holds deterministic code. Profiles hold credentials and source-specific state. The archive and indexes hold cumulative memory. Reports are disposable views over those stores.

## Target Layout

```text
telegram-research/
  engine/                       shared import, transcription, indexing, search code
  profiles/
    denis/
      config.env                credentials and proxy settings, mode 0600
      telegram.session          account-specific authorization, mode 0600
      archive.sqlite3           dialogs, messages, entities, provenance
      checkpoints/
      media/                    content-addressed originals
      indexes/                  FTS, OCR, visual vectors, perceptual hashes
    another-consented-owner/
      ...                       fully isolated identity and storage
  results/
    <task-id>/                  manifests, ranked matches, exports
```

Do not duplicate the engine per profile. Do not put credentials or personal data inside the skill directory.

## Canonical Data Model

Use stable compound keys: `(profile_id, dialog_id, message_id)`. Keep source records immutable where practical and put derived data in separate tables.

- `profiles`: account identity, consent/scope metadata, creation and sync times.
- `dialogs`: Telegram ID, type, title, username.
- `messages`: sender, timestamp, text, reply relation, edit timestamp.
- `media`: message key, Telegram document/photo ID, MIME type, size, hash, local path.
- `transcripts`: media key, model/version/language, text, timestamps, quality state.
- `ocr`: media key, engine/version, extracted text and regions.
- `visual_embeddings`: media key, model/version, vector or vector-store reference.
- `perceptual_hashes`: media key, pHash/dHash for duplicate and near-duplicate lookup.
- `external_links`: message key, URL, provider, resolution/download status, local artifacts.
- `jobs`: resumable cursor, status, attempts, error, resource policy.

Every derived row should record its model/tool version so indexes can be rebuilt selectively.

## Search Layers

1. SQLite FTS5 for message text, captions, transcripts, OCR, filenames, and URLs.
2. Exact cryptographic hashes for identical files.
3. Perceptual hashes for resized/recompressed copies of the same photo.
4. CLIP-compatible visual embeddings for semantic prompts such as "photo of a red stage costume".
5. Optional face embeddings only when explicitly requested, legally appropriate, and consented; keep them profile-scoped and never silently identify unknown people.

Combine lexical and visual scores, then return a small ranked contact sheet with message provenance. Human confirmation should precede bulk copying or conclusions.

## Incremental Processing

- Sync by Telegram message cursors and upsert idempotently.
- Address media by SHA-256 so the same file is stored once per permitted scope.
- Queue only missing derived artifacts. Never retranscribe or re-embed unchanged media with the same model version.
- Persist job cursors after each item and make downloads resumable.
- Apply existing charging, idle, thermal, and power guards to transcription, OCR, and embedding jobs.

## Profile Onboarding

Require an explicit profile name and confirmation that the account owner authorized access. Create new credentials/session/database paths; authenticate interactively without recording login codes. Begin with a small read-only dialog listing, show the selected scope, then start bounded synchronization. Never repurpose the active Denis session for another owner.
