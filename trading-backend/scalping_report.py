"""Scalping performance report — runs a fixed query suite on the `trades` table
and emits a Markdown report.

Usage:
    # Local SQLite:
    python3 scalping_report.py --db sqlite:///trading.db > report.md

    # Remote Postgres:
    DATABASE_URL=postgresql+psycopg2://trading:***@host:5432/trading \
        python3 scalping_report.py > report.md

    # Filter window (default: last 30 days):
    python3 scalping_report.py --days 7 > last_week.md

Requirements: sqlalchemy, psycopg2-binary (for postgres).
Designed to be SAFE: read-only, no mutations.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import fmean, median

try:
    from sqlalchemy import create_engine, text, inspect
except ImportError:
    sys.stderr.write("Missing dependency: pip install sqlalchemy psycopg2-binary\n")
    sys.exit(1)


def _probe_columns(engine) -> set:
    """Return the set of columns actually present on `trades`."""
    try:
        insp = inspect(engine)
        if "trades" not in insp.get_table_names():
            return set()
        return {c["name"] for c in insp.get_columns("trades")}
    except Exception:
        return set()


def _render_sql(sql: str, cols: set) -> str:
    """Substitute column-name tokens based on availability.

    {NET_PNL}    -> net_pnl if present else pnl
    {LAT_MS}     -> signal_to_send_ms if present else NULL
    {CONFIDENCE} -> signal_confidence if present else NULL
    """
    pnl_col = "net_pnl" if "net_pnl" in cols else "pnl"
    lat_col = "signal_to_send_ms" if "signal_to_send_ms" in cols else "NULL"
    conf_col = "signal_confidence" if "signal_confidence" in cols else "NULL"
    comm_col = "commission" if "commission" in cols else "0"
    return (sql.replace("{NET_PNL}", pnl_col)
               .replace("{LAT_MS}", lat_col)
               .replace("{CONFIDENCE}", conf_col)
               .replace("{COMMISSION}", comm_col))


# ── Queries ───────────────────────────────────────────────────────────────────
# Use SQLAlchemy `text()` with bind params — works on SQLite + Postgres.
# Dates are filtered via exit_time >= :cutoff (UTC).

Q_SUMMARY = """
SELECT
    COUNT(*)                                           AS total_trades,
    SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END)       AS wins,
    SUM(CASE WHEN {NET_PNL} <= 0 THEN 1 ELSE 0 END)      AS losses,
    ROUND(CAST(AVG({NET_PNL}) AS NUMERIC), 4)                             AS avg_net_pnl,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)                             AS total_net_pnl,
    ROUND(CAST(SUM(CASE WHEN {NET_PNL} > 0 THEN {NET_PNL} ELSE 0 END) AS NUMERIC), 2) AS gross_wins,
    ROUND(CAST(SUM(CASE WHEN {NET_PNL} < 0 THEN {NET_PNL} ELSE 0 END) AS NUMERIC), 2) AS gross_losses,
    ROUND(CAST(MAX({NET_PNL}) AS NUMERIC), 2)                             AS best_trade,
    ROUND(CAST(MIN({NET_PNL}) AS NUMERIC), 2)                             AS worst_trade,
    ROUND(CAST(SUM({COMMISSION}) AS NUMERIC), 2)                        AS total_commission
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
"""

Q_LATENCY_VS_PNL = """
SELECT
    CASE
        WHEN {LAT_MS} IS NULL            THEN '9_no_timing'
        WHEN {LAT_MS} < 2000             THEN '1_<2s'
        WHEN {LAT_MS} < 5000             THEN '2_2-5s'
        WHEN {LAT_MS} < 8000             THEN '3_5-8s'
        WHEN {LAT_MS} < 10000            THEN '4_8-10s'
        ELSE                                            '5_>=10s'
    END                                           AS bucket,
    COUNT(*)                                      AS n,
    ROUND(CAST(AVG({NET_PNL}) AS NUMERIC), 4)                        AS avg_pnl,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)                        AS total_pnl,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                                                  AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY bucket
