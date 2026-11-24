"""The SQL guard: only one plain read query, on known tables, ever reaches the DB.

Rules, in order:
1. The text must parse. Anything the parser does not understand is refused
   (that is where vendor tricks like INTO OUTFILE or WAITFOR live).
2. Exactly one statement, and it must be SELECT / UNION / INTERSECT / EXCEPT.
3. No write, DDL, transaction, session, file or exec node anywhere in the tree,
   including inside CTEs and subqueries (e.g. WITH x AS (DELETE ...)).
4. No dangerous functions (sleep, file access, shell, remote connections).
5. Every table must exist in the discovered schema. No cross-database names,
   no system catalogs.
6. Every column must exist in some table the query uses (or be an alias).
7. Sensitive columns are never selected, filtered, joined or wrapped in a function,
   and SELECT * is refused on tables that have any.
8. A row limit is added or tightened.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .schema import SchemaInfo, Table


class UnsafeQueryError(Exception):
    """The SQL was refused. The message is written so the model can fix it."""


_BLOCKED_NODE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
    "AlterColumn", "RenameTable", "TruncateTable", "Command", "Into", "Lock", "Pragma",
    "Attach", "Detach", "Copy", "Execute", "Set", "SetItem", "Transaction", "Commit",
    "Rollback", "Use", "Grant", "Revoke", "Kill", "LoadData", "Cache", "Uncache",
    "Refresh", "Analyze", "Describe", "Show", "Comment", "Declare", "Export",
    "Install", "Summarize",
]
_BLOCKED_NODES = tuple(t for t in (getattr(exp, n, None) for n in _BLOCKED_NODE_NAMES)
                       if isinstance(t, type))

_READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

BLOCKED_FUNCTIONS = {
    # time-wasting / denial of service
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "benchmark", "waitfor",
    "randomblob", "zeroblob",
    # file system
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "pg_ls_logdir",
    "pg_ls_waldir", "pg_ls_tmpdir", "lo_import", "lo_export", "lo_get", "lo_put",
    "lo_from_bytea", "lo_open", "load_file", "readfile", "writefile", "edit",
    "load_extension", "fts3_tokenizer",
    # shell / remote / dynamic SQL
    "xp_cmdshell", "xp_dirtree", "xp_fileexist", "xp_regread", "sp_oacreate",
    "sp_executesql", "openrowset", "opendatasource", "openquery", "openxml",
    "dblink", "dblink_exec", "dblink_connect", "query_to_xml", "query_to_xml_and_xmlschema",
    "table_to_xml", "cursor_to_xml", "sys_exec", "sys_eval",
    # server state
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock", "get_lock",
    "pg_promote", "pg_switch_wal", "pg_create_restore_point", "nextval", "setval",
    "currval", "lastval", "txid_current", "pg_current_xact_id",
}

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "sys", "mysql",
                   "performance_schema", "sqlite_master", "sqlite_schema", "sqlite_temp_master"}

_FENCE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n?(.*?)```", re.DOTALL)


def extract_sql(text: str) -> str:
    """Pull the SQL out of a model reply (fences, labels, trailing semicolons)."""
    if not text:
        return ""
    match = _FENCE.search(text)
    sql = match.group(1) if match else text
    if not match and "```" in sql:
        # Generation stopped at the closing fence, so it is missing: drop the opening one.
        sql = re.sub(r"^.*?```[a-zA-Z]*[ \t]*\n?", "", sql, count=1, flags=re.DOTALL)
        sql = sql.replace("```", "")
    sql = sql.strip()
    sql = re.sub(r"^(sql|query)\s*:\s*", "", sql, flags=re.IGNORECASE)
    if not match:
        # Unfenced replies sometimes carry an explanation; keep from the first SELECT/WITH.
        start = re.search(r"\b(SELECT|WITH)\b", sql, flags=re.IGNORECASE)
        if start:
            sql = sql[start.start():]
    sql = sql.strip().rstrip(";").strip()
    return sql


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").lower()
    name = getattr(node, "sql_name", None)
    try:
        return (name() if callable(name) else node.key).lower()
    except Exception:
        return node.key.lower()


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}


def _aliases(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for alias in tree.find_all(exp.Alias):
        if alias.alias:
            names.add(alias.alias.lower())
    for ta in tree.find_all(exp.TableAlias):
        if ta.name:
            names.add(ta.name.lower())
        for col in ta.columns or []:
            names.add(col.name.lower())
    return names


def check_sql(sql: str, schema: SchemaInfo, *, max_rows: int) -> tuple[str, list[str]]:
    """Validate and normalise one query. Returns (safe_sql, tables_used)."""
    dialect = schema.dialect
    if not sql or not sql.strip():
        raise UnsafeQueryError("The reply contained no SQL query.")
    if len(sql) > 20_000:
        raise UnsafeQueryError("The query is too long. Write a shorter query.")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, TokenError) as exc:
        detail = str(exc).splitlines()[0][:200]
        raise UnsafeQueryError(f"The SQL could not be parsed as {dialect} ({detail}). "
                               "Write one standard SELECT query.") from exc
    if not statements:
        raise UnsafeQueryError("The reply contained no SQL query.")
    if len(statements) > 1:
        raise UnsafeQueryError("Only one statement is allowed. Write a single SELECT query.")
    tree = statements[0]

    if not isinstance(tree, _READ_ROOTS):
        raise UnsafeQueryError(f"Only SELECT queries are allowed, not {tree.key.upper()}.")

    for node in tree.walk():
        if isinstance(node, _BLOCKED_NODES):
            raise UnsafeQueryError(f"{node.key.upper()} is not allowed. Only read data with SELECT.")
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name in BLOCKED_FUNCTIONS:
                raise UnsafeQueryError(f"The function {name.upper()} is not allowed.")

    ctes = _cte_names(tree)
    used: dict[str, Table] = {}
    alias_to_table: dict[str, Table] = {}

    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            # A table-valued function, e.g. generate_series(...). Functions were checked above.
            continue
        name, db, catalog = t.name, t.db, t.catalog
        if catalog:
            raise UnsafeQueryError(f"Cross-database reference '{catalog}.{db}.{name}' is not allowed.")
        if not db and name.lower() in ctes:
            continue
        if (db or "").lower() in _SYSTEM_SCHEMAS or name.lower() in _SYSTEM_SCHEMAS:
            raise UnsafeQueryError("System catalog tables are not available. Use the listed tables.")
        table = schema.find(name, db or None)
        if table is None:
            shown = f"{db}.{name}" if db else name
            raise UnsafeQueryError(f"Table '{shown}' does not exist. Use only the tables listed in the schema.")
        used[table.key] = table
        alias_to_table[(t.alias or name).lower()] = table
        alias_to_table[table.name.lower()] = table

    if not used and not ctes and tree.find(exp.Table) is None and not tree.find(exp.Column):
        pass  # e.g. SELECT CURRENT_DATE: allowed, touches no data

    sensitive = set()
    for table in used.values():
        sensitive |= table.sensitive_names

    # Stars: SELECT * / t.* would leak sensitive columns.
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            star_table = None
            if isinstance(projection, exp.Star):
                star_table = "*"
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                star_table = (projection.table or "").lower()
            if star_table is None:
                continue
            targets = used.values() if star_table == "*" else \
                [alias_to_table[star_table]] if star_table in alias_to_table else []
            for table in targets:
                if table.sensitive_names:
                    raise UnsafeQueryError(
                        f"SELECT * is not allowed on {table.full_name} because it has protected "
                        "columns. List the columns you need explicitly.")

    known_columns: set[str] = set()
    for table in used.values():
        known_columns |= {c.name.lower() for c in table.columns}
    query_aliases = _aliases(tree)

    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        cname = col.name.lower()
        if not cname:
            continue
        qualifier = (col.table or "").lower()
        if cname in sensitive and (not qualifier or qualifier in alias_to_table):
            owner = alias_to_table.get(qualifier) if qualifier else None
            if owner is None or cname in owner.sensitive_names:
                raise UnsafeQueryError(f"The column '{col.name}' is protected and cannot be used.")
        if qualifier and qualifier in alias_to_table:
            if alias_to_table[qualifier].column(cname) is None:
                owner = alias_to_table[qualifier]
                raise UnsafeQueryError(f"Column '{col.name}' does not exist in {owner.full_name}."
                                       + _where_it_is(col.name, [owner], schema))
        elif not qualifier and used and cname not in known_columns and cname not in query_aliases:
            # Could be a string the dialect reads as an identifier ("abc" in MySQL is fine).
            if not _is_quoted_string_identifier(col, dialect):
                raise UnsafeQueryError(f"Column '{col.name}' does not exist in the tables used "
                                       f"({', '.join(t.full_name for t in used.values())})."
                                       + _where_it_is(col.name, list(used.values()), schema))

    if dialect == "mysql" and any((j.side or "").upper() == "FULL" for j in tree.find_all(exp.Join)):
        raise UnsafeQueryError(
            "MySQL does not support FULL OUTER JOIN. Combine the two sides another way, for example "
            "a UNION of the keys followed by LEFT JOINs to each side.")

    shape = _shape(tree)
    _apply_limit(tree, max_rows)
    out = tree.sql(dialect=dialect)
    # Writing the query back out for this dialect must never change what it means. The parser library
    # can rewrite features a dialect lacks (e.g. FULL OUTER JOIN into a UNION), which breaks
    # aggregates. If the structure changed, refuse instead of running something else.
    try:
        regenerated = sqlglot.parse_one(out, read=dialect)
    except (ParseError, TokenError) as exc:
        raise UnsafeQueryError("The query could not be prepared for this database. Write it more simply.") from exc
    if _shape(regenerated) != shape:
        raise UnsafeQueryError("This database does not support part of that query as written. "
                               "Rewrite it with features this database supports.")
    return out, [t.full_name for t in used.values()]


def _shape(tree: exp.Expression) -> tuple[int, int, int]:
    """A rough fingerprint of a query's structure: SELECTs, set operations, joins."""
    return (sum(1 for _ in tree.find_all(exp.Select)),
            sum(1 for _ in tree.find_all(exp.Union, exp.Intersect, exp.Except)),
            sum(1 for _ in tree.find_all(exp.Join)))


