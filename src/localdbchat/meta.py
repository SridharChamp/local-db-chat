"""Questions about the database's structure, answered from the loaded schema.

"How many tables are there?" or "What columns does orders have?" are about the
schema, not the data. The model would try to query system catalogs, which the
SQL guard rightly blocks (they can reveal excluded tables and protected
columns). The app already knows the schema exactly, so it answers these
directly: instantly, with no model call and no query, and only with the tables
and columns it is allowed to show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .retrieval import stem
from .schema import SchemaInfo, Table

_TABLE_WORD = r"(?:tables?|views?)"
_HOW_MANY_TABLES = re.compile(
    rf"\b(how\s+many|number\s+of|count\s+(?:of\s+)?(?:the\s+)?|total\s+(?:number\s+of\s+)?)\s*{_TABLE_WORD}\b", re.I)
_LIST_TABLES = re.compile(
    rf"^\s*(?:(?:please\s+)?(?:list|show|give|tell|display)\s+(?:me\s+)?(?:all\s+|the\s+)*{_TABLE_WORD}"
    rf"|(?:what|which)\s+{_TABLE_WORD}\s+(?:are|do|does|exist|is)"
    rf"|(?:what|which)\s+(?:are\s+)?(?:all\s+)?the\s+{_TABLE_WORD}\b"
    rf"|{_TABLE_WORD}\s*\??\s*$)", re.I)
_COL = r"(?:columns?|fields?|attributes?)"
# Only clear questions about structure. "Show the name and email columns of active
# customers" is a data question and must not match.
_COLUMNS = re.compile(
    rf"^\s*(?:(?:what|which)\s+(?:are\s+)?(?:all\s+)?(?:the\s+)?{_COL}\b"
    rf"|(?:please\s+)?(?:list|show|give|tell)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?{_COL}\s+(?:in|of|for|on)\b"
    rf"|(?:how\s+many|number\s+of)\s+{_COL}\b)"
    rf"|\b{_COL}\s+(?:does|do|did)\s+[\w.\" ]+?\s+have\b", re.I)
_DESCRIBE = re.compile(
    r"^\s*(?:please\s+)?(?:describe|explain)\s+(?:the\s+)?[\w.\" ]+?(?:\s+table)?\s*\??\s*$"
    r"|\b(?:structure|schema|definition|layout)\s+of\b", re.I)
_COUNT = re.compile(r"\b(how\s+many|number\s+of)\b", re.I)
_ROWS_WORD = re.compile(r"\b(rows?|records?|entries|entry)\b", re.I)


@dataclass
class MetaAnswer:
    answer: str
    columns: list[str]
    rows: list[list]


def answer_schema_question(question: str, schema: SchemaInfo) -> MetaAnswer | None:
    """Return an answer if this is a question about the structure; None otherwise."""
    q = question.strip()
    if not q or _ROWS_WORD.search(q):
        return None  # "how many rows in orders" is about data: let the model write SQL

    tables = sorted(schema.tables.values(), key=lambda t: t.full_name.lower())

    if _COLUMNS.search(q) or _DESCRIBE.search(q):
        table = _mentioned_table(q, schema)
        if table is None:
            return None
        cols = table.visible_columns
        if _COUNT.search(q):
            return MetaAnswer(f"{table.full_name} has {len(cols)} columns.", *_column_rows(table))
        return MetaAnswer(f"{table.full_name} has {len(cols)} columns:", *_column_rows(table))

    if _HOW_MANY_TABLES.search(q):
        n_tables = sum(1 for t in tables if t.kind == "table")
        n_views = len(tables) - n_tables
        text = f"There are {n_tables} tables" + (f" and {n_views} views" if n_views else "") + "."
        return MetaAnswer(text, *_table_rows(tables))

    if _LIST_TABLES.search(q) and not _mentioned_table(q, schema):
        return MetaAnswer(f"The database has {len(tables)} tables and views you can ask about:",
                          *_table_rows(tables))
    return None


def _table_rows(tables: list[Table]) -> tuple[list[str], list[list]]:
    return (["table", "kind", "columns", "description"],
            [[t.full_name, t.kind, len(t.visible_columns), t.comment or None] for t in tables])


def _column_rows(table: Table) -> tuple[list[str], list[list]]:
    rows = []
    fk_cols = {c.lower(): f"{fk.ref_table}.{fk.ref_columns[i] if i < len(fk.ref_columns) else '?'}"
               for fk in table.foreign_keys for i, c in enumerate(fk.columns)}
    for c in table.visible_columns:
        notes = []
        if c.primary_key:
            notes.append("primary key")
        if c.name.lower() in fk_cols:
            notes.append(f"links to {fk_cols[c.name.lower()]}")
        if c.values:
            notes.append("values: " + ", ".join(c.values))
        if c.comment:
            notes.append(c.comment)
        rows.append([c.name, c.type, "yes" if c.nullable else "no", "; ".join(notes) or None])
    return ["column", "type", "can be empty", "notes"], rows


def _mentioned_table(question: str, schema: SchemaInfo) -> Table | None:
    """The table a question names, allowing plurals, spaces for underscores and schema prefixes."""
    q = question.lower()
    words = {stem(w) for w in re.findall(r"[a-z0-9_]+", q)}
    best: tuple[int, Table] | None = None
    for t in schema.tables.values():
        names = {t.name.lower(), t.full_name.lower(), t.name.lower().replace("_", " ")}
        hit = any(re.search(rf"(?<![a-z0-9_]){re.escape(n)}(?![a-z0-9_])", q) for n in names)
        if not hit:
            hit = stem(t.name.lower()) in words or (
                "_" in t.name and all(stem(p) in words for p in t.name.lower().split("_") if p))
        if hit:
            score = len(t.name)  # prefer the most specific match: "order_items" over "orders"
            if best is None or score > best[0]:
                best = (score, t)
    return best[1] if best else None
