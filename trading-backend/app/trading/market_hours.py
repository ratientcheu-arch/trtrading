"""
Market session awareness — determines if a market is currently open.
Ported from App.tsx MARKET_HOURS constant.
"""
from datetime import datetime, time
import pytz

# All times in CET (Europe/Paris) unless noted
CET = pytz.timezone("Europe/Paris")

MARKET_HOURS = {
    "EU": {"open": time(9, 0), "close": time(17, 30), "label": "Euronext Paris", "weekdays_only": True},
    "US": {"open": time(15, 30), "close": time(22, 0), "label": "NYSE / NASDAQ", "weekdays_only": True},
    "JP": {"open": time(1, 0), "close": time(7, 30), "label": "Tokyo (TSE/OSE)", "weekdays_only": True},
    "ASIA": {"open": time(1, 0), "close": time(7, 30), "label": "Asian Markets", "weekdays_only": True},
    "CRYPTO": {"open": time(0, 0), "close": time(23, 59), "label": "Crypto (24/7)", "weekdays_only": False},
    "FOREX": {"open": time(0, 0), "close": time(23, 59), "label": "Forex (24/5)", "weekdays_only": True},
    "COMMODITY": {"open": time(1, 5), "close": time(23, 55), "label": "Matieres Premieres (Or/Petrole)", "weekdays_only": True},
}

# Indices → per-symbol market mapping (used by is_index_market_open)
INDEX_MARKET = {
    "DAX40": "EU", "CAC40": "EU",
    "SP500": "US", "NASDAQ": "US", "DJ30": "US",
    "NKY": "JP", "HK50": "ASIA", "AUS200": "ASIA",
    "UK100": "EU",
}


def is_market_open(market: str) -> bool:
    now = datetime.now(CET)
    hours = MARKET_HOURS.get(market)
    if not hours:
        return False

    # Weekend check
    if hours["weekdays_only"] and now.weekday() >= 5:
        return False

    current_time = now.time()
    return hours["open"] <= current_time <= hours["close"]


def get_open_markets() -> list[str]:
    return [m for m in MARKET_HOURS if is_market_open(m)]


def is_market_closing_soon(market: str, minutes: int = 15) -> bool:
    """Check if a market is closing within the next N minutes."""
    from datetime import timedelta
    now = datetime.now(CET)
    hours = MARKET_HOURS.get(market)
    if not hours:
        return False
    if hours["weekdays_only"] and now.weekday() >= 5:
        return False

    close_dt = now.replace(hour=hours["close"].hour, minute=hours["close"].minute, second=0)
    time_to_close = (close_dt - now).total_seconds() / 60
    return 0 < time_to_close <= minutes


def just_closed(market: str, minutes: int = 5) -> bool:
    """Check if a market just closed within the last N minutes."""
    from datetime import timedelta
    now = datetime.now(CET)
    hours = MARKET_HOURS.get(market)
    if not hours:
        return False
    if hours["weekdays_only"] and now.weekday() >= 5:
        return False

    close_dt = now.replace(hour=hours["close"].hour, minute=hours["close"].minute, second=0)
    time_since_close = (now - close_dt).total_seconds() / 60
    return 0 < time_since_close <= minutes
