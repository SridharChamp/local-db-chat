import pytest

from localdbchat.meta import answer_schema_question


@pytest.mark.parametrize("q", [
    "how many tables are there in this DB",
    "How many tables are there?",
    "number of tables",
    "count of tables in the database",
    "total number of tables",
])
def test_how_many_tables(q, schema):
    ans = answer_schema_question(q, schema)
    assert ans is not None and ans.answer == "There are 6 tables."
    assert [r[0] for r in ans.rows] == ["agents", "customers", "order_items", "orders", "products", "support_tickets"]


@pytest.mark.parametrize("q", [
    "list the tables", "show me all tables", "What tables are there?", "which tables exist",
    "what are the tables", "tables?",
])
def test_list_tables(q, schema):
    ans = answer_schema_question(q, schema)
    assert ans is not None and len(ans.rows) == 6


@pytest.mark.parametrize("q,table", [
    ("What columns does orders have?", "orders"),
    ("what are the columns in support_tickets", "support_tickets"),
    ("list the columns of order items", "order_items"),
    ("show me the fields in the customers table", "customers"),
    ("describe agents", "agents"),
    ("Describe the products table", "products"),
    ("what is the structure of orders", "orders"),
    ("which columns does the order_items table have?", "order_items"),
])
def test_columns_of_a_table(q, table, schema):
    ans = answer_schema_question(q, schema)
    assert ans is not None and ans.answer.startswith(f"{table} has")
    assert ans.columns == ["column", "type", "can be empty", "notes"]


def test_how_many_columns(schema):
    ans = answer_schema_question("how many columns are in customers?", schema)
    assert ans.answer == "customers has 7 columns."  # password_hash is protected and not counted


def test_protected_columns_are_never_listed(schema):
    ans = answer_schema_question("what columns does customers have", schema)
    assert "password_hash" not in [r[0] for r in ans.rows]


def test_notes_include_keys_and_values(schema):
    ans = answer_schema_question("describe support_tickets", schema)
    notes = {r[0]: r[3] for r in ans.rows}
    assert notes["ticket_id"] == "primary key"
    assert notes["customer_id"] == "links to customers.customer_id"


@pytest.mark.parametrize("q", [
    # data questions: must go to the model
    "how many orders are there",
    "how many rows are in the orders table",
    "how many records in customers",
    "show the name and email columns of active customers",
    "list customers in Chennai",
    "which agent closed the most tickets",
    "how many tickets are open",
    "describe the most common ticket subject",
    # structure question about a table that does not exist
    "what columns does invoices have",
])
def test_data_questions_are_not_intercepted(q, schema):
    assert answer_schema_question(q, schema) is None


def test_engine_answers_without_model_or_query(make_assistant):
    a, llm = make_assistant([])
    ans = a.ask("how many tables are there in this DB")
    assert ans.ok and ans.source == "schema" and ans.sql == "" and llm.calls == []
    assert ans.answer == "There are 6 tables."
