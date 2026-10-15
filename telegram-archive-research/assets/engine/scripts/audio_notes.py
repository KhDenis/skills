"""Voice messages, audio files and video notes of approved chats: count, download, transcribe, triage.

Works from cheap to expensive: durations first (no download), then files of the chosen projects,
then Whisper tiny on the first seconds, and a larger model only where the triage score asks for it.
State lives in data/audio_notes.sqlite3 of the profile; the archive database is only read.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tg_config import PROJECT_ROOT

ARCHIVE_DB = PROJECT_ROOT / "data" / "work_projects.sqlite3"
DB_PATH = PROJECT_ROOT / "data" / "audio_notes.sqlite3"
AUDIO_ROOT = PROJECT_ROOT / "data" / "work_projects" / "audio"
SESSION_LOCK = PROJECT_ROOT / "telegram_export.lock"
AUDIO_KINDS = ("voice", "audio", "video_note")
MAX_FLOOD_WAIT = 900
PROBE_SECONDS = 30
WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

# Words that mark a message as carrying project facts. Stems, matched as prefixes.
SIGNAL_STEMS = (
    "размер", "метр", "сантиметр", "миллиметр", "высот", "ширин", "длин", "глубин", "диаметр", "квадрат",
    "монтаж", "демонтаж", "застройк", "площадк", "сцен", "подиум", "стенд", "баннер", "лайтбокс", "конструкц",
    "фанер", "мдф", "пластик", "оракал", "ткан", "печат", "покрас", "краск", "металл", "каркас", "свет",
    "заказчик", "клиент", "бриф", "макет", "рендер", "чертеж", "чертёж", "смет", "бюджет", "договор", "счет",
    "счёт", "оплат", "рубл", "тысяч", "срок", "дедлайн", "доставк", "газел", "машин", "склад", "цех",
    "согласова", "утверд", "передел", "брак", "проблем", "ошибк",
)
NUMBER_WORDS = (
    "один", "два", "три", "четыр", "пят", "шест", "сем", "восем", "девят", "десят", "двадцат", "тридцат",
    "сорок", "пятьдесят", "сто", "двест", "трист", "полтор", "половин",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            date TEXT NOT NULL,
            sender TEXT,
            size_bytes INTEGER,
            duration_s REAL,
            mime TEXT,
            project_id TEXT,
            local_path TEXT,
            downloaded_at TEXT,
            PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS transcripts (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            scope TEXT NOT NULL,            -- 'probe' = first seconds only, 'full'
            language TEXT,
            text TEXT NOT NULL,
            avg_logprob REAL,
            no_speech_prob REAL,
            compression_ratio REAL,
            seconds REAL NOT NULL,          -- wall time spent
            created_at TEXT NOT NULL,
            PRIMARY KEY(dialog_id, message_id, model, scope)
        );
        CREATE TABLE IF NOT EXISTS triage (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            score REAL NOT NULL,
            decision TEXT NOT NULL,         -- 'skip', 'tiny', 'small', 'large'
            reasons TEXT NOT NULL,          -- JSON list
            updated_at TEXT NOT NULL,
            PRIMARY KEY(dialog_id, message_id)
        );
    """)
    return db


def open_archive() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def manifest_projects(path: Optional[Path]) -> Optional[set[str]]:
    """Project ids of the current manifest; the archive still holds rows of an earlier one."""
    if not path:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    projects = data["projects"] if isinstance(data, dict) else data
    return {project["id"] for project in projects}


def load_items(db: sqlite3.Connection, manifest: Optional[set[str]]) -> int:
    """Copy audio rows of approved dialogs from the archive; keeps what is already known."""
    archive = open_archive()
    marks = ",".join("?" * len(AUDIO_KINDS))
    rows = archive.execute(f"""
        SELECT m.dialog_id, m.id, m.media_kind, m.date, m.sender, m.media_size
        FROM messages m JOIN dialogs d ON d.id=m.dialog_id
        WHERE d.review_status='approved' AND m.media_kind IN ({marks})""", AUDIO_KINDS).fetchall()
    project_of: dict[tuple[int, int], str] = {}
    for row in archive.execute("SELECT project_id, dialog_id, message_id FROM project_messages"):
        if manifest is None or row["project_id"] in manifest:
            project_of[(row["dialog_id"], row["message_id"])] = row["project_id"]
    with db:
        for row in rows:
            db.execute("""INSERT INTO items(dialog_id,message_id,kind,date,sender,size_bytes,project_id)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(dialog_id,message_id) DO UPDATE SET
                kind=excluded.kind, date=excluded.date, sender=excluded.sender,
                size_bytes=excluded.size_bytes, project_id=excluded.project_id""",
                (row["dialog_id"], row["id"], row["media_kind"], row["date"], row["sender"],
                 row["media_size"], project_of.get((row["dialog_id"], row["id"]))))
    archive.close()
    return len(rows)


