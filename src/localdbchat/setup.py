"""Connection setup shared by the web setup screen and `ldbc setup`.

Turns plain fields (host, port, user, password...) into a correct connection URL,
including passwords with special characters, tests it, and saves it.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy.engine import URL

from .config import Config
from .db import Database, DatabaseError
from .schema import introspect

KINDS = {
    "postgresql": {"label": "PostgreSQL", "driver": "postgresql+psycopg", "port": 5432, "extra": "postgres"},
    "mysql": {"label": "MySQL / MariaDB", "driver": "mysql+pymysql", "port": 3306, "extra": "mysql"},
    "mssql": {"label": "SQL Server", "driver": "mssql+pyodbc", "port": 1433, "extra": "mssql"},
    "sqlite": {"label": "SQLite file", "driver": "sqlite", "port": None, "extra": None},
}

_HOST = re.compile(r"^[A-Za-z0-9._\-:\[\]\\]+$")


class SetupError(Exception):
    """A field is missing or wrong; the message says which."""


@dataclass
class ConnectionFields:
    kind: str
    host: str = ""
    port: int | None = None
    database: str = ""
    user: str = ""
    password: str = ""
    path: str = ""                 # SQLite only
    ssl: bool = False              # PostgreSQL / MySQL: require TLS
    trust_certificate: bool = False  # SQL Server: accept self-signed certificates


def build_url(f: ConnectionFields) -> str:
    """Build a connection URL; special characters in any field are escaped properly."""
    if f.kind not in KINDS:
        raise SetupError(f"Unknown database type '{f.kind}'. Choose one of: {', '.join(KINDS)}.")
    info = KINDS[f.kind]

    if f.kind == "sqlite":
        path = (f.path or "").strip().strip('"')
        if not path:
            raise SetupError("Enter the path to the SQLite database file.")
        p = Path(path).expanduser()
        if not p.is_file():
            raise SetupError(f"No file found at {p}.")
        return f"sqlite:///{p.resolve().as_posix()}"

    host = (f.host or "").strip()
    if not host:
        raise SetupError("Enter the database server's host name or IP address.")
    if not _HOST.match(host):
        raise SetupError("The host name contains characters that are not allowed.")
    port = f.port or info["port"]
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise SetupError("The port must be a number.") from exc
    if not 0 < port < 65536:
        raise SetupError("The port must be between 1 and 65535.")
    database = (f.database or "").strip()
    if not database:
        raise SetupError("Enter the database name.")
    user = (f.user or "").strip()
    if not user:
        raise SetupError("Enter the database user name.")

    query: dict[str, str] = {}
    if f.kind == "postgresql":
        query["sslmode"] = "require" if f.ssl else "prefer"
        query["application_name"] = "local-db-chat"
    elif f.kind == "mysql":
        query["charset"] = "utf8mb4"
        if f.ssl:
            query["ssl"] = "true"
    elif f.kind == "mssql":
        query["driver"] = detect_odbc_driver()
        query["TrustServerCertificate"] = "yes" if f.trust_certificate else "no"
        query["ApplicationIntent"] = "ReadOnly"  # routes to readable replicas in Always On groups

    url = URL.create(info["driver"], username=user, password=f.password or None,
                     host=host, port=port, database=database, query=query)
    return url.render_as_string(hide_password=False)


def detect_odbc_driver() -> str:
    try:
        import pyodbc  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise SetupError('The SQL Server driver is not installed. Run: pip install "local-db-chat[mssql]" '
                         "and install the Microsoft ODBC Driver 18 for SQL Server.") from exc
    found = [d for d in pyodbc.drivers() if re.match(r"ODBC Driver \d+ for SQL Server", d)]
    if not found:
        raise SetupError("No Microsoft ODBC Driver for SQL Server was found. Install 'ODBC Driver 18 for "
                         "SQL Server' from Microsoft, then try again.")
    return max(found, key=lambda d: int(re.search(r"\d+", d).group()))


def check_driver(kind: str) -> None:
    """Fail early, with the install command, when the Python driver is missing."""
    modules = {"postgresql": "psycopg", "mysql": "pymysql", "mssql": "pyodbc"}
    module = modules.get(kind)
    if not module:
        return
    try:
        __import__(module)
    except ImportError as exc:
        raise SetupError(f"The {KINDS[kind]['label']} driver is not installed. Run: "
                         f'pip install "local-db-chat[{KINDS[kind]["extra"]}]"') from exc


@dataclass
class ConnectionCheck:
    ok: bool
    message: str
    tables: int = 0
    protected_columns: int = 0


def try_connection(url: str, cfg: Config | None = None) -> ConnectionCheck:
    cfg = cfg or Config()
    try:
        db = Database(url, timeout_seconds=cfg.timeout_seconds, max_rows=cfg.max_rows)
    except DatabaseError as exc:
        return ConnectionCheck(False, str(exc))
    try:
        db.check_connection()
        schema = introspect(db, schemas=cfg.schemas, include_tables=cfg.include_tables,
                            exclude_tables=cfg.exclude_tables, sensitive_patterns=cfg.sensitive_columns)
        protected = sum(1 for t in schema.tables.values() for c in t.columns if c.sensitive)
        if len(schema) == 0:
            return ConnectionCheck(False, "Connected, but no tables are visible to this user. "
                                     "Check the database name and the user's SELECT permissions.")
        return ConnectionCheck(True, f"Connected. Found {len(schema)} tables; {protected} protected column(s) "
                                "will be hidden.", len(schema), protected)
    except DatabaseError as exc:
        return ConnectionCheck(False, str(exc))
    finally:
        db.dispose()


def save_database_url(config_path: str | os.PathLike, url: str, model: str | None = None) -> Path:
    """Write database_url (and model) into config.yaml, keeping every other setting."""
    path = Path(config_path)
    data: dict = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SetupError(f"{path} is not valid YAML; fix or delete it first ({exc}).") from exc
        if not isinstance(data, dict):
            raise SetupError(f"{path} does not contain settings; fix or delete it first.")
    data["database_url"] = url
    if model:
        data["model"] = model
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("# local-db-chat settings. Holds the database password: keep it private,\n"
            "# never commit it. See config.example.yaml for every option.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # owner only (no effect on Windows ACLs)
    except OSError:
        pass
    return path
