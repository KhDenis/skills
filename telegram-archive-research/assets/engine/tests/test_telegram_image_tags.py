import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1]
          / 'scripts' / 'image_tags.py')
spec = importlib.util.spec_from_file_location('image_tags', SCRIPT)
image_tags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_tags)

# The tables of work_project_archive.open_db that the script reads, written by hand (no Telethon in CI).
SCHEMA = '''
CREATE TABLE messages (dialog_id INTEGER NOT NULL, id INTEGER NOT NULL, date TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', PRIMARY KEY(dialog_id, id));
CREATE TABLE media (dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL,
    original_name TEXT, size_bytes INTEGER, sha256 TEXT, local_path TEXT, downloaded_at TEXT,
    PRIMARY KEY(dialog_id, message_id));
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE project_messages (project_id TEXT NOT NULL, dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, dialog_id, message_id));
'''
# message id, kind, file name, caption
MEDIA = [
    (1, 'document', 'План этажа.pdf', ''),
    (2, 'document', 'kitchen.dwg', ''),
    (3, 'image_document', 'Render_view_03.png', ''),
    (4, 'image_document', 'IMG_20240305_1234.jpg', ''),
    (5, 'photo', 'photo_5.jpg', 'Рендер гостиной СЕКРЕТ'),
    (6, 'photo', 'photo_6.jpg', 'смотрите'),
    (7, 'image_document', 'планировка.jpg', ''),
    (8, 'video', 'clip.mp4', 'рендер в движении'),
    (9, 'document', 'смета.xlsx', 'чертеж'),
]
HAS_PIL = importlib.util.find_spec('PIL') is not None


