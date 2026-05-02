#!/usr/bin/env python3
"""
auto-login.py — Fallback helper if MT5's /config:/config.ini flag fails.

Watches the MT5 login dialog via xdotool and injects credentials automatically.
Used only as a safety net; the primary path is the /config.ini loaded at startup.
"""
import os
import subprocess
import time
import sys

LOGIN = os.environ.get("MT5_LOGIN")
PASSWORD = os.environ.get("MT5_PASSWORD")
SERVER = os.environ.get("MT5_SERVER")

if not all([LOGIN, PASSWORD, SERVER]):
    print("[auto-login] Missing MT5_LOGIN/PASSWORD/SERVER", file=sys.stderr)
    sys.exit(1)


def find_window(name: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", name],
            env={**os.environ, "DISPLAY": ":99"},
            timeout=5,
        ).decode().strip()
        return out.splitlines()[0] if out else None
    except subprocess.CalledProcessError:
        return None


def type_into_window(win_id: str, text: str):
    subprocess.run(
        ["xdotool", "windowactivate", "--sync", win_id, "type", "--delay", "30", text],
        env={**os.environ, "DISPLAY": ":99"},
        check=False,
    )


def press_key(key: str):
    subprocess.run(
        ["xdotool", "key", key],
        env={**os.environ, "DISPLAY": ":99"},
        check=False,
    )


def main():
    print("[auto-login] Watching for MT5 login dialog...")
    deadline = time.time() + 120
    while time.time() < deadline:
        win = find_window("Open an Account") or find_window("Login")
        if win:
            print(f"[auto-login] Login window detected (id={win}), injecting...")
            time.sleep(1)
            type_into_window(win, LOGIN)
            press_key("Tab")
            type_into_window(win, PASSWORD)
            press_key("Tab")
            type_into_window(win, SERVER)
            press_key("Return")
            print("[auto-login] Credentials submitted.")
            return
        time.sleep(2)
    print("[auto-login] Timed out waiting for login dialog.")


if __name__ == "__main__":
    main()
