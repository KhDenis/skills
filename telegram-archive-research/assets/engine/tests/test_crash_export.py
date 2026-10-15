import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import work_project_archive as app


def seed(root):
    db = app.open_db(root / 'archive.sqlite3')
    with db:
        for did, status in [(1, 'approved'), (2, 'rejected'), (3, 'pending')]:
            db.execute('INSERT INTO dialogs(id,title,username,kind,review_status,last_discovered_at) VALUES (?,?,?,\'group\',?,\'test\')',
                       (did, 'Synthetic '+status, 'test_dialog', status))
            for mid in (1, 2):
                app.upsert_message(db, did, dict(id=mid, date='2024-01-01T00:00:00+00:00',sender_id=7,sender='Synthetic',
                    text=f'fixture-{did}-{mid}',reply_to_msg_id=1 if mid==2 else None,media_kind='photo',media_name='photo.jpg',media_size=10))
    return db


class CrashExportTests(unittest.TestCase):
    def test_export_preserves_provenance_excludes_unapproved_and_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = seed(root)
            self.addCleanup(db.close)
            with patch.object(app, 'RESULTS_ROOT', root / 'out'), contextlib.redirect_stdout(io.StringIO()):
                path = app.export_analysis(db)
                first = path.read_bytes()
                rows = [json.loads(line) for line in first.splitlines()]
                self.assertEqual([(r['dialog_id'], r['id']) for r in rows], [(1,1),(1,2)])
                self.assertEqual(rows[1]['reply_to_msg_id'],1)
                for row in rows:
                    self.assertEqual((row['date'],row['sender_id'],row['username']),('2024-01-01T00:00:00+00:00',7,'test_dialog'))
                    self.assertEqual(db.execute('SELECT text FROM messages WHERE dialog_id=? AND id=?',(row['dialog_id'],row['id'])).fetchone()[0],row['text'])
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(app.export_analysis(db).read_bytes(),first)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_failed_export_preserves_previous_complete_file(self):
        class Broken:
            def execute(self, query):
                yield {'dialog_id':1,'id':1,'text':'unfinished replacement'}
                raise OSError('simulated export interruption')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(exist_ok=True)
            path = root / 'approved_messages.jsonl'
            path.write_text('previous complete export\n')
            with patch.object(app,'RESULTS_ROOT',root):
                with self.assertRaises(OSError): app.export_analysis(Broken())
            self.assertEqual(path.read_text(),'previous complete export\n')
            self.assertEqual(list(root.glob('*.tmp')),[])

    def test_interrupted_file_copy_does_not_publish_partial_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'incoming.jpg';source.write_bytes(b'complete image bytes')
            destination=root/'stored.jpg';destination.write_bytes(b'previous bytes')
            def interrupted(incoming, output):
                output.write(b'partial')
                raise OSError('copy interrupted')
            with patch.object(app.shutil,'copyfileobj',interrupted):
                with self.assertRaises(OSError):
                    app.store_media(source,destination,hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(destination.read_bytes(),b'previous bytes')
            self.assertEqual(list(root.glob('*.tmp')),[])

    def test_sigkill_during_transfer_resumes_committed_rows_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = seed(root);db.close()
            (root/'tmp').mkdir()
            worker = Path(__file__).with_name('fixtures')/'crash_media_worker.py'
            env = dict(os.environ,TMPDIR=str(root/'tmp'))
            proc = subprocess.Popen([sys.executable,str(worker),str(root),'pause'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+20
                while not (root/'paused').exists():
                    if proc.poll() is not None: self.fail(proc.stderr.read().decode())
                    if time.monotonic()>deadline: self.fail('worker failed to reach transfer checkpoint')
                    time.sleep(.02)
                proc.kill();proc.wait(timeout=5)
                self.assertLess(proc.returncode,0)
                with app.open_db(root/'archive.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM media WHERE local_path IS NOT NULL').fetchone()[0],1)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                # Simulate an incomplete content-addressed destination left by an older cross-filesystem copy.
                digest = hashlib.sha256(b'complete-2').hexdigest()
                destination = root/'media'/digest[:2]/(digest+'.jpg')
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(b'broken')
                for _ in range(2):
                    subprocess.run([sys.executable,str(worker),str(root),'resume'],env=env,check=True,capture_output=True,timeout=20)
                self.assertEqual((root/'requests.txt').read_text().splitlines(),['1','2','2'])
                with app.open_db(root/'archive.sqlite3') as db:
                    rows=db.execute('SELECT dialog_id,message_id,sha256,local_path FROM media WHERE local_path IS NOT NULL').fetchall()
                    self.assertEqual([(r['dialog_id'],r['message_id']) for r in rows],[(1,1),(1,2)])
                    for row in rows:
                        data=(root/row['local_path']).read_bytes()
                        self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'])
                        self.assertEqual(data,f"complete-{row['message_id']}".encode())
                self.assertEqual(len(list((root/'media').rglob('*.jpg'))),2)
            finally:
                if proc.poll() is None: proc.kill();proc.wait(timeout=5)
                proc.stderr.close()
