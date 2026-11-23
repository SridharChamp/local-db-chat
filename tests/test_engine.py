import datetime as dt

from localdbchat.db import QueryResult
from localdbchat.engine import Answer, describe, numbers_are_grounded
from localdbchat.llm import LLMError


def fenced(sql):
    return f"```sql\n{sql}\n```"


def test_happy_path(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS active_customers FROM customers WHERE status = 'A'")])
    ans = a.ask("How many active customers?")
    assert ans.ok and ans.attempts == 1
    assert ans.columns == ["active_customers"] and isinstance(ans.rows[0][0], int)
    assert ans.answer.startswith("Active customers: ")
    assert ans.tables == ["customers"]
    system, question = llm.calls[0][0]["content"], llm.calls[0][-1]["content"]
    assert "password_hash" not in system            # protected column never shown to the model
    assert dt.date.today().isoformat() in question  # relative dates can be resolved
    assert ans.schema_in_prompt == "full"


# ------------------------------------------------------------ speed: preloaded schema
def test_every_question_starts_with_the_same_schema_block(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM orders"),
                             fenced("SELECT COUNT(*) AS n FROM agents")])
    a.ask("How many orders?")
    a.ask("How many agents are there?")
    first, second = llm.calls[0][0]["content"], llm.calls[1][0]["content"]
    assert first == second                           # identical prefix -> Ollama reuses it
    for table in ("agents", "customers", "order_items", "orders", "products", "support_tickets"):
        assert f"TABLE {table} (" in first           # the whole schema, not a per-question pick
    assert dt.date.today().isoformat() not in first  # nothing that changes lives in the prefix


def test_schema_block_does_not_change_across_days(make_assistant):
    from localdbchat.prompts import build_messages
    a, _ = make_assistant([])
    _ = a.schema
    day1 = build_messages(a._prefix, "q", examples=[], history=[], today=dt.date(2026, 1, 1))
    day2 = build_messages(a._prefix, "q", examples=[], history=[], today=dt.date(2026, 1, 2))
    assert day1[0] == day2[0] and day1[-1] != day2[-1]


def test_preload_reads_schema_once_up_front(make_assistant):
    a, llm = make_assistant([""])
    stats = a.preload()
    assert a.preloaded and stats == {}
    assert len(llm.calls) == 1 and llm.calls[0][0]["content"] == a._prefix
    assert llm.kwargs[0]["max_tokens"] == 1          # read only; don't generate


def test_refresh_rebuilds_the_block_and_needs_new_preload(make_assistant, demo_db):
    import sqlite3
    a, _ = make_assistant([""])
    a.preload()
    con = sqlite3.connect(demo_db)
    con.execute("CREATE TABLE refunds (refund_id INTEGER PRIMARY KEY, amount NUMERIC)")
    con.commit()
    con.close()
    a.refresh_schema()
    assert not a.preloaded and "TABLE refunds (" in a._prefix


