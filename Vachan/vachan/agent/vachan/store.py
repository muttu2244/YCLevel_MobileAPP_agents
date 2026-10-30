"""Storage: SQLite by default, Postgres when DATABASE_URL says so.

`db/schema.sql` contains no dialect-specific SQL, so the same file creates both
schemas. The only differences are the parameter placeholder and the driver.

Swapping SQLite for Neon/Supabase is one environment variable — that is the
point of keeping the schema portable. Supabase needs two things beyond the URL
(see `_connect_postgres`): TLS, and no server-side prepared statements when you
connect through its pooler.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .config import SCHEMA_PATH


def _connect_postgres(database_url: str):
    """Open a Postgres connection that also survives Supabase's pooler.

    Two hosted-Postgres details that bite only in production:

    * Supabase's pooler (port 6543, host `...pooler.supabase.com`) runs
      pgbouncer in transaction mode. psycopg3 starts using server-side prepared
      statements after five executions of a statement, and pgbouncer does not
      keep them across transactions. The symptom is nasty: it works for the
      first few runs and then fails with "prepared statement \"_pg3_0\" does not
      exist", which reads like corruption rather than a connection setting.
    * Supabase requires TLS. If the URL already carries `sslmode`, passing it
      again as a keyword is an error, so only add it when it is absent — and
      only for a remote host, never for a local Postgres.
    """
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError(
            "Postgres needs the optional driver: pip install 'vachan[postgres]'"
        ) from exc

    parts = urlsplit(database_url)
    host = parts.hostname or ""
    options: dict = {"application_name": "vachan"}
    if parts.port == 6543 or "pooler" in host:
        options["prepare_threshold"] = None
    has_ssl = any(k in parse_qs(parts.query) for k in ("sslmode", "ssl"))
    if not has_ssl and host not in ("", "localhost", "127.0.0.1", "::1"):
        options["sslmode"] = "require"
    return psycopg.connect(database_url, **options)


def now() -> str:
    """ISO-8601 UTC, second precision — the format every timestamp column uses."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.is_postgres = database_url.startswith(("postgres://", "postgresql://"))
        self.ph = "%s" if self.is_postgres else "?"
        if self.is_postgres:
            self.conn = _connect_postgres(database_url)
        else:
            path = database_url.split("sqlite:///")[-1]
            self.conn = sqlite3.connect(path)
            # SQLite needs this per connection; the schema file cannot set it.
            self.conn.execute("PRAGMA foreign_keys = ON")

    # ---------------------------------------------------------------- schema --
    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        if self.is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(sql)
        else:
            self.conn.executescript(sql)
        self.conn.commit()

    # ------------------------------------------------------------ statements --
    def insert(self, table: str, row: dict) -> None:
        cols = list(row)
        placeholders = ", ".join([self.ph] * len(cols))
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        self.conn.execute(sql, [row[c] for c in cols])

    def select(self, sql: str, params: tuple = ()) -> list[dict]:
        """`?` placeholders in, translated for the active driver."""
        cur = self.conn.execute(self._translate(sql), params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.execute(self._translate(sql), params)

    def _translate(self, sql: str) -> str:
        """Swap `?` for the driver's placeholder, ignoring `?` inside literals.

        A blind str.replace turns a question mark in a quoted string into a
        broken statement — and chat text is full of question marks.
        """
        if not self.is_postgres:
            return sql
        out: list[str] = []
        quote: str | None = None
        for char in sql:
            if quote:
                out.append(char)
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
                out.append(char)
            elif char == "?":
                out.append(self.ph)
            else:
                out.append(char)
        return "".join(out)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # A half-applied change is worse than none: a run that raises must not
        # leave promises closed by an agent that crashed mid-thought.
        if exc_type is None:
            self.commit()
        else:
            try:
                self.conn.rollback()
            except Exception:  # pragma: no cover - driver already closed
                pass
        self.close()
