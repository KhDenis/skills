"""Cumulative work-chat discovery, approved export, targeted media archive, and project folders."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import (
    Channel, Chat, DocumentAttributeFilename, MessageMediaDocument, MessageMediaPhoto, User,
)

from export_chat import entity_name, media_info, sender_display
from tg_config import PROJECT_ROOT, telegram_credentials, telethon_proxy


DB_PATH = PROJECT_ROOT / "data" / "work_projects.sqlite3"
MEDIA_ROOT = PROJECT_ROOT / "data" / "work_projects" / "media"
RESULTS_ROOT = PROJECT_ROOT / "output" / "work_projects"
SESSION_PATH = PROJECT_ROOT / "telegram_export"
WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
IMAGE_KINDS = ("photo", "image_document")
MEDIA_KINDS = IMAGE_KINDS + ("video", "video_note", "voice", "audio", "document")
DEFAULT_WORK_WORDS = {
    "проект", "объект", "работа", "заказ", "заказчик", "клиент", "дизайн",
    "дизайнер", "рендер", "визуализация", "чертеж", "планировка", "смета",
    "договор", "монтаж", "декор", "мебель", "поставка", "производство",
    "project", "design", "render", "decor", "drawing", "client",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return value[:100] or "project"


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            kind TEXT NOT NULL,
            relevance_score INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            preview_text TEXT NOT NULL DEFAULT '',
            preview_image_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(review_status IN ('pending', 'approved', 'rejected')),
            last_discovered_at TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            dialog_id INTEGER NOT NULL,
            id INTEGER NOT NULL,
            date TEXT NOT NULL,
            sender_id INTEGER,
            sender TEXT,
            text TEXT NOT NULL DEFAULT '',
            reply_to_msg_id INTEGER,
            media_kind TEXT,
            media_name TEXT,
            media_size INTEGER,
            PRIMARY KEY(dialog_id, id)
        );
        CREATE INDEX IF NOT EXISTS messages_date_idx ON messages(date);
        CREATE TABLE IF NOT EXISTS media (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            original_name TEXT,
            size_bytes INTEGER,
            sha256 TEXT,
            local_path TEXT,
            downloaded_at TEXT,
            PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            dialog_id INTEGER PRIMARY KEY,
            newest_id INTEGER,
            oldest_id INTEGER,
            complete INTEGER NOT NULL DEFAULT 0,
            last_sync_at TEXT
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS search_hits (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            date TEXT NOT NULL,
            sender_id INTEGER,
            sender TEXT,
            text TEXT NOT NULL DEFAULT '',
            media_kind TEXT,
            media_name TEXT,
            found_at TEXT NOT NULL,
            PRIMARY KEY(dialog_id, message_id, keyword)
        );
        CREATE TABLE IF NOT EXISTS project_messages (
            project_id TEXT NOT NULL,
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY(project_id, dialog_id, message_id)
        );
    """)
    sync_columns = {row["name"] for row in db.execute("PRAGMA table_info(sync_state)")}
    if "oldest_id" not in sync_columns:
        db.execute("ALTER TABLE sync_state ADD COLUMN oldest_id INTEGER")
    # Attachments synchronized before all kinds were tracked exist only in messages.
    db.execute(f"""INSERT OR IGNORE INTO media(dialog_id,message_id,kind,original_name,size_bytes)
        SELECT dialog_id,id,media_kind,media_name,media_size FROM messages
        WHERE media_kind IN ({",".join("?" * len(MEDIA_KINDS))})""", MEDIA_KINDS)
    db.commit()
    return db


def parse_keywords(brief: str, extra: str) -> set[str]:
    words = {word.casefold() for word in WORD.findall(f"{brief} {extra}")}
    return DEFAULT_WORK_WORDS | words


def score_dialog(title: str, texts: Iterable[str], keywords: set[str], image_count: int) -> tuple[int, list[str]]:
    title_words = {word.casefold() for word in WORD.findall(title)}
    title_hits = sorted(title_words & keywords)
    body = " ".join(texts).casefold()
    body_hits = sorted({word for word in keywords if word in body})
    score = len(title_hits) * 8 + min(len(body_hits), 12) * 2 + min(image_count, 5)
    reasons = [f"title:{word}" for word in title_hits]
    reasons.extend(f"messages:{word}" for word in body_hits[:12])
    if image_count:
        reasons.append(f"recent-images:{image_count}")
    return score, reasons


def image_metadata(message: Any) -> Optional[tuple[str, str, Optional[int]]]:
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        sizes = [getattr(size, "size", 0) or 0 for size in getattr(media.photo, "sizes", [])]
        return "photo", f"photo_{message.id}.jpg", max(sizes, default=0) or None
    if isinstance(media, MessageMediaDocument) and media.document:
        mime = (media.document.mime_type or "").casefold()
        if mime.startswith("image/"):
            name = media_info(message)[2] or f"image_{message.id}"
            if "." not in name:
                extension = mime.split("/", 1)[-1].replace("jpeg", "jpg")
                name = f"{name}.{extension}"
            return "image_document", name, media.document.size or None
    return None


def media_metadata(message: Any) -> Optional[tuple[str, str, Optional[int]]]:
    """Kind, file name and size of any downloadable attachment; None for contacts, polls and the like."""
    image = image_metadata(message)
    if image:
        return image
    media = message.media
    if isinstance(media, MessageMediaDocument) and media.document:
        kind, _duration, _label = media_info(message)
        name = next((attr.file_name for attr in media.document.attributes
                     if isinstance(attr, DocumentAttributeFilename) and attr.file_name), None)
        if not name:
            extension = mimetypes.guess_extension((media.document.mime_type or "").casefold()) or ""
            name = f"{kind}_{message.id}{extension}"
        return kind, name, media.document.size or None
    return None


