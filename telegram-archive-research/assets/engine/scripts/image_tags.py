"""Tag project attachments as render, drawing, photo, site photo or finished object.

Cheap rules first (file type, file name, caption, camera EXIF); an agent or the owner then
corrects tags by hand after looking at contact sheets. Works on data/work_projects.sqlite3
and the downloaded files only: no Telethon, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "work_projects.sqlite3"
# photo = taken with a camera, stage not decided yet; site_photo and finished are set by review.
TAGS = ("render", "drawing", "photo", "site_photo", "finished", "other")
BEFORE_TAGS = ("render", "drawing")
AFTER_TAGS = ("finished",)
DRAWING_EXTENSIONS = {".pdf", ".dwg", ".dxf", ".skp", ".3dm", ".rvt", ".pln"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif"}
NAME_RULES = (
    ("render", re.compile(r"render|рендер|визуал|vray|v-ray|corona|3d[ _-]?view", re.IGNORECASE)),
    ("drawing", re.compile(r"чертеж|чертёж|планировк|развертк|развёртк|схема|plan|layout|drawing|scheme", re.IGNORECASE)),
    ("photo", re.compile(r"^(img|dsc|dscn|dscf|pxl|mvimg)[_-]?\d{3,}|^p\d{7}|^\d{8}_\d{6}", re.IGNORECASE)),
)
CAPTION_RULES = (
    ("render", re.compile(r"рендер|визуализац|render", re.IGNORECASE)),
    ("drawing", re.compile(r"чертеж|чертёж|планировк|развертк|развёртк|drawing", re.IGNORECASE)),
)
EXIF_MAKE, EXIF_MODEL, EXIF_IFD, EXIF_ORIGINAL = 271, 272, 0x8769, 36867


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise sqlite3.OperationalError(f"database not found: {path}")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""CREATE TABLE IF NOT EXISTS media_tags (
        dialog_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        source TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(dialog_id, message_id)
    )""")
    db.commit()
    return db


def has_camera_exif(path: Path) -> Optional[bool]:
    """True if the image carries camera data; None if it cannot be checked (no Pillow, unreadable file)."""
    try:
        from PIL import Image
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            return bool(exif.get(EXIF_MAKE) or exif.get(EXIF_MODEL) or exif.get_ifd(EXIF_IFD).get(EXIF_ORIGINAL))
    except Exception:
        return None


def rule_tag(kind: str, name: Optional[str], caption: Optional[str], path: Optional[Path]) -> Optional[tuple[str, str]]:
    """Return (tag, source) from the first rule that applies, or None to leave the file for review."""
    name = name or ""
    extension = Path(name).suffix.casefold() or (path.suffix.casefold() if path else "")
    if extension in DRAWING_EXTENSIONS:
        return "drawing", "rule:extension"
    if kind not in ("photo", "image_document") and extension not in IMAGE_EXTENSIONS:
        return None
    for tag, pattern in NAME_RULES:
        # Telegram names compressed photos itself, so only real file names are evidence.
        if kind != "photo" and pattern.search(Path(name).stem):
            return tag, "rule:name"
    if path is not None and path.is_file() and has_camera_exif(path):
        return "photo", "rule:exif"
    for tag, pattern in CAPTION_RULES:
        if pattern.search(caption or ""):
            return tag, "rule:caption"
    return None


def parse_ref(value: str) -> tuple[int, int]:
    try:
        dialog_id, message_id = value.strip().split(":", 1)
        return int(dialog_id), int(message_id)
    except ValueError:
        raise SystemExit(f"Bad message reference '{value}'; expected dialog_id:message_id") from None


def collect_refs(args: argparse.Namespace) -> list[tuple[int, int]]:
    values = list(args.refs or [])
    if args.refs_file:
        for line in args.refs_file.read_text(encoding="utf-8").splitlines():
            values.extend(line.split("#", 1)[0].replace(",", " ").split())
    if not values:
        raise SystemExit("Give --refs dialog_id:message_id ... or --refs-file PATH")
    return list(dict.fromkeys(parse_ref(value) for value in values))


def project_media(db: sqlite3.Connection, projects: Optional[Iterable[str]]) -> list[sqlite3.Row]:
    """Attachments of materialized projects, one row per project and attachment."""
    wanted = [value.casefold() for value in projects or []]
    for project_id in wanted:
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise SystemExit(f"Unknown project '{project_id}'")
    where = f"AND pm.project_id IN ({','.join('?' * len(wanted))})" if wanted else ""
    return db.execute(f"""SELECT pm.project_id,md.dialog_id,md.message_id,md.kind,md.original_name,md.local_path,
            m.text,t.tag,t.source
        FROM project_messages pm
        JOIN media md ON md.dialog_id=pm.dialog_id AND md.message_id=pm.message_id
        JOIN messages m ON m.dialog_id=md.dialog_id AND m.id=md.message_id
        LEFT JOIN media_tags t ON t.dialog_id=md.dialog_id AND t.message_id=md.message_id
        WHERE 1=1 {where} ORDER BY pm.project_id,md.dialog_id,md.message_id""", wanted).fetchall()


