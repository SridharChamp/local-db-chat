"""Database access: read-only sessions, timeouts, row caps, clean errors.

Defence in depth. The SQL guard (safety.py) is the first wall; this module adds
a second one at the session level for every dialect that supports it:

- PostgreSQL: every transaction is READ ONLY, plus statement_timeout.
- MySQL / MariaDB: session transactions are READ ONLY, plus an execution limit.
- SQLite: PRAGMA query_only, plus an interrupt when the deadline passes.
- SQL Server: has no session read-only switch, so a query timeout is set and
  every connection is rolled back. Use a db_datareader login (see README).

The strongest wall is still yours: connect with a read-only database user.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, NoSuchModuleError, SQLAlchemyError

from .util import redact_url, scrub, to_jsonable

SQLGLOT_DIALECTS = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "tsql",
    "sqlite": "sqlite",
}

DRIVER_HINTS = {
    "postgresql": 'pip install "local-db-chat[postgres]"',
    "mysql": 'pip install "local-db-chat[mysql]"',
    "mariadb": 'pip install "local-db-chat[mysql]"',
    "mssql": 'pip install "local-db-chat[mssql]"  (and install the Microsoft ODBC Driver for SQL Server)',
}

TIMEOUT_MARKERS = (
    "statement timeout",            # PostgreSQL
    "canceling statement",          # PostgreSQL
    "maximum statement execution time",  # MySQL
    "max_statement_time",           # MariaDB
    "query execution was interrupted",   # MySQL / MariaDB
    "query timeout expired",        # SQL Server (ODBC HYT00)
    "interrupted",                  # SQLite
)


class DatabaseError(Exception):
    """A database problem explained in words a support person can act on."""

    def __init__(self, message: str, *, timeout: bool = False, connection: bool = False):
        super().__init__(message)
        self.timeout = timeout
        self.connection = connection


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool
    elapsed_ms: int
    row_count: int = field(init=False)

    def __post_init__(self):
        self.row_count = len(self.rows)


class Database:
    def __init__(self, url: str, *, timeout_seconds: int = 15, max_rows: int = 200):
        if not url:
            raise DatabaseError(
                "No database configured. Set database_url in config.yaml or the "
                "LDBC_DATABASE_URL environment variable.", connection=True)
        if timeout_seconds <= 0:
            raise DatabaseError("timeout_seconds must be greater than 0.")
        if max_rows <= 0:
            raise DatabaseError("max_rows must be greater than 0.")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows

        try:
            parsed = make_url(url)
        except ArgumentError as exc:
            raise DatabaseError(f"The database URL is not valid: {redact_url(url)}") from exc

        self.backend = parsed.get_backend_name()
        if self.backend not in SQLGLOT_DIALECTS:
            raise DatabaseError(
                f"Unsupported database '{self.backend}'. Supported: PostgreSQL, MySQL, "
                "MariaDB, SQL Server, SQLite.")

        if self.backend == "sqlite":
            db_path = parsed.database or ""
            if db_path in ("", ":memory:") and "mode=memory" not in url:
                raise DatabaseError("An in-memory SQLite database has no data to query. "
                                    "Point database_url at a .db file.")
            if db_path and not db_path.startswith("file:") and not os.path.exists(db_path):
                # sqlite would silently create an empty file; refuse instead.
                raise DatabaseError(f"SQLite file not found: {db_path}", connection=True)

        try:
            self.engine = sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5) \
                if self.backend != "sqlite" else sa.create_engine(url)
        except NoSuchModuleError as exc:
            raise DatabaseError(f"Unknown database driver in URL {redact_url(url)}.") from exc
        except ModuleNotFoundError as exc:
            hint = DRIVER_HINTS.get(self.backend, "")
            raise DatabaseError(f"The driver for {self.backend} is not installed. {hint}") from exc

        self._install_session_guards()

    # ------------------------------------------------------------------ setup
    @property
    def sqlglot_dialect(self) -> str:
        return SQLGLOT_DIALECTS[self.backend]

    @property
    def display_url(self) -> str:
        return redact_url(self.url)

    def _install_session_guards(self) -> None:
        backend = self.backend
        ms = int(self.timeout_seconds * 1000)
        seconds = int(self.timeout_seconds)

        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_conn, _record):  # pragma: no cover - driver specific
            if backend == "sqlite":
                dbapi_conn.execute("PRAGMA query_only = ON")
                return
            if backend == "mssql":
                # pyodbc: query timeout in seconds for every statement.
                try:
                    dbapi_conn.timeout = seconds
                except Exception:
                    pass
                cur = dbapi_conn.cursor()
                try:
                    cur.execute(f"SET LOCK_TIMEOUT {ms}")
                finally:
                    cur.close()
                return
            cur = dbapi_conn.cursor()
            try:
                if backend == "postgresql":
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    cur.execute(f"SET statement_timeout = {ms}")
                    cur.execute(f"SET lock_timeout = {ms}")
                elif backend in ("mysql", "mariadb"):
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    try:
                        cur.execute(f"SET SESSION max_execution_time = {ms}")  # MySQL
                    except Exception:
                        try:
                            cur.execute(f"SET SESSION max_statement_time = {seconds}")  # MariaDB
                        except Exception:
                            pass
            finally:
                cur.close()
            try:
                dbapi_conn.commit()
            except Exception:
                pass

    # ---------------------------------------------------------------- queries
    def check_connection(self) -> None:
        try:
            with self.engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
        except DBAPIError as exc:
            raise self._wrap(exc, connecting=True) from exc
        except ModuleNotFoundError as exc:
            hint = DRIVER_HINTS.get(self.backend, "")
            raise DatabaseError(f"The driver for {self.backend} is not installed. {hint}") from exc

    def run(self, sql: str, *, max_rows: int | None = None) -> QueryResult:
        """Run one already-validated SELECT. Never commits."""
        cap = max_rows or self.max_rows
        start = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                raw = conn.connection.driver_connection
                deadline = time.monotonic() + self.timeout_seconds
                if self.backend == "sqlite":
                    raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 5000)
                try:
                    result = conn.exec_driver_sql(sql)
                    if not result.returns_rows:
                        return QueryResult([], [], False, self._ms(start))
                    columns = [str(c) for c in result.keys()]
                    fetched = result.fetchmany(cap + 1)
                    result.close()
                finally:
                    if self.backend == "sqlite":
                        raw.set_progress_handler(None, 0)
                    conn.rollback()
        except DBAPIError as exc:
            raise self._wrap(exc) from exc
        except SQLAlchemyError as exc:
            raise DatabaseError(scrub(str(exc).split("\n")[0], self.url)) from exc

        truncated = len(fetched) > cap
        rows = [[to_jsonable(v) for v in row] for row in fetched[:cap]]
        return QueryResult(columns, rows, truncated, self._ms(start))

    def select_sample(self, table: str, schema: str | None, columns: list[str], n: int) -> list[list]:
        """A few rows from one table, built with proper quoting for the dialect."""
        if not columns or n <= 0:
            return []
        stmt = sa.select(*[sa.column(c) for c in columns]) \
            .select_from(sa.table(table, schema=schema)).limit(n)
        compiled = str(stmt.compile(self.engine, compile_kwargs={"literal_binds": True}))
        return self.run(compiled, max_rows=n).rows

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    def _wrap(self, exc: DBAPIError, *, connecting: bool = False) -> DatabaseError:
        original = str(getattr(exc, "orig", exc) or exc).strip()
        message = scrub(original.split("\n")[0][:500], self.url)
        lower = original.lower()
        if any(marker in lower for marker in TIMEOUT_MARKERS):
            return DatabaseError(
                f"The query took longer than {self.timeout_seconds}s and was stopped.", timeout=True)
        if connecting or exc.connection_invalidated or isinstance(exc, sa.exc.InterfaceError):
            return DatabaseError(
                f"Could not connect to the database at {self.display_url}: {message}", connection=True)
        if "read-only" in lower or "read only" in lower or "readonly" in lower:
            return DatabaseError("The database refused the query because the session is read-only.")
        return DatabaseError(message)

    def dispose(self) -> None:
        self.engine.dispose()