def telegram_record(message: Any) -> dict[str, Any]:
    image = media_metadata(message)
    kind, _duration, attachment = media_info(message)
    return {
        "id": message.id,
        "date": message.date.isoformat(),
        "sender_id": message.sender_id,
        "sender": sender_display(message.sender, message.sender_id),
        "text": message.message or "",
        "reply_to_msg_id": message.reply_to_msg_id,
        "media_kind": image[0] if image else kind,
        "media_name": image[1] if image else attachment,
        "media_size": image[2] if image else None,
    }


def upsert_message(db: sqlite3.Connection, dialog_id: int, record: dict[str, Any]) -> None:
    db.execute("""
        INSERT INTO messages(dialog_id,id,date,sender_id,sender,text,reply_to_msg_id,
            media_kind,media_name,media_size) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dialog_id,id) DO UPDATE SET date=excluded.date,
            sender_id=excluded.sender_id,sender=excluded.sender,text=excluded.text,
            reply_to_msg_id=excluded.reply_to_msg_id,media_kind=excluded.media_kind,
            media_name=excluded.media_name,media_size=excluded.media_size
    """, (dialog_id, record["id"], record["date"], record["sender_id"], record["sender"],
          record["text"], record["reply_to_msg_id"], record["media_kind"],
          record["media_name"], record["media_size"]))
    if record["media_kind"] in MEDIA_KINDS:
        db.execute("""INSERT INTO media(dialog_id,message_id,kind,original_name,size_bytes)
            VALUES (?,?,?,?,?) ON CONFLICT(dialog_id,message_id) DO UPDATE SET
            kind=excluded.kind,original_name=excluded.original_name,
            size_bytes=COALESCE(excluded.size_bytes,media.size_bytes)""",
            (dialog_id, record["id"], record["media_kind"], record["media_name"], record["media_size"]))


async def telegram_client() -> TelegramClient:
    api_id, api_hash, phone = telegram_credentials()
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash, proxy=telethon_proxy())
    await client.start(phone=phone)
    return client


