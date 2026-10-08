from __future__ import annotations

import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

from tg_config import load_project_env, telethon_proxy


TELEGRAM_DCS = [
    ("149.154.167.51", 443),
    ("149.154.167.91", 443),
    ("91.108.56.130", 443),
]


def run(args: list[str], timeout: int = 8) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    out = (proc.stdout + proc.stderr).strip()
    return out or f"exit={proc.returncode}"


def tcp_connect(host: str, port: int, timeout: float = 8.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "ok"
    except OSError as exc:
        return f"failed: {exc}"


def socks_tcp_connect(host: str, port: int, proxy_host: str, proxy_port: int, timeout: float = 8.0) -> str:
    try:
        import socks
    except ImportError:
        return "PySocks not installed"

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port, rdns=True)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return "ok"
    except OSError as exc:
        return f"failed: {exc}"
    finally:
        sock.close()


def https_head(host: str, timeout: float = 8.0) -> str:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                request = f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                ssock.sendall(request.encode("ascii"))
                response = ssock.recv(160).decode("iso-8859-1", errors="replace")
                return response.splitlines()[0] if response else "empty response"
    except OSError as exc:
        return f"failed: {exc}"


def curl_head(args: list[str]) -> str:
    if not shutil.which("curl"):
        return "curl not found"
    output = run(["curl", "-I", "--connect-timeout", "10", *args], timeout=15)
    return "\n".join(output.splitlines()[:8])


def main() -> None:
    load_project_env()

    print("== System ==")
    print(f"OS: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Project: {Path(__file__).resolve().parents[1]}")
    print()

    print("== Tools ==")
    for name in ("git", "ffmpeg", "curl"):
        path = shutil.which(name)
        print(f"{name}: {path or 'not found'}")
        if path and name in {"git", "ffmpeg"}:
            print(run([name, "--version"], timeout=5).splitlines()[0])
    print()

    print("== Proxy environment ==")
    found_env = False
    for key, value in sorted(os.environ.items()):
        if "proxy" in key.lower():
            found_env = True
            print(f"{key}={value}")
    if not found_env:
        print("No proxy environment variables found.")
    print()

    print("== macOS system proxy ==")
    if sys.platform == "darwin":
        print(run(["scutil", "--proxy"]))
    else:
        print("Not macOS.")
    print()

    print("== Direct terminal connectivity ==")
    print(f"telegram.org HTTPS: {https_head('telegram.org')}")
    for host, port in TELEGRAM_DCS:
        print(f"{host}:{port}: {tcp_connect(host, port)}")
    print()

    print("== MTProto TCP via SOCKS5 127.0.0.1:1080 ==")
    for host, port in TELEGRAM_DCS:
        print(f"{host}:{port}: {socks_tcp_connect(host, port, '127.0.0.1', 1080)}")
    print()

    print("== curl via local proxy candidates ==")
    print("-- SOCKS5 127.0.0.1:1080 --")
    print(curl_head(["--socks5-hostname", "127.0.0.1:1080", "https://telegram.org"]))
    print("-- HTTP 127.0.0.1:1087 --")
    print(curl_head(["--proxy", "http://127.0.0.1:1087", "https://telegram.org"]))
    print()

    print("== Telethon proxy config ==")
    try:
        proxy = telethon_proxy()
        if proxy:
            print(f"Configured via TG_PROXY_*: type={proxy[0]} host={proxy[1]} port={proxy[2]}")
        else:
            print("No TG_PROXY_* configured.")
    except SystemExit as exc:
        print(exc)


if __name__ == "__main__":
    main()
