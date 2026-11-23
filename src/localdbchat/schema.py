"""Schema discovery: what tables and columns exist, and how they connect.

Reads the database catalog in batched calls (one round trip per kind of
metadata, not one per table), so large schemas load quickly. Sample rows are
fetched lazily, only for tables a question actually needs, and kept in a
bounded LRU cache.
"""

from __future__ import annotations

import fnmatch
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlglot import exp

from .db import Database, DatabaseError

SAMPLE_CACHE_SIZE = 256
MAX_SAMPLE_COLUMNS = 12
MAX_PROMPT_VALUE_CHARS = 60

_PLAIN_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {
    "all", "and", "as", "asc", "between", "by", "case", "check", "column", "create",
    "date", "default", "delete", "desc", "distinct", "else", "end", "from", "group",
    "having", "in", "index", "insert", "into", "is", "join", "key", "like", "limit",
    "not", "null", "offset", "on", "or", "order", "primary", "references", "select",
    "table", "then", "time", "timestamp", "to", "union", "update", "user", "using",
    "value", "values", "when", "where", "with", "year", "month", "day", "level",
    "role", "session", "status", "type", "comment", "number", "rows", "row",
}


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    comment: str = ""
    sensitive: bool = False


@dataclass
class ForeignKey:
    columns: list[str]
    ref_schema: str | None
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    schema: str | None
    kind: str = "table"  # or "view"
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    comment: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def key(self) -> str:
        return self.full_name.lower()

    def column(self, name: str) -> Column | None:
        lname = name.lower()
        for col in self.columns:
            if col.name.lower() == lname:
                return col
        return None

    @property
    def visible_columns(self) -> list[Column]:
        return [c for c in self.columns if not c.sensitive]

    @property
    def sensitive_names(self) -> set[str]:
        return {c.name.lower() for c in self.columns if c.sensitive}


def quote_ident(name: str, dialect: str) -> str:
    """Quote an identifier only when it needs it (spaces, case, reserved words)."""
    needs_quotes = not _PLAIN_IDENT.match(name) or name.lower() in _RESERVED
    if dialect == "postgres" and name != name.lower():
        needs_quotes = True  # unquoted names fold to lower case in PostgreSQL
    return exp.Identifier(this=name, quoted=needs_quotes).sql(dialect=dialect)


class SchemaInfo:
    def __init__(self, tables: list[Table], dialect: str, default_schema: str | None,
                 warnings: list[str] | None = None):
        self.dialect = dialect
        self.default_schema = default_schema
        self.tables: dict[str, Table] = {t.key: t for t in tables}
        self.warnings = warnings or []
        self._adjacency: dict[str, dict[str, Table]] | None = None
        self._by_name: dict[str, list[Table]] = {}
        for t in tables:
            self._by_name.setdefault(t.name.lower(), []).append(t)

    def __len__(self) -> int:
        return len(self.tables)

    def find(self, name: str, schema: str | None = None) -> Table | None:
        """Resolve a table the way the database would; None if unknown or ambiguous."""
        candidates = self._by_name.get(name.lower(), [])
        if not candidates:
            return None
        if schema:
            lschema = schema.lower()
            for t in candidates:
                tschema = (t.schema or self.default_schema or "").lower()
                if tschema == lschema:
                    return t
            return None
        if len(candidates) == 1:
            return candidates[0]
        for t in candidates:  # ambiguous: the default schema wins, like search_path
            if t.schema is None or (self.default_schema and t.schema.lower() == self.default_schema.lower()):
                return t
        return None

    def neighbours(self, table: Table) -> list[Table]:
        """Tables joined to this one by a foreign key, in either direction."""
        if self._adjacency is None:
            adj: dict[str, dict[str, Table]] = {k: {} for k in self.tables}
            for t in self.tables.values():
                for fk in t.foreign_keys:
                    ref = self.find(fk.ref_table, fk.ref_schema)
                    if ref is not None and ref.key != t.key:
                        adj[t.key][ref.key] = ref
                        adj[ref.key][t.key] = t
            self._adjacency = adj
        return list(self._adjacency.get(table.key, {}).values())


