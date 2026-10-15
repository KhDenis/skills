"""The stages after synchronization work together on one archive: no Telegram, no network."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import chronology
import image_tags
import portfolio
import portfolio_html
import work_project_archive as app

NAME = "Дом у озера (2024)"
# message id, day, text, kind, file name, stored file
MESSAGES = [
    (1, 1, "Начинаем дом у озера", None, None, None),
    (2, 2, "Рендер гостиной", "photo", "photo_2.jpg", "aa/render.jpg"),
    (3, 3, "", "document", "План 1 этажа.pdf", "bb/plan.pdf"),
    (4, 9, "Сдали объект", "image_document", "IMG_20240309_0001.jpg", "cc/final.jpg"),
    (5, 9, "видео с объекта", "video", "clip.mp4", None),
]


def quiet(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        result = function(*args)
    return result, output.getvalue()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.original = app.PROJECT_ROOT
        app.PROJECT_ROOT = self.root
        self.addCleanup(setattr, app, "PROJECT_ROOT", self.original)
        self.db_path = self.root / "data" / "work_projects.sqlite3"
        db = app.open_db(self.db_path)
        with db:
            db.execute("""INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at)
                VALUES (-100,'Озеро | рабочий','group','approved',?)""", (app.utc_now(),))
            for message_id, day, text, kind, name, stored in MESSAGES:
                db.execute("""INSERT INTO messages(dialog_id,id,date,sender,text,media_kind,media_name)
                    VALUES (-100,?,?,?,?,?,?)""",
                    (message_id, f"2024-03-{day:02d}T10:00:00+00:00", "Анна", text, kind, name))
                if kind:
                    local = f"data/work_projects/media/{stored}" if stored else None
                    db.execute("INSERT INTO media(dialog_id,message_id,kind,original_name,local_path) VALUES (-100,?,?,?,?)",
                               (message_id, kind, name, local))
                    if local:
                        (self.root / local).parent.mkdir(parents=True, exist_ok=True)
                        (self.root / local).write_bytes(f"file-{message_id}".encode())
        self.db = db
        self.addCleanup(db.close)
        self.manifest = self.root / "projects.json"
        self.manifest.write_text(json.dumps({"projects": [
            {"id": "lake-house", "name": NAME, "description": "Дом", "dialog_ids": [-100]}]}), encoding="utf-8")
        self.out = self.root / "Drive" / "Projects"

    def test_manifest_to_printable_portfolio(self):
        quiet(app.check_manifest, SimpleNamespace(manifest=self.manifest), self.db)
        quiet(app.materialize, SimpleNamespace(manifest=self.manifest, output_dir=self.out), self.db)
        folder = self.out / app.slug(NAME)
        self.assertEqual(app.slug(NAME), chronology.slug(NAME))
        self.assertEqual(sorted(path.name for path in (folder / "media").iterdir()), [
            "2024-03-02_-100_2_photo_2.jpg", "2024-03-03_-100_3_План_1_этажа.pdf",
            "2024-03-09_-100_4_IMG_20240309_0001.jpg"])

        common = ["--db", str(self.db_path)]
        self.assertEqual(quiet(image_tags.main, [*common, "--root", str(self.root), "auto"])[0], 0)
        self.assertEqual(quiet(image_tags.main, [*common, "set", "--tag", "finished", "--refs", "-100:4"])[0], 0)
        tags = dict(self.db.execute("SELECT message_id,tag FROM media_tags").fetchall())
        self.assertEqual(tags, {2: "render", 3: "drawing", 4: "finished"})

        self.assertEqual(quiet(chronology.main, ["timeline", *common, "--output-dir", str(self.out)])[0], 0)
        records = [json.loads(line) for line in (self.out / "chronology.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 5)
        linked = {record["ref"]: record["media"]["file"] for record in records if record["media"] and record["media"]["file"]}
        self.assertEqual(sorted(linked), ["-100:2", "-100:3", "-100:4"])
        for file in linked.values():
            self.assertTrue((self.out / file).is_file(), file)
        code, output = quiet(chronology.main, ["report", *common])
        self.assertEqual((code, json.loads(output)["projects"]), (0, 1))

        base = [*common, "--output-dir", str(self.out)]
        code, output = quiet(portfolio.main, [*base, "build"])
        self.assertEqual(json.loads(output)["projects"]["lake-house"], {"before": 2, "after": 1, "progress": 0, "untagged": 1})
        path = folder / "portfolio.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["editorial"].update(date="2024-03", event="Сдача объекта", summary="Дом у озера сдан.",
                                 before=["-100:2", "-100:3"], after=["-100:4"])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, output = quiet(portfolio.main, [*base, "check"])
        self.assertEqual((code, json.loads(output)["ready"]), (0, 1))

        code, output = quiet(portfolio_html.main, ["--output-dir", str(self.out)])
        self.assertEqual(json.loads(output)["rendered"], ["lake-house"])
        page = (self.out / "portfolio.html").read_text(encoding="utf-8")
        self.assertIn("Дом у озера (2024)", page)
        self.assertEqual(page.count("<img "), 2)
        self.assertIn('class="file"', page)

        # A second pass over every stage changes nothing that was written by hand.
        quiet(app.materialize, SimpleNamespace(manifest=self.manifest, output_dir=self.out), self.db)
        quiet(image_tags.main, [*common, "--root", str(self.root), "auto"])
        quiet(portfolio.main, [*base, "build"])
        again = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(again["editorial"], data["editorial"])
        self.assertEqual(dict(self.db.execute("SELECT message_id,tag FROM media_tags").fetchall()), tags)
        self.assertEqual(len(list((folder / "media").iterdir())), 3)

    def test_negative_dialog_ids_are_accepted_on_the_command_line(self):
        args = app.parse_args(["download-media", "--refs", "-1001234567890:45", "12:3", "--around", "2", "--confirm"])
        self.assertEqual([app.parse_ref(value) for value in args.refs], [(-1001234567890, 45), (12, 3)])
        self.assertEqual((args.around, args.confirm), (2, True))
        args = app.parse_args(["approve", "--chat", "-1001234567890", "--chat", "5"])
        self.assertEqual(args.chat, ["-1001234567890", "5"])
        args = image_tags.parse_args(["set", "--tag", "render", "--refs", "-100:4", "-100:5", "--note", "x"])
        self.assertEqual([image_tags.parse_ref(value) for value in args.refs], [(-100, 4), (-100, 5)])


if __name__ == "__main__":
    unittest.main()