def run(db, *argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = image_tags.main(['--db', str(db), '--root', str(Path(db).parent), *[str(a) for a in argv]])
    return code, stdout.getvalue(), stderr.getvalue()


class ImageTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'work_projects.sqlite3'
        db = sqlite3.connect(str(self.db))
        db.executescript(SCHEMA)
        for message_id, kind, name, caption in MEDIA:
            db.execute('INSERT INTO messages VALUES (1,?,?,?)', (message_id, '2024-03-01T10:00:00+00:00', caption))
            db.execute('INSERT INTO media(dialog_id,message_id,kind,original_name) VALUES (1,?,?,?)', (message_id, kind, name))
            db.execute("INSERT INTO project_messages VALUES ('house',1,?)", (message_id,))
        db.execute('INSERT INTO messages VALUES (1,50,?,?)', ('2024-03-02T10:00:00+00:00', 'рендер вне проекта'))
        db.execute("INSERT INTO media(dialog_id,message_id,kind,original_name) VALUES (1,50,'photo','photo_50.jpg')")
        db.execute("INSERT INTO project_messages VALUES ('loft',1,3)")
        db.executemany("INSERT INTO projects(id,name,updated_at) VALUES (?,?,'2024')", [('house', 'House'), ('loft', 'Loft')])
        db.commit()
        db.close()

    def tags(self):
        db = sqlite3.connect(str(self.db))
        try:
            return {row[0]: (row[1], row[2]) for row in db.execute('SELECT message_id,tag,source FROM media_tags')}
        finally:
            db.close()

    def test_rules_tag_by_extension_name_and_caption(self):
        code, stdout, _ = run(self.db, 'auto')
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(), {
            1: ('drawing', 'rule:extension'), 2: ('drawing', 'rule:extension'), 3: ('render', 'rule:name'),
            4: ('photo', 'rule:name'), 5: ('render', 'rule:caption'), 7: ('drawing', 'rule:name')})
        summary = json.loads(stdout)
        self.assertEqual((summary['attachments'], summary['untagged']), (9, 3))
        self.assertEqual(summary['tag:drawing'], 3)
        self.assertNotIn('СЕКРЕТ', stdout)

    def test_manual_tags_survive_auto_and_can_be_cleared(self):
        refs = self.root / 'refs.txt'
        refs.write_text('1:6 # готовый объект\n1:3\n', encoding='utf-8')
        code, stdout, _ = run(self.db, 'set', '--tag', 'finished', '--refs-file', refs, '--refs', '1:8')
        self.assertEqual((code, json.loads(stdout)), (0, {'tagged': 3, 'tag': 'finished'}))
        code, stdout, _ = run(self.db, 'auto')
        self.assertEqual(json.loads(stdout)['kept_manual'], 3)
        tags = self.tags()
        self.assertEqual((tags[6], tags[3], tags[8]), (('finished', 'manual'),) * 3)
        self.assertEqual(tags[1], ('drawing', 'rule:extension'))
        code, stdout, _ = run(self.db, 'clear', '--refs', '1:3', '1:999')
        self.assertEqual(json.loads(stdout), {'cleared': 1})
        run(self.db, 'auto')
        self.assertEqual(self.tags()[3], ('render', 'rule:name'))

    def test_auto_is_limited_to_projects_and_is_idempotent(self):
        run(self.db, 'auto', '--project', 'loft')
        self.assertEqual(self.tags(), {3: ('render', 'rule:name')})
        run(self.db, 'auto')
        first = self.tags()
        run(self.db, 'auto')
        self.assertEqual(self.tags(), first)
        self.assertNotIn(50, first)

    def test_report_counts_without_text_or_names(self):
        run(self.db, 'auto')
        run(self.db, 'set', '--tag', 'finished', '--refs', '1:6')
        code, stdout, _ = run(self.db, 'report', '--list-untagged')
        self.assertEqual(code, 0)
        data = json.loads(stdout)
        house = data['projects']['house']
        self.assertEqual(house['by_tag'], {'drawing': 3, 'finished': 1, 'photo': 1, 'render': 2})
        self.assertEqual((house['attachments'], house['untagged'], house['before'], house['after']), (9, 2, 5, 1))
        self.assertEqual(house['untagged_refs'], ['1:8', '1:9'])
        self.assertEqual(data['projects']['loft']['by_tag'], {'render': 1})
        for private in ('СЕКРЕТ', 'План этажа', 'House', 'kitchen'):
            self.assertNotIn(private, stdout)
        code, stdout, _ = run(self.db, 'report', '--project', 'loft')
        self.assertEqual(list(json.loads(stdout)['projects']), ['loft'])
        self.assertNotIn('untagged_refs', stdout)

    def test_negative_chat_ids_work_in_references(self):
        db = sqlite3.connect(str(self.db))
        db.execute("INSERT INTO messages VALUES (-1001234567890,4,'2024-03-01T10:00:00+00:00','')")
        db.execute("INSERT INTO media(dialog_id,message_id,kind,original_name) VALUES (-1001234567890,4,'photo','p.jpg')")
        db.commit()
        db.close()
        code, stdout, _ = run(self.db, 'set', '--tag', 'finished', '--refs', '-1001234567890:4', '1:6')
        self.assertEqual((code, json.loads(stdout)['tagged']), (0, 2))
        code, stdout, _ = run(self.db, 'clear', '--refs', '-1001234567890:4')
        self.assertEqual(json.loads(stdout), {'cleared': 1})

    def test_bad_input_is_refused(self):
        for argv in (['set', '--tag', 'finished', '--refs', '1:777'], ['set', '--tag', 'finished'],
                     ['set', '--tag', 'finished', '--refs', 'nonsense'], ['auto', '--project', 'missing']):
            with self.subTest(argv), self.assertRaises(SystemExit):
                run(self.db, *argv)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(self.db, 'set', '--tag', 'hologram', '--refs', '1:1')
        missing = self.root / 'absent.sqlite3'
        code, stdout, stderr = run(missing, 'report')
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('Cannot use archive database', stderr)
        self.assertFalse(missing.exists())

    @unittest.skipUnless(HAS_PIL, 'Pillow not installed')
    def test_camera_exif_marks_a_photo(self):
        from PIL import Image
        media = self.root / 'data'
        media.mkdir()
        exif = Image.Exif()
        exif[image_tags.EXIF_MAKE] = 'Canon'
        Image.new('RGB', (8, 8), 'blue').save(media / 'camera.jpg', exif=exif)
        Image.new('RGB', (8, 8), 'red').save(media / 'plain.jpg')
        db = sqlite3.connect(str(self.db))
        db.execute("UPDATE media SET local_path='data/camera.jpg' WHERE message_id=6")
        db.execute("UPDATE media SET local_path='data/plain.jpg' WHERE message_id=5")
        db.execute("UPDATE media SET local_path='data/missing.jpg' WHERE message_id=3")
        db.commit()
        db.close()
        self.assertTrue(image_tags.has_camera_exif(media / 'camera.jpg'))
        self.assertFalse(image_tags.has_camera_exif(media / 'plain.jpg'))
        self.assertIsNone(image_tags.has_camera_exif(media / 'missing.jpg'))
        run(self.db, 'auto')
        tags = self.tags()
        self.assertEqual(tags[6], ('photo', 'rule:exif'))
        self.assertEqual(tags[5], ('render', 'rule:caption'))
        self.assertEqual(tags[3], ('render', 'rule:name'))


if __name__ == '__main__':
    unittest.main()
