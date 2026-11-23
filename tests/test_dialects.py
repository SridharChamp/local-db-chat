"""The guard must work for every supported dialect, not only SQLite."""

import pytest

from localdbchat.safety import UnsafeQueryError, check_sql
from localdbchat.schema import Column, ForeignKey, SchemaInfo, Table


def make_schema(dialect):
    customers = Table("customers", None, columns=[
        Column("id", "INT", primary_key=True), Column("name", "TEXT"),
        Column("created_at", "TIMESTAMP"), Column("password", "TEXT", sensitive=True)])
    orders = Table("orders", None, columns=[
        Column("id", "INT", primary_key=True), Column("customer_id", "INT"), Column("amount", "DECIMAL"),
        Column("created_at", "TIMESTAMP")],
        foreign_keys=[ForeignKey(["customer_id"], None, "customers", ["id"])])
    return SchemaInfo([customers, orders], dialect, {"postgres": "public", "tsql": "dbo"}.get(dialect))


@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "SELECT name FROM customers WHERE created_at > CURRENT_DATE - INTERVAL '1 month'"),
    ("postgres", "SELECT DATE_TRUNC('month', created_at) AS m, SUM(amount) FROM orders GROUP BY 1"),
    ("postgres", "SELECT name FROM public.customers WHERE name ILIKE '%ravi%'"),
    ("postgres", "SELECT EXTRACT(YEAR FROM created_at) AS y FROM orders"),
    ("mysql", "SELECT name FROM customers WHERE created_at > DATE_SUB(CURDATE(), INTERVAL 1 MONTH)"),
    ("mysql", "SELECT DATE_FORMAT(created_at, '%Y-%m') AS m FROM orders"),
    ("tsql", "SELECT TOP 10 name FROM customers ORDER BY created_at DESC"),
    ("tsql", "SELECT name FROM dbo.customers WHERE created_at > DATEADD(month, -1, GETDATE())"),
    ("tsql", "SELECT COUNT(*) FROM orders WHERE DATEDIFF(day, created_at, GETDATE()) < 7"),
    ("tsql", "SELECT [name] FROM [customers]"),
])
def test_dialect_reads_allowed(dialect, sql):
    check_sql(sql, make_schema(dialect), max_rows=100)


@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "SELECT pg_read_file('/etc/passwd')"),
    ("postgres", "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)"),
    ("postgres", "COPY customers TO '/tmp/x'"),
    ("postgres", "SET ROLE postgres"),
    ("postgres", "CALL do_things()"),
    ("postgres", "EXPLAIN ANALYZE DELETE FROM orders"),
    ("postgres", "SELECT name FROM customers FOR UPDATE"),
    ("postgres", "SELECT * FROM pg_catalog.pg_user"),
    ("postgres", "SELECT * FROM information_schema.tables"),
    ("postgres", "SELECT set_config('statement_timeout', '0', false)"),
    ("mysql", "SELECT name FROM customers INTO OUTFILE '/tmp/x'"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT BENCHMARK(100000000, MD5('x'))"),
    ("mysql", "SELECT * FROM mysql.user"),
    ("tsql", "EXEC xp_cmdshell 'dir'"),
    ("tsql", "WAITFOR DELAY '00:00:10'"),
    ("tsql", "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'select 1')"),
    ("tsql", "SELECT name INTO #tmp FROM customers"),
    ("tsql", "SELECT * FROM sys.tables"),
    ("tsql", "SELECT TOP 5 password FROM customers"),
])
def test_dialect_attacks_refused(dialect, sql):
    with pytest.raises(UnsafeQueryError):
        check_sql(sql, make_schema(dialect), max_rows=100)


def test_tsql_limit_becomes_top():
    sql, _ = check_sql("SELECT name FROM customers ORDER BY name", make_schema("tsql"), max_rows=100)
    assert "TOP 101" in sql and "LIMIT" not in sql


def test_tsql_big_top_is_tightened():
    sql, _ = check_sql("SELECT TOP 100000 name FROM customers", make_schema("tsql"), max_rows=100)
    assert "TOP 101" in sql


def test_schema_qualified_name_resolves():
    _, used = check_sql("SELECT name FROM public.customers", make_schema("postgres"), max_rows=10)
    assert used == ["customers"]


def test_wrong_schema_refused():
    with pytest.raises(UnsafeQueryError, match="does not exist"):
        check_sql("SELECT name FROM sales.customers", make_schema("postgres"), max_rows=10)
