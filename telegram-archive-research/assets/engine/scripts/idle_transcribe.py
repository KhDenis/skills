"""Transcribe a small batch of selected Telegram voice/video notes while idle on AC."""

from __future__ import annotations

import asyncio
import fcntl
import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterRoundVideo, InputMessagesFilterVoice

from export_chat import Transcriber, media_info
from personal_text_archive import open_db, telegram_record, upsert_text
from tg_config import PROJECT_ROOT, telegram_credentials, telethon_proxy

IDLE_SECONDS = 5 * 60
MAX_BATTERY_TEMP_C = 40.0
MAX_SYSTEM_POWER_MW = 18_000
MIN_CPU_SPEED_PERCENT = 80
FILTERS = (("voice", InputMessagesFilterVoice), ("video_note", InputMessagesFilterRoundVideo))


def pause_reason() -> Optional[str]:
    power = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, check=True).stdout
    if "Now drawing from 'AC Power'" not in power:
        return "Mac is running on battery"
    idle = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, check=True).stdout
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', idle)
    if not match or int(match.group(1)) < IDLE_SECONDS * 1_000_000_000:
        return "Mac has been idle for less than 5 minutes"

    thermal = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, check=True).stdout
    speed = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", thermal)
    if "No thermal warning level" not in thermal:
        return "macOS reports thermal pressure"
    if speed and int(speed.group(1)) < MIN_CPU_SPEED_PERCENT:
        return f"CPU speed is thermally limited to {speed.group(1)}%"

    battery = subprocess.run(
        ["ioreg", "-r", "-c", "AppleSmartBattery", "-a"],
        capture_output=True, check=True,
    ).stdout
    entries = plistlib.loads(battery)
    if entries:
        info = entries[0]
        temperature = float(info.get("Temperature", 0)) / 100
        system_power = int(info.get("BatteryData", {}).get("SystemPower", 0))
        if temperature >= MAX_BATTERY_TEMP_C:
            return f"battery temperature is {temperature:.1f} C"
        if system_power >= MAX_SYSTEM_POWER_MW:
            return f"system power is {system_power / 1000:.1f} W"
    return None


async def run() -> None:
    reason = pause_reason()
    if reason:
        print(f"Skipped: {reason}", flush=True)
        return
    db = open_db()
    db.execute("""CREATE TABLE IF NOT EXISTS transcription_scan (
        dialog_id INTEGER NOT NULL, kind TEXT NOT NULL, oldest_seen_id INTEGER,
        last_checked_at TEXT, PRIMARY KEY(dialog_id, kind))""")
    db.commit()
    api_id, api_hash, phone = telegram_credentials()
    client = TelegramClient(str(PROJECT_ROOT / "telegram_export"), api_id, api_hash, proxy=telethon_proxy())
    try:
        await client.start(phone=phone)
        dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}
        selected = db.execute("""SELECT d.id, k.kind, s.oldest_seen_id
            FROM dialogs d CROSS JOIN (SELECT 'voice' AS kind UNION ALL SELECT 'video_note') k
            LEFT JOIN transcription_scan s ON s.dialog_id=d.id AND s.kind=k.kind
            ORDER BY COALESCE(s.last_checked_at, '') ASC, d.id ASC LIMIT 1""").fetchone()
        if not selected:
            return
        dialog_id = selected["id"]
        kind = selected["kind"] or "voice"
        dialog = dialogs.get(dialog_id)
        if dialog is None:
            print(f"Dialog {dialog_id} unavailable", flush=True)
            return
        filter_type = dict(FILTERS)[kind]
        cursor = selected["oldest_seen_id"]
        batch = [m async for m in client.iter_messages(dialog.entity, filter=filter_type(),
                  max_id=cursor or 0, limit=30)]
        if not batch:
            db.execute("""INSERT INTO transcription_scan VALUES (?, ?, NULL, datetime('now'))
                ON CONFLICT(dialog_id, kind) DO UPDATE SET oldest_seen_id=NULL,
                last_checked_at=excluded.last_checked_at""", (dialog_id, kind))
            db.commit()
            print(f"{dialog_id}/{kind}: reached oldest media; restarting next pass", flush=True)
            return
        next_cursor = min(m.id for m in batch)
        for message in batch:
            if media_info(message)[0] != kind:
                continue
            existing = db.execute("SELECT transcript FROM messages WHERE dialog_id=? AND id=?",
                                  (dialog_id, message.id)).fetchone()
            if existing and existing["transcript"]:
                continue
            reason = pause_reason()
            if reason:
                print(f"Paused before next transcription: {reason}", flush=True)
                return
            with tempfile.TemporaryDirectory(prefix="idle-transcribe-") as directory:
                path = await message.download_media(file=str(Path(directory) / f"{message.id}_"))
                if not path:
                    print("Download failed", flush=True)
                    return
                transcriber = Transcriber(os.environ.get("WHISPER_MODEL", "small"), "ru")
                # Disable the exporter's ID-only cache: IDs repeat across dialogs.
                transcriber.transcribe = _uncached_transcribe(transcriber)
                transcript = await asyncio.to_thread(transcriber.transcribe, Path(path), message.id)
            if transcript:
                record = telegram_record(message)
                record.update(transcript=transcript, media_kind=kind)
                with db:
                    upsert_text(db, dialog_id, record, priority=5)
                print(f"{dialog_id}/{message.id}/{kind}: transcribed", flush=True)
            next_cursor = message.id
            break
        db.execute("""INSERT INTO transcription_scan VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(dialog_id, kind) DO UPDATE SET oldest_seen_id=excluded.oldest_seen_id,
            last_checked_at=excluded.last_checked_at""", (dialog_id, kind, next_cursor))
        db.commit()
    finally:
        await client.disconnect()
        db.close()


def _uncached_transcribe(transcriber):
    def transcribe(path: Path, _message_id: int) -> str:
        backend, model = transcriber._load()
        if backend == "faster-whisper":
            segments, _ = model.transcribe(str(path), language="ru")
            return " ".join(segment.text.strip() for segment in segments).strip()
        return str(model.transcribe(str(path), language="ru").get("text", "")).strip()
    return transcribe


if __name__ == "__main__":
    lock_path = PROJECT_ROOT / "data" / "idle_transcribe.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Skipped: previous transcription still running", flush=True)
        else:
            os.environ.setdefault("OMP_NUM_THREADS", "2")
            os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
            os.environ.setdefault("MKL_NUM_THREADS", "2")
            os.nice(15)
            asyncio.run(run())
