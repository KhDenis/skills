# Telegram Archive

Canonical source: `KhDenis/skills`, `telegram-archive-research/assets/engine`.
Version: **0.3.0**, a source migration candidate from the Go Decor archive engine
at commit `3342ccf6153f0160799269fb6c8ed01c77f477e6`.

The CLI works independently of an agent: approve dialogs, synchronize text into
SQLite, plan/download selected media, export analysis, materialize project folders
and produce source-linked chronologies. See [module.json](module.json) for commands,
[work-projects.md](references/work-projects.md) for staged use and
[deployment.md](references/deployment.md) for installation and profile isolation.
Existing image/portfolio/audio helpers are preserved; they are not new product
releases or acceptance of later roadmap stages.

## Application boundary

The private application's `audio_notes.py export-kb` command writes its specific
assets/transcripts schema and is **not part of this package**. Its implementation
and schema-specific test remain in Go Decor. The M4 shell wrapper also stays there.
All archive data, account profiles, `.env`, sessions, databases, media and runtime
logs remain outside this repository. The portable HTML helper defaults to a generic
company name; customize it with a theme.

## Verification and compatibility

```sh
python -m unittest discover -s telegram-archive-research/assets/engine/tests -v
bash -n telegram-archive-research/scripts/install.sh
```

The module-local suite includes the chronology, image-tag, portfolio and HTML
regressions previously kept in Go Decor's root tests. These tests use synthetic
fixtures and fake Telegram objects. They do not prove authorization, network
exports, transcription quality or a clean installation.

Go Decor still uses its existing copy. Do not remove it or replace a running
profile with this candidate. The next roadmap steps verify clean installation,
export/resume and an application switch to a fixed version with rollback.
To roll back this source change, use the previous skills main `06fe346`; no live
installation is changed by merging the source. Do not downgrade a live data schema
without its own compatibility check and backup.