def test_huge_schema_falls_back_to_per_question_tables(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM agents")], num_ctx=1700)
    assert not a.schema_preloadable
    assert a.preload() == {} and llm.calls == []
    ans = a.ask("How many agents?")
    assert ans.ok and ans.schema_in_prompt == "selected"
    assert "TABLE agents (" in llm.calls[0][0]["content"]


def test_reply_is_capped_and_stops_after_the_sql(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM agents")])
    a.ask("How many agents?")
    assert llm.kwargs[0]["max_tokens"] == 512 and llm.kwargs[0]["stop"] == ["\n```\n"]


def test_model_timings_are_reported(make_assistant):
    from localdbchat.llm import Reply
    reply = Reply(fenced("SELECT COUNT(*) AS n FROM agents"),
                  {"prompt_tokens": 18, "read_ms": 90, "output_tokens": 14, "write_ms": 700, "load_ms": 0})
    a, _ = make_assistant([reply])
    ans = a.ask("How many agents?")
    assert ans.model == {"prompt_tokens": 18, "read_ms": 90, "output_tokens": 14, "write_ms": 700, "load_ms": 0}


def test_glossary_reaches_the_prompt(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) FROM customers WHERE status = 'C'")],
                            glossary={"churned customer": "customers.status = 'C'"})
    assert a.ask("How many churned customers?").ok
    assert "churned customer: customers.status = 'C'" in llm.calls[0][0]["content"]


def test_retries_after_unknown_column(make_assistant):
    a, llm = make_assistant([fenced("SELECT phone FROM customers"), fenced("SELECT email FROM customers LIMIT 3")])
    ans = a.ask("customer phone numbers")
    assert ans.ok and ans.attempts == 2 and ans.row_count == 3
    feedback = llm.calls[1][-1]["content"]
    assert "phone" in feedback and "does not exist" in feedback
    assert len(ans.problems) == 1 and "phone" in ans.problems[0]  # shown to the user, not hidden


def test_retries_after_database_error(make_assistant):
    a, llm = make_assistant([fenced("SELECT json_extract('not json', '$.a') AS v FROM orders"),
                           fenced("SELECT COUNT(*) FROM orders")])
    ans = a.ask("orders count")
    assert ans.ok and ans.attempts == 2
    assert "malformed" in llm.calls[1][-1]["content"].lower()  # the real DB error was fed back


def test_refuses_write_and_gives_up_cleanly(make_assistant):
    a, _ = make_assistant([fenced("DELETE FROM customers")] * 3)
    ans = a.ask("delete all customers")
    assert not ans.ok and "3 tries" in ans.error
    assert a.db.run("SELECT COUNT(*) FROM customers").rows[0][0] == 120


def test_cannot_answer(make_assistant):
    a, _ = make_assistant(["CANNOT_ANSWER: there is no weather data"])
    ans = a.ask("what's the weather in Chennai?")
    assert ans.cannot_answer and "weather" in ans.answer and not ans.error


def test_model_down_is_a_clear_error(make_assistant):
    a, _ = make_assistant([LLMError("Ollama is not running")])
    ans = a.ask("anything")
    assert ans.error == "Ollama is not running"


def test_input_validation(make_assistant):
    a, llm = make_assistant([])
    assert a.ask("").error
    assert a.ask("   ").error
    assert "too long" in a.ask("x" * 5000).error
    assert llm.calls == []


def test_repeat_question_skips_the_model(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM orders")])
    first = a.ask("How many orders?")
    second = a.ask("how many orders")
    assert first.ok and second.ok and second.cached
    assert len(llm.calls) == 1 and second.rows == first.rows


def test_verified_answer_is_reused_and_survives_restart(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM agents")])
    ans = a.ask("How many agents are there?")
    a.confirm(ans.question, ans.sql)
    b, llm_b = make_assistant([])  # new process, same learned file
    again = b.ask("how many agents are there?")
    assert again.ok and again.verified and again.rows == [[4]] and llm_b.calls == []


def test_verified_examples_are_shown_for_similar_questions(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM agents"),
                             fenced("SELECT COUNT(*) AS n FROM agents WHERE team = 'Technical'")])
    first = a.ask("How many agents are there?")
    a.confirm(first.question, first.sql)
    a.ask("How many technical agents are there?")
    messages = llm.calls[1]
    assert any(m["role"] == "assistant" and "FROM agents" in m["content"] for m in messages[1:-1])


def test_rejecting_forgets_the_answer(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM orders"), fenced("SELECT COUNT(*) AS n FROM orders")])
    a.ask("How many orders?")
    a.reject("How many orders?")
    assert not a.ask("How many orders?").cached
    assert len(llm.calls) == 2


def test_follow_up_uses_history(make_assistant):
    a, llm = make_assistant([fenced("SELECT COUNT(*) AS n FROM support_tickets WHERE status = 'open'")])
    history = [{"question": "How many tickets?", "sql": "SELECT COUNT(*) FROM support_tickets"}]
    ans = a.ask("only the open ones", history)
    assert ans.ok
    roles = [m["role"] for m in llm.calls[0]]
    assert roles == ["system", "user", "assistant", "user"]


def test_bad_history_is_ignored(make_assistant):
    a, _ = make_assistant([fenced("SELECT 1 AS one")])
    assert a.ask("one", history=[{"question": 5}, "junk", None]).ok


def test_answer_text_comes_from_data():
    assert describe(QueryResult(["n"], [], False, 0), 50) == "No matching records found."
    assert describe(QueryResult(["total_amount"], [["1499.00"]], False, 0), 50) == "Total amount: 1499.00"
    assert describe(QueryResult(["a", "b"], [[1, None]], False, 0), 50) == "A: 1 · B: none"
    assert "first 50" in describe(QueryResult(["a"], [[1]] * 50, True, 0), 50)


def test_explanations_with_invented_numbers_are_dropped(make_assistant):
    ans = Answer(question="revenue in 2026?", rows=[["12500.50"]], columns=["revenue"], row_count=1)
    assert numbers_are_grounded("Revenue in 2026 was 12,500.50.", ans)
    assert not numbers_are_grounded("Revenue was 13,000.", ans)
    a, _ = make_assistant([fenced("SELECT COUNT(*) AS n FROM agents"), "There are 7 agents."],
                          explain_answers=True)
    out = a.ask("How many agents?")
    assert out.answer == "N: 4"  # the made-up 7 was rejected


def test_schema_changes_are_picked_up_by_refresh(make_assistant, demo_db):
    import sqlite3
    a, _ = make_assistant([])
    assert a.schema.find("refunds") is None
    con = sqlite3.connect(demo_db)
    con.execute("CREATE TABLE refunds (refund_id INTEGER PRIMARY KEY, amount NUMERIC)")
    con.commit()
    con.close()
    assert a.refresh_schema().find("refunds") is not None


def test_retrieval_picks_relevant_tables(make_assistant):
    from localdbchat.retrieval import TableRetriever
    a, _ = make_assistant([])
    retriever = TableRetriever(a.schema)
    names = [t.name for t in retriever.select("Which agent closed the most tickets?", 3)]
    assert "support_tickets" in names and "agents" in names
    names = [t.name for t in retriever.select("total revenue by product", 3)]
    assert "products" in names
