"""Portfolio skeleton per project: dates, partners and before/after candidates from the tagged archive.

`build` writes portfolio.json into every materialized project folder. The "generated" part is
rewritten on each run; the "editorial" part (title, date, event, text, chosen images) is written
by an agent or the owner and is kept. Reads data/work_projects.sqlite3 only: no Telethon, no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from chronology import DB_PATH, RESULTS_ROOT, open_db_readonly, project_files, slug


SCHEMA_VERSION = 1
FILE_NAME = "portfolio.json"
GROUPS = {"render": "before", "drawing": "before", "finished": "after", "site_photo": "progress", "photo": "progress"}
CAPTION_LIMIT = 300
EDITORIAL_DEFAULTS: dict[str, Any] = {
    "title": "", "date": "", "event": "", "summary": "", "collaborators": [], "before": [], "after": [],
}
REQUIRED_TEXT = ("title", "date", "event", "summary")


def generated_part(db: sqlite3.Connection, project: sqlite3.Row, directory: Path, tagged: bool) -> dict[str, Any]:
    files = project_files(directory)
    rows = db.execute(f"""SELECT m.dialog_id,m.id message_id,m.date,m.sender,m.sender_id,m.text,d.title dialog_title,
            md.kind,md.local_path,
            {"(SELECT tag FROM media_tags t WHERE t.dialog_id=m.dialog_id AND t.message_id=m.id)" if tagged else "NULL"} tag
        FROM project_messages pm
        JOIN messages m ON m.dialog_id=pm.dialog_id AND m.id=pm.message_id
        LEFT JOIN dialogs d ON d.id=m.dialog_id
        LEFT JOIN media md ON md.dialog_id=m.dialog_id AND md.message_id=m.id
        WHERE pm.project_id=? ORDER BY m.date,m.dialog_id,m.id""", (project["id"],)).fetchall()
    partners: Counter[str] = Counter()
    chats: dict[int, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = {"before": [], "after": [], "progress": []}
    counts = Counter()
    for row in rows:
        sender = row["sender"] or (str(row["sender_id"]) if row["sender_id"] is not None else None)
        if sender:
            partners[sender] += 1
        chat = chats.setdefault(row["dialog_id"], {"dialog_id": row["dialog_id"], "title": row["dialog_title"], "messages": 0})
        chat["messages"] += 1
        if not row["kind"]:
            continue
        counts["attachments"] += 1
        inside = files.get((row["dialog_id"], row["message_id"])) if row["local_path"] else None
        if not row["tag"]:
            counts["untagged"] += 1
            continue
        group = GROUPS.get(row["tag"])
        if not group:
            counts["other"] += 1
            continue
        if not inside:
            counts["without_file"] += 1
            continue
        groups[group].append({
            "ref": f"{row['dialog_id']}:{row['message_id']}", "date": row["date"], "tag": row["tag"],
            "kind": row["kind"], "file": inside, "caption": " ".join((row["text"] or "").split())[:CAPTION_LIMIT],
        })
    return {
        "project_id": project["id"], "name": project["name"], "description": project["description"],
        "first_date": rows[0]["date"][:10] if rows else None, "last_date": rows[-1]["date"][:10] if rows else None,
        "messages": len(rows), "attachments": counts["attachments"], "untagged": counts["untagged"],
        "without_file": counts["without_file"],
        "partners": [{"name": name, "messages": count} for name, count in partners.most_common()],
        "chats": sorted(chats.values(), key=lambda chat: -chat["messages"]),
        **groups,
    }


def read_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix or remove it, it may hold manual work") from None
    if not isinstance(data, dict) or not isinstance(data.get("editorial", {}), dict):
        raise SystemExit(f"{path} has an unexpected structure; fix or remove it, it may hold manual work")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def selected_projects(db: sqlite3.Connection, wanted: Optional[list[str]]) -> list[sqlite3.Row]:
    rows = db.execute("SELECT id,name,description FROM projects ORDER BY name,id").fetchall()
    if not wanted:
        return rows
    ids = [slug(value).casefold() for value in wanted]
    missing = sorted(set(ids) - {row["id"] for row in rows})
    if missing:
        raise SystemExit(f"Unknown project(s): {', '.join(missing)}")
    return [row for row in rows if row["id"] in ids]


def build(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    tagged = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_tags'").fetchone())
    summary = {"written": 0, "skipped_no_folder": [], "projects": {}}
    for project in selected_projects(db, args.project):
        directory = args.output_dir / slug(project["name"])
        if not directory.is_dir():
            summary["skipped_no_folder"].append(project["id"])
            continue
        path = directory / FILE_NAME
        existing = read_existing(path)
        editorial = {**EDITORIAL_DEFAULTS, **existing.get("editorial", {})}
        if not editorial["title"]:
            editorial["title"] = project["name"]
        generated = generated_part(db, project, directory, tagged)
        write_json(path, {"schema": SCHEMA_VERSION, "generated": generated, "editorial": editorial})
        summary["written"] += 1
        summary["projects"][project["id"]] = {key: len(generated[key]) for key in ("before", "after", "progress")}
        summary["projects"][project["id"]]["untagged"] = generated["untagged"]
    return summary


def check_one(directory: Path) -> dict[str, Any]:
    path = directory / FILE_NAME
    result: dict[str, Any] = {"errors": [], "missing": [], "ready": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        generated, editorial = data["generated"], data["editorial"]
        candidates = {group: {item["ref"]: item for item in generated[group]} for group in ("before", "after", "progress")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["errors"].append(f"unreadable {FILE_NAME}: {type(exc).__name__}")
        return result
    for field in REQUIRED_TEXT:
        if not str(editorial.get(field) or "").strip():
            result["missing"].append(field)
    known = {ref: item for group in candidates.values() for ref, item in group.items()}
    for side in ("before", "after"):
        chosen = editorial.get(side) or []
        if not isinstance(chosen, list):
            result["errors"].append(f"{side} must be a list of references")
            continue
        if not chosen:
            result["missing"].append(side)
        for ref in chosen:
            if ref not in known:
                result["errors"].append(f"{side} {ref}: not among the tagged files of this project")
            elif not (directory / known[ref]["file"]).is_file():
                result["errors"].append(f"{side} {ref}: file is missing")
    result["ready"] = not result["errors"] and not result["missing"]
    return result


def check(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"projects": {}, "ready": 0, "errors": 0}
    for project in selected_projects(db, args.project):
        directory = args.output_dir / slug(project["name"])
        if not (directory / FILE_NAME).is_file():
            report["projects"][project["id"]] = {"errors": [], "missing": [FILE_NAME], "ready": False}
            continue
        result = check_one(directory)
        report["projects"][project["id"]] = result
        report["ready"] += result["ready"]
        report["errors"] += len(result["errors"])
    return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_ROOT / "projects",
                        help="Directory with the materialized project folders")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, text in (("build", f"Write {FILE_NAME} into every project folder, keeping the editorial part"),
                          ("check", "Report which projects are ready for the designer stage; exits 1 on errors")):
        item = sub.add_parser(command, help=text)
        item.add_argument("--project", action="append", help="Project ID; repeatable. Default: all projects")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        db = open_db_readonly(args.db)
        try:
            result = (build if args.command == "build" else check)(db, args)
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(f"Cannot read archive database {args.db}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.command == "check" and result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