def _is_sensitive(table: str, column: str, patterns: list[str]) -> bool:
    lcol = column.lower()
    ltable = table.lower()
    for pattern in patterns:
        p = pattern.lower().strip()
        if not p:
            continue
        if "." in p:  # exact "table.column" (globs allowed)
            if fnmatch.fnmatch(f"{ltable}.{lcol}", p):
                return True
        elif any(ch in p for ch in "*?["):
            if fnmatch.fnmatch(lcol, p):
                return True
        elif p in lcol:
            return True
    return False


def _matches(name: str, patterns: list[str]) -> bool:
    lname = name.lower()
    return any(fnmatch.fnmatch(lname, p.lower()) for p in patterns)


def introspect(db: Database, *, schemas: list[str] | None = None,
               include_tables: list[str] | None = None, exclude_tables: list[str] | None = None,
               sensitive_patterns: list[str] | None = None) -> SchemaInfo:
    """Read tables, views, columns, keys and comments from the live database."""
    try:
        insp = sa.inspect(db.engine)
        default_schema = insp.default_schema_name
    except sa.exc.DBAPIError as exc:
        raise db._wrap(exc, connecting=True) from exc

    warnings: list[str] = []
    targets: list[str | None] = list(schemas) if schemas else [None]
    tables: list[Table] = []
    sensitive_patterns = sensitive_patterns or []
    include_tables = include_tables or []
    exclude_tables = exclude_tables or []

    for target in targets:
        label = target or default_schema or "default"
        # When several schemas are configured, names are always schema-qualified.
        stored_schema = target if schemas and (len(targets) > 1 or target != default_schema) else None
        try:
            table_names = insp.get_table_names(schema=target)
        except sa.exc.SQLAlchemyError as exc:
            warnings.append(f"Could not list tables in schema '{label}': {str(exc).splitlines()[0]}")
            continue
        try:
            view_names = insp.get_view_names(schema=target)
        except (sa.exc.SQLAlchemyError, NotImplementedError):
            view_names = []

        kinds = {name: "table" for name in table_names}
        kinds.update({name: "view" for name in view_names if name not in kinds})
        wanted = [n for n in kinds
                  if (not include_tables or _matches(n, include_tables))
                  and not _matches(n, exclude_tables)]
        if not wanted:
            continue

        columns = _multi(insp.get_multi_columns, target, warnings, label, "columns",
                         kind=sa.engine.reflection.ObjectKind.ANY)
        pks = _multi(insp.get_multi_pk_constraint, target, warnings, label, "primary keys")
        fks = _multi(insp.get_multi_foreign_keys, target, warnings, label, "foreign keys")
        comments = _multi(insp.get_multi_table_comment, target, warnings, label, "comments",
                          kind=sa.engine.reflection.ObjectKind.ANY, quiet=True)

        for name in sorted(wanted, key=str.lower):
            key = (target, name)
            cols_raw = columns.get(key)
            if cols_raw is None:  # batch call failed or skipped this object; try alone
                try:
                    cols_raw = insp.get_columns(name, schema=target)
                except sa.exc.SQLAlchemyError as exc:
                    warnings.append(f"Skipped {label}.{name}: {str(exc).splitlines()[0]}")
                    continue
            pk_cols = {c.lower() for c in (pks.get(key) or {}).get("constrained_columns") or []}
            cols = []
            for c in cols_raw:
                try:
                    type_text = str(c["type"])
                except Exception:
                    type_text = "UNKNOWN"
                cols.append(Column(
                    name=str(c["name"]),
                    type=type_text,
                    nullable=bool(c.get("nullable", True)),
                    primary_key=str(c["name"]).lower() in pk_cols,
                    comment=(c.get("comment") or "").strip(),
                    sensitive=_is_sensitive(name, str(c["name"]), sensitive_patterns),
                ))
            if not cols:
                continue
            fk_list = []
            for fk in fks.get(key) or []:
                if fk.get("referred_table") and fk.get("constrained_columns"):
                    ref_schema = fk.get("referred_schema")
                    fk_list.append(ForeignKey(
                        columns=list(fk["constrained_columns"]),
                        ref_schema=ref_schema if ref_schema and ref_schema != default_schema else None,
                        ref_table=fk["referred_table"],
                        ref_columns=list(fk.get("referred_columns") or []),
                    ))
            comment = ((comments.get(key) or {}).get("text") or "").strip()
            tables.append(Table(name=name, schema=stored_schema, kind=kinds[name],
                                columns=cols, foreign_keys=fk_list, comment=comment))

    if not tables:
        warnings.append("No tables were found. Check the database URL, the user's permissions, "
                        "and the schemas / include_tables settings.")
    return SchemaInfo(tables, db.sqlglot_dialect, default_schema, warnings)


