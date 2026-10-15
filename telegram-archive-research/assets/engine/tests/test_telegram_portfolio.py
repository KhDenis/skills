import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('portfolio', SCRIPTS / 'portfolio.py')
portfolio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portfolio)

# The tables the script reads, written by hand (schema of work_project_archive.open_db and image_tags.open_db).
SCHEMA = '''
CREATE TABLE dialogs (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE messages (dialog_id INTEGER NOT NULL, id INTEGER NOT NULL, date TEXT NOT NULL, sender_id INTEGER,
    sender TEXT, text TEXT NOT NULL DEFAULT '', PRIMARY KEY(dialog_id, id));
CREATE TABLE media (dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL,
    original_name TEXT, local_path TEXT, PRIMARY KEY(dialog_id, message_id));
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE project_messages (project_id TEXT NOT NULL, dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(project_id, dialog_id, message_id));
'''
TAGS = '''CREATE TABLE media_tags (dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, tag TEXT NOT NULL,
    source TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(dialog_id, message_id));'''
# message id, day, sender, text, kind, downloaded, tag
ROWS = [
    (1, 1, 'Анна', 'начинаем проект', None, False, None),
    (2, 2, 'Анна', 'рендер\nгостиной', 'photo', True, 'render'),
    (3, 3, 'Bob', 'план', 'document', True, 'drawing'),
    (4, 5, 'Bob', 'монтаж', 'photo', True, 'site_photo'),
    (5, 9, 'Анна', 'готово', 'photo', True, 'finished'),
    (6, 9, 'Анна', 'ещё', 'photo', True, None),
    (7, 9, None, 'не скачано', 'photo', False, 'finished'),
    (8, 9, 'Анна', 'мем', 'photo', True, 'other'),
]
FLAT = 'Квартира_на_Лесной'