class SessionLock:
    """One Telethon process per session file: a second one corrupts or blocks it."""

    def __enter__(self) -> "SessionLock":
        self.handle = SESSION_LOCK.open("w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("Another audio_notes process is using the Telegram session") from exc
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.handle.close()


async def after_flood_wait(call: Any) -> Any:
    from telethon.errors import FloodWaitError

    while True:
        try:
            return await call()
        except FloodWaitError as exc:
            if exc.seconds > MAX_FLOOD_WAIT:
                raise
            print(f"Flood wait {exc.seconds}s, then retrying", flush=True)
            await asyncio.sleep(exc.seconds + 1)


def document_duration(message: Any) -> tuple[Optional[float], Optional[str]]:
    document = getattr(getattr(message, "media", None), "document", None)
    if not document:
        return None, None
    duration = next((getattr(attr, "duration", None) for attr in document.attributes
                     if getattr(attr, "duration", None) is not None), None)
    return duration, document.mime_type


async def scan(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    from work_project_archive import telegram_client

    total = load_items(db, manifest_projects(args.manifest))
    pending: dict[int, list[int]] = {}
    for row in db.execute("SELECT dialog_id, message_id FROM items WHERE duration_s IS NULL"):
        pending.setdefault(row["dialog_id"], []).append(row["message_id"])
    print(f"Audio items: {total}; without duration: {sum(map(len, pending.values()))} in {len(pending)} chats", flush=True)
    if not pending:
        return
    with SessionLock():
        client = await telegram_client()
        try:
            dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}
            for dialog_id, ids in pending.items():
                dialog = dialogs.get(dialog_id)
                if not dialog:
                    print(f"Chat not in the dialog list: {len(ids)} items skipped", flush=True)
                    continue
                for start in range(0, len(ids), 100):
                    chunk = ids[start:start + 100]
                    messages = await after_flood_wait(lambda: client.get_messages(dialog.entity, ids=chunk))
                    with db:
                        for message in messages:
                            if not message:
                                continue
                            duration, mime = document_duration(message)
                            db.execute("UPDATE items SET duration_s=?, mime=? WHERE dialog_id=? AND message_id=?",
                                       (duration, mime, dialog_id, message.id))
                    await asyncio.sleep(args.pause)
        finally:
            await client.disconnect()
    report(args, db)


def selection(db: sqlite3.Connection, args: argparse.Namespace, extra: str = "") -> list[sqlite3.Row]:
    where, params = ["1=1"], []
    if args.project:
        where.append(f"project_id IN ({','.join('?' * len(args.project))})")
        params += args.project
    if getattr(args, "kinds", None):
        kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    if getattr(args, "min_seconds", None):
        where.append("duration_s >= ?")
        params.append(args.min_seconds)
    return db.execute(f"SELECT * FROM items WHERE {' AND '.join(where)} {extra} ORDER BY date", params).fetchall()


async def download(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    from telethon.errors import FloodWaitError, RPCError
    from work_project_archive import telegram_client

    rows = [row for row in selection(db, args) if not row["local_path"]]
    size = sum(row["size_bytes"] or 0 for row in rows)
    print(f"To download: {len(rows)} files, {size / 1024 / 1024:.1f} MB", flush=True)
    if not rows or not args.confirm:
        return
    rows = rows[:args.max_items]
    done = failed = 0
    with SessionLock():
        client = await telegram_client()
        try:
            dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}
            for row in rows:
                dialog = dialogs.get(row["dialog_id"])
                if not dialog:
                    continue
                folder = AUDIO_ROOT / str(row["dialog_id"])
                folder.mkdir(parents=True, exist_ok=True)
                try:
                    message = await after_flood_wait(
                        lambda: client.get_messages(dialog.entity, ids=row["message_id"]))
                    if not message or not message.media:
                        continue
                    path_raw = await after_flood_wait(
                        lambda: message.download_media(file=str(folder / str(row["message_id"]))))
                except FloodWaitError as exc:
                    print(f"Stopped: Telegram asks to wait {exc.seconds}s. Re-run later to continue.", flush=True)
                    break
                except (OSError, asyncio.TimeoutError, RPCError) as exc:
                    failed += 1
                    print(f"Failed {row['dialog_id']}:{row['message_id']}: {type(exc).__name__}", flush=True)
                    continue
                if not path_raw:
                    continue
                with db:
                    db.execute("UPDATE items SET local_path=?, downloaded_at=? WHERE dialog_id=? AND message_id=?",
                               (str(Path(path_raw).relative_to(PROJECT_ROOT)), utc_now(),
                                row["dialog_id"], row["message_id"]))
                done += 1
                if done % 20 == 0:
                    print(f"[{done}/{len(rows)}]", flush=True)
                await asyncio.sleep(args.pause)
        finally:
            await client.disconnect()
    print(f"Downloaded {done}, failed {failed}", flush=True)


def transcribe(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    from faster_whisper import WhisperModel

    scope = "probe" if args.probe else "full"
    rows = [row for row in selection(db, args) if row["local_path"]]
    if args.decision:
        wanted = {(row["dialog_id"], row["message_id"]) for row in db.execute(
            "SELECT dialog_id, message_id FROM triage WHERE decision=?", (args.decision,))}
        rows = [row for row in rows if (row["dialog_id"], row["message_id"]) in wanted]
    have = {(row["dialog_id"], row["message_id"]) for row in db.execute(
        "SELECT dialog_id, message_id FROM transcripts WHERE model=? AND scope=?", (args.model, scope))}
    rows = [row for row in rows if (row["dialog_id"], row["message_id"]) not in have][:args.max_items]
    print(f"To transcribe with {args.model} ({scope}): {len(rows)} files, "
          f"{sum(row['duration_s'] or 0 for row in rows) / 60:.1f} min of audio", flush=True)
    if not rows:
        return
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
    started = time.monotonic()
    for index, row in enumerate(rows, 1):
        begin = time.monotonic()
        options: dict[str, Any] = {"language": args.language, "beam_size": args.beam, "vad_filter": True,
                                   "condition_on_previous_text": False}
        if args.probe:
            options["clip_timestamps"] = [0, PROBE_SECONDS]
        try:
            segments, info = model.transcribe(str(PROJECT_ROOT / row["local_path"]), **options)
            segments = list(segments)
        except Exception as exc:  # a broken file must not stop the batch
            print(f"Failed {row['dialog_id']}:{row['message_id']}: {type(exc).__name__}", flush=True)
            continue
        weight = sum(segment.end - segment.start for segment in segments) or 1.0

        def mean(name: str) -> Optional[float]:
            if not segments:
                return None
            return sum(getattr(segment, name) * (segment.end - segment.start) for segment in segments) / weight

        with db:
            db.execute("""INSERT INTO transcripts(dialog_id,message_id,model,scope,language,text,avg_logprob,
                no_speech_prob,compression_ratio,seconds,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dialog_id,message_id,model,scope) DO UPDATE SET text=excluded.text,
                avg_logprob=excluded.avg_logprob, no_speech_prob=excluded.no_speech_prob,
                compression_ratio=excluded.compression_ratio, seconds=excluded.seconds,
                created_at=excluded.created_at""",
                (row["dialog_id"], row["message_id"], args.model, scope, info.language,
                 " ".join(segment.text.strip() for segment in segments).strip(),
                 mean("avg_logprob"), mean("no_speech_prob"), mean("compression_ratio"),
                 time.monotonic() - begin, utc_now()))
        if index % 20 == 0:
            print(f"[{index}/{len(rows)}] {time.monotonic() - started:.0f}s", flush=True)
    print(f"Done in {time.monotonic() - started:.0f}s", flush=True)


def signal_words(text: str) -> tuple[int, int]:
    """Number of project-fact words and of numbers in the text."""
    words = [word.casefold() for word in WORD.findall(text)]
    facts = sum(1 for word in words if word.startswith(SIGNAL_STEMS))
    numbers = sum(1 for word in words if word.isdigit() or word.startswith(NUMBER_WORDS))
    return facts, numbers


SENTENCE = re.compile(r"[^.!?]+[.!?]?")


def extract_summary(text: str, limit: int = 300) -> tuple[int, str]:
    """Without a model: (useful, the one or two sentences densest in project facts and numbers)."""
    facts, numbers = signal_words(text)
    if facts + numbers < 3 or facts == 0:
        return 0, ""
    scored = []
    for position, sentence in enumerate(part.strip() for part in SENTENCE.findall(text)):
        sentence_facts, sentence_numbers = signal_words(sentence)
        if sentence_facts + sentence_numbers:
            scored.append((sentence_facts * 2 + sentence_numbers, position, sentence))
    best = sorted(sorted(scored, reverse=True)[:2], key=lambda item: item[1])
    return 1, " ".join(sentence for _score, _position, sentence in best)[:limit]


def neighbours(archive: sqlite3.Connection, dialog_id: int, message_id: int, radius: int) -> list[sqlite3.Row]:
    before = archive.execute("""SELECT id, sender, text, media_kind FROM messages
        WHERE dialog_id=? AND id<? ORDER BY id DESC LIMIT ?""", (dialog_id, message_id, radius)).fetchall()
    after = archive.execute("""SELECT id, sender, text, media_kind FROM messages
        WHERE dialog_id=? AND id>? ORDER BY id LIMIT ?""", (dialog_id, message_id, radius)).fetchall()
    return list(before) + list(after)


def score_item(row: sqlite3.Row, probe: Optional[sqlite3.Row], around: Iterable[sqlite3.Row],
               dialog_kind: str) -> tuple[float, list[str]]:
    """Context score of one audio item. Positive = worth a full transcript."""
    score, reasons = 0.0, []
    duration = row["duration_s"] or 0
    if duration < 8:
        score -= 3
        reasons.append("short<8s")
    elif duration >= 60:
        score += 2
        reasons.append("long>=60s")
    elif duration >= 20:
        score += 1
        reasons.append("20-60s")
    if row["project_id"]:
        score += 1
        reasons.append("in-project")
    if dialog_kind == "user":
        score -= 0.5
        reasons.append("private-chat")
    around = list(around)
    context = " ".join(item["text"] or "" for item in around)
    facts, numbers = signal_words(context)
    if facts >= 3:
        score += 1
        reasons.append(f"context-facts:{facts}")
    if any(item["media_kind"] in ("photo", "image_document", "document") for item in around):
        score += 0.5
        reasons.append("near-files")
    if row["kind"] == "video_note":
        score -= 0.5
        reasons.append("video-note")
    if probe is not None:
        text = probe["text"]
        words = len(WORD.findall(text))
        facts, numbers = signal_words(text)
        if words < 4 or (probe["no_speech_prob"] or 0) > 0.6:
            score -= 3
            reasons.append("no-speech")
        else:
            density = (facts + numbers) / max(words, 1)
            if facts + numbers >= 3 or density >= 0.08:
                score += 2
                reasons.append(f"probe-facts:{facts}+{numbers}")
            elif facts + numbers == 0:
                score -= 1
                reasons.append("probe-no-facts")
        if (probe["compression_ratio"] or 0) > 2.4:
            score -= 1
            reasons.append("repetitive")
    return score, reasons


def triage(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    archive = open_archive()
    kinds = {row["id"]: row["kind"] for row in archive.execute("SELECT id, kind FROM dialogs")}
    counts: dict[str, int] = {}
    with db:
        for row in selection(db, args):
            probe = db.execute("""SELECT * FROM transcripts WHERE dialog_id=? AND message_id=? AND model='tiny'
                ORDER BY scope='probe' DESC LIMIT 1""", (row["dialog_id"], row["message_id"])).fetchone()
            around = neighbours(archive, row["dialog_id"], row["message_id"], args.radius)
            score, reasons = score_item(row, probe, around, kinds.get(row["dialog_id"], ""))
            if score < args.keep:
                decision = "skip"
            elif probe is not None and (probe["avg_logprob"] or 0) < args.weak_logprob:
                decision = "small"
            else:
                decision = "tiny"
            counts[decision] = counts.get(decision, 0) + 1
            db.execute("""INSERT INTO triage(dialog_id,message_id,score,decision,reasons,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(dialog_id,message_id) DO UPDATE SET score=excluded.score,
                decision=excluded.decision, reasons=excluded.reasons, updated_at=excluded.updated_at""",
                (row["dialog_id"], row["message_id"], score, decision,
                 json.dumps(reasons, ensure_ascii=False), utc_now()))
    archive.close()
    print("Triage:", ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))


def export_kb(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    """Write audio assets and transcripts into the knowledge base; touches only its own columns."""
    kb = sqlite3.connect(args.kb)
    kb.execute("PRAGMA busy_timeout = 30000")
    kb.execute("PRAGMA foreign_keys = ON")
    known = {row[0] for row in kb.execute("SELECT id FROM projects")}
    rows = [row for row in selection(db, args) if row["local_path"]]
    assets = texts = 0
    for start in range(0, len(rows), 200):
        with kb:
            for row in rows[start:start + 200]:
                asset_id = f"tg:{row['dialog_id']}:{row['message_id']}"
                kb.execute("""INSERT INTO assets(id,project_id,kind,source,path,bytes,duration_s,taken_at,author)
                    VALUES (?,?,?,'telegram',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    project_id=COALESCE(assets.project_id, excluded.project_id), path=excluded.path,
                    bytes=excluded.bytes, duration_s=excluded.duration_s, taken_at=excluded.taken_at,
                    author=excluded.author, updated_at=datetime('now')""",
                    (asset_id, row["project_id"] if row["project_id"] in known else None,
                     "video" if row["kind"] == "video_note" else "audio",
                     str(PROJECT_ROOT / row["local_path"]), row["size_bytes"], row["duration_s"],
                     row["date"], row["sender"]))
                assets += 1
                grade = db.execute("SELECT score, decision, reasons FROM triage WHERE dialog_id=? AND message_id=?",
                                   (row["dialog_id"], row["message_id"])).fetchone()
                if grade:
                    kb.execute("UPDATE assets SET triage=?, triage_score=?, triage_reason=? WHERE id=?",
                               ("skip" if grade["decision"] == "skip" else "keep", grade["score"],
                                ", ".join(json.loads(grade["reasons"])), asset_id))
                if not grade or grade["decision"] == "skip":
                    continue
                # Only models good enough to be read: tiny is for triage.
                for text in db.execute("""SELECT model, language, text, avg_logprob FROM transcripts
                        WHERE dialog_id=? AND message_id=? AND scope='full' AND text<>'' AND model<>'tiny'""",
                        (row["dialog_id"], row["message_id"])):
                    useful, summary = extract_summary(text["text"])
                    kb.execute("""INSERT INTO transcripts(asset_id,model,language,text,avg_logprob,useful,summary)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(asset_id,model) DO UPDATE SET language=excluded.language,
                        text=excluded.text, avg_logprob=excluded.avg_logprob, useful=excluded.useful,
                        summary=excluded.summary""",
                        (asset_id, f"whisper-{text['model']}", text["language"], text["text"],
                         text["avg_logprob"], useful, summary))
                    texts += 1
    kb.close()
    print(f"Knowledge base: {assets} assets, {texts} transcripts written")


def import_jsonl(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    """Load transcripts made by whisper_batch.py; paths are <dialog_id>/<message_id>.<ext>."""
    count = 0
    with db:
        for line in args.file.read_text(encoding="utf-8").splitlines():
            record = json.loads(line) if line else {}
            if not record or "error" in record:
                continue
            path = Path(record["path"])
            db.execute("""INSERT INTO transcripts(dialog_id,message_id,model,scope,language,text,avg_logprob,
                no_speech_prob,compression_ratio,seconds,created_at) VALUES (?,?,?,'full',?,?,?,?,?,?,?)
                ON CONFLICT(dialog_id,message_id,model,scope) DO UPDATE SET text=excluded.text,
                avg_logprob=excluded.avg_logprob, no_speech_prob=excluded.no_speech_prob,
                compression_ratio=excluded.compression_ratio, seconds=excluded.seconds,
                created_at=excluded.created_at""",
                (int(path.parent.name), int(path.stem), record["model"], record.get("language"), record["text"],
                 record["avg_logprob"], record["no_speech_prob"], record["compression_ratio"],
                 record["seconds"], utc_now()))
            count += 1
    print(f"Imported {count} transcripts")


def report(args: argparse.Namespace, db: sqlite3.Connection) -> None:
    print("kind         count  with_duration  hours   MB   in_projects")
    for row in db.execute("""SELECT kind, COUNT(*) n, COUNT(duration_s) known, SUM(duration_s)/3600.0 hours,
            SUM(size_bytes)/1048576.0 mb, COUNT(project_id) projects FROM items GROUP BY kind"""):
        print(f"{row['kind']:<12} {row['n']:>5}  {row['known']:>13}  {row['hours'] or 0:>5.1f}  "
              f"{row['mb'] or 0:>4.0f}  {row['projects']:>5}")
    print("\nduration bucket   count  hours")
    for row in db.execute("""SELECT CASE WHEN duration_s<8 THEN '0 <8s' WHEN duration_s<20 THEN '1 8-20s'
            WHEN duration_s<60 THEN '2 20-60s' WHEN duration_s<180 THEN '3 1-3min' WHEN duration_s<600 THEN '4 3-10min'
            ELSE '5 >10min' END bucket, COUNT(*) n, SUM(duration_s)/3600.0 hours
            FROM items WHERE duration_s IS NOT NULL GROUP BY bucket ORDER BY bucket"""):
        print(f"{row['bucket']:<16} {row['n']:>6}  {row['hours']:>5.1f}")
    if args.by_project:
        print("\nproject  count  minutes")
        for row in db.execute("""SELECT COALESCE(project_id,'(no project)') project, COUNT(*) n,
                SUM(duration_s)/60.0 minutes FROM items GROUP BY project ORDER BY minutes DESC"""):
            print(f"{row['project']:<40} {row['n']:>5}  {row['minutes'] or 0:>6.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="Read durations from Telegram; downloads nothing")
    scan_parser.add_argument("--manifest", type=Path, help="projects.json; limits project links to its ids")
    scan_parser.add_argument("--pause", type=float, default=1.0)
    scan_parser.add_argument("--by-project", action="store_true")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--by-project", action="store_true")
    for name in ("download", "transcribe", "triage"):
        command = sub.add_parser(name)
        command.add_argument("--project", action="append", help="Project id; repeatable. Default: all")
        command.add_argument("--kinds", help="Comma-separated: voice,audio,video_note")
        command.add_argument("--min-seconds", type=float)
        if name == "download":
            command.add_argument("--confirm", action="store_true", help="Without it only the plan is printed")
            command.add_argument("--max-items", type=int, default=100000)
            command.add_argument("--pause", type=float, default=0.3)
        if name == "transcribe":
            command.add_argument("--model", default="tiny")
            command.add_argument("--language", default="ru")
            command.add_argument("--probe", action="store_true", help=f"Only the first {PROBE_SECONDS} seconds")
            command.add_argument("--decision", help="Only items with this triage decision")
            command.add_argument("--beam", type=int, default=5)
            command.add_argument("--threads", type=int, default=4)
            command.add_argument("--max-items", type=int, default=100000)
        if name == "triage":
            command.add_argument("--radius", type=int, default=4, help="Neighbour messages on each side")
            command.add_argument("--keep", type=float, default=1.0, help="Minimal score to keep")
            command.add_argument("--weak-logprob", type=float, default=-0.75)
    export = sub.add_parser("export-kb", help="Write assets and transcripts into the knowledge base")
    export.add_argument("--kb", type=Path, required=True, help="kb.sqlite3 with the assets and transcripts tables")
    export.add_argument("--project", action="append")
    import_parser = sub.add_parser("import-jsonl", help="Load transcripts written by whisper_batch.py")
    import_parser.add_argument("file", type=Path)
    args = parser.parse_args()
    db = open_db()
    try:
        if args.command in ("scan", "download"):
            asyncio.run({"scan": scan, "download": download}[args.command](args, db))
        else:
            {"report": report, "transcribe": transcribe, "triage": triage, "export-kb": export_kb,
             "import-jsonl": import_jsonl}[args.command](args, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