def auto_tag(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    result: Counter[str] = Counter()
    seen: set[tuple[int, int]] = set()
    with db:
        for row in project_media(db, args.project):
            key = (row["dialog_id"], row["message_id"])
            if key in seen:
                continue
            seen.add(key)
            if row["source"] == "manual":
                result["kept_manual"] += 1
                continue
            path = args.root / row["local_path"] if row["local_path"] else None
            found = rule_tag(row["kind"], row["original_name"], row["text"], path)
            if found is None:
                # A rule tag that no longer applies (for example after a rule change) is dropped.
                db.execute("DELETE FROM media_tags WHERE dialog_id=? AND message_id=?", key)
                result["untagged"] += 1
                continue
            db.execute("""INSERT INTO media_tags(dialog_id,message_id,tag,source,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(dialog_id,message_id) DO UPDATE SET tag=excluded.tag,source=excluded.source,
                updated_at=excluded.updated_at""", (*key, found[0], found[1], utc_now()))
            result[found[1]] += 1
            result[f"tag:{found[0]}"] += 1
    return {"attachments": len(seen), **dict(sorted(result.items()))}


def set_tags(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    refs = collect_refs(args)
    missing = [f"{d}:{m}" for d, m in refs
               if not db.execute("SELECT 1 FROM media WHERE dialog_id=? AND message_id=?", (d, m)).fetchone()]
    if missing:
        raise SystemExit(f"No attachment for: {', '.join(missing)}")
    with db:
        for key in refs:
            db.execute("""INSERT INTO media_tags(dialog_id,message_id,tag,source,note,updated_at)
                VALUES (?,?,?,'manual',?,?) ON CONFLICT(dialog_id,message_id) DO UPDATE SET tag=excluded.tag,
                source='manual',note=excluded.note,updated_at=excluded.updated_at""",
                (*key, args.tag, args.note or "", utc_now()))
    return {"tagged": len(refs), "tag": args.tag}


def clear_tags(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    refs = collect_refs(args)
    with db:
        removed = sum(db.execute("DELETE FROM media_tags WHERE dialog_id=? AND message_id=?", key).rowcount
                      for key in refs)
    return {"cleared": removed}


def report(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    projects: dict[str, dict[str, Any]] = {}
    for row in project_media(db, args.project):
        item = projects.setdefault(row["project_id"], {
            "attachments": 0, "downloaded": 0, "by_tag": Counter(), "by_source": Counter(),
            "untagged": 0, "untagged_refs": [], "before": 0, "after": 0})
        item["attachments"] += 1
        item["downloaded"] += bool(row["local_path"])
        if row["tag"]:
            item["by_tag"][row["tag"]] += 1
            item["by_source"][row["source"]] += 1
            item["before"] += row["tag"] in BEFORE_TAGS
            item["after"] += row["tag"] in AFTER_TAGS
        else:
            item["untagged"] += 1
            item["untagged_refs"].append(f"{row['dialog_id']}:{row['message_id']}")
    for item in projects.values():
        item["by_tag"] = dict(sorted(item["by_tag"].items()))
        item["by_source"] = dict(sorted(item["by_source"].items()))
        if not args.list_untagged:
            del item["untagged_refs"]
    return {"generated_at": utc_now(), "tags": list(TAGS), "projects": projects}


NEGATIVE_REF = re.compile(r"^-\d+:\d+$")


def protect_negative_refs(argv: Optional[list[str]]) -> list[str]:
    """Group and channel IDs are negative; keep argparse from reading '-100123:45' as an option."""
    return [" " + value if NEGATIVE_REF.match(value) else value for value in (sys.argv[1:] if argv is None else argv)]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Engine directory that media paths are relative to")
    sub = parser.add_subparsers(dest="command", required=True)
    auto = sub.add_parser("auto", help="Apply the cheap rules to project attachments; manual tags are kept")
    auto.add_argument("--project", action="append", help="Project ID; repeatable. Default: all projects")
    for command, text in (("set", "Set a tag by hand"), ("clear", "Remove tags")):
        manual = sub.add_parser(command, help=text)
        manual.add_argument("--refs", nargs="+", help="dialog_id:message_id references")
        manual.add_argument("--refs-file", type=Path, help="UTF-8 file with references; # starts a comment")
        if command == "set":
            manual.add_argument("--tag", required=True, choices=TAGS)
            manual.add_argument("--note", help="Why, in a few words; no customer data")
    status = sub.add_parser("report", help="Print tag counts per project as JSON, without names or text")
    status.add_argument("--project", action="append")
    status.add_argument("--list-untagged", action="store_true", help="Include references of attachments still without a tag")
    return parser.parse_args(protect_negative_refs(argv))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        db = open_db(args.db)
        try:
            command = {"auto": auto_tag, "set": set_tags, "clear": clear_tags, "report": report}[args.command]
            print(json.dumps(command(db, args), ensure_ascii=False, indent=2))
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(f"Cannot use archive database {args.db}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
