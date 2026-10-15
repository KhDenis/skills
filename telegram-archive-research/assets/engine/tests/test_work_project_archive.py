import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_HAS_PIL = importlib.util.find_spec("PIL") is not None

from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel, Document, DocumentAttributeAudio, DocumentAttributeFilename, DocumentAttributeVideo,
    MessageMediaContact, MessageMediaDocument, MessageMediaPhoto, PeerChannel, PeerUser, Photo, PhotoSize, User,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import work_project_archive as app


class WorkProjectArchiveTests(unittest.TestCase):
    def test_score_dialog_uses_title_messages_and_images(self):
        keywords = app.parse_keywords("Studio decor renders", "интерьер")
        score, reasons = app.score_dialog(
            "Studio Decor Projects",
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

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed")
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


def fake_user(user_id, name):
    return User(id=user_id, first_name=name)


def fake_channel(channel_id, title, broadcast=False):
    return Channel(id=channel_id, title=title, photo=None, date=None,
                   broadcast=broadcast, megagroup=not broadcast)


class FakeMessage:
    def __init__(self, message_id, chat, peer, day, text):
        self.id = message_id
        self.chat = chat
        self.peer_id = peer
        self.date = datetime(2025, 1, day, 12, tzinfo=timezone.utc)
        self.message = text
        self.sender = fake_user(7, "Designer")
        self.sender_id = 7
        self.reply_to_msg_id = None
        self.media = None

    async def download_media(self, *args, **kwargs):
        raise AssertionError("search must not download media")


class FakeClient:
    def __init__(self, messages, flood_once=False):
        self.messages = messages
        self.flood_once = flood_once
        self.calls = []

    async def iter_dialogs(self):
        for chat in {id(m.chat): m.chat for m in self.messages}.values():
            peer_id = -1000000000000 - chat.id if isinstance(chat, Channel) else chat.id
            yield SimpleNamespace(id=peer_id, entity=chat)

    async def iter_messages(self, target, search=None, limit=None, offset_date=None):
        self.calls.append((target, search))
        if self.flood_once:
            self.flood_once = False
            raise FloodWaitError(request=None, capture=0)
        found = [m for m in sorted(self.messages, key=lambda m: m.date, reverse=True)
                 if search.casefold() in m.message.casefold()
                 and (target is None or m.chat is target)
                 and (offset_date is None or m.date < offset_date)]
        for message in found[:limit]:
            yield message

    async def download_media(self, *args, **kwargs):
        raise AssertionError("search must not download media")

    async def disconnect(self):
        pass


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.original = app.RESULTS_ROOT, app.telegram_client
        app.RESULTS_ROOT = self.root / "output"
        self.db = app.open_db(self.root / "archive.sqlite3")
        team = fake_channel(100, "Archive Projects team")
        news = fake_channel(200, "Decor news", broadcast=True)
        client = fake_user(5, "Client")
        self.messages = [
            FakeMessage(1, team, PeerChannel(100), 3, "Рендер гостиной готов"),
            FakeMessage(2, team, PeerChannel(100), 10, "Смета и рендер фасада"),
            FakeMessage(3, client, PeerUser(5), 20, "Когда будет рендер?"),
            FakeMessage(4, news, PeerChannel(200), 15, "Новый рендер в канале"),
        ]

    def tearDown(self):
        app.RESULTS_ROOT, app.telegram_client = self.original
        self.db.close()
        self.directory.cleanup()

    def run_search(self, client, **overrides):
        async def fake_client():
            return client
        app.telegram_client = fake_client
        args = dict(keywords="рендер, смета", keyword_file=None, since=None, until=None,
                    max_hits_per_keyword=500, chat=None, pause=0, include_broadcasts=False)
        args.update(overrides)
        asyncio.run(app.search(SimpleNamespace(**args), self.db))

    def test_search_records_hits_and_pending_dialogs(self):
        self.run_search(FakeClient(self.messages))
        hits = self.db.execute("SELECT dialog_id,message_id,keyword FROM search_hits ORDER BY 1,2,3").fetchall()
        self.assertEqual([tuple(row) for row in hits], [
            (-1000000000100, 1, "рендер"), (-1000000000100, 2, "рендер"),
            (-1000000000100, 2, "смета"), (5, 3, "рендер"),
        ])
        statuses = dict(self.db.execute("SELECT id,review_status FROM dialogs").fetchall())
        self.assertEqual(statuses, {-1000000000100: "pending", 5: "pending"})

    def test_search_keeps_review_decisions_and_is_idempotent(self):
        self.run_search(FakeClient(self.messages))
        app.set_review(self.db, ["-1000000000100"], "approved")
        self.run_search(FakeClient(self.messages))
        status = self.db.execute("SELECT review_status FROM dialogs WHERE id=-1000000000100").fetchone()[0]
        self.assertEqual(status, "approved")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0], 4)

    def test_search_filters_dates_inclusively(self):
        self.run_search(FakeClient(self.messages), keywords="рендер", since="2025-01-03", until="2025-01-10")
        ids = [row[0] for row in self.db.execute("SELECT message_id FROM search_hits ORDER BY 1")]
        self.assertEqual(ids, [1, 2])

    def test_search_inside_given_chats_only(self):
        client = FakeClient(self.messages)
        self.run_search(client, keywords="рендер", chat=["5"])
        ids = [row[0] for row in self.db.execute("SELECT message_id FROM search_hits")]
        self.assertEqual(ids, [3])
        self.assertTrue(all(target is not None for target, _ in client.calls))

    def test_search_retries_after_flood_wait(self):
        self.run_search(FakeClient(self.messages, flood_once=True), keywords="смета")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0], 1)

    def test_report_groups_hits_by_chat(self):
        self.run_search(FakeClient(self.messages))
        report = (app.RESULTS_ROOT / "search_hits.md").read_text(encoding="utf-8")
        self.assertLess(report.index("Archive Projects team"), report.index("## Client"))
        self.assertIn("Matching messages: **2** (рендер 2, смета 1)", report)
        self.assertIn("Dates: 2025-01-03 … 2025-01-10", report)
        self.assertIn("`-1000000000100:2`", report)
        self.assertNotIn("Decor news", report)