def run(db, out, *argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = portfolio.main(['--db', str(db), '--output-dir', str(out), *argv])
    return code, stdout.getvalue(), stderr.getvalue()


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db, self.out = root / 'work_projects.sqlite3', root / 'Projects'
        db = sqlite3.connect(str(self.db))
        db.executescript(SCHEMA + TAGS)
        db.execute("INSERT INTO dialogs VALUES (-100,'Лесная рабочий')")
        db.executemany("INSERT INTO projects(id,name,updated_at) VALUES (?,?,'2024')",
                       [('flat', 'Квартира на Лесной'), ('nofolder', 'No Folder'), ('empty', 'Empty')])
        for message_id, day, sender, text, kind, downloaded, tag in ROWS:
            date = f'2024-03-{day:02d}T10:00:00+00:00'
            db.execute('INSERT INTO messages VALUES (-100,?,?,?,?,?)', (message_id, date, 7 if sender else None, sender, text))
            db.execute("INSERT INTO project_messages VALUES ('flat',-100,?)", (message_id,))
            if kind:
                db.execute('INSERT INTO media VALUES (-100,?,?,?,?)',
                           (message_id, kind, f'f{message_id}.jpg', f'data/{message_id}' if downloaded else None))
                if downloaded:
                    path = self.out / FLAT / 'media' / f'2024-03-{day:02d}_-100_{message_id}_f{message_id}.jpg'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'x')
            if tag:
                db.execute("INSERT INTO media_tags VALUES (-100,?,?,'manual','','2024')", (message_id, tag))
        db.commit()
        db.close()
        (self.out / 'Empty').mkdir()
        self.file = self.out / FLAT / 'portfolio.json'

    def test_build_groups_tagged_files_into_before_after_progress(self):
        code, stdout, _ = run(self.db, self.out, 'build')
        self.assertEqual(code, 0)
        summary = json.loads(stdout)
        self.assertEqual((summary['written'], summary['skipped_no_folder']), (2, ['nofolder']))
        self.assertEqual(summary['projects']['flat'], {'before': 2, 'after': 1, 'progress': 1, 'untagged': 1})
        self.assertNotIn('Лесн', stdout)
        data = json.loads(self.file.read_text(encoding='utf-8'))
        generated = data['generated']
        self.assertEqual(data['schema'], 1)
        self.assertEqual((generated['first_date'], generated['last_date'], generated['messages']), ('2024-03-01', '2024-03-09', 8))
        self.assertEqual((generated['attachments'], generated['untagged'], generated['without_file']), (7, 1, 1))
        self.assertEqual(generated['partners'], [{'name': 'Анна', 'messages': 5}, {'name': 'Bob', 'messages': 2}])
        self.assertEqual(generated['chats'], [{'dialog_id': -100, 'title': 'Лесная рабочий', 'messages': 8}])
        self.assertEqual([(i['ref'], i['tag']) for i in generated['before']], [('-100:2', 'render'), ('-100:3', 'drawing')])
        self.assertEqual(generated['before'][0], {
            'ref': '-100:2', 'date': '2024-03-02T10:00:00+00:00', 'tag': 'render', 'kind': 'photo',
            'file': 'media/2024-03-02_-100_2_f2.jpg', 'caption': 'рендер гостиной'})
        self.assertEqual([i['ref'] for i in generated['after']], ['-100:5'])
        self.assertEqual([i['ref'] for i in generated['progress']], ['-100:4'])
        for group in ('before', 'after', 'progress'):
            for item in generated[group]:
                self.assertTrue((self.out / FLAT / item['file']).is_file())
        self.assertEqual(data['editorial'], {**portfolio.EDITORIAL_DEFAULTS, 'title': 'Квартира на Лесной'})
        empty = json.loads((self.out / 'Empty' / 'portfolio.json').read_text(encoding='utf-8'))['generated']
        self.assertEqual((empty['messages'], empty['first_date'], empty['before']), (0, None, []))

    def test_rebuild_keeps_editorial_and_refreshes_generated(self):
        run(self.db, self.out, 'build')
        data = json.loads(self.file.read_text(encoding='utf-8'))
        data['editorial'].update(title='Лесная, 120 м²', date='2024-03', event='Сдача объекта', summary='Текст',
                                 before=['-100:2'], after=['-100:5'], note='своё поле')
        self.file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        db = sqlite3.connect(str(self.db))
        db.execute("UPDATE media_tags SET tag='finished' WHERE message_id=4")
        db.commit()
        db.close()
        run(self.db, self.out, 'build', '--project', 'flat')
        rebuilt = json.loads(self.file.read_text(encoding='utf-8'))
        self.assertEqual(rebuilt['editorial'], data['editorial'])
        self.assertEqual([i['ref'] for i in rebuilt['generated']['after']], ['-100:4', '-100:5'])
        self.assertEqual(list(self.file.parent.glob('*.tmp')), [])

    def test_build_refuses_to_overwrite_a_broken_file_and_works_without_tags(self):
        self.file.write_text('{broken', encoding='utf-8')
        with self.assertRaises(SystemExit):
            run(self.db, self.out, 'build')
        self.assertEqual(self.file.read_text(encoding='utf-8'), '{broken')
        self.file.unlink()
        db = sqlite3.connect(str(self.db))
        db.execute('DROP TABLE media_tags')
        db.commit()
        db.close()
        code, stdout, _ = run(self.db, self.out, 'build', '--project', 'flat')
        self.assertEqual(json.loads(stdout)['projects']['flat'], {'before': 0, 'after': 0, 'progress': 0, 'untagged': 7})
        with self.assertRaises(SystemExit):
            run(self.db, self.out, 'build', '--project', 'missing')

    def test_check_reports_readiness_and_errors(self):
        code, stdout, _ = run(self.db, self.out, 'check')
        report = json.loads(stdout)
        self.assertEqual((code, report['ready']), (0, 0))
        self.assertEqual(report['projects']['flat']['missing'], ['portfolio.json'])
        run(self.db, self.out, 'build')
        code, stdout, _ = run(self.db, self.out, 'check', '--project', 'flat')
        self.assertEqual(json.loads(stdout)['projects']['flat'],
                         {'errors': [], 'missing': ['date', 'event', 'summary', 'before', 'after'], 'ready': False})
        data = json.loads(self.file.read_text(encoding='utf-8'))
        data['editorial'].update(date='2024-03', event='Сдача', summary='Текст', before=['-100:2'], after=['-100:5'])
        self.file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        code, stdout, _ = run(self.db, self.out, 'check')
        report = json.loads(stdout)
        self.assertEqual((code, report['ready'], report['errors']), (0, 1, 0))
        self.assertTrue(report['projects']['flat']['ready'])
        data['editorial'].update(before=['-100:2', '-100:6'], after=['-100:5'])
        self.file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        (self.out / FLAT / 'media' / '2024-03-09_-100_5_f5.jpg').unlink()
        code, stdout, _ = run(self.db, self.out, 'check')
        flat = json.loads(stdout)['projects']['flat']
        self.assertEqual(code, 1)
        self.assertEqual(flat['errors'], ['before -100:6: not among the tagged files of this project',
                                          'after -100:5: file is missing'])
        self.assertFalse(flat['ready'])
        self.file.write_text('[]', encoding='utf-8')
        code, stdout, _ = run(self.db, self.out, 'check')
        self.assertEqual(code, 1)

    def test_missing_database_exits_1(self):
        code, stdout, stderr = run(Path(self.tmp.name) / 'absent.sqlite3', self.out, 'build')
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('Cannot read archive database', stderr)


if __name__ == '__main__':
    unittest.main()
