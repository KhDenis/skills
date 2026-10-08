from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    Message,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaVenue,
    User,
)
from tqdm import tqdm

from tg_config import PROJECT_ROOT, env, load_project_env, telegram_credentials, telethon_proxy


DATA_DIR = PROJECT_ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
OUTPUT_DIR = PROJECT_ROOT / "output"


@dataclass
class ExportedMessage:
    id: int
    date: str
    sender_id: Optional[int]
    sender: str
    text: str
    reply_to_msg_id: Optional[int]
    media_kind: Optional[str]
    media_path: Optional[str]
    duration_seconds: Optional[int]
    transcript: Optional[str]
    attachment: Optional[str]


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_")
    return value[:120] or "file"


def entity_name(entity: Any) -> str:
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(part for part in parts if part).strip()
        return name or entity.username or f"user:{entity.id}"
    if isinstance(entity, (Chat, Channel)):
        return entity.title or getattr(entity, "username", None) or f"chat:{entity.id}"
    return str(getattr(entity, "id", "unknown"))


def sender_display(sender: Any, fallback_id: Optional[int]) -> str:
    if sender is None:
        return f"id:{fallback_id}" if fallback_id else "Unknown"
    return entity_name(sender)


def media_info(message: Message) -> tuple[Optional[str], Optional[int], Optional[str]]:
    media = message.media
    if media is None:
        return None, None, None

    if isinstance(media, MessageMediaPhoto):
        return "photo", None, "Photo"
    if isinstance(media, MessageMediaContact):
        return "contact", None, "Contact"
    if isinstance(media, MessageMediaGeo):
        return "geo", None, "Geo location"
    if isinstance(media, MessageMediaVenue):
        return "venue", None, "Venue"
    if isinstance(media, MessageMediaPoll):
        return "poll", None, "Poll"

    if isinstance(media, MessageMediaDocument) and media.document:
        duration = None
        filename = None
        is_voice = False
        is_video_note = False
        is_video = False
        is_audio = False

        for attr in media.document.attributes:
            if isinstance(attr, DocumentAttributeAudio):
                duration = attr.duration
                is_voice = bool(attr.voice)
                is_audio = True
            elif isinstance(attr, DocumentAttributeVideo):
                duration = attr.duration
                is_video_note = bool(attr.round_message)
                is_video = True
            elif isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name

        if is_voice:
            return "voice", duration, "Voice message"
        if is_video_note:
            return "video_note", duration, "Video note"
        if is_video:
            return "video", duration, filename or "Video"
        if is_audio:
            return "audio", duration, filename or "Audio"
        return "document", duration, filename or "Document"

    return type(media).__name__, None, type(media).__name__


