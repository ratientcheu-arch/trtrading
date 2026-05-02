"""
IPC (Inter-Process Communication) utilities for the two-process architecture.

The trading system runs as TWO separate processes:
  1. API process (uvicorn) — serves HTTP/WS, reads from shared files and DB
  2. Bot process (bot_runner) — talks to MT5 via ZMQ, writes to shared files and DB

Communication:
  - Bot → API: JSON files in IPC_DIR (status, account, positions, signals, config, etc.)
  - API → Bot: Command files (cmd_*.json) with response files (resp_*.json)
"""
import os
import json
import time
import uuid
import glob
import asyncio
from typing import Optional

IPC_DIR = os.environ.get("BOT_IPC_DIR", "/tmp")

# ── State files (written by bot, read by API) ────────────────────────────

STATUS_FILE = os.path.join(IPC_DIR, "bot_status.json")
ACCOUNT_FILE = os.path.join(IPC_DIR, "bot_account.json")
POSITIONS_FILE = os.path.join(IPC_DIR, "bot_positions.json")
SIGNALS_FILE = os.path.join(IPC_DIR, "bot_signals.json")
CONFIG_FILE = os.path.join(IPC_DIR, "bot_config.json")
DAILY_FILE = os.path.join(IPC_DIR, "bot_daily.json")
TRADES_FILE = os.path.join(IPC_DIR, "bot_trades.json")
OPERATOR_FILE = os.path.join(IPC_DIR, "bot_operator.json")
EVENTS_FILE = os.path.join(IPC_DIR, "bot_events.jsonl")
MANUAL_BALANCE_FILE = os.path.join(IPC_DIR, "bot_manual_balance.json")
# 2026-04-23 : persistance des garde-fous à travers les restarts du bot
GUARDS_FILE = os.path.join(IPC_DIR, "bot_guards.json")


def write_json(path: str, data) -> None:
    """Atomic write: write to .tmp then rename (prevents partial reads)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def read_json(path: str, default=None):
    """Read JSON file, return default on any error."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


# ── Command / Response IPC (API → Bot → API) ─────────────────────────────

async def send_command(cmd: str, params: Optional[dict] = None, timeout: float = 15) -> dict:
    """Send a command to the bot process and wait for response (async, non-blocking)."""
    cmd_id = uuid.uuid4().hex[:8]
    cmd_file = os.path.join(IPC_DIR, f"cmd_{cmd_id}.json")
    resp_file = os.path.join(IPC_DIR, f"resp_{cmd_id}.json")

    write_json(cmd_file, {
        "id": cmd_id,
        "cmd": cmd,
        "params": params or {},
        "ts": time.time(),
    })

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(resp_file):
            result = read_json(resp_file, {"success": False, "error": "Parse error"})
            # Cleanup
            _safe_remove(cmd_file)
            _safe_remove(resp_file)
            return result
        await asyncio.sleep(0.2)

    # Timeout — cleanup stale command
    _safe_remove(cmd_file)
    return {"success": False, "error": "Timeout — le bot ne repond pas"}


def poll_commands() -> list[dict]:
    """Get all pending command files (bot side)."""
    pattern = os.path.join(IPC_DIR, "cmd_*.json")
    cmds = []
    for f in sorted(glob.glob(pattern)):
        data = read_json(f)
        if data:
            cmds.append(data)
    return cmds


def send_response(cmd_id: str, success: bool, data=None, error=None) -> None:
    """Write a response file for a command (bot side)."""
    resp_file = os.path.join(IPC_DIR, f"resp_{cmd_id}.json")
    write_json(resp_file, {
        "id": cmd_id,
        "success": success,
        "data": data,
        "error": error,
    })
    # Remove the command file
    cmd_file = os.path.join(IPC_DIR, f"cmd_{cmd_id}.json")
    _safe_remove(cmd_file)


def append_event(event_type: str, data: dict) -> None:
    """Append a WebSocket event for the API to pick up and broadcast."""
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps({"type": event_type, "data": data, "ts": time.time()}) + "\n")
    except Exception:
        pass


def read_new_events(last_ts: float) -> tuple[list[dict], float]:
    """Read events newer than last_ts. Returns (events, new_last_ts)."""
    events = []
    new_ts = last_ts
    try:
        with open(EVENTS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if evt.get("ts", 0) > last_ts:
                        events.append(evt)
                        new_ts = max(new_ts, evt["ts"])
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return events, new_ts


def truncate_events_file() -> None:
    """Truncate events file (call periodically to prevent unbounded growth)."""
    try:
        # Keep only last 5 minutes of events
        cutoff = time.time() - 300
        kept = []
        with open(EVENTS_FILE, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line.strip())
                    if evt.get("ts", 0) > cutoff:
                        kept.append(line)
                except (json.JSONDecodeError, ValueError):
                    continue
        with open(EVENTS_FILE, "w") as f:
            f.writelines(kept)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def cleanup_stale_commands(max_age: float = 60) -> None:
    """Remove command files older than max_age seconds (bot startup cleanup)."""
    pattern = os.path.join(IPC_DIR, "cmd_*.json")
    now = time.time()
    for f in glob.glob(pattern):
        data = read_json(f)
        if data and now - data.get("ts", 0) > max_age:
            _safe_remove(f)
    # Also clean up orphaned response files
    for f in glob.glob(os.path.join(IPC_DIR, "resp_*.json")):
        try:
            mtime = os.path.getmtime(f)
            if now - mtime > max_age:
                _safe_remove(f)
        except OSError:
            pass