def fake_document(mime, size, *attributes):
    return MessageMediaDocument(document=Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime, size=size, dc_id=1,
        attributes=list(attributes)))


def fake_photo(size):
    return MessageMediaPhoto(photo=Photo(
        id=1, access_hash=1, file_reference=b"", date=None, dc_id=1,
        sizes=[PhotoSize(type="x", w=10, h=10, size=size)]))


FAKE_MEDIA = {
    1: lambda: fake_photo(100),
    2: lambda: fake_document("video/mp4", 5000, DocumentAttributeVideo(duration=5, w=1, h=1),
                             DocumentAttributeFilename(file_name="clip.mp4")),
    3: lambda: fake_document("audio/ogg", 300, DocumentAttributeAudio(duration=3, voice=True)),
    4: lambda: fake_document("application/pdf", 2000, DocumentAttributeFilename(file_name="plan.pdf")),
    5: lambda: fake_document("video/mp4", 700, DocumentAttributeVideo(duration=4, w=1, h=1, round_message=True)),
    6: lambda: fake_document("image/png", 400, DocumentAttributeFilename(file_name="render.png")),
    7: lambda: fake_document("audio/mpeg", 900, DocumentAttributeAudio(duration=9),
                             DocumentAttributeFilename(file_name="song.mp3")),
    8: lambda: MessageMediaContact(phone_number="0", first_name="A", last_name="B", vcard="", user_id=1),
    9: lambda: None,
}


class MediaMessage(FakeMessage):
    downloads = 0

    def __init__(self, message_id, chat, peer, day, text, media, payload=None):
        super().__init__(message_id, chat, peer, day, text)
        self.media = media
        self.errors = []
        self.payload = payload if payload is not None else f"payload-{message_id}".encode()

    async def download_media(self, file=None):
        if self.errors:
            raise self.errors.pop(0)
        MediaMessage.downloads += 1
        path = Path(file) / f"download_{self.id}{Path(app.media_metadata(self)[1]).suffix}"
        path.write_bytes(self.payload)
        return str(path)


class MediaClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_dialogs(self):
        for dialog_id in sorted({dialog_id for dialog_id, _ in self.messages}):
            yield SimpleNamespace(id=dialog_id, entity=dialog_id)

    async def get_messages(self, entity, ids=None):
        return self.messages.get((entity, ids))

    async def disconnect(self):
        pass


class TargetedMediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.original = app.PROJECT_ROOT, app.MEDIA_ROOT, app.telegram_client
        app.PROJECT_ROOT = self.root
        app.MEDIA_ROOT = self.root / "data" / "work_projects" / "media"
        self.db = app.open_db(self.root / "archive.sqlite3")
        chat = fake_channel(100, "Design team")
        self.messages = {}
        with self.db:
            for dialog_id, status in ((1, "approved"), (2, "rejected")):
                self.db.execute("""INSERT INTO dialogs(id,title,kind,review_status,last_discovered_at)
                    VALUES (?,?,?,?,?)""", (dialog_id, f"Chat {dialog_id}", "group", status, app.utc_now()))
                for message_id, media in FAKE_MEDIA.items():
                    message = MediaMessage(message_id, chat, PeerChannel(100), message_id, f"message {message_id}", media())
                    self.messages[(dialog_id, message_id)] = message
                    app.upsert_message(self.db, dialog_id, app.telegram_record(message))
            self.db.execute("INSERT INTO projects(id,name,updated_at) VALUES ('house','House',?)", (app.utc_now(),))
            self.db.executemany("INSERT INTO project_messages VALUES ('house',?,?)", [(1, 2), (1, 9), (2, 1)])
        MediaMessage.downloads = 0

        async def fake_client():
            return MediaClient(self.messages)
        app.telegram_client = fake_client

    def tearDown(self):
        app.PROJECT_ROOT, app.MEDIA_ROOT, app.telegram_client = self.original
        self.db.close()
        self.directory.cleanup()

    def args(self, **overrides):
        values = dict(project=None, refs=None, refs_file=None, around=0, kinds=None,
                      confirm=True, max_items=100000, max_bytes=0)
        values.update(overrides)
        return SimpleNamespace(**values)

    def selected(self, **overrides):
        return [item["row"]["message_id"] for item in app.selected_media(self.db, self.args(**overrides))]

    def test_sync_records_metadata_for_every_attachment_kind(self):
        rows = {row["message_id"]: row for row in self.db.execute("SELECT * FROM media WHERE dialog_id=1")}
        self.assertEqual({message_id: row["kind"] for message_id, row in rows.items()}, {
            1: "photo", 2: "video", 3: "voice", 4: "document", 5: "video_note", 6: "image_document", 7: "audio"})
        self.assertEqual((rows[2]["original_name"], rows[2]["size_bytes"]), ("clip.mp4", 5000))
        self.assertEqual((rows[4]["original_name"], rows[4]["size_bytes"]), ("plan.pdf", 2000))
        self.assertTrue(rows[3]["original_name"].startswith("voice_3"))
        self.assertEqual(rows[3]["size_bytes"], 300)
        self.assertTrue(all(row["local_path"] is None for row in rows.values()))
        self.assertEqual(MediaMessage.downloads, 0)
        kinds = dict(self.db.execute("SELECT id,media_kind FROM messages WHERE dialog_id=1 AND id IN (8,9)").fetchall())
        self.assertEqual(kinds, {8: "contact", 9: None})

    def test_old_attachments_are_backfilled_when_the_database_opens(self):
        with self.db:
            self.db.execute("DELETE FROM media WHERE kind NOT IN ('photo','image_document')")
        self.db.close()
        self.db = app.open_db(self.root / "archive.sqlite3")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM media WHERE dialog_id=1").fetchone()[0], 7)

    def test_selection_is_required(self):
        for command in (app.selected_media, app.media_plan):
            with self.assertRaises(SystemExit) as raised:
                command(self.db, self.args())
            self.assertIn("--project", str(raised.exception))
        with self.assertRaises(SystemExit):
            asyncio.run(app.download_media(self.args(), self.db))
        self.assertEqual(MediaMessage.downloads, 0)

    def test_select_by_project_refs_and_refs_file(self):
        self.assertEqual(self.selected(project=["house"]), [2])
        self.assertEqual(self.selected(project=["House"]), [2])
        self.assertEqual(self.selected(refs=["1:4", "1:1", "1:9", "2:3"]), [1, 4])
        refs_file = self.root / "refs.txt"
        refs_file.write_text("# renders\n1:6, 1:7\n1:3 # voice\n", encoding="utf-8")
        self.assertEqual(self.selected(refs_file=refs_file, refs=["1:1"]), [1, 3, 6, 7])
        with self.assertRaises(SystemExit):
            self.selected(project=["missing"])
        with self.assertRaises(SystemExit):
            self.selected(refs=["not-a-ref"])

    def test_around_adds_neighbouring_attachments_of_the_same_chat(self):
        self.assertEqual(self.selected(refs=["1:4"], around=1), [3, 4, 5])
        self.assertEqual(self.selected(refs=["1:9"], around=2), [7])
        self.assertEqual(self.selected(project=["house"], around=1), [1, 2, 3])
        plan = app.media_plan(self.db, self.args(project=["house"], around=1))
        self.assertEqual(plan["by_project"], {"house": {"count": 3, "bytes": 5400, "unknown": 0}})

    def test_kinds_filter_and_plan_totals(self):
        self.assertEqual(self.selected(refs=["1:5"], around=10, kinds="video,voice"), [2, 3])
        with self.assertRaises(SystemExit):
            self.selected(refs=["1:5"], kinds="video,hologram")
        plan = app.media_plan(self.db, self.args(refs=["1:5"], around=10))
        self.assertEqual((plan["count"], plan["bytes"], plan["selected"], plan["downloaded"]), (7, 9400, 7, 0))
        self.assertEqual(plan["by_kind"]["video"], {"count": 1, "bytes": 5000, "unknown": 0})
        self.assertEqual(sorted(plan["by_kind"]), sorted(app.MEDIA_KINDS))
        self.assertEqual(MediaMessage.downloads, 0)

    def test_download_media_is_confirmed_bounded_content_addressed_and_idempotent(self):
        chosen = dict(refs=["1:2", "1:3", "1:4", "2:2"])
        with self.assertRaises(SystemExit):
            asyncio.run(app.download_media(self.args(confirm=False, **chosen), self.db))
        with self.assertRaises(SystemExit):
            asyncio.run(app.download_media(self.args(max_bytes=1000, **chosen), self.db))
        self.assertEqual(MediaMessage.downloads, 0)
        asyncio.run(app.download_media(self.args(max_items=2, **chosen), self.db))
        self.assertEqual(MediaMessage.downloads, 2)
        asyncio.run(app.download_media(self.args(**chosen), self.db))
        self.assertEqual(MediaMessage.downloads, 3)
        asyncio.run(app.download_media(self.args(**chosen), self.db))
        self.assertEqual(MediaMessage.downloads, 3)
        rows = self.db.execute("SELECT * FROM media WHERE local_path IS NOT NULL ORDER BY message_id").fetchall()
        self.assertEqual([(row["dialog_id"], row["message_id"]) for row in rows], [(1, 2), (1, 3), (1, 4)])
        import hashlib
        for row in rows:
            digest = hashlib.sha256(f"payload-{row['message_id']}".encode()).hexdigest()
            self.assertEqual(row["sha256"], digest)
            self.assertEqual(Path(row["local_path"]).parts[-2:], (digest[:2], digest + Path(row["local_path"]).suffix))
            self.assertTrue((self.root / row["local_path"]).is_file())
        self.assertEqual(Path(rows[0]["local_path"]).suffix, ".mp4")
        self.assertEqual(Path(rows[2]["local_path"]).suffix, ".pdf")

    def test_legacy_image_commands_ignore_other_kinds(self):
        self.assertEqual(app.image_plan(self.db), {"count": 2, "bytes": 500, "unknown": 0})
        asyncio.run(app.download_images(self.args(), self.db))
        kinds = [row[0] for row in self.db.execute("SELECT kind FROM media WHERE local_path IS NOT NULL ORDER BY 1")]
        self.assertEqual(kinds, ["image_document", "photo"])
        self.assertEqual(MediaMessage.downloads, 2)

    def test_materialize_puts_files_in_media_and_links_them_in_the_timeline(self):
        asyncio.run(app.download_media(self.args(refs=["1:1", "1:2", "1:4"]), self.db))
        manifest = self.root / "projects.json"
        manifest.write_text(json.dumps({"projects": [{"id": "house", "name": "House", "dialog_ids": [1]}]}), encoding="utf-8")
        output = self.root / "results"
        app.materialize(SimpleNamespace(manifest=manifest, output_dir=output), self.db)
        project = output / "House"
        self.assertEqual(sorted(path.name for path in (project / "media").iterdir()), [
            "2025-01-01_1_1_photo_1.jpg", "2025-01-02_1_2_clip.mp4", "2025-01-04_1_4_plan.pdf"])
        self.assertEqual([path.name for path in (project / "images").iterdir()], ["2025-01-01_1_1_photo_1.jpg"])
        timeline = (project / "timeline.md").read_text(encoding="utf-8")
        self.assertIn("message 2 — [video](media/2025-01-02_1_2_clip.mp4)", timeline)
        self.assertIn("message 4 — [document](media/2025-01-04_1_4_plan.pdf)", timeline)
        self.assertIn("message 1 — [photo](media/2025-01-01_1_1_photo_1.jpg)", timeline)
        self.assertIn("message 3 — voice (not downloaded)", timeline)
        self.assertTrue(any(line.endswith("message 9") for line in timeline.splitlines()))
        for line in timeline.splitlines():
            if "](media/" in line:
                self.assertTrue((project / line.split("](", 1)[1].rstrip(")")).is_file())
        records = [json.loads(line) for line in (project / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[1]["media_file"], "media/2025-01-02_1_2_clip.mp4")
        self.assertIsNone(records[2]["media_file"])


    def test_download_waits_out_short_flood_waits_and_skips_failed_files(self):
        self.messages[(1, 2)].errors = [FloodWaitError(request=None, capture=0)]
        self.messages[(1, 3)].errors = [OSError("connection lost")]
        asyncio.run(app.download_media(self.args(refs=["1:2", "1:3", "1:4"]), self.db))
        done = [row[0] for row in self.db.execute("SELECT message_id FROM media WHERE local_path IS NOT NULL ORDER BY 1")]
        self.assertEqual(done, [2, 4])
        asyncio.run(app.download_media(self.args(refs=["1:2", "1:3", "1:4"]), self.db))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM media WHERE local_path IS NOT NULL").fetchone()[0], 3)

    def test_download_stops_cleanly_on_a_long_flood_wait(self):
        self.messages[(1, 3)].errors = [FloodWaitError(request=None, capture=app.MAX_FLOOD_WAIT + 1)]
        asyncio.run(app.download_media(self.args(refs=["1:2", "1:3", "1:4"]), self.db))
        done = [row[0] for row in self.db.execute("SELECT message_id FROM media WHERE local_path IS NOT NULL")]
        self.assertEqual(done, [2])

    def check(self, *projects):
        manifest = self.root / "projects.json"
        manifest.write_text(json.dumps({"projects": list(projects)}), encoding="utf-8")
        return app.check_manifest(SimpleNamespace(manifest=manifest), self.db)

    def test_check_manifest_reports_selection_without_writing(self):
        asyncio.run(app.download_media(self.args(refs=["1:2"]), self.db))
        before = self.db.execute("SELECT COUNT(*) FROM project_messages").fetchone()[0]
        report = self.check({"id": "House", "name": "House", "message_refs": ["1:2", "1:4", "1:9"]},
                            {"name": "All", "dialog_ids": [1], "keywords": ["message 4"]})
        self.assertEqual(report["projects"][0], {
            "id": "house", "name": "House", "messages": 3, "first_date": "2025-01-02", "last_date": "2025-01-09",
            "attachments": 2, "downloaded": 1, "problems": []})
        self.assertEqual(report["projects"][1]["messages"], 1)
        self.assertEqual((report["shared_messages"], report["problems"]), (1, 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM project_messages").fetchone()[0], before)
        self.assertFalse((self.root / "output").exists())

    def test_check_manifest_fails_on_problems(self):
        cases = {
            "ref 1:999: not synchronized": [{"name": "A", "message_refs": ["1:2", "1:999"]}],
            "ref 2:1: dialog is rejected": [{"name": "A", "message_refs": ["2:1", "1:1"]}],
            "ref 7:1: dialog is unknown": [{"name": "A", "message_refs": ["7:1", "1:1"]}],
            "dialog 2 is rejected, not approved": [{"name": "A", "dialog_ids": [1, 2]}],
            "no message_refs, dialog_ids or keywords": [{"name": "A"}],
            "no messages selected": [{"name": "A", "dialog_ids": [1], "keywords": ["absent"]}],
            "folder 'A' is already used by project #1": [{"name": "A", "dialog_ids": [1]}, {"id": "b", "name": "A", "dialog_ids": [1]}],
        }
        import contextlib, io
        for expected, projects in cases.items():
            output = io.StringIO()
            with self.subTest(expected), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                self.check(*projects)
            self.assertEqual(raised.exception.code, 1)
            self.assertIn(f"! {expected}", output.getvalue())
        for bad in ("{}", "not json", json.dumps({"projects": [{"id": "x"}]})):
            manifest = self.root / "bad.json"
            manifest.write_text(bad, encoding="utf-8")
            for command in (app.check_manifest, app.materialize):
                with self.assertRaises(SystemExit):
                    command(SimpleNamespace(manifest=manifest, output_dir=self.root / "never"), self.db)
        self.assertFalse((self.root / "never").exists())


    @unittest.skipUnless(_HAS_PIL, "Pillow not installed")
    def test_contact_sheets_take_images_only_filter_by_project_and_show_tags(self):
        from PIL import Image
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), "green").save(buffer, "JPEG")
        for message_id in (1, 6):
            self.messages[(1, message_id)].payload = buffer.getvalue() + bytes([message_id])
        asyncio.run(app.download_media(self.args(refs=["1:1", "1:2", "1:4", "1:6"]), self.db))
        output = self.root / "sheets"
        app.contact_sheets(SimpleNamespace(output_dir=output, columns=2, rows=2), self.db)
        records = [json.loads(line) for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["message_ref"], r["cell"], r["tag"]) for r in records], [("1:1", 1, None), ("1:6", 2, None)])
        with self.db:
            self.db.execute("""CREATE TABLE media_tags (dialog_id INTEGER, message_id INTEGER, tag TEXT, source TEXT,
                note TEXT, updated_at TEXT, PRIMARY KEY(dialog_id, message_id))""")
            self.db.execute("INSERT INTO media_tags VALUES (1,6,'render','manual','','2025')")
            self.db.execute("INSERT INTO project_messages VALUES ('house',1,6)")
        app.contact_sheets(SimpleNamespace(output_dir=output, columns=2, rows=2, project=["House"]), self.db)
        records = [json.loads(line) for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["message_ref"], r["tag"]) for r in records], [("1:6", "render")])
        with self.assertRaises(SystemExit):
            app.contact_sheets(SimpleNamespace(output_dir=output, columns=2, rows=2, project=["missing"]), self.db)


if __name__ == "__main__":
    unittest.main()
