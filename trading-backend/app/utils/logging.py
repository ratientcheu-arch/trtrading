import logging
import logging.handlers
import os
import sys
import json
import time
from typing import Any
from app.config import settings


# Directory for persistent logs (overridable via env LOG_DIR)
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/trading-bot")
MAIN_LOG = "bot.log"
TIMING_LOG = "timing.jsonl"


def _ensure_log_dir() -> str:
    """Create log dir if missing; fall back to /tmp if unwritable."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        # Write-test
        test = os.path.join(LOG_DIR, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return LOG_DIR
    except Exception:
        fallback = "/tmp/trading-bot-logs"
        os.makedirs(fallback, exist_ok=True)
        return fallback


def setup_logging():
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Stdout handler (systemd/docker) ─────────────────────────────────
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    # ── Rotating file handler — 10 MB × 10 files = 100 MB retention ────
    log_dir = _ensure_log_dir()
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, MAIN_LOG),
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)

    # Quiet noisy libs
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # Log setup info once
    logging.getLogger(__name__).info(
        f"Logging initialized: level={settings.log_level} log_dir={log_dir}"
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── Structured timing log (JSONL, grep-/pandas-friendly) ───────────────
_timing_logger: logging.Logger | None = None


def _get_timing_logger() -> logging.Logger:
    global _timing_logger
    if _timing_logger is not None:
        return _timing_logger
    lg = logging.getLogger("trading.timing")
    lg.setLevel(logging.INFO)
    lg.propagate = False  # Do not duplicate into main log
    log_dir = _ensure_log_dir()
    h = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, TIMING_LOG),
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    # Raw JSONL — one event per line
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    _timing_logger = lg
    return lg


def log_timing(event: str, **fields: Any) -> None:
    """Emit a structured timing event as JSONL.

    Usage:
        log_timing("signal_detected", symbol="EURUSD", confidence=85, signal_ts=1712345.6)
        log_timing("order_sent", symbol="EURUSD", signal_ts=..., sent_ts=..., latency_ms=2300)
    """
    try:
        payload = {"ts": time.time(), "event": event, **fields}
        _get_timing_logger().info(json.dumps(payload, default=str))
    except Exception:
        # Never let logging break a live trade
        pass
