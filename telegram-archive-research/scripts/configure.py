#!/usr/bin/env python3
"""Interactively configure and authorize a portable Telegram archive profile."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
from pathlib import Path


KEYS = (
    "TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_CHAT", "TG_PROXY_TYPE",
    "TG_PROXY_HOST", "TG_PROXY_PORT", "TG_PROXY_USER", "TG_PROXY_PASSWORD",
    "WHISPER_MODEL", "WHISPER_LANGUAGE",
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def ask(label: str, current: str = "", secret: bool = False) -> str:
    suffix = " [configured]" if secret and current else f" [{current}]" if current else ""
    value = (getpass.getpass if secret else input)(f"{label}{suffix}: ").strip()
    return value or current


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=Path.home() / "telegram-chat-exporter")
    parser.add_argument("--no-auth", action="store_true")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    env_path = target / ".env"
    if not (target / ".venv/bin/python3").exists():
        raise SystemExit(f"Run install.sh first; missing {target}/.venv")

    values = read_env(env_path)
    print("Get API ID/hash at https://my.telegram.org/apps")
    values["TG_API_ID"] = ask("Telegram API ID", values.get("TG_API_ID", ""))
    values["TG_API_HASH"] = ask("Telegram API hash", values.get("TG_API_HASH", ""), secret=True)
    values["TG_PHONE"] = ask("Phone in international format", values.get("TG_PHONE", ""))
    use_proxy = ask("Proxy type (socks5/http, empty for direct)", values.get("TG_PROXY_TYPE", ""))
    values["TG_PROXY_TYPE"] = use_proxy
    if use_proxy:
        values["TG_PROXY_HOST"] = ask("Proxy host", values.get("TG_PROXY_HOST", "127.0.0.1"))
        values["TG_PROXY_PORT"] = ask("Proxy port", values.get("TG_PROXY_PORT", ""))
        values["TG_PROXY_USER"] = ask("Proxy user (optional)", values.get("TG_PROXY_USER", ""))
        values["TG_PROXY_PASSWORD"] = ask("Proxy password (optional)", values.get("TG_PROXY_PASSWORD", ""), secret=True)
    else:
        for key in ("TG_PROXY_HOST", "TG_PROXY_PORT", "TG_PROXY_USER", "TG_PROXY_PASSWORD"):
            values[key] = ""
    values.setdefault("TG_CHAT", "")
    values.setdefault("WHISPER_MODEL", "small")
    values.setdefault("WHISPER_LANGUAGE", "ru")

    env_path.write_text("\n".join(f"{key}={values.get(key, '')}" for key in KEYS) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    print(f"Saved {env_path}")
    if not args.no_auth:
        print("Telegram will request a login code and possibly your 2FA password.")
        subprocess.run(
            [str(target / ".venv/bin/python3"), str(target / "scripts/export_chat.py"), "--list-chats"],
            cwd=target,
            check=True,
        )


if __name__ == "__main__":
    main()
