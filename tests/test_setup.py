import pytest
import yaml
from conftest import FakeLLM
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from localdbchat.config import Config, load_config
from localdbchat.engine import Assistant
from localdbchat.setup import ConnectionFields, SetupError, build_url, save_database_url, try_connection
from localdbchat.web.app import create_app


# ------------------------------------------------------------ URL building
def test_password_with_special_characters_round_trips():
    pw = "p@ss:w/rd#?%&= 'x\""
    url = build_url(ConnectionFields("postgresql", host="db.local", database="app", user="ro", password=pw))
    parsed = make_url(url)
    assert parsed.password == pw and parsed.host == "db.local" and parsed.port == 5432
    assert parsed.query["sslmode"] == "prefer"


def test_defaults_and_options():
    url = make_url(build_url(ConnectionFields("mysql", host="10.0.0.5", database="shop", user="ro", ssl=True)))
    assert url.port == 3306 and url.query["ssl"] == "true" and url.query["charset"] == "utf8mb4"
    url = make_url(build_url(ConnectionFields("postgresql", host="h", port=6543, database="d", user="u", ssl=True)))
    assert url.port == 6543 and url.query["sslmode"] == "require"


@pytest.mark.parametrize("fields,match", [
    (ConnectionFields("postgresql", database="d", user="u"), "host"),
    (ConnectionFields("postgresql", host="h", user="u"), "database name"),
    (ConnectionFields("postgresql", host="h", database="d"), "user"),
    (ConnectionFields("postgresql", host="h; rm -rf /", database="d", user="u"), "not allowed"),
    (ConnectionFields("postgresql", host="h", port=70000, database="d", user="u"), "between"),
    (ConnectionFields("oracle", host="h", database="d", user="u"), "Unknown database type"),
    (ConnectionFields("sqlite"), "path"),
    (ConnectionFields("sqlite", path="C:/nope/missing.db"), "No file"),
])
def test_validation(fields, match):
    with pytest.raises(SetupError, match=match):
        build_url(fields)


def test_sqlite_path_with_quotes_and_spaces(tmp_path, demo_db):
    target = tmp_path / "my data" / "app.db"
    target.parent.mkdir()
    target.write_bytes(demo_db.read_bytes())
    url = build_url(ConnectionFields("sqlite", path=f'"{target}"'))
    assert try_connection(url).ok


def test_try_connection_reports_problems(tmp_path):
    import sqlite3
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    result = try_connection(f"sqlite:///{empty.as_posix()}")
    assert not result.ok and "no tables" in result.message
    result = try_connection("postgresql+psycopg://u:secret@127.0.0.1:1/x")
    assert not result.ok and "secret" not in result.message


def test_save_keeps_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: llama3.1:8b\nglossary:\n  vip: customers.tier = 'gold'\n", encoding="utf-8")
    save_database_url(path, "sqlite:///x.db")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"model": "llama3.1:8b", "glossary": {"vip": "customers.tier = 'gold'"},
                    "database_url": "sqlite:///x.db"}
    assert load_config(path).database_url == "sqlite:///x.db"


def test_save_refuses_to_clobber_broken_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: [unclosed", encoding="utf-8")
    with pytest.raises(SetupError, match="not valid YAML"):
        save_database_url(path, "sqlite:///x.db")
    assert path.read_text(encoding="utf-8") == "model: [unclosed"


# ------------------------------------------------------------ web first run
class FakeOllama:
    def __init__(self, installed=False):
        self.installed = installed

    def has_model(self, name=None):
        return self.installed

    def pull(self, name=None, on_progress=None):
        for done in (0, 50, 100):
            on_progress("pulling", done, 100)
        self.installed = True

    def close(self):
        pass


@pytest.fixture()
def first_run(tmp_path):
    ollama = FakeOllama()
    replies = FakeLLM(["```sql\nSELECT COUNT(*) AS n FROM agents\n```"])
    cfg = Config(learned_examples_file=str(tmp_path / "learned.json"))
    app = create_app(None, cfg=cfg, config_path=str(tmp_path / "config.yaml"),
                     assistant_factory=lambda c: Assistant(c, llm=replies),
                     llm_factory=lambda c: ollama)
    return TestClient(app, base_url="http://127.0.0.1:8765"), tmp_path, ollama


def test_first_run_flow(first_run, demo_db):
    client, tmp_path, ollama = first_run
    status = client.get("/api/status").json()
    assert status["configured"] is False and status["model_installed"] is False
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 409

    # Download the model
    assert client.post("/api/model/pull", json={}).json()["started"]
    for _ in range(50):
        status = client.get("/api/status").json()
        if not status["pull"]["running"]:
            break
    assert status["model_installed"] and status["pull"]["done"]

    # Test, then save the database
    body = {"kind": "sqlite", "path": str(demo_db)}
    assert client.post("/api/setup/test", json=body).json()["ok"]
    saved = client.post("/api/setup/save", json=body).json()
    assert saved["ok"] and saved["tables"] == 6
    assert "database_url" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    status = client.get("/api/status").json()
    assert status["configured"] and status["dialect"] == "sqlite"
    answer = client.post("/api/ask", json={"question": "How many agents?"}).json()
    assert answer["ok"] and answer["rows"] == [[4]]


def test_bad_connection_is_not_saved(first_run, tmp_path):
    client, cfg_dir, _ = first_run
    import sqlite3
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    r = client.post("/api/setup/save", json={"kind": "sqlite", "path": str(empty)})
    assert r.status_code == 400 and "no tables" in r.json()["error"]
    assert not (cfg_dir / "config.yaml").exists()


def test_invalid_fields_are_explained(first_run):
    client, _, _ = first_run
    r = client.post("/api/setup/test", json={"kind": "sqlite", "path": ""})
    assert r.status_code == 409 and "path" in r.json()["error"]
    r = client.post("/api/setup/test", json={"kind": "postgresql", "host": "h", "port": 0})
    assert r.status_code == 422


def test_setup_is_refused_from_other_machines(demo_db, tmp_path):
    app = create_app(None, cfg=Config(), config_path=str(tmp_path / "c.yaml"), host="0.0.0.0",
                     llm_factory=lambda c: FakeOllama())
    client = TestClient(app, base_url="http://192.168.1.9:8765", client=("192.168.1.20", 50000))
    r = client.post("/api/setup/save", json={"kind": "sqlite", "path": str(demo_db)},
                    headers={"x-ldbc-token": app.state.token})
    assert r.status_code == 403 and "machine running" in r.json()["error"]


def test_status_when_ollama_is_missing(tmp_path):
    from localdbchat.llm import LLMError

    def broken(_cfg):
        class Down(FakeOllama):
            def has_model(self, name=None):
                raise LLMError("Ollama is not running at http://127.0.0.1:11434.")
        return Down()
    app = create_app(None, cfg=Config(), config_path=str(tmp_path / "c.yaml"), llm_factory=broken)
    status = TestClient(app, base_url="http://127.0.0.1:8765").get("/api/status").json()
    assert status["ollama_running"] is False and "not running" in status["ollama_error"]