async def discover(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    keywords = parse_keywords(args.brief, args.keywords)
    client = await telegram_client()
    scanned = 0
    try:
        async for dialog in client.iter_dialogs(limit=args.max_dialogs or None):
            if dialog.is_user and getattr(dialog.entity, "bot", False):
                continue
            if dialog.is_channel and getattr(dialog.entity, "broadcast", False) and not args.include_broadcasts:
                continue
            messages = [m async for m in client.iter_messages(dialog.entity, limit=args.preview_messages)]
            texts = [(m.message or "").strip() for m in messages if (m.message or "").strip()]
            image_count = sum(image_metadata(message) is not None for message in messages)
            score, reasons = score_dialog(dialog.name or "", texts, keywords, image_count)
            kind = "user" if dialog.is_user else "group" if dialog.is_group else "channel"
            preview = "\n".join(texts[:8])[:4000]
            with db:
                db.execute("""
                    INSERT INTO dialogs(id,title,username,kind,relevance_score,reasons_json,
                        preview_text,preview_image_count,last_discovered_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                        username=excluded.username,kind=excluded.kind,
                        relevance_score=excluded.relevance_score,
                        reasons_json=excluded.reasons_json,preview_text=excluded.preview_text,
                        preview_image_count=excluded.preview_image_count,
                        last_discovered_at=excluded.last_discovered_at
                """, (dialog.id, dialog.name or str(dialog.id),
                      getattr(dialog.entity, "username", None), kind, score,
                      json.dumps(reasons, ensure_ascii=False), preview, image_count, utc_now()))
            scanned += 1
            if scanned % 25 == 0:
                print(f"Scanned {scanned} dialogs", flush=True)
    finally:
        await client.disconnect()
    write_candidates(db, args.min_score)
    print(f"Discovery complete: {scanned} dialogs", flush=True)


def write_candidates(db: sqlite3.Connection, min_score: int = 1) -> Path:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = RESULTS_ROOT / "candidates.md"
    rows = db.execute("""SELECT * FROM dialogs WHERE relevance_score>=?
        ORDER BY CASE review_status WHEN 'approved' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
        relevance_score DESC,title""", (min_score,)).fetchall()
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Candidate work chats\n\n")
        handle.write("Review before approving. Discovery never downloads media.\n\n")
        for row in rows:
            reasons = ", ".join(json.loads(row["reasons_json"]))
            handle.write(f"## {row['title']}\n\n")
            handle.write(f"- ID: `{row['id']}`\n- Type: `{row['kind']}`\n")
            handle.write(f"- Status: `{row['review_status']}`\n- Score: **{row['relevance_score']}**\n")
            handle.write(f"- Signals: {reasons or 'none'}\n- Recent images: {row['preview_image_count']}\n\n")
            if row["preview_text"]:
                preview = row["preview_text"].replace("\n", " ")[:600]
                handle.write(f"> {preview}\n\n")
    print(f"Wrote {path}", flush=True)
    return path


SEARCH_TEXT_LIMIT = 2000


def parse_search_keywords(keywords: str, keyword_file: Optional[Path]) -> list[str]:
    """Each line or comma-separated item is one phrase; order is kept, duplicates dropped."""
    raw = keywords.replace(",", "\n").splitlines()
    if keyword_file:
        raw.extend(keyword_file.read_text(encoding="utf-8").replace(",", "\n").splitlines())
    seen: dict[str, str] = {}
    for item in raw:
        phrase = item.strip()
        if phrase and not phrase.startswith("#") and phrase.casefold() not in seen:
            seen[phrase.casefold()] = phrase
    return list(seen.values())


def parse_day(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def dialog_kind(entity: Any) -> str:
    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Channel) and getattr(entity, "broadcast", False):
        return "channel"
    return "group"


def is_skipped_entity(entity: Any, include_broadcasts: bool) -> bool:
    if isinstance(entity, User) and getattr(entity, "bot", False):
        return True
    return not include_broadcasts and dialog_kind(entity) == "channel"


def record_search_hit(db: sqlite3.Connection, keyword: str, message: Any, entity: Any) -> int:
    dialog_id = utils.get_peer_id(message.peer_id)
    record = telegram_record(message)
    title = entity_name(entity) if entity is not None else str(dialog_id)
    reason = f"search:{keyword}"
    with db:
        row = db.execute("SELECT reasons_json FROM dialogs WHERE id=?", (dialog_id,)).fetchone()
        if row is None:
            db.execute("""INSERT INTO dialogs(id,title,username,kind,reasons_json,last_discovered_at)
                VALUES (?,?,?,?,?,?)""",
                (dialog_id, title, getattr(entity, "username", None), dialog_kind(entity),
                 json.dumps([reason], ensure_ascii=False), utc_now()))
        else:
            reasons = json.loads(row["reasons_json"] or "[]")
            if reason not in reasons:
                reasons.append(reason)
            db.execute("""UPDATE dialogs SET title=?,username=COALESCE(?,username),kind=?,
                reasons_json=?,last_discovered_at=? WHERE id=?""",
                (title, getattr(entity, "username", None), dialog_kind(entity),
                 json.dumps(reasons, ensure_ascii=False), utc_now(), dialog_id))
        db.execute("""INSERT INTO search_hits(dialog_id,message_id,keyword,date,sender_id,sender,
                text,media_kind,media_name,found_at) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dialog_id,message_id,keyword) DO UPDATE SET date=excluded.date,
                sender_id=excluded.sender_id,sender=excluded.sender,text=excluded.text,
                media_kind=excluded.media_kind,media_name=excluded.media_name,
                found_at=excluded.found_at""",
            (dialog_id, record["id"], keyword, record["date"], record["sender_id"], record["sender"],
             record["text"][:SEARCH_TEXT_LIMIT], record["media_kind"], record["media_name"], utc_now()))
    return dialog_id


async def search_keyword(client: Any, db: sqlite3.Connection, keyword: str, targets: list[Any],
                         args: argparse.Namespace) -> int:
    """Store text hits for one keyword; never touches media. Returns the number of hits stored."""
    since, until = parse_day(args.since), parse_day(args.until)
    offset_date = datetime.combine(until + timedelta(days=1), time.min, timezone.utc) if until else None
    limit = args.max_hits_per_keyword or None
    stored = 0
    for target in targets:
        while True:
            try:
                async for message in client.iter_messages(target, search=keyword, limit=limit,
                                                          offset_date=offset_date):
                    if since and message.date.date() < since:
                        break
                    if until and message.date.date() > until:
                        continue
                    entity = target if target is not None else getattr(message, "chat", None)
                    if entity is not None and is_skipped_entity(entity, args.include_broadcasts):
                        continue
                    record_search_hit(db, keyword, message, entity)
                    stored += 1
                break
            except FloodWaitError as exc:
                print(f"Flood wait {exc.seconds}s on '{keyword}', then retrying", flush=True)
                await asyncio.sleep(exc.seconds + 1)
    return stored


async def search(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    keywords = parse_search_keywords(args.keywords, args.keyword_file)
    if not keywords:
        raise SystemExit("Give --keywords or --keyword-file")
    client = await telegram_client()
    try:
        targets: list[Any] = [None]
        if args.chat:
            wanted = {int(value) for value in args.chat}
            targets = [dialog.entity async for dialog in client.iter_dialogs() if dialog.id in wanted]
            missing = len(wanted) - len(targets)
            if missing:
                print(f"{missing} chat(s) from --chat are not in this account's dialogs", flush=True)
        for index, keyword in enumerate(keywords, 1):
            stored = await search_keyword(client, db, keyword, targets, args)
            print(f"[{index}/{len(keywords)}] '{keyword}': {stored} hit(s)", flush=True)
            if index < len(keywords) and args.pause:
                await asyncio.sleep(args.pause)
    finally:
        await client.disconnect()
    write_search_hits(db)


def write_search_hits(db: sqlite3.Connection) -> Path:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = RESULTS_ROOT / "search_hits.md"
    rows = db.execute("""SELECT h.*,d.title,d.kind,d.review_status FROM search_hits h
        JOIN dialogs d ON d.id=h.dialog_id ORDER BY h.date""").fetchall()
    chats: dict[int, dict[str, Any]] = {}
    for row in rows:
        chat = chats.setdefault(row["dialog_id"], {
            "title": row["title"], "kind": row["kind"], "status": row["review_status"],
            "messages": {}, "keywords": Counter(),
        })
        chat["keywords"][row["keyword"]] += 1
        chat["messages"].setdefault(row["message_id"], row)
    ordered = sorted(chats.items(), key=lambda item: (-len(item[1]["messages"]), item[1]["title"]))
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Search hits\n\n")
        handle.write("Text search only; no media was downloaded. Approve work chats by ID, reject the rest.\n\n")
        handle.write(f"Chats: {len(chats)}; matching messages: {sum(len(c['messages']) for c in chats.values())}\n\n")
        for dialog_id, chat in ordered:
            messages = list(chat["messages"].values())
            media = Counter(row["media_kind"] for row in messages if row["media_kind"])
            words = ", ".join(f"{word} {count}" for word, count in chat["keywords"].most_common())
            handle.write(f"## {chat['title']}\n\n")
            handle.write(f"- ID: `{dialog_id}`\n- Type: `{chat['kind']}`\n- Status: `{chat['status']}`\n")
            handle.write(f"- Matching messages: **{len(messages)}** ({words})\n")
            handle.write(f"- Dates: {messages[0]['date'][:10]} … {messages[-1]['date'][:10]}\n")
            attachments = ", ".join(f"{kind} {count}" for kind, count in media.most_common())
            handle.write(f"- With attachments: {sum(media.values())}{f' ({attachments})' if attachments else ''}\n\n")
            for row in messages[-5:]:
                text = (row["text"] or "").replace("\n", " ")[:200]
                handle.write(f"  - `{dialog_id}:{row['message_id']}` {row['date'][:10]} [{row['keyword']}] {text}\n")
            handle.write("\n")
    os.chmod(path, 0o600)
    print(f"Wrote {path}: {len(chats)} chat(s)", flush=True)
    return path


def set_review(db: sqlite3.Connection, values: list[str], status: str) -> None:
    changed = 0
    with db:
        for value in values:
            if re.fullmatch(r"-?\d+", value):
                cursor = db.execute("UPDATE dialogs SET review_status=?,reviewed_at=? WHERE id=?",
                                    (status, utc_now(), int(value)))
            else:
                cursor = db.execute("UPDATE dialogs SET review_status=?,reviewed_at=? WHERE lower(title)=lower(?)",
                                    (status, utc_now(), value))
            changed += cursor.rowcount
    if changed != len(values):
        raise SystemExit(f"Updated {changed}/{len(values)} dialogs; use an exact title or numeric ID")
    write_candidates(db)
    print(f"Marked {changed} dialog(s) as {status}", flush=True)


async def sync_approved(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT * FROM dialogs WHERE review_status='approved' ORDER BY title").fetchall()
    if not rows:
        raise SystemExit("No approved dialogs. Run discover, review candidates.md, then approve chat IDs.")
    client = await telegram_client()
    dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}
    try:
        for row in rows:
            dialog = dialogs.get(row["id"])
            if not dialog:
                print(f"Unavailable dialog: {row['title']} ({row['id']})", flush=True)
                continue
            state = db.execute("SELECT * FROM sync_state WHERE dialog_id=?", (row["id"],)).fetchone()
            limit = args.limit_per_chat or None
            count = 0
            newest = state["newest_id"] if state else 0
            oldest = state["oldest_id"] if state else None
            complete = bool(state["complete"]) if state else False

            if newest:
                async for message in client.iter_messages(dialog.entity, min_id=newest, reverse=True):
                    with db:
                        upsert_message(db, row["id"], telegram_record(message))
                    newest = max(newest, message.id)
                    count += 1

            if not state:
                older = [message async for message in client.iter_messages(dialog.entity, limit=limit)]
            elif not complete and oldest:
                older = [message async for message in client.iter_messages(dialog.entity, max_id=oldest, limit=limit)]
            else:
                older = []
            for message in older:
                with db:
                    upsert_message(db, row["id"], telegram_record(message))
                newest = max(newest or 0, message.id)
                oldest = min(oldest or message.id, message.id)
                count += 1
            if not complete:
                complete = args.limit_per_chat == 0 or len(older) < args.limit_per_chat
            with db:
                db.execute("""INSERT INTO sync_state(dialog_id,newest_id,oldest_id,complete,last_sync_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(dialog_id) DO UPDATE SET
                    newest_id=MAX(COALESCE(sync_state.newest_id,0),excluded.newest_id),
                    oldest_id=COALESCE(excluded.oldest_id,sync_state.oldest_id),
                    complete=excluded.complete,last_sync_at=excluded.last_sync_at""",
                    (row["id"], newest or None, oldest, int(complete), utc_now()))
            print(f"{row['title']}: {count} message(s) synchronized", flush=True)
    finally:
        await client.disconnect()
    export_analysis(db)


def image_plan(db: sqlite3.Connection) -> dict[str, int]:
    row = db.execute(f"""SELECT COUNT(*) count,
        COALESCE(SUM(CASE WHEN m.size_bytes IS NOT NULL THEN m.size_bytes ELSE 0 END),0) bytes,
        SUM(CASE WHEN m.size_bytes IS NULL THEN 1 ELSE 0 END) unknown
        FROM media m JOIN dialogs d ON d.id=m.dialog_id
        WHERE m.local_path IS NULL AND d.review_status='approved'
        AND m.kind IN ({",".join("?" * len(IMAGE_KINDS))})""", IMAGE_KINDS).fetchone()
    plan = {"count": row["count"], "bytes": row["bytes"], "unknown": row["unknown"] or 0}
    print(f"Pending images: {plan['count']}; known size: {plan['bytes']/1024/1024:.1f} MB; unknown size: {plan['unknown']}")
    return plan


MAX_FLOOD_WAIT = 900


async def after_flood_wait(call: Any, max_wait: int = MAX_FLOOD_WAIT) -> Any:
    """Await call(), sleeping through Telegram flood waits no longer than max_wait seconds."""
    while True:
        try:
            return await call()
        except FloodWaitError as exc:
            if exc.seconds > max_wait:
                raise
            print(f"Flood wait {exc.seconds}s, then retrying", flush=True)
            await asyncio.sleep(exc.seconds + 1)


async def download_rows(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    """Download the given media rows into the content-addressed store; returns the number stored.

    A failed file is reported and skipped; a flood wait longer than MAX_FLOOD_WAIT ends the run
    cleanly, and the next run continues with what is still pending.
    """
    if not rows:
        return 0
    client = await telegram_client()
    downloaded = failed = 0
    try:
        dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}
        for row in rows:
            dialog = dialogs.get(row["dialog_id"])
            if not dialog:
                continue
            with tempfile.TemporaryDirectory(prefix="telegram-media-") as directory:
                try:
                    message = await after_flood_wait(
                        lambda: client.get_messages(dialog.entity, ids=row["message_id"]))
                    if not message or media_metadata(message) is None:
                        continue
                    path_raw = await after_flood_wait(lambda: message.download_media(file=directory))
                except FloodWaitError as exc:
                    print(f"Stopped: Telegram asks to wait {exc.seconds}s. Re-run later to continue.", flush=True)
                    break
                except (OSError, asyncio.TimeoutError, RPCError) as exc:
                    failed += 1
                    print(f"Failed {row['dialog_id']}:{row['message_id']}: {type(exc).__name__}", flush=True)
                    continue
                if not path_raw:
                    continue
                source = Path(path_raw)
                digest_hash = hashlib.sha256()
                with source.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest_hash.update(chunk)
                digest = digest_hash.hexdigest()
                suffix = source.suffix.casefold() or Path(row["original_name"] or "").suffix.casefold() or ".bin"
                destination = MEDIA_ROOT / digest[:2] / f"{digest}{suffix}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.move(str(source), destination)
                relative = str(destination.relative_to(PROJECT_ROOT))
                with db:
                    db.execute("""UPDATE media SET sha256=?,local_path=?,size_bytes=?,downloaded_at=?
                        WHERE dialog_id=? AND message_id=?""",
                        (digest, relative, destination.stat().st_size, utc_now(), row["dialog_id"], row["message_id"]))
                downloaded += 1
                print(f"[{downloaded}/{len(rows)}] {row['title']} / {row['message_id']}", flush=True)
    finally:
        await client.disconnect()
    if failed:
        print(f"Failed files: {failed}; they stay pending for the next run", flush=True)
    return downloaded


async def download_images(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    plan = image_plan(db)
    if not args.confirm:
        raise SystemExit("Dry run only. Re-run with --confirm after reviewing the count and size.")
    if args.max_bytes and plan["bytes"] > args.max_bytes:
        raise SystemExit("Known pending size exceeds --max-bytes")
    rows = db.execute(f"""SELECT m.*,d.title FROM media m JOIN dialogs d ON d.id=m.dialog_id
        WHERE m.local_path IS NULL AND d.review_status='approved'
        AND m.kind IN ({",".join("?" * len(IMAGE_KINDS))})
        ORDER BY m.dialog_id,m.message_id LIMIT ?""", (*IMAGE_KINDS, args.max_items)).fetchall()
    print(f"Downloaded {await download_rows(db, rows)} image(s)", flush=True)


def parse_ref(value: str) -> tuple[int, int]:
    try:
        dialog_id, message_id = value.strip().split(":", 1)
        return int(dialog_id), int(message_id)
    except ValueError:
        raise SystemExit(f"Bad message reference '{value}'; expected dialog_id:message_id") from None


def parse_kinds(value: Optional[str]) -> tuple[str, ...]:
    kinds = tuple(dict.fromkeys(item.strip() for item in (value or "").split(",") if item.strip())) or MEDIA_KINDS
    unknown = sorted(set(kinds) - set(MEDIA_KINDS))
    if unknown:
        raise SystemExit(f"Unknown media kind(s): {', '.join(unknown)}. Known: {', '.join(MEDIA_KINDS)}")
    return kinds


def selected_media(db: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Media rows chosen by project, explicit references and their neighbours, in approved dialogs only."""
    groups: dict[tuple[int, int], set[str]] = {}
    for value in args.project or []:
        project_id = slug(value).casefold()
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise SystemExit(f"Unknown project '{value}'. Run materialize with the manifest first.")
        for row in db.execute("SELECT dialog_id,message_id FROM project_messages WHERE project_id=?", (project_id,)):
            groups.setdefault((row["dialog_id"], row["message_id"]), set()).add(project_id)
    refs = list(args.refs or [])
    if args.refs_file:
        for line in args.refs_file.read_text(encoding="utf-8").splitlines():
            refs.extend(line.split("#", 1)[0].replace(",", " ").split())
    for value in refs:
        groups.setdefault(parse_ref(value), set()).add("refs")
    if not groups:
        raise SystemExit("Choose what to download: --project ID, --refs dialog_id:message_id ... or --refs-file PATH. "
                         "Whole-chat media download is not a default.")
    if args.around:
        for (dialog_id, message_id), labels in list(groups.items()):
            for comparison, order in (("<", "DESC"), (">", "ASC")):
                for row in db.execute(f"""SELECT id FROM messages WHERE dialog_id=? AND id{comparison}?
                        ORDER BY id {order} LIMIT ?""", (dialog_id, message_id, args.around)):
                    groups.setdefault((dialog_id, row["id"]), set()).update(labels)
    kinds = parse_kinds(args.kinds)
    selected = []
    for (dialog_id, message_id), labels in sorted(groups.items()):
        row = db.execute(f"""SELECT m.*,d.title FROM media m JOIN dialogs d ON d.id=m.dialog_id
            WHERE m.dialog_id=? AND m.message_id=? AND d.review_status='approved'
            AND m.kind IN ({",".join("?" * len(kinds))})""", (dialog_id, message_id, *kinds)).fetchone()
        if row:
            selected.append({"row": row, "groups": sorted(labels)})
    return selected


def media_plan(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    selected = selected_media(db, args)
    pending = [item for item in selected if not item["row"]["local_path"]]

    def totals(items: Iterable[dict[str, Any]]) -> dict[str, int]:
        rows = [item["row"] for item in items]
        return {"count": len(rows), "bytes": sum(row["size_bytes"] or 0 for row in rows),
                "unknown": sum(row["size_bytes"] is None for row in rows)}

    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in pending:
        by_kind[item["row"]["kind"]].append(item)
        for label in item["groups"]:
            by_group[label].append(item)
    plan = {**totals(pending), "selected": len(selected), "downloaded": len(selected) - len(pending),
            "by_kind": {kind: totals(items) for kind, items in sorted(by_kind.items())},
            "by_project": {label: totals(items) for label, items in sorted(by_group.items())},
            "rows": [item["row"] for item in pending]}

    def line(label: str, item: dict[str, int]) -> str:
        return f"{label}: {item['count']}; known size: {item['bytes']/1024/1024:.1f} MB; unknown size: {item['unknown']}"

    print(line("Pending media", plan))
    print(f"Selected attachments: {plan['selected']}; already downloaded: {plan['downloaded']}")
    for title, table in (("By kind", plan["by_kind"]), ("By project", plan["by_project"])):
        if table:
            print(f"{title}:")
            for label, item in table.items():
                print("  " + line(label, item))
    return plan


async def download_media(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    plan = media_plan(db, args)
    if not args.confirm:
        raise SystemExit("Dry run only. Re-run with --confirm after reviewing the count and size.")
    if args.max_bytes and plan["bytes"] > args.max_bytes:
        raise SystemExit("Known pending size exceeds --max-bytes")
    print(f"Downloaded {await download_rows(db, plan['rows'][:args.max_items])} file(s)", flush=True)


def export_analysis(db: sqlite3.Connection) -> Path:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = RESULTS_ROOT / "approved_messages.jsonl"
    query = """SELECT m.*,d.title dialog_title,d.username FROM messages m JOIN dialogs d ON d.id=m.dialog_id
        WHERE d.review_status='approved' ORDER BY m.date,m.dialog_id,m.id"""
    with path.open("w", encoding="utf-8") as handle:
        for row in db.execute(query):
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)
    print(f"Wrote {path}", flush=True)
    return path


def contact_sheets(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
    except ImportError as exc:
        raise SystemExit("Install Pillow and pillow-heif from requirements.txt") from exc
    if args.columns < 1 or args.rows < 1:
        raise SystemExit("--columns and --rows must be positive")

    # Tags come from image_tags.py; the table exists only after its first run.
    tagged = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_tags'").fetchone()
    projects = [slug(value).casefold() for value in getattr(args, "project", None) or []]
    for project_id in projects:
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise SystemExit(f"Unknown project '{project_id}'. Run materialize with the manifest first.")
    in_projects = f"""AND EXISTS (SELECT 1 FROM project_messages pm WHERE pm.dialog_id=md.dialog_id
        AND pm.message_id=md.message_id AND pm.project_id IN ({",".join("?" * len(projects))}))""" if projects else ""
    media_rows = db.execute(f"""SELECT md.*,m.date,m.text,d.title dialog_title,
            {"(SELECT tag FROM media_tags t WHERE t.dialog_id=md.dialog_id AND t.message_id=md.message_id)" if tagged else "NULL"} tag
        FROM media md JOIN messages m ON m.dialog_id=md.dialog_id AND m.id=md.message_id
        JOIN dialogs d ON d.id=md.dialog_id
        WHERE d.review_status='approved' AND md.local_path IS NOT NULL
        AND md.kind IN ({",".join("?" * len(IMAGE_KINDS))}) {in_projects}
        ORDER BY m.date,md.dialog_id,md.message_id""", (*IMAGE_KINDS, *projects)).fetchall()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    width, image_height, label_height = 320, 240, 64
    per_page = args.columns * args.rows
    manifest: list[dict[str, Any]] = []
    font = ImageFont.load_default()
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 12)
            break
    page_number = 0
    for offset in range(0, len(media_rows), per_page):
        page_number += 1
        page_rows = media_rows[offset:offset + per_page]
        canvas = Image.new("RGB", (args.columns * width, args.rows * (image_height + label_height)), "white")
        draw = ImageDraw.Draw(canvas)
        for cell, row in enumerate(page_rows):
            source = PROJECT_ROOT / row["local_path"]
            if not source.exists():
                continue
            try:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    thumb = ImageOps.contain(image, (width, image_height))
            except (OSError, ValueError):
                continue
            x = (cell % args.columns) * width
            y = (cell // args.columns) * (image_height + label_height)
            canvas.paste(thumb, (x + (width - thumb.width) // 2, y + (image_height - thumb.height) // 2))
            ref = f"{row['dialog_id']}:{row['message_id']}"
            caption = (row["text"] or "").replace("\n", " ")[:42]
            label = f"{ref} | {row['date'][:10]} | {row['tag'] or 'no tag'}\n{row['dialog_title'][:32]}\n{caption}"
            draw.multiline_text((x + 4, y + image_height + 3), label, fill="black", font=font, spacing=2)
            manifest.append({
                "page": page_number, "cell": cell + 1, "message_ref": ref,
                "dialog_title": row["dialog_title"], "date": row["date"],
                "caption": row["text"], "local_path": row["local_path"], "tag": row["tag"],
            })
        page_path = args.output_dir / f"page_{page_number:03d}.jpg"
        canvas.save(page_path, quality=88)
        print(f"Wrote {page_path}", flush=True)
    manifest_path = args.output_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record in manifest:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Contact sheets: {page_number}; indexed images: {len(manifest)}", flush=True)


def matches_project(row: sqlite3.Row, project: dict[str, Any], explicit: set[tuple[int, int]]) -> bool:
    key = (row["dialog_id"], row["id"])
    if key in explicit:
        return True
    dialog_ids = {int(value) for value in project.get("dialog_ids", [])}
    if dialog_ids and row["dialog_id"] not in dialog_ids:
        return False
    keywords = [str(value).casefold() for value in project.get("keywords", [])]
    if keywords and not any(keyword in (row["text"] or "").casefold() for keyword in keywords):
        return False
    return bool(dialog_ids or keywords)


PROJECT_ROWS = """SELECT m.*,d.title dialog_title,md.local_path,md.original_name,md.kind stored_kind
    FROM messages m JOIN dialogs d ON d.id=m.dialog_id
    LEFT JOIN media md ON md.dialog_id=m.dialog_id AND md.message_id=m.id
    WHERE d.review_status='approved' ORDER BY m.date,m.dialog_id,m.id"""


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read manifest {path}: {exc}") from None
    projects = manifest.get("projects", []) if isinstance(manifest, dict) else []
    if not projects:
        raise SystemExit("Manifest must contain a non-empty projects array")
    for index, project in enumerate(projects, 1):
        if not isinstance(project, dict) or not str(project.get("name") or "").strip():
            raise SystemExit(f"Project #{index} in the manifest has no name")
    return projects


def project_selection(project: dict[str, Any], all_rows: list[sqlite3.Row]) -> tuple[str, str, set[tuple[int, int]], list[sqlite3.Row]]:
    project_id = slug(str(project.get("id") or project["name"])).casefold()
    explicit = {parse_ref(str(value)) for value in project.get("message_refs", [])}
    selected = [row for row in all_rows if matches_project(row, project, explicit)]
    return project_id, str(project["name"]), explicit, selected


def check_manifest(args: argparse.Namespace, db: sqlite3.Connection) -> dict[str, Any]:
    """Report what materialize would select, without writing folders or project membership."""
    projects = load_manifest(args.manifest)
    all_rows = db.execute(PROJECT_ROWS).fetchall()
    statuses = dict(db.execute("SELECT id,review_status FROM dialogs").fetchall())
    known = {(row["dialog_id"], row["id"]) for row in all_rows}
    owners: dict[tuple[int, int], set[str]] = defaultdict(set)
    report: dict[str, Any] = {"projects": [], "problems": 0}
    seen: dict[tuple[str, str], int] = {}
    for index, project in enumerate(projects, 1):
        project_id, name, explicit, selected = project_selection(project, all_rows)
        problems = []
        for label, value in (("id", project_id), ("folder", slug(name))):
            if seen.setdefault((label, value), index) != index:
                problems.append(f"{label} '{value}' is already used by project #{seen[(label, value)]}")
        if not (explicit or project.get("dialog_ids") or project.get("keywords")):
            problems.append("no message_refs, dialog_ids or keywords")
        for dialog_id in sorted({int(value) for value in project.get("dialog_ids", [])}):
            if statuses.get(dialog_id) != "approved":
                problems.append(f"dialog {dialog_id} is {statuses.get(dialog_id, 'unknown')}, not approved")
        for dialog_id, message_id in sorted(explicit - known):
            reason = "not synchronized" if statuses.get(dialog_id) == "approved" else f"dialog is {statuses.get(dialog_id, 'unknown')}"
            problems.append(f"ref {dialog_id}:{message_id}: {reason}")
        if not selected:
            problems.append("no messages selected")
        for row in selected:
            owners[(row["dialog_id"], row["id"])].add(name)
        attachments = [row for row in selected if row["stored_kind"]]
        report["projects"].append({
            "id": project_id, "name": name, "messages": len(selected),
            "first_date": selected[0]["date"][:10] if selected else None,
            "last_date": selected[-1]["date"][:10] if selected else None,
            "attachments": len(attachments),
            "downloaded": sum(bool(row["local_path"]) for row in attachments),
            "problems": problems,
        })
        report["problems"] += len(problems)
    report["shared_messages"] = sum(len(names) > 1 for names in owners.values())
    for item in report["projects"]:
        print(f"{item['name']} [{item['id']}]: {item['messages']} message(s), "
              f"{item['first_date'] or '—'} … {item['last_date'] or '—'}, "
              f"attachments {item['attachments']} (downloaded {item['downloaded']})")
        for problem in item["problems"]:
            print(f"  ! {problem}")
    print(f"Projects: {len(report['projects'])}; messages in more than one project: {report['shared_messages']}; "
          f"problems: {report['problems']}")
    if report["problems"]:
        raise SystemExit(1)
    return report


def materialize(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    projects = load_manifest(args.manifest)
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    all_rows = db.execute(PROJECT_ROWS).fetchall()
    for project in projects:
        project_id, name, _explicit, selected = project_selection(project, all_rows)
        directory = root / slug(name)
        images = directory / "images"
        media = directory / "media"
        images.mkdir(parents=True, exist_ok=True)
        media.mkdir(parents=True, exist_ok=True)
        with db:
            db.execute("""INSERT INTO projects(id,name,description,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,
                updated_at=excluded.updated_at""",
                (project_id, name, str(project.get("description", "")), utc_now()))
            db.execute("DELETE FROM project_messages WHERE project_id=?", (project_id,))
            db.executemany("INSERT INTO project_messages VALUES (?,?,?)",
                           ((project_id, row["dialog_id"], row["id"]) for row in selected))
        timeline = directory / "timeline.md"
        records = directory / "messages.jsonl"
        with timeline.open("w", encoding="utf-8") as md, records.open("w", encoding="utf-8") as out:
            md.write(f"# {name}\n\n{project.get('description', '')}\n\n")
            for row in selected:
                text = (row["text"] or "").strip().replace("\n", " ")
                kind = row["stored_kind"] or row["media_kind"]
                attachment = ""
                record = dict(row)
                record["media_file"] = None
                if row["local_path"]:
                    source = PROJECT_ROOT / row["local_path"]
                    file_name = f"{row['date'][:10]}_{row['dialog_id']}_{row['id']}_{slug(row['original_name'] or source.name)}"
                    targets = [media / file_name] + ([images / file_name] if kind in IMAGE_KINDS else [])
                    for target in targets:
                        if not target.exists():
                            try:
                                os.link(source, target)
                            except OSError:
                                shutil.copy2(source, target)
                    record["media_file"] = f"media/{file_name}"
                    attachment = f" — [{kind}]({quote(record['media_file'], safe='/')})"
                elif kind:
                    attachment = f" — {kind} (not downloaded)"
                md.write(f"- **{row['date']}** — {row['sender'] or row['sender_id']} in "
                         f"{row['dialog_title']} (`{row['dialog_id']}:{row['id']}`): {text}{attachment}\n")
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"{name}: {len(selected)} messages -> {directory}", flush=True)


def status(db: sqlite3.Connection) -> None:
    for label, query in (
        ("dialogs", "SELECT COUNT(*) FROM dialogs"),
        ("search_hits", "SELECT COUNT(*) FROM search_hits"),
        ("pending", "SELECT COUNT(*) FROM dialogs WHERE review_status='pending'"),
        ("approved", "SELECT COUNT(*) FROM dialogs WHERE review_status='approved'"),
        ("messages", "SELECT COUNT(*) FROM messages"),
        ("images", "SELECT COUNT(*) FROM media WHERE kind IN ('photo','image_document')"),
        ("downloaded_images", "SELECT COUNT(*) FROM media WHERE local_path IS NOT NULL AND kind IN ('photo','image_document')"),
        ("media", "SELECT COUNT(*) FROM media"),
        ("downloaded_media", "SELECT COUNT(*) FROM media WHERE local_path IS NOT NULL"),
        ("projects", "SELECT COUNT(*) FROM projects"),
    ):
        print(f"{label}: {db.execute(query).fetchone()[0]}")


NEGATIVE_REF = re.compile(r"^-\d+:\d+$")


def protect_negative_refs(argv: Optional[list[str]]) -> list[str]:
    """Group and channel IDs are negative; keep argparse from reading '-100123:45' as an option."""
    return [" " + value if NEGATIVE_REF.match(value) else value for value in (sys.argv[1:] if argv is None else argv)]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    discover_parser = sub.add_parser("discover", help="Rank dialogs from bounded recent previews; downloads no media")
    discover_parser.add_argument("--brief", required=True, help="Natural-language description of the work to find")
    discover_parser.add_argument("--keywords", default="", help="Additional comma- or space-separated terms")
    discover_parser.add_argument("--max-dialogs", type=int, default=300)
    discover_parser.add_argument("--preview-messages", type=int, default=20)
    discover_parser.add_argument("--min-score", type=int, default=1)
    discover_parser.add_argument("--include-broadcasts", action="store_true")

    search_parser = sub.add_parser("search", help="Search the full history by text; downloads no media")
    search_parser.add_argument("--keywords", default="", help="Comma- or newline-separated phrases, each searched separately")
    search_parser.add_argument("--keyword-file", type=Path, help="UTF-8 file, one phrase per line; # starts a comment")
    search_parser.add_argument("--since", help="YYYY-MM-DD, inclusive")
    search_parser.add_argument("--until", help="YYYY-MM-DD, inclusive")
    search_parser.add_argument("--max-hits-per-keyword", type=int, default=500, help="0 means no limit")
    search_parser.add_argument("--chat", action="append", help="Numeric dialog ID; repeatable. Default: global search")
    search_parser.add_argument("--pause", type=float, default=2.0, help="Seconds between keywords")
    search_parser.add_argument("--include-broadcasts", action="store_true")
    sub.add_parser("search-report", help="Rewrite search_hits.md from the database")

    for command, review_status in (("approve", "approved"), ("reject", "rejected")):
        review = sub.add_parser(command)
        review.add_argument("--chat", action="append", required=True, help="Numeric dialog ID or exact title; repeatable")
        review.set_defaults(review_status=review_status)

    sync_parser = sub.add_parser("sync", help="Synchronize text and attachment metadata from approved dialogs only")
    sync_parser.add_argument("--limit-per-chat", type=int, default=0, help="0 means complete history")
    sub.add_parser("plan-images", help="Show pending image count and known size without downloading")
    for command, text in (("plan-media", "Show count and size of the selected attachments without downloading"),
                          ("download-media", "Download the selected attachments of any kind from approved dialogs")):
        selection = sub.add_parser(command, help=text)
        selection.add_argument("--project", action="append", help="Project ID from materialize; repeatable")
        selection.add_argument("--refs", nargs="+", help="dialog_id:message_id references")
        selection.add_argument("--refs-file", type=Path, help="UTF-8 file with dialog_id:message_id references; # starts a comment")
        selection.add_argument("--around", type=int, default=0,
                               help="Also take attachments from up to N synchronized messages before and after each selected one")
        selection.add_argument("--kinds", help=f"Comma-separated kinds; default all: {','.join(MEDIA_KINDS)}")
        if command == "download-media":
            selection.add_argument("--confirm", action="store_true", help="Required acknowledgement after plan-media")
            selection.add_argument("--max-items", type=int, default=100000)
            selection.add_argument("--max-bytes", type=int, default=0)
    download = sub.add_parser("download-images", help="Download every image of approved dialogs (legacy; prefer download-media)")
    download.add_argument("--confirm", action="store_true", help="Required acknowledgement after plan-images")
    download.add_argument("--max-items", type=int, default=100000)
    download.add_argument("--max-bytes", type=int, default=0)
    sheets = sub.add_parser("contact-sheets", help="Build labeled image grids for visual review")
    sheets.add_argument("--output-dir", type=Path, default=RESULTS_ROOT / "contact_sheets")
    sheets.add_argument("--project", action="append", help="Only images of this project; repeatable")
    sheets.add_argument("--columns", type=int, default=4)
    sheets.add_argument("--rows", type=int, default=3)
    sub.add_parser("export-analysis", help="Write approved messages as JSONL for analysis")
    check_parser = sub.add_parser("check-manifest", help="Show what materialize would select; writes nothing, exits 1 on problems")
    check_parser.add_argument("--manifest", type=Path, required=True)
    materialize_parser = sub.add_parser("materialize", help="Create per-project folders and timelines from a reviewed manifest")
    materialize_parser.add_argument("--manifest", type=Path, required=True)
    materialize_parser.add_argument("--output-dir", type=Path, default=RESULTS_ROOT / "projects")
    sub.add_parser("status")
    return parser.parse_args(protect_negative_refs(argv))


def main() -> None:
    args = parse_args()
    db = open_db(args.db)
    try:
        if args.command == "discover":
            asyncio.run(discover(args, db))
        elif args.command == "search":
            asyncio.run(search(args, db))
        elif args.command == "search-report":
            write_search_hits(db)
        elif args.command in {"approve", "reject"}:
            set_review(db, args.chat, args.review_status)
        elif args.command == "sync":
            asyncio.run(sync_approved(args, db))
        elif args.command == "plan-images":
            image_plan(db)
        elif args.command == "download-images":
            asyncio.run(download_images(args, db))
        elif args.command == "plan-media":
            media_plan(db, args)
        elif args.command == "download-media":
            asyncio.run(download_media(args, db))
        elif args.command == "contact-sheets":
            contact_sheets(args, db)
        elif args.command == "export-analysis":
            export_analysis(db)
        elif args.command == "check-manifest":
            check_manifest(args, db)
        elif args.command == "materialize":
            materialize(args, db)
        elif args.command == "status":
            status(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
