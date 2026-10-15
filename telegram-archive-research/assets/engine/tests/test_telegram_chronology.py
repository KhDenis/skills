import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

SCRIPT = (Path(__file__).resolve().parents[1]
          / 'scripts' / 'chronology.py')
spec = importlib.util.spec_from_file_location('chronology', SCRIPT)
chronology = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chronology)

# Schema of work_project_archive.open_db, written by hand so the test needs no Telethon.
SCHEMA = '''
CREATE TABLE dialogs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT, kind TEXT NOT NULL,
    relevance_score INTEGER NOT NULL DEFAULT 0, reasons_json TEXT NOT NULL DEFAULT '[]',
    preview_text TEXT NOT NULL DEFAULT '', preview_image_count INTEGER NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'pending', last_discovered_at TEXT NOT NULL, reviewed_at TEXT);
CREATE TABLE messages (dialog_id INTEGER NOT NULL, id INTEGER NOT NULL, date TEXT NOT NULL, sender_id INTEGER,
    sender TEXT, text TEXT NOT NULL DEFAULT '', reply_to_msg_id INTEGER, media_kind TEXT, media_name TEXT,
    media_size INTEGER, PRIMARY KEY(dialog_id, id));
CREATE TABLE media (dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL,
    original_name TEXT, size_bytes INTEGER, sha256 TEXT, local_path TEXT, downloaded_at TEXT,
    PRIMARY KEY(dialog_id, message_id));
CREATE TABLE sync_state (dialog_id INTEGER PRIMARY KEY, newest_id INTEGER, oldest_id INTEGER,
    complete INTEGER NOT NULL DEFAULT 0, last_sync_at TEXT);
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE project_messages (project_id TEXT NOT NULL, dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, dialog_id, message_id));
'''
FLAT = 'Квартира на Лесной'
FLAT_DIR = 'Квартира_на_Лесной'
LOFT = 'Office Loft'
LOFT_DIR = 'Office_Loft'
SECRET = 'смета на кухню СЕКРЕТНЫЙ-ТЕКСТ'


def build(root: Path) -> tuple[Path, Path]:
    db_path = root / 'work_projects.sqlite3'
    out = root / 'Projects'
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA)
    db.executemany('INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at) VALUES (?,?,?,?,?)', [
        (-100, 'Лесная | рабочий', 'group', 'approved', '2024-01-01'),
        (200, 'Loft chat', 'user', 'approved', '2024-01-01'),
        (300, 'Не начат', 'group', 'approved', '2024-01-01'),
        (400, 'Личный', 'user', 'rejected', '2024-01-01'),
        (500, 'Новый', 'group', 'pending', '2024-01-01'),
    ])
    db.executemany('INSERT INTO messages(dialog_id,id,date,sender_id,sender,text,media_kind,media_name) VALUES (?,?,?,?,?,?,?,?)', [
        (-100, 1, '2024-03-01T10:00:00+00:00', 1, 'Анна', SECRET, None, None),
        (200, 7, '2024-03-02T09:30:00+00:00', 2, 'Bob', 'loft\nplan', 'image', 'plan.jpg'),
        (-100, 2, '2024-03-05T12:00:00+00:00', 1, 'Анна', 'фото объекта', 'image', 'фото 1.jpg'),
        (200, 8, '2024-03-07T08:00:00+00:00', None, None, 'voice', 'hologram', None),
        (200, 9, '2024-03-09T08:00:00+00:00', 2, 'Bob', 'later', 'image', 'late.jpg'),
        (400, 1, '2024-03-03T08:00:00+00:00', 3, 'X', 'не в проекте', None, None),
    ])
    db.executemany('INSERT INTO media(dialog_id,message_id,kind,original_name,local_path) VALUES (?,?,?,?,?)', [
        (200, 7, 'image', 'plan.jpg', 'data/work_projects/media/aa/aa.jpg'),
        (-100, 2, 'image', 'фото 1.jpg', 'data/work_projects/media/bb/bb.jpg'),
        (200, 8, 'hologram', None, 'data/work_projects/media/cc/cc.bin'),
        (200, 9, 'image', 'late.jpg', None),
    ])
    db.executemany('INSERT INTO sync_state(dialog_id,newest_id,complete,last_sync_at) VALUES (?,?,?,?)', [
        (-100, 2, 1, '2024-04-01T00:00:00+00:00'),
        (200, 9, 0, '2024-04-02T00:00:00+00:00'),
    ])
    db.executemany('INSERT INTO projects(id,name,updated_at) VALUES (?,?,?)', [
        ('flat', FLAT, '2024-04-01'), ('loft', LOFT, '2024-04-01'), ('empty', 'Empty', '2024-04-01'),
    ])
    db.executemany('INSERT INTO project_messages VALUES (?,?,?)', [
        ('flat', -100, 1), ('flat', -100, 2), ('loft', 200, 7), ('loft', 200, 8), ('loft', 200, 9),
    ])
    db.commit()
    db.close()
    for relative in (f'{FLAT_DIR}/images/2024-03-05_-100_2_фото_1.jpg', f'{LOFT_DIR}/images/2024-03-02_200_7_plan.jpg',
                     f'{LOFT_DIR}/media/2024-03-07_200_8_cc.bin', f'{LOFT_DIR}/timeline.md'):
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    return db_path, out


