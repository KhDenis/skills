"""Incremental text-only archive of personal chats and Hygge work discussions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient

if __package__:
    from .tg_config import PROJECT_ROOT, telegram_credentials, telethon_proxy
else:
    from tg_config import PROJECT_ROOT, telegram_credentials, telethon_proxy


DB_PATH = PROJECT_ROOT / "data" / "personal_text_archive.sqlite3"
LEGACY_PATH = PROJECT_ROOT / "data" / "chat_archive.sqlite3"
EXPORT_DIR = PROJECT_ROOT / "output" / "personal_text_export"
HYGGE = re.compile(r"хюгге|hygge", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_hygge(title: str) -> bool:
    return bool(HYGGE.search(title or ""))


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    os.chmod(path, 0o600)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            dialog_id INTEGER NOT NULL,
            id INTEGER NOT NULL,
            date TEXT NOT NULL,
            sender_id INTEGER,
            sender TEXT,
            text TEXT NOT NULL CHECK(length(trim(text)) > 0),
            reply_to_msg_id INTEGER,
            edited_at TEXT,
            PRIMARY KEY(dialog_id, id),
            FOREIGN KEY(dialog_id) REFERENCES dialogs(id)
        );
        CREATE INDEX IF NOT EXISTS messages_date_idx ON messages(date);
        CREATE INDEX IF NOT EXISTS messages_sender_idx ON messages(sender_id);
        CREATE TABLE IF NOT EXISTS scan_state (
            dialog_id INTEGER PRIMARY KEY,
            eligible INTEGER,
            last_scanned_at TEXT,
            oldest_seen_id INTEGER,
            newest_seen_id INTEGER,
            backfill_complete INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS archive_sources (
            path TEXT PRIMARY KEY,
            dialog_id INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            imported_at TEXT NOT NULL
        );
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
    if "typed_text" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN typed_text TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE messages SET typed_text=text")
    if "transcript" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN transcript TEXT")
    if "transcript_kind" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN transcript_kind TEXT")
    if "transcript_priority" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN transcript_priority INTEGER NOT NULL DEFAULT 0")
    db.commit()
    return db


def upsert_dialog(db: sqlite3.Connection, dialog_id: int, title: str, kind: str, username: str = "") -> None:
    db.execute("""
        INSERT INTO dialogs(id, title, username, kind) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET title=excluded.title,
            username=excluded.username, kind=excluded.kind
    """, (dialog_id, title, username, kind))


def combined_text(typed: str, transcript: str, kind: str) -> str:
    if not transcript:
        return typed
    label = "Кружок" if kind == "video_note" else "Голосовое" if kind == "voice" else "Аудио"
    spoken = f"[{label}, расшифровка]\n{transcript}"
    return f"{typed}\n\n{spoken}" if typed else spoken


def upsert_text(db: sqlite3.Connection, dialog_id: int, record: dict,
                remove_empty: bool = False, from_archive: bool = False,
                priority: int = 0) -> bool:
    existing = db.execute("""
        SELECT typed_text, transcript, transcript_kind, transcript_priority
        FROM messages WHERE dialog_id=? AND id=?
    """, (dialog_id, record["id"])).fetchone()
    typed = (record.get("text") or "").strip()
    if from_archive and existing and existing["typed_text"]:
        typed = existing["typed_text"]
    transcript = existing["transcript"] or "" if existing else ""
    kind = existing["transcript_kind"] or "" if existing else ""
    transcript_priority = existing["transcript_priority"] if existing else 0
    incoming = (record.get("transcript") or "").strip()
    if incoming and priority >= transcript_priority:
        transcript = incoming
        kind = record.get("media_kind") or "audio"
        transcript_priority = priority
    text = combined_text(typed, transcript, kind)
    if not text:
        if remove_empty:
            db.execute("DELETE FROM messages WHERE dialog_id=? AND id=?", (dialog_id, record["id"]))
        return False
    db.execute("""
        INSERT INTO messages(dialog_id, id, date, sender_id, sender, text, reply_to_msg_id,
            edited_at, typed_text, transcript, transcript_kind, transcript_priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dialog_id, id) DO UPDATE SET
            date=excluded.date, sender_id=excluded.sender_id,
            sender=COALESCE(excluded.sender, messages.sender),
            text=excluded.text, reply_to_msg_id=excluded.reply_to_msg_id,
            edited_at=COALESCE(excluded.edited_at, messages.edited_at),
            typed_text=excluded.typed_text, transcript=excluded.transcript,
            transcript_kind=excluded.transcript_kind,
            transcript_priority=excluded.transcript_priority
    """, (dialog_id, record["id"], record["date"], record.get("sender_id"),
          record.get("sender"), text, record.get("reply_to_msg_id"), record.get("edited_at"),
          typed, transcript or None, kind or None, transcript_priority))
    return True


def migrate_legacy(db: sqlite3.Connection, legacy_path: Path, self_id: int) -> None:
    if not legacy_path.exists():
        raise SystemExit(f"Legacy archive not found: {legacy_path}")
    old = sqlite3.connect(str(legacy_path))
    old.row_factory = sqlite3.Row
    try:
        eligible = []
        for dialog in old.execute("SELECT * FROM dialogs"):
            if dialog["id"] == self_id:
                continue
            username = (dialog["username"] or "").lower()
            is_bot = username.endswith("bot") or username == "botfather" or dialog["id"] == 93372553
            if (dialog["kind"] != "user" and is_hygge(dialog["title"])) or (
                dialog["kind"] == "user" and not is_bot and old.execute(
                    "SELECT 1 FROM messages WHERE dialog_id=? AND sender_id=? LIMIT 1",
                    (dialog["id"], self_id),
                ).fetchone()
            ):
                eligible.append(dialog)
        with db:
            for dialog in eligible:
                dialog_id = dialog["id"]
                upsert_dialog(db, dialog_id, dialog["title"], dialog["kind"], dialog["username"] or "")
                bounds = old.execute(
                    "SELECT MIN(id), MAX(id) FROM messages WHERE dialog_id=?", (dialog_id,)
                ).fetchone()
                db.execute("""
                    INSERT INTO scan_state(dialog_id, eligible, oldest_seen_id, newest_seen_id)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(dialog_id) DO UPDATE SET eligible=1,
                        oldest_seen_id=COALESCE(scan_state.oldest_seen_id, excluded.oldest_seen_id),
                        newest_seen_id=CASE
                            WHEN scan_state.newest_seen_id IS NULL THEN excluded.newest_seen_id
                            WHEN excluded.newest_seen_id IS NULL THEN scan_state.newest_seen_id
                            ELSE MAX(scan_state.newest_seen_id, excluded.newest_seen_id)
                        END
                """, (dialog_id, bounds[0], bounds[1]))
                for record in old.execute("SELECT * FROM messages WHERE dialog_id=?", (dialog_id,)):
                    upsert_text(db, dialog_id, dict(record))
        print(f"Migrated {len(eligible)} dialogs from previous archive", flush=True)
    finally:
        old.close()


def archive_paths() -> list[Path]:
    paths = list((PROJECT_ROOT / "data" / "checkpoints").glob("*.jsonl"))
    paths.extend(p for p in (PROJECT_ROOT / "output").glob("*_full_chat.jsonl")
                 if not p.name.startswith(("test_", "debug_")))
    sonya = PROJECT_ROOT / "output" / "sonya_50.jsonl"
    if sonya.exists():
        paths.append(sonya)
    return sorted((p for p in paths if p.is_file()), key=lambda p: (p.parent.name != "checkpoints", str(p)))


def archive_slug(title: str) -> str:
    return re.sub(r"[^\w.-]+", "_", title, flags=re.UNICODE).strip("_")[:120].casefold()


def resolve_archive(path: Path, catalog: dict, sender_ids: set, self_id: int):
    stem = path.stem
    if stem.endswith("_full_chat"):
        stem = stem[:-len("_full_chat")]
    elif stem == "sonya_50":
        stem = "Сонья"
    matches = [item for item in catalog.values() if archive_slug(item["title"]) == stem.casefold()]
    if len(matches) != 1:
        other_ids = sender_ids - {self_id, None}
        matches = [catalog[peer_id] for peer_id in other_ids if peer_id in catalog] if len(other_ids) == 1 else []
    if len(matches) != 1:
        return None
    item = matches[0]
    if item["kind"] == "user":
        if self_id not in sender_ids and not item["selected"]:
            return None
    elif not is_hygge(item["title"]):
        return None
    return item


def selected_catalog(db: sqlite3.Connection) -> dict:
    return {
        row["id"]: {"id": row["id"], "title": row["title"], "kind": row["kind"],
                    "username": row["username"] or "", "selected": True}
        for row in db.execute("SELECT * FROM dialogs")
    }


def import_archives(db: sqlite3.Connection, self_id: int, catalog: dict) -> int:
    imported = 0
    for path in archive_paths():
        relative = str(path.relative_to(PROJECT_ROOT))
        stat = path.stat()
        previous = db.execute("SELECT * FROM archive_sources WHERE path=?", (relative,)).fetchone()
        if previous and previous["size_bytes"] == stat.st_size and previous["mtime_ns"] == stat.st_mtime_ns:
            continue
        try:
            sender_ids = set()
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    sender_ids.add(json.loads(line).get("sender_id"))
            target = resolve_archive(path, catalog, sender_ids, self_id)
            if not target:
                print(f"Skipped unmatched archive: {relative}", flush=True)
                continue
            priority = 10 if path.parent.name == "checkpoints" else 20
            count = 0
            with db:
                upsert_dialog(db, target["id"], target["title"], target["kind"], target["username"])
                db.execute("""
                    INSERT INTO scan_state(dialog_id, eligible) VALUES (?, 1)
                    ON CONFLICT(dialog_id) DO UPDATE SET eligible=1
                """, (target["id"],))
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        upsert_text(db, target["id"], json.loads(line), from_archive=True, priority=priority)
                        count += 1
                db.execute("""
                    INSERT INTO archive_sources(path, dialog_id, size_bytes, mtime_ns, imported_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET dialog_id=excluded.dialog_id,
                        size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,
                        imported_at=excluded.imported_at
                """, (relative, target["id"], stat.st_size, stat.st_mtime_ns, utc_now()))
            imported += count
            print(f"Imported {relative}: {count} records", flush=True)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            db.rollback()
            print(f"Archive import failed for {relative}: {type(exc).__name__}: {exc}", flush=True)
    return imported


def telegram_record(message) -> dict:
    sender = message.sender
    sender_name = None
    if sender:
        sender_name = " ".join(
            part for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if part
        ) or getattr(sender, "title", None)
    return {
        "id": message.id,
        "date": message.date.isoformat(),
        "sender_id": message.sender_id,
        "sender": sender_name,
        "text": message.message or "",
        "reply_to_msg_id": message.reply_to_msg_id,
        "edited_at": message.edit_date.isoformat() if message.edit_date else None,
    }


async def check_authored(client: TelegramClient, entity, state: sqlite3.Row,
                         self_id: int, batch_size: int) -> tuple[bool, int, int, bool]:
    oldest, newest = state["oldest_seen_id"], state["newest_seen_id"]
    if newest is None:
        recent = [m async for m in client.iter_messages(entity, limit=batch_size)]
    else:
        recent = [m async for m in client.iter_messages(
            entity, min_id=newest, reverse=True, limit=batch_size
        )]
    if recent:
        newest = max(newest or 0, *(m.id for m in recent))
        oldest = min(oldest or recent[0].id, *(m.id for m in recent))
    if any(m.sender_id == self_id for m in recent):
        return True, oldest, newest, False
    if state["eligible"] == 0:
        return False, oldest, newest, True
    if state["newest_seen_id"] is None:
        older = []
        complete = len(recent) < batch_size
    else:
        older = [m async for m in client.iter_messages(entity, max_id=oldest, limit=batch_size)]
        complete = len(older) < batch_size
    if older:
        oldest = min(oldest, *(m.id for m in older))
    return any(m.sender_id == self_id for m in older), oldest, newest, complete


async def sync_dialog(client: TelegramClient, db: sqlite3.Connection, dialog, batch_size: int) -> tuple[int, int]:
    dialog_id = dialog.id
    state = db.execute("SELECT * FROM scan_state WHERE dialog_id=?", (dialog_id,)).fetchone()
    oldest, newest = state["oldest_seen_id"], state["newest_seen_id"]
    complete = bool(state["backfill_complete"])
    new_text = old_text = 0
    with db:
        if newest is None:
            batch = [message async for message in client.iter_messages(dialog.entity, limit=batch_size)]
            for message in batch:
                new_text += upsert_text(db, dialog_id, telegram_record(message))
            if batch:
                newest = max(message.id for message in batch)
                oldest = min(message.id for message in batch)
            complete = len(batch) < batch_size
        else:
            batch = [message async for message in client.iter_messages(
                dialog.entity, min_id=newest, reverse=True, limit=batch_size
            )]
            for message in batch:
                new_text += upsert_text(db, dialog_id, telegram_record(message))
            if batch:
                newest = max(message.id for message in batch)
            if not complete and oldest is not None:
                batch = [message async for message in client.iter_messages(
                    dialog.entity, max_id=oldest, limit=batch_size
                )]
                for message in batch:
                    old_text += upsert_text(db, dialog_id, telegram_record(message))
                if batch:
                    oldest = min(message.id for message in batch)
                complete = len(batch) < batch_size
            async for message in client.iter_messages(dialog.entity, limit=20):
                upsert_text(db, dialog_id, telegram_record(message), remove_empty=True)
        db.execute("""
            UPDATE scan_state SET eligible=1, last_scanned_at=?, oldest_seen_id=?,
                newest_seen_id=?, backfill_complete=? WHERE dialog_id=?
        """, (utc_now(), oldest, newest, int(complete), dialog_id))
    return new_text, old_text


async def sync(db: sqlite3.Connection, batch_size: int, max_dialogs: int, recent_dialogs: int) -> None:
    api_id, api_hash, phone = telegram_credentials()
    client = TelegramClient(str(PROJECT_ROOT / "telegram_export"), api_id, api_hash, proxy=telethon_proxy())
    await client.start(phone=phone)
    try:
        self_id = (await client.get_me()).id
        candidates = []
        async for dialog in client.iter_dialogs():
            if dialog.id == self_id:
                continue
            if (dialog.is_user and not getattr(dialog.entity, "bot", False)) or (
                not dialog.is_user and is_hygge(dialog.name or "")
            ):
                candidates.append(dialog)
                db.execute("INSERT OR IGNORE INTO scan_state(dialog_id) VALUES (?)", (dialog.id,))
        db.commit()
        catalog = selected_catalog(db)
        for dialog in candidates:
            catalog[dialog.id] = {
                "id": dialog.id,
                "title": dialog.name or str(dialog.id),
                "kind": "user" if dialog.is_user else "group" if dialog.is_group else "channel",
                "username": getattr(dialog.entity, "username", None) or "",
                "selected": dialog.id in catalog,
            }
        import_archives(db, self_id, catalog)
        states = {row["dialog_id"]: row for row in db.execute("SELECT * FROM scan_state")}
        recent = sorted(
            (d for d in candidates if states[d.id]["eligible"] == 1 or states[d.id]["last_scanned_at"] is None),
            key=lambda d: d.date.timestamp() if d.date else 0,
            reverse=True,
        )[:recent_dialogs]
        recent_ids = {dialog.id for dialog in recent}
        pending = sorted(
            (d for d in candidates if d.id not in recent_ids),
            key=lambda d: (states[d.id]["last_scanned_at"] or "", d.id),
        )
        selected = recent + pending[:max_dialogs - len(recent)]
        for dialog in selected:
            try:
                state = states[dialog.id]
                eligible = (not dialog.is_user and is_hygge(dialog.name or "")) or state["eligible"] == 1
                if not eligible:
                    eligible, oldest, newest, complete = await check_authored(
                        client, dialog.entity, state, self_id, batch_size
                    )
                if not eligible:
                    db.execute("""
                        UPDATE scan_state SET eligible=?, last_scanned_at=?,
                            oldest_seen_id=?, newest_seen_id=? WHERE dialog_id=?
                    """, (0 if complete else None, utc_now(), oldest, newest, dialog.id))
                    db.commit()
                    continue
                kind = "user" if dialog.is_user else "group" if dialog.is_group else "channel"
                upsert_dialog(db, dialog.id, dialog.name or str(dialog.id), kind,
                              getattr(dialog.entity, "username", None) or "")
                if state["eligible"] != 1:
                    db.execute("""
                        UPDATE scan_state SET eligible=1, oldest_seen_id=NULL,
                            newest_seen_id=NULL, backfill_complete=0 WHERE dialog_id=?
                    """, (dialog.id,))
                db.commit()
                new, old = await sync_dialog(client, db, dialog, batch_size)
                print(f"{dialog.id}: +{new} new texts, +{old} older texts", flush=True)
            except Exception as exc:
                db.rollback()
                print(f"{dialog.id}: {type(exc).__name__}: {exc}", flush=True)
        status(db)
        print(f"Scanned {len(selected)}/{len(candidates)} candidate dialogs", flush=True)
    finally:
        await client.disconnect()


def export(db: sqlite3.Connection, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for name, query in (
        ("dialogs.jsonl", "SELECT * FROM dialogs ORDER BY id"),
        ("messages.jsonl", "SELECT * FROM messages ORDER BY date, dialog_id, id"),
    ):
        path = directory / name
        with path.open("w", encoding="utf-8") as handle:
            for row in db.execute(query):
                handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
        print(f"Wrote {path}", flush=True)


def status(db: sqlite3.Connection) -> None:
    dialogs = db.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0]
    messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    transcripts = db.execute("SELECT COUNT(*) FROM messages WHERE transcript IS NOT NULL AND transcript<>''").fetchone()[0]
    sources = db.execute("SELECT COUNT(*) FROM archive_sources").fetchone()[0]
    checked = db.execute("SELECT COUNT(*) FROM scan_state WHERE last_scanned_at IS NOT NULL").fetchone()[0]
    print(f"Selected dialogs: {dialogs}; text records: {messages}; transcribed audio: {transcripts}; archives: {sources}; candidates checked: {checked}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal text-only Telegram archive")
    parser.add_argument("command", choices=("migrate", "import", "sync", "export", "status"))
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-dialogs", type=int, default=30)
    parser.add_argument("--recent-dialogs", type=int, default=5)
    parser.add_argument("--self-id", type=int, default=292635778, help="Your Telegram user ID for one-time legacy migration")
    parser.add_argument("--export-dir", type=Path, default=EXPORT_DIR)
    args = parser.parse_args()
    if args.batch_size < 1 or args.max_dialogs < 1 or not 0 <= args.recent_dialogs <= args.max_dialogs:
        parser.error("batch-size and max-dialogs must be positive; recent-dialogs must be within max-dialogs")
    db = open_db()
    try:
        if args.command == "migrate":
            migrate_legacy(db, LEGACY_PATH, args.self_id)
        elif args.command == "import":
            import_archives(db, args.self_id, selected_catalog(db))
        elif args.command == "sync":
            asyncio.run(sync(db, args.batch_size, args.max_dialogs, args.recent_dialogs))
        elif args.command == "export":
            export(db, args.export_dir)
        status(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