def duration_label(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    return f" · {minutes:02d}:{sec:02d}"


def media_label(kind: Optional[str]) -> str:
    return {
        "voice": "Голосовое",
        "video_note": "Кружок",
        "photo": "Фото",
        "video": "Видео",
        "audio": "Аудио",
        "document": "Файл",
        "contact": "Контакт",
        "geo": "Геолокация",
        "venue": "Место",
        "poll": "Опрос",
    }.get(kind or "", kind or "Вложение")


def record_from_dict(data: dict[str, Any]) -> ExportedMessage:
    fields = {field: data.get(field) for field in ExportedMessage.__dataclass_fields__}
    return ExportedMessage(**fields)


def load_checkpoint(path: Path) -> dict[int, ExportedMessage]:
    records: dict[int, ExportedMessage] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = record_from_dict(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
            records[record.id] = record
    return records


def append_checkpoint(path: Path, record: ExportedMessage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        fh.flush()


async def list_chats(client: TelegramClient) -> None:
    print("Dialog id | Type | Title | Username")
    print("-" * 80)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        username = getattr(entity, "username", "") or ""
        print(f"{dialog.id} | {type(entity).__name__} | {dialog.name} | {username}")


async def resolve_chat(client: TelegramClient, chat_ref: str):
    try:
        return await client.get_entity(int(chat_ref))
    except (ValueError, TypeError):
        pass
    except Exception:
        pass

    try:
        return await client.get_entity(chat_ref)
    except Exception:
        pass

    needle = chat_ref.strip().lower()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        values = [
            str(dialog.id),
            dialog.name or "",
            getattr(entity, "username", "") or "",
        ]
        if any(value.lower() == needle for value in values):
            return entity

    raise SystemExit(f"Could not find chat: {chat_ref!r}. Run with --list-chats first.")


async def download_transcribable_media(message: Message, kind: Optional[str]) -> Optional[Path]:
    if kind not in {"voice", "video_note"}:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = MEDIA_DIR / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = await message.download_media(file=str(target_dir / f"{message.id}_"))
    return Path(downloaded) if downloaded else None


class Transcriber:
    def __init__(self, model_name: str, language: str):
        self.model_name = model_name
        self.language = language or None
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel

                self._model = ("faster-whisper", WhisperModel(self.model_name, device="cpu", compute_type="int8"))
                return self._model
            except ImportError:
                pass

            try:
                import imageio_ffmpeg

                ffmpeg_path = Path(imageio_ffmpeg.get_ffmpeg_exe())
                bin_dir = DATA_DIR / "bin"
                bin_dir.mkdir(parents=True, exist_ok=True)
                ffmpeg_link = bin_dir / "ffmpeg"
                if not ffmpeg_link.exists():
                    try:
                        os.symlink(ffmpeg_path, ffmpeg_link)
                    except OSError:
                        shutil.copy2(ffmpeg_path, ffmpeg_link)
                        ffmpeg_link.chmod(0o755)
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{ffmpeg_path.parent}{os.pathsep}{os.environ.get('PATH', '')}"
            except ImportError:
                pass

            try:
                import whisper
            except ImportError as exc:
                raise SystemExit(
                    "Transcription requires Whisper dependencies. Install them with: "
                    "pip install -r requirements-transcribe.txt"
                ) from exc
            self._model = ("openai-whisper", whisper.load_model(self.model_name))
        return self._model

    def transcribe(self, media_path: Path, message_id: int) -> str:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        model_key = safe_name(self.model_name)
        language_key = safe_name(self.language or "auto")
        cache_path = TRANSCRIPT_DIR / f"{message_id}.{model_key}.{language_key}.txt"
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8").strip()

        backend, model = self._load()
        if backend == "faster-whisper":
            segments, _info = model.transcribe(str(media_path), language=self.language)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        else:
            result = model.transcribe(str(media_path), language=self.language)
            text = str(result.get("text", "")).strip()
        cache_path.write_text(text + "\n", encoding="utf-8")
        return text


async def export_chat(args: argparse.Namespace) -> None:
    load_project_env()
    api_id, api_hash, phone = telegram_credentials()
    proxy = telethon_proxy()

    session_name = str(PROJECT_ROOT / "telegram_export")
    client = TelegramClient(session_name, api_id, api_hash, proxy=proxy)

    await client.start(phone=phone)

    if args.list_chats:
        await list_chats(client)
        await client.disconnect()
        return

    chat_ref = args.chat or env("TG_CHAT")
    if not chat_ref:
        raise SystemExit("Pass --chat or set TG_CHAT in .env. Use --list-chats to find it.")

    entity = await resolve_chat(client, chat_ref)
    chat_title = entity_name(entity)
    chat_slug = safe_name(chat_title)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = CHECKPOINT_DIR / f"{chat_slug}.jsonl"

    transcriber = None
    if not args.no_transcribe:
        transcriber = Transcriber(args.whisper_model or env("WHISPER_MODEL", "small"), args.language or env("WHISPER_LANGUAGE"))

    records_by_id = load_checkpoint(checkpoint_path)
    iterator = client.iter_messages(entity, limit=args.limit, reverse=True)
    progress = tqdm(desc="Exporting messages", unit="msg")

    async for message in iterator:
        progress.update(1)
        if not isinstance(message, Message):
            continue
        if message.id in records_by_id:
            continue

        sender = await message.get_sender()
        sender_name = sender_display(sender, message.sender_id)
        kind, duration, attachment = media_info(message)
        media_path = await download_transcribable_media(message, kind) if args.download_media else None

        transcript = None
        if transcriber and media_path:
            progress.set_postfix_str(f"transcribing {message.id}")
            transcript = transcriber.transcribe(media_path, message.id)
            progress.set_postfix_str("")

        record = ExportedMessage(
            id=message.id,
            date=message.date.isoformat(),
            sender_id=message.sender_id,
            sender=sender_name,
            text=message.message or "",
            reply_to_msg_id=message.reply_to_msg_id,
            media_kind=kind,
            media_path=str(media_path.relative_to(PROJECT_ROOT)) if media_path else None,
            duration_seconds=duration,
            transcript=transcript,
            attachment=attachment,
        )
        records_by_id[record.id] = record
        append_checkpoint(checkpoint_path, record)

    progress.close()
    await client.disconnect()

    jsonl_path = OUTPUT_DIR / "full_chat.jsonl"
    md_path = OUTPUT_DIR / "full_chat.md"
    records = sorted(records_by_id.values(), key=lambda record: (record.date, record.id))

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    write_markdown(md_path, records, chat_title)
    print(f"Wrote {jsonl_path}")
    print(f"Wrote {md_path}")
    print(f"Messages exported: {len(records)}")


def write_markdown(path: Path, records: list[ExportedMessage], chat_title: str) -> None:
    lines: list[str] = [f"# {chat_title}", ""]
    current_day = None

    for record in records:
        dt = datetime.fromisoformat(record.date)
        day = dt.date().isoformat()
        if day != current_day:
            current_day = day
            lines.extend([f"## {day}", ""])

        reply = f" · reply_to: {record.reply_to_msg_id}" if record.reply_to_msg_id else ""
        lines.extend([f"### {dt:%H:%M} — {record.sender}", ""])

        if record.media_kind:
            label = media_label(record.media_kind)
            lines.append(f"[{label}{duration_label(record.duration_seconds)} · message_id: {record.id}{reply}]")
            if record.attachment and record.media_kind not in {"voice", "video_note"}:
                lines.append(f"[Вложение: {record.attachment}]")
            if record.media_path:
                lines.append(f"[Файл: {record.media_path}]")
            lines.append("")
        elif reply:
            lines.extend([f"[message_id: {record.id}{reply}]", ""])

        if record.text:
            lines.extend([record.text.strip(), ""])
        if record.transcript:
            lines.extend([record.transcript.strip(), ""])
        if not record.text and not record.transcript and not record.media_kind:
            lines.extend([f"[Пустое/служебное сообщение · message_id: {record.id}]", ""])

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export one Telegram chat with voice/video-note transcription.")
    parser.add_argument("--list-chats", action="store_true", help="List available dialogs and exit.")
    parser.add_argument("--chat", help="Dialog id, username, or exact title.")
    parser.add_argument("--limit", type=int, help="Limit messages for a test run. Omit for full history.")
    parser.add_argument("--no-transcribe", action="store_true", help="Skip Whisper transcription.")
    parser.add_argument("--no-download-media", dest="download_media", action="store_false", help="Do not download media.")
    parser.add_argument("--whisper-model", help="Whisper model name. Overrides WHISPER_MODEL.")
    parser.add_argument("--language", help="Whisper language code. Overrides WHISPER_LANGUAGE.")
    parser.set_defaults(download_media=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(export_chat(args))


if __name__ == "__main__":
    main()
