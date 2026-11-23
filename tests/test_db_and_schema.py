import pytest

from localdbchat.config import Config
from localdbchat.db import Database, DatabaseError
from localdbchat.schema import SampleCache, introspect, render_table


def test_session_is_read_only_even_if_guard_is_bypassed(db):
    # Second wall: the connection itself refuses writes.
    with pytest.raises(DatabaseError):
        db.run("DELETE FROM customers")
    assert db.run("SELECT COUNT(*) FROM customers").rows[0][0] == 120


def test_timeout_stops_runaway_query(demo_db):
    slow = Database(f"sqlite:///{demo_db.as_posix()}", timeout_seconds=1, max_rows=10)
    try:
        with pytest.raises(DatabaseError) as info:
            slow.run("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r")
        assert info.value.timeout
        # and the connection is still usable afterwards
        assert slow.run("SELECT 1").rows == [[1]]
    finally:
        slow.dispose()


def test_row_cap_and_truncation_flag(db):
    result = db.run("SELECT * FROM support_tickets")
    assert result.row_count == 50 and result.truncated
    result = db.run("SELECT * FROM agents")
    assert result.row_count == 4 and not result.truncated


def test_values_are_json_safe(db):
    row = db.run("SELECT 1.5, NULL, x'00ff', 'text'").rows[0]
    assert row == [1.5, None, "<binary 2 bytes>", "text"]


def test_percent_and_colon_in_sql_are_literal(db):
    assert db.run("SELECT COUNT(*) FROM customers WHERE email LIKE '%@example.com'").rows[0][0] == 120
    assert db.run("SELECT '10:30 :name'").rows[0][0] == "10:30 :name"


def test_missing_sqlite_file_is_not_created(tmp_path):
    target = tmp_path / "nope.db"
    with pytest.raises(DatabaseError, match="not found"):
        Database(f"sqlite:///{target.as_posix()}")
    assert not target.exists()


@pytest.mark.parametrize("url,match", [
    ("", "No database configured"),
    ("not a url", "not valid"),
    ("oracle://u:p@h/db", "Unsupported"),
    ("sqlite://", "in-memory"),
])
def test_bad_urls(url, match):
    with pytest.raises(DatabaseError, match=match):
        Database(url)


def test_password_never_shown(tmp_path):
    from localdbchat.util import redact_url, scrub
    url = "postgresql://bob:s3cr3t!@db.local/app"
    assert "s3cr3t" not in redact_url(url)
    assert "s3cr3t" not in scrub("auth failed for s3cr3t!", url)


def test_introspection_finds_tables_keys_and_hides_secrets(schema):
    assert set(schema.tables) == {"agents", "customers", "order_items", "orders", "products", "support_tickets"}
    customers = schema.find("CUSTOMERS")
    assert customers.column("password_hash").sensitive
    assert "password_hash" not in render_table(customers, schema.dialect)
    tickets = schema.find("support_tickets")
    assert {fk.ref_table for fk in tickets.foreign_keys} == {"customers", "agents"}
    assert {t.name for t in schema.neighbours(customers)} == {"orders", "support_tickets"}


def test_awkward_names(weird_db):
    db = Database(f"sqlite:///{weird_db.as_posix()}")
    try:
        schema = introspect(db, sensitive_patterns=Config().sensitive_columns)
        assert schema.find("order items") is not None
        assert schema.find("big_items").kind == "view"
        assert schema.find("café") is not None
        log = schema.find("log_entries")
        assert log.column("api_token").sensitive
        text = render_table(schema.find("Order Items"), "sqlite")
        assert '"Order Items"' in text and '"Item ID"' in text and '"user"' in text
        samples = SampleCache(db, 3)
        cols, rows = samples.get(log)
        assert "api_token" not in cols
        assert rows == [["hello", "<binary 2 bytes>"]]
        cols, rows = samples.get(schema.find("Order Items"))
        assert len(rows) == 2
    finally:
        db.dispose()


def test_include_and_exclude(db):
    only = introspect(db, include_tables=["cust*", "orders"])
    assert set(only.tables) == {"customers", "orders"}
    without = introspect(db, exclude_tables=["order*"])
    assert "orders" not in without.tables and "order_items" not in without.tables


def test_empty_database_warns(tmp_path):
    import sqlite3
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    db = Database(f"sqlite:///{path.as_posix()}")
    try:
        schema = introspect(db)
        assert len(schema) == 0 and schema.warnings
    finally:
        db.dispose()


def test_sample_cache_is_bounded(db, schema):
    cache = SampleCache(db, 2, max_tables=2)
    for t in schema.tables.values():
        cache.get(t)
    assert len(cache._data) == 2


def test_value_hints_for_short_text_columns(db, schema):
    cache = SampleCache(db, 0)
    tickets = schema.find("support_tickets")
    cache.hints(tickets)
    assert tickets.column("priority").values == ["high", "low", "medium", "urgent"]
    assert tickets.column("opened_at").values == []          # many distinct values: no list
    customers = schema.find("customers")
    cache.hints(customers)
    assert customers.column("full_name").values == []         # names are not a category
    assert customers.column("password_hash").values is None   # protected: never looked at
    assert "values: 'high', 'low', 'medium', 'urgent'" in render_table(tickets, "sqlite")