ORDER BY bucket
"""

Q_BY_SYMBOL = """
SELECT
    symbol,
    COUNT(*)                       AS n,
    SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                                   AS win_rate_pct,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)         AS total_pnl,
    ROUND(CAST(AVG({NET_PNL}) AS NUMERIC), 4)         AS avg_pnl,
    ROUND(CAST(AVG({LAT_MS}) AS NUMERIC), 0) AS avg_lat_ms
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY symbol
ORDER BY total_pnl DESC
"""

Q_BY_EXIT_REASON = """
SELECT
    COALESCE(exit_reason, 'unknown') AS exit_reason,
    COUNT(*)                         AS n,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)           AS total_pnl,
    ROUND(CAST(AVG({NET_PNL}) AS NUMERIC), 4)           AS avg_pnl,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                                     AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY exit_reason
ORDER BY n DESC
"""

# Hour-of-day extraction: SQLite uses strftime, Postgres uses EXTRACT.
Q_BY_HOUR_SQLITE = """
SELECT
    CAST(strftime('%H', entry_time) AS INTEGER) AS hour_utc,
    COUNT(*)                                    AS n,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)                      AS total_pnl,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                                                AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY hour_utc
ORDER BY hour_utc
"""
Q_BY_HOUR_POSTGRES = """
SELECT
    EXTRACT(HOUR FROM entry_time AT TIME ZONE 'UTC')::INT AS hour_utc,
    COUNT(*)                                              AS n,
    ROUND(SUM({NET_PNL})::numeric, 2)                       AS total_pnl,
    ROUND(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0), 1)
                                                          AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY hour_utc
ORDER BY hour_utc
"""

Q_BY_CONFIDENCE = """
SELECT
    CASE
        WHEN {CONFIDENCE} IS NULL THEN 'n/a'
        WHEN {CONFIDENCE} < 70    THEN '1_<70'
        WHEN {CONFIDENCE} < 80    THEN '2_70-79'
        WHEN {CONFIDENCE} < 90    THEN '3_80-89'
        ELSE                                '4_>=90'
    END                        AS conf_bucket,
    COUNT(*)                   AS n,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)     AS total_pnl,
    ROUND(CAST(AVG({NET_PNL}) AS NUMERIC), 4)     AS avg_pnl,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                               AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY conf_bucket
