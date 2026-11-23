import pytest

from localdbchat.safety import UnsafeQueryError, check_sql, extract_sql


def ok(sql, schema, max_rows=50):
    return check_sql(sql, schema, max_rows=max_rows)[0]


def refused(sql, schema, match=None):
    with pytest.raises(UnsafeQueryError, match=match):
        check_sql(sql, schema, max_rows=50)


# --------------------------------------------------------------- allowed
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM customers",
    "SELECT c.full_name, COUNT(o.order_id) AS n FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.full_name ORDER BY n DESC",
    "WITH recent AS (SELECT * FROM orders WHERE order_date > date('now','-30 day')) SELECT COUNT(*) FROM recent",
    "SELECT full_name FROM customers WHERE customer_id IN (SELECT customer_id FROM support_tickets WHERE status = 'open')",
    "SELECT status FROM orders UNION SELECT status FROM support_tickets",
    "SELECT strftime('%Y-%m', order_date) AS month, SUM(total_amount) AS revenue FROM orders GROUP BY month",
    "SELECT full_name FROM customers WHERE full_name LIKE '%ravi%'",
    "SELECT '10:30' AS t, full_name FROM customers",
    "SELECT CURRENT_DATE",
    "SELECT * FROM orders",  # no protected columns on orders
])
def test_allows_plain_reads(sql, schema):
    ok(sql, schema)


# --------------------------------------------------------------- writes, DDL, sessions
@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "UPDATE customers SET status = 'C'",
    "INSERT INTO agents VALUES (9, 'x', 'y')",
    "DROP TABLE customers",
    "CREATE TABLE x (a int)",
    "ALTER TABLE customers ADD COLUMN x int",
    "PRAGMA query_only = OFF",
    "ATTACH DATABASE 'other.db' AS o",
    "BEGIN",
    "VACUUM",
])
def test_refuses_non_select(sql, schema):
    refused(sql, schema)


def test_refuses_stacked_statements(schema):
    refused("SELECT 1; DROP TABLE customers", schema, "one statement")


def test_comment_hidden_statement_is_harmless(schema):
    # The DROP is inside a comment: it is text, not a statement.
    ok("SELECT 1 /* ; DROP TABLE customers */", schema)


def test_refuses_write_hidden_in_cte(schema):
    refused("WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d", schema)


def test_refuses_select_into(schema):
    refused("SELECT * INTO backup FROM orders", schema)


def test_refuses_unparseable(schema):
    refused("SELECT FROM WHERE (((", schema, "parsed")


def test_empty(schema):
    refused("", schema, "no SQL")
    refused("   ", schema, "no SQL")


# --------------------------------------------------------------- functions
@pytest.mark.parametrize("sql", [
    "SELECT pg_sleep(10)",
    "SELECT SLEEP(5)",
    "SELECT load_extension('evil')",
    "SELECT randomblob(1000000000)",
    "SELECT readfile('/etc/passwd')",
])
def test_refuses_dangerous_functions(sql, schema):
    refused(sql, schema, "not allowed")


# --------------------------------------------------------------- tables & columns
def test_refuses_unknown_table(schema):
    refused("SELECT * FROM invoices", schema, "does not exist")


def test_refuses_system_catalog(schema):
    refused("SELECT * FROM sqlite_master", schema, "System catalog")


def test_refuses_cross_database(schema):
    refused("SELECT * FROM otherdb.main.customers", schema, "Cross-database")


def test_refuses_unknown_column(schema):
    refused("SELECT c.phone FROM customers c", schema, "does not exist")
    refused("SELECT phone FROM customers", schema, "does not exist")


def test_allows_column_aliases(schema):
    ok("SELECT COUNT(*) AS n FROM orders ORDER BY n", schema)
    ok("SELECT x.cnt FROM (SELECT COUNT(*) AS cnt FROM orders) x", schema)


# --------------------------------------------------------------- protected columns
@pytest.mark.parametrize("sql", [
    "SELECT password_hash FROM customers",
    "SELECT c.password_hash FROM customers c",
    "SELECT UPPER(password_hash) FROM customers",
    "SELECT full_name FROM customers WHERE password_hash LIKE 'a%'",
    "SELECT s.p FROM (SELECT password_hash AS p FROM customers) s",
    "SELECT * FROM customers",
    "SELECT c.* FROM customers c",
    "SELECT o.order_id, c.* FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
])
def test_refuses_protected_columns(sql, schema):
    refused(sql, schema)


def test_star_on_safe_table_in_join_is_allowed(schema):
    ok("SELECT o.*, c.full_name FROM orders o JOIN customers c ON c.customer_id = o.customer_id", schema)


# --------------------------------------------------------------- limits
def test_adds_limit(schema):
    assert "LIMIT 51" in ok("SELECT full_name FROM customers", schema)


def test_tightens_big_limit(schema):
    assert "LIMIT 51" in ok("SELECT full_name FROM customers LIMIT 100000", schema)


def test_keeps_small_limit(schema):
    assert "LIMIT 5" in ok("SELECT full_name FROM customers LIMIT 5", schema)


# --------------------------------------------------------------- extraction
@pytest.mark.parametrize("reply,expected", [
    ("```sql\nSELECT 1;\n```", "SELECT 1"),
    ("Here you go:\n```\nSELECT 2\n```\nHope it helps", "SELECT 2"),
    ("SQL: SELECT 3;", "SELECT 3"),
    ("The query is SELECT 4 FROM t", "SELECT 4 FROM t"),
    ("```SQL\nWITH a AS (SELECT 1) SELECT * FROM a\n```", "WITH a AS (SELECT 1) SELECT * FROM a"),
    ("", ""),
])
def test_extract_sql(reply, expected):
    assert extract_sql(reply) == expected