def _multi(fn, schema, warnings, label, what, quiet=False, **kwargs) -> dict:
    try:
        return dict(fn(schema=schema, **kwargs))
    except NotImplementedError:
        return {}
    except sa.exc.SQLAlchemyError as exc:
        if not quiet:
            warnings.append(f"Could not read {what} in schema '{label}': {str(exc).splitlines()[0]}")
        return {}


class SampleCache:
    """Bounded, thread-safe cache of a few example rows per table."""

    def __init__(self, db: Database, rows_per_table: int, max_tables: int = SAMPLE_CACHE_SIZE):
        self.db = db
        self.rows_per_table = rows_per_table
        self.max_tables = max_tables
        self._data: OrderedDict[str, list[list]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, table: Table) -> tuple[list[str], list[list]]:
        cols = [c.name for c in table.visible_columns[:MAX_SAMPLE_COLUMNS]]
        if self.rows_per_table <= 0 or not cols:
            return cols, []
        with self._lock:
            if table.key in self._data:
                self._data.move_to_end(table.key)
                return cols, self._data[table.key]
        try:
            rows = self.db.select_sample(table.name, table.schema, cols, self.rows_per_table)
        except DatabaseError:
            rows = []  # a broken view or missing grant must not break the question
        rows = [[_short(v) for v in row] for row in rows]
        with self._lock:
            self._data[table.key] = rows
            self._data.move_to_end(table.key)
            while len(self._data) > self.max_tables:
                self._data.popitem(last=False)
        return cols, rows

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def _short(value):
    if isinstance(value, str) and len(value) > MAX_PROMPT_VALUE_CHARS:
        return value[:MAX_PROMPT_VALUE_CHARS] + "..."
    return value


def render_table(table: Table, dialect: str, samples: tuple[list[str], list[list]] | None = None) -> str:
    """Describe one table compactly, in a CREATE TABLE-like form the model reads well."""
    name = ".".join(quote_ident(p, dialect) for p in table.full_name.split(".", 1)) \
        if table.schema else quote_ident(table.name, dialect)
    header = f"{'VIEW' if table.kind == 'view' else 'TABLE'} {name} ("
    lines = [header + (f"  -- {table.comment}" if table.comment else "")]
    body: list[str] = []
    for col in table.visible_columns:
        bits = [f"  {quote_ident(col.name, dialect)} {col.type}"]
        if col.primary_key:
            bits.append("PRIMARY KEY")
        if not col.nullable and not col.primary_key:
            bits.append("NOT NULL")
        body.append((" ".join(bits), col.comment))
    for fk in table.foreign_keys:
        ref = f"{fk.ref_schema}.{fk.ref_table}" if fk.ref_schema else fk.ref_table
        body.append((f"  FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES {ref}({', '.join(fk.ref_columns)})", ""))
    for i, (text, comment) in enumerate(body):
        text += "," if i < len(body) - 1 else ""
        lines.append(text + (f"  -- {comment}" if comment else ""))
    lines.append(")")
    if samples and samples[1]:
        cols, rows = samples
        lines.append(f"-- sample rows ({', '.join(cols)}):")
        for row in rows:
            lines.append("--   " + " | ".join("NULL" if v is None else str(v) for v in row))
    return "\n".join(lines)
