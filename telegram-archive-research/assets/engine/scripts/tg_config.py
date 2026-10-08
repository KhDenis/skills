from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_project_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def telegram_credentials() -> tuple[int, str, str]:
    load_project_env()
    api_id_raw = required_env("TG_API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_API_ID must be an integer") from exc
    return api_id, required_env("TG_API_HASH"), required_env("TG_PHONE")


def telethon_proxy() -> Optional[Tuple]:
    load_project_env()
    proxy_type = env("TG_PROXY_TYPE").lower()
    host = env("TG_PROXY_HOST")
    port_raw = env("TG_PROXY_PORT")
    if not proxy_type or not host or not port_raw:
        return None

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit("TG_PROXY_PORT must be an integer") from exc

    try:
        import socks
    except ImportError as exc:
        raise SystemExit("Install PySocks first: pip install PySocks") from exc

    proxy_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }
    if proxy_type not in proxy_map:
        raise SystemExit("TG_PROXY_TYPE must be one of: socks5, socks4, http, https")

    user = env("TG_PROXY_USER") or None
    password = env("TG_PROXY_PASSWORD") or None
    return (proxy_map[proxy_type], host, port, True, user, password)