ORDER BY conf_bucket
"""

Q_BY_SIDE = """
SELECT
    side,
    COUNT(*)                   AS n,
    ROUND(CAST(SUM({NET_PNL}) AS NUMERIC), 2)     AS total_pnl,
    ROUND(CAST(100.0 * SUM(CASE WHEN {NET_PNL} > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS NUMERIC), 1)
                               AS win_rate_pct
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff
GROUP BY side
"""

Q_HOLD_DURATION = """
SELECT
    COUNT(*)                                                       AS n,
    ROUND(AVG((julianday(exit_time) - julianday(entry_time)) * 1440), 1) AS avg_hold_min_sqlite,
    ROUND(AVG(EXTRACT(EPOCH FROM (exit_time - entry_time))/60), 1)    AS avg_hold_min_pg
FROM trades
WHERE LOWER(CAST(status AS TEXT)) = 'closed' AND exit_time >= :cutoff AND entry_time IS NOT NULL
"""  # We run the appropriate variant below.


# ── Rendering ────────────────────────────────────────────────────────────────

def _md_table(rows, cols):
    if not rows:
        return "_(aucune donnée)_\n"
    header = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    lines = [header, sep]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c) if isinstance(r, dict) else getattr(r, c, "")
            cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _rows(conn, sql, cols=None, **params):
    rendered = _render_sql(sql, cols or set())
    try:
        result = conn.execute(text(rendered), params)
        keys = list(result.keys())
        return [dict(zip(keys, r)) for r in result.fetchall()]
    except Exception as e:
        sys.stderr.write(f"[warn] query failed, returning empty: {e}\n")
        # Rollback to unpoison the transaction (Postgres: InFailedSqlTransaction
        # cascades into every following query otherwise).
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def run_report(db_url: str, days: int) -> str:
    engine = create_engine(db_url, future=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    is_pg = db_url.startswith("postgres")
    is_sqlite = db_url.startswith("sqlite")

    cols = _probe_columns(engine)
    if not cols:
        return f"# Scalping Report\n\n🔴 Table `trades` introuvable sur `{db_url}`. Lance d'abord le backfill `/admin/sync-icmarkets-history`.\n"

    out = []
    out.append(f"# Scalping Report — last {days} days\n")
    out.append(f"_Generated {datetime.now(timezone.utc).isoformat()}Z · cutoff {cutoff.isoformat()} · db={db_url.split('://')[0]}_\n")
    out.append(f"_Schema detected: {len(cols)} columns on `trades`. "
               f"Latency instrumentation: {'✓' if 'signal_to_send_ms' in cols else '✗ (run new code + restart bot)'}_\n")

    with engine.connect() as conn:
        # 1. Summary
        summary = _rows(conn, Q_SUMMARY, cols=cols, cutoff=cutoff)
        out.append("## 1. Summary\n")
        out.append(_md_table(summary, list(summary[0].keys()) if summary else []))

        if summary and summary[0].get("total_trades"):
            s = summary[0]
            gw = float(s.get("gross_wins") or 0)
            gl = abs(float(s.get("gross_losses") or 0))
            pf = round(gw / gl, 2) if gl > 0 else "∞"
            n = int(s.get("total_trades") or 0)
            wins = int(s.get("wins") or 0)
            win_rate = round(100 * wins / n, 1) if n else 0
            out.append(f"- **Profit factor** = gross_wins / |gross_losses| = **{pf}**")
            out.append(f"- **Win rate** = **{win_rate}%**")
            expectancy = float(s.get("avg_net_pnl") or 0)
            out.append(f"- **Expectancy per trade** = {expectancy:+.2f} EUR")
            out.append("")

        # 2. Latency vs PnL
        out.append("## 2. Latency signal→order vs P&L\n")
        out.append(
            "_Si les buckets `<2s` et `2-5s` ont un avg_pnl > 0 et les buckets `8-10s`/`>=10s` < 0, "
            "la latence tue directement ton edge. C'est la VRAIE preuve chiffrée de l'impact scalping._\n"
        )
        lat = _rows(conn, Q_LATENCY_VS_PNL, cols=cols, cutoff=cutoff)
        out.append(_md_table(lat, ["bucket", "n", "avg_pnl", "total_pnl", "win_rate_pct"]))

        # 3. Symbols
        out.append("## 3. P&L par symbole (top 20)\n")
        sym = _rows(conn, Q_BY_SYMBOL, cols=cols, cutoff=cutoff)
        out.append(_md_table(sym[:20], ["symbol", "n", "wins", "win_rate_pct", "total_pnl", "avg_pnl", "avg_lat_ms"]))
        if len(sym) > 20:
            out.append(f"\n_… {len(sym) - 20} autres symboles omis._\n")
        losers = [r for r in sym if (r.get("total_pnl") or 0) < 0]
        if losers:
            out.append(f"\n⚠️ **{len(losers)} symboles perdants** cumulés = {round(sum(r['total_pnl'] for r in losers), 2)} EUR. Candidats blacklist.\n")

        # 4. Exit reason
        out.append("## 4. P&L par sortie (SL/TP/trailing/manual)\n")
        er = _rows(conn, Q_BY_EXIT_REASON, cols=cols, cutoff=cutoff)
        out.append(_md_table(er, ["exit_reason", "n", "total_pnl", "avg_pnl", "win_rate_pct"]))

        # 5. Hour-of-day (UTC)
        out.append("## 5. P&L par heure UTC (windows horaires)\n")
        hq = Q_BY_HOUR_POSTGRES if is_pg else Q_BY_HOUR_SQLITE
        try:
            hh = _rows(conn, hq, cols=cols, cutoff=cutoff)
            out.append(_md_table(hh, ["hour_utc", "n", "total_pnl", "win_rate_pct"]))
            negatives = [h for h in hh if (h.get("total_pnl") or 0) < 0 and h.get("n", 0) >= 3]
            if negatives:
                out.append(f"\n⚠️ Heures UTC perdantes (n≥3) : {[h['hour_utc'] for h in negatives]}\n")
        except Exception as e:
            out.append(f"_(hourly query failed: {e})_\n")

        # 6. Confidence
        out.append("## 6. P&L par seuil de confidence du signal\n")
        out.append("_Si `<70` est déjà rentable, le min_confidence actuel (75) est peut-être trop strict._\n")
        cf = _rows(conn, Q_BY_CONFIDENCE, cols=cols, cutoff=cutoff)
        out.append(_md_table(cf, ["conf_bucket", "n", "total_pnl", "avg_pnl", "win_rate_pct"]))

        # 7. Side
        out.append("## 7. BUY vs SELL\n")
        sd = _rows(conn, Q_BY_SIDE, cols=cols, cutoff=cutoff)
        out.append(_md_table(sd, ["side", "n", "total_pnl", "win_rate_pct"]))

        # 8. Hold duration
        out.append("## 8. Durée moyenne de hold\n")
        try:
            if is_pg:
                hd = _rows(conn, "SELECT AVG(EXTRACT(EPOCH FROM (exit_time - entry_time))/60) AS avg_min FROM trades WHERE LOWER(CAST(status AS TEXT))='closed' AND exit_time >= :cutoff AND entry_time IS NOT NULL", cols=cols, cutoff=cutoff)
            else:
                hd = _rows(conn, "SELECT AVG((julianday(exit_time) - julianday(entry_time)) * 1440) AS avg_min FROM trades WHERE LOWER(CAST(status AS TEXT))='closed' AND exit_time >= :cutoff AND entry_time IS NOT NULL", cols=cols, cutoff=cutoff)
            avg_min = hd[0].get("avg_min") if hd else None
            out.append(f"- Durée moyenne : **{round(float(avg_min), 1) if avg_min else 'n/a'} min**\n")
        except Exception as e:
            out.append(f"_(hold query failed: {e})_\n")

    out.append("\n---\n")
    out.append("## Recommandations automatiques\n")
    out.append("_(à relire avec du jugement — ce sont des heuristiques, pas des ordres)_\n")
    if summary and summary[0].get("total_trades"):
        s = summary[0]
        total_pnl = float(s.get("total_net_pnl") or 0)
        if total_pnl < 0:
            out.append(f"- 🔴 **Perte nette {total_pnl} EUR** sur la période. Avant toute optimisation de latence, regarder §3/§4/§5 pour identifier la cause racine.")
        # Latency-based
        if lat:
            slow_rows = [r for r in lat if r.get("bucket") in ("4_8-10s", "5_>=10s")]
            fast_rows = [r for r in lat if r.get("bucket") in ("1_<2s", "2_2-5s")]
            if slow_rows and fast_rows:
                slow_avg = fmean([float(r["avg_pnl"] or 0) for r in slow_rows if r.get("avg_pnl") is not None] or [0])
                fast_avg = fmean([float(r["avg_pnl"] or 0) for r in fast_rows if r.get("avg_pnl") is not None] or [0])
                if fast_avg - slow_avg > 0.5:
                    out.append(f"- 🔴 **Latence tue l'edge** : avg_pnl rapide={fast_avg:.2f} vs lent={slow_avg:.2f} EUR. → pousser atomicité SL/TP + ramener scan_interval à 5s si rate limit le permet.")
        # Losers-blacklist
        if losers:
            worst = sorted(losers, key=lambda r: r.get("total_pnl") or 0)[:3]
            out.append(f"- ⚠️ **Candidats blacklist** (top 3 pires) : {', '.join(r['symbol'] for r in worst)}")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///trading.db"))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    print(run_report(args.db, args.days))


if __name__ == "__main__":
    main()
