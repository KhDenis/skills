"""Combined chronology across materialized projects and a machine-readable archive report.

Reads data/work_projects.sqlite3 only: no Telethon, no network, no writes to the database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "work_projects.sqlite3"
RESULTS_ROOT = PROJECT_ROOT / "output" / "work_projects"
# materialize names project files "<date>_<dialog_id>_<message_id>_<name>".
PROJECT_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}_(-?\d+)_(\d+)_")


def slug(value: str) -> str:
    # Same rule as work_project_archive.slug, which names the project folders;
    # copied because importing that module requires Telethon.
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return value[:100] or "project"


def open_db_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise sqlite3.OperationalError(f"database not found: {path}")
    db = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    return db


def link(path: str) -> str:
    return quote(path, safe="/")


def one_line(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def project_files(directory: Path) -> dict[tuple[int, int], str]:
    """Map (dialog_id, message_id) to a file inside the project folder, relative to it."""
    found: dict[tuple[int, int], str] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.rglob("*")):
        match = PROJECT_FILE.match(path.name)
        if match and path.is_file():
            found.setdefault((int(match.group(1)), int(match.group(2))), path.relative_to(directory).as_posix())
    return found


def timeline_records(db: sqlite3.Connection, output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projects = []
    files: dict[str, dict[tuple[int, int], str]] = {}
    for row in db.execute("SELECT id,name,description FROM projects ORDER BY name,id"):
        folder = slug(row["name"])
        files[row["id"]] = project_files(output_dir / folder)
        projects.append({"id": row["id"], "name": row["name"], "description": row["description"],
                         "folder": folder, "first_date": None, "last_date": None,
                         "messages": 0, "media": 0, "media_downloaded": 0})
    by_id = {project["id"]: project for project in projects}
    records = []
    rows = db.execute("""SELECT pm.project_id,m.dialog_id,m.id message_id,m.date,m.sender,m.sender_id,m.text,
            m.media_kind message_media_kind,m.media_name,d.title dialog_title,
            md.kind media_kind,md.original_name,md.local_path
        FROM project_messages pm
        JOIN projects p ON p.id=pm.project_id
        JOIN messages m ON m.dialog_id=pm.dialog_id AND m.id=pm.message_id
        LEFT JOIN dialogs d ON d.id=m.dialog_id
        LEFT JOIN media md ON md.dialog_id=m.dialog_id AND md.message_id=m.id
        ORDER BY m.date,m.dialog_id,m.id,pm.project_id""")
    for row in rows:
        project = by_id[row["project_id"]]
        kind = row["media_kind"] or row["message_media_kind"]
        media = None
        if kind:
            downloaded = bool(row["local_path"])
            inside = files[project["id"]].get((row["dialog_id"], row["message_id"])) if downloaded else None
            media = {"kind": kind, "name": row["original_name"] or row["media_name"], "downloaded": downloaded,
                     "file": f"{project['folder']}/{inside}" if inside else None}
            project["media"] += 1
            project["media_downloaded"] += int(downloaded)
        project["messages"] += 1
        project["first_date"] = project["first_date"] or row["date"]
        project["last_date"] = row["date"]
        records.append({
            "date": row["date"], "project_id": project["id"], "project": project["name"],
            "project_dir": project["folder"] + "/", "dialog_id": row["dialog_id"],
            "dialog": row["dialog_title"], "message_id": row["message_id"],
            "ref": f"{row['dialog_id']}:{row['message_id']}",
            "sender": row["sender"] or (str(row["sender_id"]) if row["sender_id"] is not None else None),
            "text": row["text"] or "", "media": media,
        })
    return projects, records


def cell(value: Any) -> str:
    return one_line(str(value)).replace("|", "\\|")


def write_timeline(db: sqlite3.Connection, output_dir: Path) -> tuple[Path, Path, int, int]:
    projects, records = timeline_records(db, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown = output_dir / "chronology.md"
    jsonl = output_dir / "chronology.jsonl"
    with jsonl.open("w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    with markdown.open("w", encoding="utf-8") as md:
        md.write("# Хронология проектов\n\n")
        md.write(f"Проектов: {len(projects)}, сообщений: {len(records)}.\n\n")
        md.write("| Проект | Первая дата | Последняя дата | Сообщений | Медиа | Скачано | Папка |\n")
        md.write("| --- | --- | --- | ---: | ---: | ---: | --- |\n")
        for project in projects:
            first = (project["first_date"] or "—")[:10]
            last = (project["last_date"] or "—")[:10]
            md.write(f"| {cell(project['name'])} | {first} | {last} | {project['messages']} | {project['media']} | "
                     f"{project['media_downloaded']} | [{cell(project['folder'])}/]({link(project['folder'])}/) |\n")
        day = None
        for record in records:
            if record["date"][:10] != day:
                day = record["date"][:10]
                md.write(f"\n## {day}\n\n")
            line = (f"- **{record['date'][11:16] or record['date']}** — "
                    f"[{one_line(record['project'])}]({link(record['project_dir'])}) · "
                    f"{one_line(record['dialog']) or record['dialog_id']} · {one_line(record['sender']) or '—'} "
                    f"(`{record['ref']}`): {one_line(record['text'])}")
            media = record["media"]
            if media:
                label = one_line(f"{media['kind']}: {media['name']}" if media["name"] else media["kind"])
                if media["file"]:
                    line += f" — [{label}]({link(media['file'])})"
                else:
                    line += f" — {label} ({'файла нет в папке проекта' if media['downloaded'] else 'не скачано'})"
            md.write(line.rstrip() + "\n")
    return markdown, jsonl, len(projects), len(records)


def report(db: sqlite3.Connection, path: Path) -> dict[str, Any]:
    def count(query: str) -> int:
        return db.execute(query).fetchone()[0]

    by_kind: dict[str, dict[str, int]] = {}
    for row in db.execute("""SELECT kind,COUNT(*) total,SUM(local_path IS NOT NULL) downloaded
            FROM media GROUP BY kind ORDER BY kind"""):
        by_kind[row["kind"]] = {"total": row["total"], "downloaded": row["downloaded"] or 0}
    incomplete = [
        {"dialog_id": row["id"], "review_status": row["review_status"], "last_sync_at": row["last_sync_at"]}
        for row in db.execute("""SELECT d.id,d.review_status,s.last_sync_at FROM dialogs d
            LEFT JOIN sync_state s ON s.dialog_id=d.id
            WHERE COALESCE(s.complete,0)=0 AND (s.dialog_id IS NOT NULL OR d.review_status='approved')
            ORDER BY d.id""")
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(path),
        "dialogs": {
            "total": count("SELECT COUNT(*) FROM dialogs"),
            "by_status": {row["review_status"]: row["total"] for row in db.execute(
                "SELECT review_status,COUNT(*) total FROM dialogs GROUP BY review_status ORDER BY review_status")},
        },
        "messages": count("SELECT COUNT(*) FROM messages"),
        "media": {
            "total": sum(item["total"] for item in by_kind.values()),
            "downloaded": sum(item["downloaded"] for item in by_kind.values()),
            "by_kind": by_kind,
        },
        "projects": count("SELECT COUNT(*) FROM projects"),
        "project_messages": count("SELECT COUNT(*) FROM project_messages"),
        "last_sync_at": db.execute("SELECT MAX(last_sync_at) FROM sync_state").fetchone()[0],
        "incomplete_dialogs": incomplete,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    timeline = sub.add_parser("timeline", help="Write chronology.md and chronology.jsonl next to the project folders")
    timeline.add_argument("--db", type=Path, default=DB_PATH)
    timeline.add_argument("--output-dir", type=Path, default=RESULTS_ROOT / "projects")
    status = sub.add_parser("report", help="Print archive state as JSON without message text")
    status.add_argument("--db", type=Path, default=DB_PATH)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        db = open_db_readonly(args.db)
        try:
            if args.command == "timeline":
                markdown, jsonl, projects, messages = write_timeline(db, args.output_dir)
                print(f"{projects} projects, {messages} messages -> {markdown}, {jsonl}")
            else:
                print(json.dumps(report(db, args.db), ensure_ascii=False, indent=2))
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(f"Cannot read archive database {args.db}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