def _where_it_is(column: str, from_tables: list[Table], schema: SchemaInfo) -> str:
    """Point the model at the table that has this column and the joins to reach it."""
    owners = schema.tables_with_column(column)
    if not owners:
        return ""
    hint = f" '{column}' is in: {', '.join(t.full_name for t in owners[:4])}."
    best = None
    for start in from_tables:
        for goal in owners:
            path = schema.join_path(start, goal)
            if path and (best is None or len(path) < len(best)):
                best = path
    if best and len(best) > 1:
        hint += " Reach it by joining " + " -> ".join(t.full_name for t in best) + " (see How tables connect)."
    return hint


def _is_quoted_string_identifier(col: exp.Column, dialect: str) -> bool:
    ident = col.this
    return dialect == "mysql" and isinstance(ident, exp.Identifier) and ident.quoted


def _apply_limit(tree: exp.Expression, max_rows: int) -> None:
    """Add LIMIT/TOP max_rows+1 (so truncation is detectable) or tighten a bigger one."""
    cap = max_rows + 1
    if isinstance(tree, exp.Select):
        current = tree.args.get("limit")
        if current is not None:
            value = current.expression
            if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= cap:
                return
            current.set("expression", exp.Literal.number(cap))
            return
        if tree.args.get("fetch") is not None or tree.args.get("offset") is not None:
            return  # OFFSET/FETCH paging: keep it as written; the fetch cap still applies
        tree.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
    # UNION / INTERSECT / EXCEPT: the driver-side fetch cap in db.run still applies;
    # wrapping would break ORDER BY rules on some dialects.
