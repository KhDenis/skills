"""Subprocess transport fixture: no Telegram connection or credentials."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ENGINE / 'scripts'), str(ENGINE / 'tests')]
import work_project_archive as app
from test_work_project_archive import MediaClient, MediaMessage, fake_photo

root = Path(sys.argv[1])
app.PROJECT_ROOT = root
app.MEDIA_ROOT = root / 'media'
mode = sys.argv[2]

class Message(MediaMessage):
    async def download_media(self, file=None):
        with (root / 'requests.txt').open('a') as handle:
            handle.write(str(self.id) + '\n')
        if mode == 'pause' and self.id == 2:
            (Path(file) / 'incomplete.jpg').write_bytes(b'partial')
            (root / 'paused').write_text('second transfer is incomplete')
            await asyncio.Event().wait()
        return await super().download_media(file=file)

messages = {(1, n): Message(n, None, None, 1, 'synthetic', fake_photo(10), payload=f'complete-{n}'.encode())
            for n in (1, 2)}
async def client():
    return MediaClient(messages)
app.telegram_client = client
args = SimpleNamespace(project=None, refs=['1:1', '1:2', '2:1', '3:1'], refs_file=None,
                       around=0, kinds='photo', confirm=True, max_items=5, max_bytes=1000)
db = app.open_db(root / 'archive.sqlite3')
try:
    asyncio.run(app.download_media(args, db))
finally:
    db.close()
