"""Transcribe every audio file under a directory with faster-whisper; one JSON line per file.

Standalone on purpose: it runs on a compute node that has no archive, no Telegram session and no profile.
Finished files are skipped on a re-run.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

AUDIO_SUFFIXES = {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".mp4", ".aac", ".flac"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSONL file, appended")
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--beam", type=int, default=5)
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    done = set()
    if args.output.exists():
        done = {json.loads(line)["path"] for line in args.output.read_text(encoding="utf-8").splitlines() if line}
    files = sorted(path for path in args.input.rglob("*")
                   if path.suffix.casefold() in AUDIO_SUFFIXES and str(path.relative_to(args.input)) not in done)
    print(f"{len(files)} files to transcribe with {args.model}; {len(done)} already done", flush=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with args.output.open("a", encoding="utf-8") as handle:
        for index, path in enumerate(files, 1):
            begin = time.monotonic()
            record = {"path": str(path.relative_to(args.input)), "model": args.model}
            try:
                segments, info = model.transcribe(str(path), language=args.language, beam_size=args.beam,
                                                  vad_filter=True, condition_on_previous_text=False)
                segments = list(segments)
            except Exception as exc:  # a broken file must not stop the batch
                record["error"] = type(exc).__name__
            else:
                weight = sum(segment.end - segment.start for segment in segments) or 1.0
                for name in ("avg_logprob", "no_speech_prob", "compression_ratio"):
                    record[name] = (sum(getattr(segment, name) * (segment.end - segment.start)
                                        for segment in segments) / weight) if segments else None
                record["language"] = info.language
                record["text"] = " ".join(segment.text.strip() for segment in segments).strip()
            record["seconds"] = round(time.monotonic() - begin, 2)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if index % 50 == 0:
                print(f"[{index}/{len(files)}] {time.monotonic() - started:.0f}s", flush=True)
    print(f"Done in {time.monotonic() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
