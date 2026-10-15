# Skills

**План и текущая задача:** [ROADMAP.md](ROADMAP.md) → [Issue #1](https://github.com/KhDenis/skills/issues/1). Перед продолжением работы агент читает [AGENTS.md](AGENTS.md) (Claude — [CLAUDE.md](CLAUDE.md)); результаты подтверждаются PR и проверками, а не итогами чата.

Portable Codex skills maintained by KhDenis.

## Telegram Archive Research

`telegram-archive-research` installs a sanitized local engine for authorized Telegram archival, transcription, linked-file downloads, and search. It can also rank likely work chats from bounded previews, require explicit approval before archival, download targeted media for chosen project messages, tag images, build per-project folders with source-linked timelines and chronology, generate portfolio skeletons, and transcribe voice/audio/video notes. It contains no credentials, Telegram sessions, databases, messages, media, or logs.

```bash
mkdir -p ~/.codex/skills
git clone git@github.com:KhDenis/skills.git ~/codex-skills
cp -R ~/codex-skills/telegram-archive-research ~/.codex/skills/

~/.codex/skills/telegram-archive-research/scripts/install.sh --with-transcription
~/.codex/skills/telegram-archive-research/scripts/configure.py
```

See [`telegram-archive-research/references/deployment.md`](telegram-archive-research/references/deployment.md) for background jobs and multiple-account isolation.

For a workflow such as reconstructing Go decor projects from work chats, see [`telegram-archive-research/references/work-projects.md`](telegram-archive-research/references/work-projects.md).
