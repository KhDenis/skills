"""Render ready portfolio.json files as a printable HTML portfolio (trial candidate for the designer stage).

Reads only the project folders: no database, no Telegram, no network. The page uses relative
links to the project files, so the folder can be copied or printed to PDF from any browser.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


FILE_NAME = "portfolio.json"
PAGE_NAME = "portfolio.html"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
REQUIRED_TEXT = ("title", "date", "event", "summary")
DEFAULT_THEME: dict[str, str] = {
    "company": "Go Decor", "tagline": "", "contacts": "", "logo": "",
    "accent": "#8a6d3b", "ink": "#1f1f1f", "paper": "#ffffff", "muted": "#6b6b6b",
    "font": "'Helvetica Neue', Helvetica, Arial, sans-serif",
    "before_label": "Было", "after_label": "Стало", "partners_label": "Работали вместе",
}
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
)
CSS = """
@page { size: A4 landscape; margin: 12mm; }
* { box-sizing: border-box; }
body { margin: 0; font-family: %(font)s; color: %(ink)s; background: %(paper)s; line-height: 1.45; }
.page { max-width: 1120px; margin: 0 auto; padding: 32px 24px; page-break-after: always; break-after: page; }
.page:last-child { page-break-after: auto; break-after: auto; }
.cover { min-height: 70vh; display: flex; flex-direction: column; justify-content: center; }
.cover img.logo { max-height: 72px; max-width: 280px; margin-bottom: 32px; align-self: flex-start; }
h1 { font-size: 44px; margin: 0 0 8px; letter-spacing: -0.5px; }
h2 { font-size: 30px; margin: 0 0 4px; }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: 1.5px; color: %(accent)s; margin: 24px 0 8px; }
.meta { color: %(muted)s; font-size: 15px; margin: 0 0 16px; }
.summary { font-size: 17px; max-width: 760px; margin: 0; white-space: pre-line; }
.rule { width: 56px; height: 3px; background: %(accent)s; margin: 16px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
figure { margin: 0; break-inside: avoid; }
figure img { width: 100%%; height: 200px; object-fit: cover; display: block; border-radius: 2px; }
figure a.file { display: flex; height: 200px; align-items: center; justify-content: center; border: 1px solid %(muted)s;
  color: %(ink)s; text-decoration: none; font-size: 14px; padding: 12px; text-align: center; word-break: break-word; }
figcaption { font-size: 12px; color: %(muted)s; margin-top: 4px; }
.contents { list-style: none; padding: 0; margin: 24px 0 0; columns: 2; }
.contents li { margin: 0 0 6px; }
.contents a { color: %(ink)s; text-decoration: none; border-bottom: 1px solid %(accent)s; }
.partners { color: %(muted)s; font-size: 14px; margin: 0; }
@media print { .page { padding: 0; max-width: none; } figure img, figure a.file { height: 52mm; } }
"""


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def load_theme(path: Optional[Path]) -> dict[str, str]:
    theme = dict(DEFAULT_THEME)
    if path:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot read theme {path}: {exc}") from None
        unknown = sorted(set(data) - set(DEFAULT_THEME)) if isinstance(data, dict) else ["<not an object>"]
        if unknown:
            raise SystemExit(f"Unknown theme key(s): {', '.join(unknown)}. Known: {', '.join(DEFAULT_THEME)}")
        theme.update({key: str(value) for key, value in data.items()})
    for key in ("accent", "ink", "paper", "muted", "font"):
        # Theme values go into a stylesheet: keep them to plain CSS tokens.
        if any(char in theme[key] for char in "{}<>;\\"):
            raise SystemExit(f"Theme value '{key}' has characters that are not allowed in CSS values")
    return theme


def load_project(directory: Path) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return the portfolio data and the reasons it cannot be rendered as a finished entry."""
    try:
        data = json.loads((directory / FILE_NAME).read_text(encoding="utf-8"))
        generated, editorial = data["generated"], data["editorial"]
        known = {item["ref"]: item for group in ("before", "after", "progress") for item in generated[group]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, [f"unreadable {FILE_NAME}: {type(exc).__name__}"]
    problems = [f"no {field}" for field in REQUIRED_TEXT if not str(editorial.get(field) or "").strip()]
    chosen: dict[str, list[dict[str, Any]]] = {}
    for side in ("before", "after"):
        refs = editorial.get(side) or []
        chosen[side] = [known[ref] for ref in refs if ref in known and (directory / known[ref]["file"]).is_file()]
        if not refs:
            problems.append(f"no {side} images chosen")
        elif len(chosen[side]) != len(refs):
            problems.append(f"{side}: {len(refs) - len(chosen[side])} chosen file(s) unknown or missing")
    return {"generated": generated, "editorial": editorial, **chosen}, problems


def figure(item: dict[str, Any], prefix: str) -> str:
    href = quote(prefix + item["file"], safe="/")
    caption = f"<figcaption>{esc(item.get('caption'))}</figcaption>" if item.get("caption") else ""
    if Path(item["file"]).suffix.casefold() in IMAGE_EXTENSIONS:
        body = f'<a href="{href}"><img src="{href}" alt="{esc(item.get("caption") or item["tag"])}" loading="lazy"></a>'
    else:
        body = f'<a class="file" href="{href}">{esc(Path(item["file"]).name)}</a>'
    return f"<figure>{body}{caption}</figure>"


def project_section(project: dict[str, Any], theme: dict[str, str], prefix: str, anchor: str) -> str:
    editorial = project["editorial"]
    collaborators = editorial.get("collaborators") or []
    parts = [f'<section class="page project" id="{esc(anchor)}">',
             f"<h2>{esc(editorial.get('title'))}</h2>",
             f'<p class="meta">{esc(editorial.get("date"))} · {esc(editorial.get("event"))}</p>',
             '<div class="rule"></div>',
             f'<p class="summary">{esc(editorial.get("summary"))}</p>']
    if collaborators:
        parts.append(f"<h3>{esc(theme['partners_label'])}</h3>"
                     f'<p class="partners">{esc(", ".join(str(value) for value in collaborators))}</p>')
    for side, label in (("before", theme["before_label"]), ("after", theme["after_label"])):
        if project[side]:
            parts.append(f"<h3>{esc(label)}</h3><div class=\"grid\">"
                         + "".join(figure(item, prefix) for item in project[side]) + "</div>")
    parts.append("</section>")
    return "\n".join(parts)


def document(title: str, body: str, theme: dict[str, str]) -> str:
    return ("<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{esc(title)}</title>\n<style>{CSS % theme}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n")


def cover(theme: dict[str, str], entries: list[tuple[str, dict[str, Any]]]) -> str:
    logo = f'<img class="logo" src="{quote(theme["logo"], safe="/")}" alt="{esc(theme["company"])}">' if theme["logo"] else ""
    items = "".join(f'<li><a href="#{esc(anchor)}">{esc(project["editorial"].get("title"))}</a> '
                    f'<span class="meta">{esc(project["editorial"].get("date"))}</span></li>'
                    for anchor, project in entries)
    tagline = f'<p class="summary">{esc(theme["tagline"])}</p>' if theme["tagline"] else ""
    contacts = f'<p class="meta">{esc(theme["contacts"])}</p>' if theme["contacts"] else ""
    return (f'<section class="page cover">{logo}<h1>{esc(theme["company"])}</h1><div class="rule"></div>'
            f'{tagline}{contacts}<ul class="contents">{items}</ul></section>')


def find_chrome() -> Optional[str]:
    for candidate in CHROME_CANDIDATES:
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    return None


def print_pdf(page: Path, chrome: Optional[str]) -> Optional[Path]:
    chrome = chrome or find_chrome()
    if not chrome:
        return None
    target = page.with_suffix(".pdf")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={target}", page.resolve().as_uri()],
                   check=True, capture_output=True, timeout=300)
    return target if target.is_file() else None


def render(args: argparse.Namespace) -> dict[str, Any]:
    theme = load_theme(args.theme)
    root: Path = args.output_dir
    if not root.is_dir():
        raise SystemExit(f"No such directory: {root}")
    wanted = set(args.project or [])
    summary: dict[str, Any] = {"rendered": [], "skipped": {}, "pdf": None}
    entries: list[tuple[str, dict[str, Any], str]] = []
    for directory in sorted(path for path in root.iterdir() if (path / FILE_NAME).is_file()):
        project, problems = load_project(directory)
        project_id = project["generated"].get("project_id", directory.name) if project else directory.name
        if wanted and project_id not in wanted:
            continue
        if project is None or (problems and not args.include_incomplete):
            summary["skipped"][project_id] = problems
            continue
        anchor = f"p-{len(entries) + 1}"
        (directory / PAGE_NAME).write_text(
            document(str(project["editorial"].get("title") or theme["company"]),
                     project_section(project, theme, "", anchor), theme), encoding="utf-8")
        entries.append((anchor, project, directory.name))
        summary["rendered"].append(project_id)
    missing = sorted(wanted - set(summary["rendered"]) - set(summary["skipped"]))
    if missing:
        raise SystemExit(f"No {FILE_NAME} for project(s): {', '.join(missing)}")
    if entries:
        entries.sort(key=lambda entry: str(entry[1]["editorial"].get("date") or ""), reverse=True)
        body = cover(theme, [(anchor, project) for anchor, project, _ in entries]) + "\n" + "\n".join(
            project_section(project, theme, folder + "/", anchor) for anchor, project, folder in entries)
        page = root / PAGE_NAME
        page.write_text(document(theme["company"], body, theme), encoding="utf-8")
        summary["page"] = str(page)
        if args.pdf:
            pdf = print_pdf(page, args.chrome)
            summary["pdf"] = str(pdf) if pdf else "no Chrome or Chromium found; print the page to PDF from a browser"
    return summary


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory with the project folders")
    parser.add_argument("--project", action="append", help="Project ID; repeatable. Default: every ready project")
    parser.add_argument("--theme", type=Path, help="JSON with company, tagline, contacts, logo, colours, font and labels")
    parser.add_argument("--include-incomplete", action="store_true", help="Also render projects that are not ready (drafts)")
    parser.add_argument("--pdf", action="store_true", help="Also print the combined page to PDF with headless Chrome")
    parser.add_argument("--chrome", help="Path to a Chrome or Chromium binary")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        summary = render(args)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"Rendering failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
