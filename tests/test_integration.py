"""Tests against real database servers.

Skipped unless a URL is given, for example:
    LDBC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ldbc
    LDBC_TEST_MYSQL_URL=mysql+pymysql://root:root@127.0.0.1:3306/ldbc
    LDBC_TEST_MSSQL_URL=mssql+pyodbc://sa:Passw0rd!@localhost:1433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes

The URL user must be allowed to create tables: the test builds its own small schema
with a privileged connection, then checks that local-db-chat's own connection
cannot change anything.
"""

import os

import pytest
import sqlalchemy as sa
from conftest import FakeLLM

from localdbchat.config import Config
from localdbchat.db import Database, DatabaseError
from localdbchat.engine import Assistant
from localdbchat.safety import UnsafeQueryError, check_sql
from localdbchat.schema import introspect, render_table

URLS = {
    "postgres": os.environ.get("LDBC_TEST_POSTGRES_URL"),
    "mysql": os.environ.get("LDBC_TEST_MYSQL_URL"),
    "mssql": os.environ.get("LDBC_TEST_MSSQL_URL"),
}

SLOW_QUERY = {
    "postgresql": "SELECT pg_sleep(5)",
    "mysql": "SELECT COUNT(*) FROM information_schema.columns a, information_schema.columns b, "
             "information_schema.columns c, information_schema.columns d",
    "mariadb": "SELECT COUNT(*) FROM information_schema.columns a, information_schema.columns b, "
               "information_schema.columns c, information_schema.columns d",
    "mssql": "WAITFOR DELAY '00:00:05'; SELECT 1",
}


def build_schema(url: str) -> None:
    meta = sa.MetaData()
    customers = sa.Table(
        "ldbc_customers", meta,
        sa.Column("customer_id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("full_name", sa.String(100), nullable=False, comment="Customer's full name"),
        sa.Column("city", sa.String(50)),
        sa.Column("password_hash", sa.String(100), nullable=False),
        comment="People who buy from us")
    orders = sa.Table(
        "ldbc_orders", meta,
        sa.Column("order_id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("ldbc_customers.customer_id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("placed_on", sa.Date, nullable=False))
    engine = sa.create_engine(url)
    try:
        meta.drop_all(engine)
        meta.create_all(engine)
        with engine.begin() as conn:
            conn.execute(customers.insert(), [
                {"customer_id": 1, "full_name": "Ravi Kumar", "city": "Chennai", "password_hash": "x1"},
                {"customer_id": 2, "full_name": "Asha Iyer", "city": "Pune", "password_hash": "x2"},
                {"customer_id": 3, "full_name": "John Smith", "city": "Chennai", "password_hash": "x3"},
            ])
            conn.execute(orders.insert(), [
                {"order_id": i, "customer_id": 1 + i % 3, "amount": 100 + i, "placed_on": f"2026-0{1 + i % 9}-15"}
                for i in range(1, 31)
            ])
    finally:
        engine.dispose()


@pytest.fixture(params=[k for k, v in URLS.items() if v] or [pytest.param("none", marks=pytest.mark.skip(
    reason="no LDBC_TEST_*_URL set"))])
def server(request):
    url = URLS[request.param]
    build_schema(url)
    db = Database(url, timeout_seconds=2, max_rows=10)
    yield url, db
    db.dispose()


def test_connects_and_introspects(server):
    _, db = server
    db.check_connection()
    schema = introspect(db, include_tables=["ldbc_*"], sensitive_patterns=Config().sensitive_columns)
    assert {t.name.lower() for t in schema.tables.values()} == {"ldbc_customers", "ldbc_orders"}
    orders = schema.find("ldbc_orders")
    assert [fk.ref_table.lower() for fk in orders.foreign_keys] == ["ldbc_customers"]
    customers = schema.find("ldbc_customers")
    assert customers.column("password_hash").sensitive
    assert "password_hash" not in render_table(customers, schema.dialect)
    if db.backend != "sqlite":
        assert customers.comment == "People who buy from us"


def test_session_refuses_writes(server):
    _, db = server
    # Bypass the SQL guard on purpose: the session itself must refuse.
    if db.backend == "mssql":
        pytest.skip("SQL Server has no session read-only mode; use a db_datareader login (see README)")
    with pytest.raises(DatabaseError):
        db.run("DELETE FROM ldbc_orders")
    assert db.run("SELECT COUNT(*) FROM ldbc_orders").rows[0][0] == 30


def test_timeout(server):
    _, db = server
    with pytest.raises(DatabaseError) as info:
        db.run(SLOW_QUERY[db.backend])
    assert info.value.timeout, str(info.value)
    assert db.run("SELECT 1").rows[0][0] == 1  # the pool recovers


def test_row_cap(server):
    _, db = server
    sql, _ = check_sql("SELECT order_id FROM ldbc_orders ORDER BY order_id", _schema(db), max_rows=10)
    result = db.run(sql)
    assert result.row_count == 10 and result.truncated


def test_dialect_attacks_refused(server):
    _, db = server
    schema = _schema(db)
    for sql in ["SELECT * FROM ldbc_customers", "SELECT password_hash FROM ldbc_customers",
                "DELETE FROM ldbc_orders", SLOW_QUERY[db.backend]]:
        with pytest.raises(UnsafeQueryError):
            check_sql(sql, schema, max_rows=10)


def test_end_to_end_question(server, tmp_path):
    url, db = server
    sql = ("SELECT c.full_name, COUNT(o.order_id) AS n, SUM(o.amount) AS total "
           "FROM ldbc_customers c JOIN ldbc_orders o ON o.customer_id = c.customer_id "
           "WHERE c.city = 'Chennai' GROUP BY c.full_name ORDER BY n DESC")
    cfg = Config(database_url=url, include_tables=["ldbc_*"], max_rows=10, explain_answers=False,
                 learned_examples_file=str(tmp_path / "learned.json"))
    assistant = Assistant(cfg, db=db, llm=FakeLLM([f"```sql\n{sql}\n```"]))
    ans = assistant.ask("How many orders and how much did each Chennai customer spend?")
    assert ans.ok, ans.error
    assert ans.row_count == 2
    assert sum(r[1] for r in ans.rows) == 20
    assert {r[0] for r in ans.rows} == {"Ravi Kumar", "John Smith"}
    assert all(isinstance(r[2], str) for r in ans.rows)  # money stays exact (Decimal -> str)


def _schema(db):
    return introspect(db, include_tables=["ldbc_*"], sensitive_patterns=Config().sensitive_columns)


def test_percent_signs_are_literal(server):
    # PyMySQL / psycopg treat '%' as a format marker when parameters are passed; we pass none.
    _, db = server
    assert db.run("SELECT COUNT(*) FROM ldbc_customers WHERE full_name LIKE '%Kumar%'").rows[0][0] == 1
    assert db.run("SELECT '100%' AS v").rows[0][0] == "100%"
