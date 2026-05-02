from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_async_engine(
    settings.database_url,
    echo=False,
    **({} if _is_sqlite else {"pool_size": 5, "max_overflow": 10}),
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ── Lightweight column migrations (safe for SQLite + Postgres) ─
        # Add columns introduced after initial schema without full Alembic.
        await conn.run_sync(_apply_column_migrations)


def _apply_column_migrations(sync_conn) -> None:
    """Add missing columns using information_schema / PRAGMA introspection.

    Runs idempotently on every startup. Safe for SQLite and PostgreSQL.
    """
    from sqlalchemy import text, inspect
    import logging as _log
    _lg = _log.getLogger(__name__)

    # columns to ensure exist: (table, column, DDL type)
    desired = [
        ("trades", "signal_ts", "FLOAT"),
        ("trades", "order_sent_ts", "FLOAT"),
        ("trades", "fill_ts", "FLOAT"),
        ("trades", "signal_to_send_ms", "INTEGER"),
        ("trades", "send_to_fill_ms", "INTEGER"),
        ("trades", "broker_deal_id", "VARCHAR(64)"),
        ("trades", "broker_position_id", "VARCHAR(64)"),
        ("trades", "source", "VARCHAR(16)"),
    ]

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())

    for table, col, coltype in desired:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if col in cols:
            continue
        try:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
            _lg.info(f"[migration] ADD COLUMN {table}.{col} {coltype}")
        except Exception as e:
            _lg.warning(f"[migration] failed to add {table}.{col}: {e}")
