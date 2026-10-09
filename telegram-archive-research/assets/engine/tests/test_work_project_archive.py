import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import work_project_archive as app


class WorkProjectArchiveTests(unittest.TestCase):
    def test_score_dialog_uses_title_messages_and_images(self):
        keywords = app.parse_keywords("Go decor renders", "интерьер")
        score, reasons = app.score_dialog(
            "Go Decor Projects",
            ["Дизайнер прислал новый рендер интерьера"],
            keywords,
            3,
        )
        self.assertGreaterEqual(score, 15)
        self.assertIn("title:decor", reasons)
        self.assertIn("recent-images:3", reasons)

    def test_image_plan_counts_only_approved_dialogs(self):
        with tempfile.TemporaryDirectory() as directory:
            db = app.open_db(Path(directory) / "archive.sqlite3")
            with db:
                for dialog_id, status in ((1, "approved"), (2, "rejected")):
                    db.execute("""INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at)
                        VALUES (?,?,?,?,?)""", (dialog_id, str(dialog_id), "user", status, app.utc_now()))
                    db.execute("INSERT INTO media(dialog_id,message_id,kind,size_bytes) VALUES (?,?,?,?)",
                               (dialog_id, 10, "photo", 1024))
            plan = app.image_plan(db)
            self.assertEqual(plan, {"count": 1, "bytes": 1024, "unknown": 0})
            db.close()

    def test_materialize_creates_timeline_and_linked_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = app.PROJECT_ROOT
            app.PROJECT_ROOT = root
            try:
                db = app.open_db(root / "archive.sqlite3")
                source = root / "data" / "media" / "source.jpg"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"image")
                with db:
                    db.execute("""INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at)
                        VALUES (1,'Design team','group','approved',?)""", (app.utc_now(),))
                    db.execute("""INSERT INTO messages(dialog_id,id,date,sender,text,media_kind,media_name)
                        VALUES (1,7,'2026-01-02T10:00:00+00:00','Designer','Northern House render','photo','render.jpg')""")
                    db.execute("""INSERT INTO media(dialog_id,message_id,kind,original_name,local_path)
                        VALUES (1,7,'photo','render.jpg','data/media/source.jpg')""")
                manifest = root / "projects.json"
                manifest.write_text(json.dumps({"projects": [{
                    "id": "northern-house", "name": "Northern House",
                    "dialog_ids": [1], "keywords": ["render"]
                }]}), encoding="utf-8")
                output = root / "results"
                app.materialize(SimpleNamespace(manifest=manifest, output_dir=output), db)
                project = output / "Northern_House"
                self.assertIn("Northern House render", (project / "timeline.md").read_text())
                self.assertEqual(len(list((project / "images").iterdir())), 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_messages").fetchone()[0], 1)
                db.close()
            finally:
                app.PROJECT_ROOT = original_root

    def test_contact_sheet_preserves_message_reference(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = app.PROJECT_ROOT
            app.PROJECT_ROOT = root
            try:
                db = app.open_db(root / "archive.sqlite3")
                source = root / "data" / "media" / "render.jpg"
                source.parent.mkdir(parents=True)
                Image.new("RGB", (80, 60), "blue").save(source)
                with db:
                    db.execute("""INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at)
                        VALUES (1,'Дизайнеры','group','approved',?)""", (app.utc_now(),))
                    db.execute("""INSERT INTO messages(dialog_id,id,date,sender,text,media_kind,media_name)
                        VALUES (1,9,'2026-01-03T10:00:00+00:00','Designer','Рендер гостиной','photo','render.jpg')""")
                    db.execute("""INSERT INTO media(dialog_id,message_id,kind,original_name,local_path)
                        VALUES (1,9,'photo','render.jpg','data/media/render.jpg')""")
                output = root / "sheets"
                app.contact_sheets(SimpleNamespace(output_dir=output, columns=2, rows=2), db)
                self.assertTrue((output / "page_001.jpg").exists())
                record = json.loads((output / "manifest.jsonl").read_text())
                self.assertEqual(record["message_ref"], "1:9")
                db.close()
            finally:
                app.PROJECT_ROOT = original_root


if __name__ == "__main__":
    unittest.main()
