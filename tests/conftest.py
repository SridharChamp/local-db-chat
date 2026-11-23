import sqlite3

import pytest

from localdbchat.config import Config
from localdbchat.db import Database
from localdbchat.demo import build
from localdbchat.engine import Assistant
from localdbchat.schema import introspect


class FakeLLM:
    """Plays back scripted replies and records every prompt it was given."""

    embed_model = ""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.kwargs = []

    def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])  # snapshot: the engine keeps appending
        self.kwargs.append(kwargs)
        if not self.replies:
            raise AssertionError("FakeLLM ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def embed(self, texts):
        raise AssertionError("not used")

    def close(self):
        pass


@pytest.fixture()
def demo_db(tmp_path):
    return build(tmp_path / "demo.db")


@pytest.fixture()
def db(demo_db):
    database = Database(f"sqlite:///{demo_db.as_posix()}", timeout_seconds=5, max_rows=50)
    yield database
    database.dispose()


@pytest.fixture()
def schema(db):
    return introspect(db, sensitive_patterns=Config().sensitive_columns)


@pytest.fixture()
def make_assistant(demo_db, tmp_path):
    made = []

    def _make(replies=None, **overrides):
        overrides.setdefault("explain_answers", False)  # tests script each model reply; opt in to the sentence
        cfg = Config(database_url=f"sqlite:///{demo_db.as_posix()}", max_rows=50, timeout_seconds=5,
                     learned_examples_file=str(tmp_path / "learned.json"), **overrides)
        llm = FakeLLM(replies)
        a = Assistant(cfg, llm=llm)
        made.append(a)
        return a, llm

    yield _make
    for a in made:
        a.db.dispose()


@pytest.fixture()
def weird_db(tmp_path):
    """Awkward names: spaces, mixed case, reserved words, a view, no primary key, unicode."""
    path = tmp_path / "weird.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE "Order Items" ("Item ID" INTEGER PRIMARY KEY, "user" TEXT, "Qty" INTEGER);
        CREATE TABLE "group" (id INTEGER, name TEXT);
        CREATE TABLE log_entries (msg TEXT, payload BLOB, api_token TEXT);
        CREATE TABLE café (prix REAL);
        CREATE VIEW big_items AS SELECT * FROM "Order Items" WHERE "Qty" > 5;
        INSERT INTO "Order Items" VALUES (1, 'ann', 7), (2, 'bob', 1);
        INSERT INTO log_entries VALUES ('hello', x'00ff', 'secret-123');
        INSERT INTO café VALUES (3.5);
    """)
    con.commit()
    con.close()
    return path
