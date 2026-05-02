"""
News embargo — blocks trading around major macro announcements.

2026-04-18: Ajout pour éviter les spread explosions lors des annonces Core PCE, BoJ, GDP, etc.

Embargo window:
- HIGH impact : 15 min avant → 15 min après (30 min total)
- MEDIUM impact : 5 min avant → 10 min après (15 min total)

To update: ajouter les événements dans NEWS_EVENTS avec leur heure UTC (pas Paris).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional


# Format: (datetime UTC, "event name", "impact", affected_pairs_pattern or None=all)
# Paris time = UTC + 2 (CEST) en avril
# Mercredi 14h30 Paris = 12h30 UTC, etc.
NEWS_EVENTS = [
    # ─── Semaine 20-26 avril 2026 ───
    # Lundi 20 avril
    {"time": datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc), "name": "PMI flash Eurozone", "impact": "medium", "pairs": ["EUR"]},
    {"time": datetime(2026, 4, 20, 8, 30, tzinfo=timezone.utc), "name": "PMI flash UK", "impact": "medium", "pairs": ["GBP"]},
    {"time": datetime(2026, 4, 20, 13, 45, tzinfo=timezone.utc), "name": "PMI flash US", "impact": "medium", "pairs": ["USD"]},

    # Mardi 21 avril
    {"time": datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc), "name": "IFO Germany", "impact": "medium", "pairs": ["EUR"]},
    {"time": datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc), "name": "New Home Sales US", "impact": "medium", "pairs": ["USD"]},

    # Mercredi 22 avril — GROSSE journée macro
    {"time": datetime(2026, 4, 22, 6, 0, tzinfo=timezone.utc), "name": "German GDP flash", "impact": "medium", "pairs": ["EUR"]},
    {"time": datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc), "name": "Core PCE US", "impact": "high", "pairs": None},  # ALL
    {"time": datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc), "name": "Consumer Confidence US", "impact": "medium", "pairs": ["USD"]},

    # Jeudi 23 avril — BoJ + GDP US
    {"time": datetime(2026, 4, 23, 3, 0, tzinfo=timezone.utc), "name": "BoJ Decision", "impact": "high", "pairs": ["JPY"]},
    {"time": datetime(2026, 4, 23, 11, 30, tzinfo=timezone.utc), "name": "GDP US Q1 preliminary", "impact": "high", "pairs": None},  # ALL
    {"time": datetime(2026, 4, 23, 12, 30, tzinfo=timezone.utc), "name": "Jobless Claims", "impact": "medium", "pairs": ["USD"]},

    # Vendredi 24 avril
    {"time": datetime(2026, 4, 24, 12, 30, tzinfo=timezone.utc), "name": "Chicago PMI", "impact": "medium", "pairs": ["USD"]},
    {"time": datetime(2026, 4, 24, 14, 0, tzinfo=timezone.utc), "name": "Michigan Consumer Sentiment", "impact": "medium", "pairs": ["USD"]},
    {"time": datetime(2026, 4, 24, 12, 30, tzinfo=timezone.utc), "name": "Canada GDP", "impact": "medium", "pairs": ["CAD"]},
]


def _embargo_window_minutes(impact: str) -> tuple[int, int]:
    """Returns (minutes_before, minutes_after) for given impact level."""
    if impact == "high":
        return 15, 15
    if impact == "medium":
        return 5, 10
    return 3, 5  # low


def is_in_news_embargo(
    symbol: str, now_utc: Optional[datetime] = None
) -> tuple[bool, str]:
    """
    Check if the current time is within an embargo window for a given symbol.
    Returns (blocked: bool, reason: str).

    - If symbol's base or quote currency is in event.pairs → blocked
    - If event.pairs is None → blocks all pairs (major events like Core PCE, GDP US)
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Extract base and quote currency from symbol (e.g. "EUR/USD" → ["EUR","USD"])
    sym_clean = symbol.replace("/", "").upper()
    # For indices, use market region
    INDEX_CCY = {
        "DAX40": ["EUR"], "CAC40": ["EUR"], "UK100": ["GBP"],
        "NKY": ["JPY"], "HK50": ["HKD", "CNY"], "AUS200": ["AUD"],
        "SP500": ["USD"], "NASDAQ": ["USD"], "DJ30": ["USD"],
    }
    if sym_clean in INDEX_CCY:
        sym_currencies = INDEX_CCY[sym_clean]
    elif len(sym_clean) == 6 and sym_clean.isalpha():
        sym_currencies = [sym_clean[:3], sym_clean[3:]]
    else:
        sym_currencies = []

    for event in NEWS_EVENTS:
        before_min, after_min = _embargo_window_minutes(event["impact"])
        start = event["time"] - timedelta(minutes=before_min)
        end = event["time"] + timedelta(minutes=after_min)
        if start <= now_utc <= end:
            # Check if this event affects this symbol
            pairs_affected = event.get("pairs")
            if pairs_affected is None:
                # High-impact global event affects everything
                mins_until = (event["time"] - now_utc).total_seconds() / 60
                tense = f"dans {mins_until:+.0f} min" if mins_until > 0 else f"il y a {abs(mins_until):.0f} min"
                return True, f"NEWS EMBARGO: {event['name']} ({event['impact'].upper()}) {tense} — bloque TOUS"
            # Otherwise, check currency overlap
            if any(ccy in pairs_affected for ccy in sym_currencies):
                mins_until = (event["time"] - now_utc).total_seconds() / 60
                tense = f"dans {mins_until:+.0f} min" if mins_until > 0 else f"il y a {abs(mins_until):.0f} min"
                return True, f"NEWS EMBARGO: {event['name']} ({event['impact'].upper()}) {tense} — impact {','.join(pairs_affected)}"
    return False, ""


def next_event_info(now_utc: Optional[datetime] = None) -> Optional[dict]:
    """Returns info about the next upcoming news event (for logging/dashboard)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    upcoming = [e for e in NEWS_EVENTS if e["time"] > now_utc]
    if not upcoming:
        return None
    upcoming.sort(key=lambda e: e["time"])
    next_ev = upcoming[0]
    mins_until = (next_ev["time"] - now_utc).total_seconds() / 60
    return {
        "name": next_ev["name"],
        "time_utc": next_ev["time"].isoformat(),
        "impact": next_ev["impact"],
        "minutes_until": round(mins_until),
        "pairs": next_ev.get("pairs") or "ALL",
    }
