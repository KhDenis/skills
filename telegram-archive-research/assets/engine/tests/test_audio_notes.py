import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audio_notes as app


def item(duration, kind="voice", project="p1"):
    return {"duration_s": duration, "kind": kind, "project_id": project}


def probe(text, no_speech=0.0, compression=1.2, logprob=-0.5):
    return {"text": text, "no_speech_prob": no_speech, "compression_ratio": compression, "avg_logprob": logprob}


class AudioNotesTests(unittest.TestCase):
    def test_signal_words_count_facts_and_numbers(self):
        facts, numbers = app.signal_words("Монтаж подиума: фанера 18 мм, высота два метра")
        self.assertEqual(facts, 5)
        self.assertEqual(numbers, 2)

    def test_short_note_without_speech_is_skipped(self):
        score, reasons = app.score_item(item(3), probe("Ага"), [], "group")
        self.assertLess(score, 1)
        self.assertIn("short<8s", reasons)
        self.assertIn("no-speech", reasons)

    def test_long_note_with_facts_is_kept(self):
        text = "Смотри, монтаж пола завтра, фанера 18 миллиметров, шесть колонн оборачиваем баннером"
        score, reasons = app.score_item(item(90), probe(text), [], "group")
        self.assertGreaterEqual(score, 4)
        self.assertIn("long>=60s", reasons)

    def test_chatter_of_medium_length_is_skipped(self):
        text = "Ну я не знаю где вы все я пока дома посижу потом может приеду попозже"
        score, _reasons = app.score_item(item(15), probe(text), [], "group")
        self.assertLess(score, 1)

    def test_context_raises_the_score(self):
        around = [{"text": "Размер баннера и монтаж каркаса согласовали", "media_kind": None},
                  {"text": "", "media_kind": "photo"}]
        with_context, reasons = app.score_item(item(15), None, around, "group")
        without, _ = app.score_item(item(15), None, [], "group")
        self.assertGreater(with_context, without)
        self.assertIn("near-files", reasons)

    def test_summary_keeps_the_sentences_with_facts(self):
        useful, summary = app.extract_summary(
            "Привет, как дела. Монтаж пола завтра, фанера 18 миллиметров. Потом созвонимся.")
        self.assertEqual(useful, 1)
        self.assertIn("фанера 18", summary)
        self.assertNotIn("Привет", summary)
        self.assertEqual(app.extract_summary("Ну я не знаю, потом приеду."), (0, ""))

    def test_export_writes_triage_and_summary_to_the_knowledge_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = app.open_db(root / "audio.sqlite3")
            kb_path = root / "kb.sqlite3"
            kb = app.sqlite3.connect(kb_path)
            kb.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY);
                INSERT INTO projects VALUES ('p1');
                CREATE TABLE assets (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, source TEXT, path TEXT,
                    bytes INTEGER, duration_s REAL, taken_at TEXT, author TEXT, updated_at TEXT,
                    triage TEXT, triage_score REAL, triage_reason TEXT);
                CREATE TABLE transcripts (asset_id TEXT, model TEXT, language TEXT, text TEXT, useful INTEGER,
                    summary TEXT, avg_logprob REAL, PRIMARY KEY(asset_id, model));""")
            kb.close()
            with db:
                db.execute("""INSERT INTO items(dialog_id,message_id,kind,date,sender,size_bytes,duration_s,
                    project_id,local_path) VALUES (-5,7,'voice','2024-01-01','A',10,30,'p1','a/7.oga')""")
                db.execute("""INSERT INTO transcripts VALUES (-5,7,'small','full','ru',
                    'Монтаж пола завтра, фанера 18 миллиметров.',-0.3,0.0,1.1,1.0,'now')""")
                db.execute("INSERT INTO triage VALUES (-5,7,4.0,'tiny','[\"in-project\"]','now')")
            args = app.argparse.Namespace(kb=kb_path, project=None)
            app.export_kb(args, db)
            db.close()
            kb = app.sqlite3.connect(kb_path)
            self.assertEqual(kb.execute("SELECT project_id, kind, triage FROM assets").fetchone(),
                             ("p1", "audio", "keep"))
            self.assertEqual(kb.execute("SELECT model, useful FROM transcripts").fetchone(), ("whisper-small", 1))
            kb.close()

    def test_database_is_created_with_the_three_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            db = app.open_db(Path(directory) / "audio.sqlite3")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
        self.assertTrue({"items", "transcripts", "triage"} <= tables)


if __name__ == "__main__":
    unittest.main()