def run(*argv: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = chronology.main([str(value) for value in argv])
    return code, stdout.getvalue(), stderr.getvalue()


class ChronologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db, self.out = build(Path(self.tmp.name))

    def test_timeline_orders_messages_across_projects(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        code, _, _ = run('timeline', '--db', self.db, '--output-dir', self.out)
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        records = [json.loads(line) for line in (self.out / 'chronology.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['project_id'], r['ref']) for r in records], [
            ('flat', '-100:1'), ('loft', '200:7'), ('flat', '-100:2'), ('loft', '200:8'), ('loft', '200:9')])
        self.assertEqual(records[0]['text'], SECRET)
        self.assertEqual(records[0]['dialog'], 'Лесная | рабочий')
        self.assertIsNone(records[0]['media'])
        text = (self.out / 'chronology.md').read_text(encoding='utf-8')
        days = [line[3:] for line in text.splitlines() if line.startswith('## ')]
        self.assertEqual(days, ['2024-03-01', '2024-03-02', '2024-03-05', '2024-03-07', '2024-03-09'])
        self.assertIn('loft plan', text)
        self.assertLess(text.index('`-100:1`'), text.index('`200:7`'))
        self.assertLess(text.index('`200:7`'), text.index('`-100:2`'))

    def test_summary_table(self):
        run('timeline', '--db', self.db, '--output-dir', self.out)
        lines = (self.out / 'chronology.md').read_text(encoding='utf-8').splitlines()
        self.assertIn(f'| {FLAT} | 2024-03-01 | 2024-03-05 | 2 | 1 | 1 | [{FLAT_DIR}/]({quote(FLAT_DIR)}/) |', lines)
        self.assertIn(f'| {LOFT} | 2024-03-02 | 2024-03-09 | 3 | 3 | 2 | [{LOFT_DIR}/]({LOFT_DIR}/) |', lines)
        self.assertIn('| Empty | — | — | 0 | 0 | 0 | [Empty/](Empty/) |', lines)
        table = [line for line in lines if line.startswith('| ')]
        self.assertLess(lines.index(table[0]), lines.index('## 2024-03-01'))

    def test_links_are_relative_and_resolve(self):
        run('timeline', '--db', self.db, '--output-dir', self.out)
        records = {r['ref']: r for r in map(json.loads, (self.out / 'chronology.jsonl').read_text(encoding='utf-8').splitlines())}
        photo = records['-100:2']
        self.assertEqual(photo['project_dir'], f'{FLAT_DIR}/')
        self.assertEqual(photo['media'], {'kind': 'image', 'name': 'фото 1.jpg', 'downloaded': True,
                                          'file': f'{FLAT_DIR}/images/2024-03-05_-100_2_фото_1.jpg'})
        for record in records.values():
            self.assertTrue((self.out / record['project_dir']).is_dir())
            if record['media'] and record['media']['file']:
                self.assertFalse(Path(record['media']['file']).is_absolute())
                self.assertTrue((self.out / record['media']['file']).is_file())
        self.assertEqual(records['200:9']['media'], {'kind': 'image', 'name': 'late.jpg', 'downloaded': False, 'file': None})
        text = (self.out / 'chronology.md').read_text(encoding='utf-8')
        self.assertIn(f'({quote(FLAT_DIR)}/images/{quote("2024-03-05_-100_2_фото_1.jpg")})', text)
        self.assertIn(f'[{LOFT}]({LOFT_DIR}/)', text)
        self.assertIn('image: late.jpg (не скачано)', text)
        self.assertNotIn(str(self.out), text)
        self.assertNotIn(self.tmp.name, (self.out / 'chronology.jsonl').read_text(encoding='utf-8'))

    def test_unknown_media_kind(self):
        code, _, _ = run('timeline', '--db', self.db, '--output-dir', self.out)
        self.assertEqual(code, 0)
        records = {r['ref']: r for r in map(json.loads, (self.out / 'chronology.jsonl').read_text(encoding='utf-8').splitlines())}
        self.assertEqual(records['200:8']['media'], {'kind': 'hologram', 'name': None, 'downloaded': True,
                                                     'file': f'{LOFT_DIR}/media/2024-03-07_200_8_cc.bin'})
        self.assertIn(f'[hologram]({LOFT_DIR}/media/2024-03-07_200_8_cc.bin)', (self.out / 'chronology.md').read_text(encoding='utf-8'))
        _, stdout, _ = run('report', '--db', self.db)
        self.assertEqual(json.loads(stdout)['media']['by_kind']['hologram'], {'total': 1, 'downloaded': 1})

    def test_report_has_counts_and_no_message_text(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        code, stdout, _ = run('report', '--db', self.db)
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        data = json.loads(stdout)
        self.assertEqual(data['dialogs'], {'total': 5, 'by_status': {'approved': 3, 'pending': 1, 'rejected': 1}})
        self.assertEqual(data['messages'], 6)
        self.assertEqual(data['media'], {'total': 4, 'downloaded': 3, 'by_kind': {
            'hologram': {'total': 1, 'downloaded': 1}, 'image': {'total': 3, 'downloaded': 2}}})
        self.assertEqual((data['projects'], data['project_messages']), (3, 5))
        self.assertEqual(data['last_sync_at'], '2024-04-02T00:00:00+00:00')
        self.assertEqual(data['incomplete_dialogs'], [
            {'dialog_id': 200, 'review_status': 'approved', 'last_sync_at': '2024-04-02T00:00:00+00:00'},
            {'dialog_id': 300, 'review_status': 'approved', 'last_sync_at': None}])
        for private in (SECRET, 'СЕКРЕТНЫЙ', 'фото объекта', 'Анна', 'Лесная', 'Loft chat'):
            self.assertNotIn(private, stdout)

    def test_missing_or_broken_database_exits_1(self):
        missing = Path(self.tmp.name) / 'absent.sqlite3'
        for command in (['report', '--db', str(missing)], ['timeline', '--db', str(missing), '--output-dir', str(self.out / 'x')]):
            result = subprocess.run([sys.executable, str(SCRIPT), *command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, command)
            self.assertIn('Cannot read archive database', result.stderr)
            self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())
        self.assertFalse((self.out / 'x').exists())
        broken = Path(self.tmp.name) / 'broken.sqlite3'
        broken.write_text('not a database', encoding='utf-8')
        code, stdout, stderr = run('report', '--db', broken)
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('Cannot read archive database', stderr)
        result = subprocess.run([sys.executable, str(SCRIPT), 'report', '--db', str(self.db)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['projects'], 3)


if __name__ == '__main__':
    unittest.main()
