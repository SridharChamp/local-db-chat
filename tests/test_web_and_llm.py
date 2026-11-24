import pytest
from fastapi.testclient import TestClient

from localdbchat.llm import LLMError, OllamaClient
from localdbchat.web.app import create_app


@pytest.fixture()
def client(make_assistant):
    a, llm = make_assistant(["```sql\nSELECT COUNT(*) AS n FROM agents\n```"])
    return TestClient(create_app(a), base_url="http://127.0.0.1:8765"), llm


def test_ui_and_assets_served(client):
    c, _ = client
    assert "local-db-chat" in c.get("/").text
    assert c.get("/app.js").status_code == 200
    r = c.get("/app.css")
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]


def test_ask_endpoint(client):
    c, _ = client
    data = c.post("/api/ask", json={"question": "How many agents?"}).json()
    assert data["ok"] and data["rows"] == [[4]]


def test_schema_endpoint_hides_protected_columns(client):
    c, _ = client
    tables = {t["name"]: t for t in c.get("/api/schema").json()["tables"]}
    assert "password_hash" not in tables["customers"]["columns"]


def test_dns_rebinding_blocked(client):
    c, _ = client
    assert c.get("/api/health", headers={"host": "evil.example.com"}).status_code == 403


def test_cross_site_requests_blocked(client):
    c, _ = client
    r = c.post("/api/ask", json={"question": "x"}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
    r = c.post("/api/ask", json={"question": "x"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_oversized_and_invalid_bodies(client):
    c, _ = client
    assert c.post("/api/ask", json={"question": "x" * 3000}).status_code == 422
    assert c.post("/api/ask", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    big = b'{"question": "' + b"x" * 300_000 + b'"}'
    assert c.post("/api/ask", content=big, headers={"content-type": "application/json"}).status_code == 413


def test_feedback_rejects_unsafe_sql(client):
    c, _ = client
    r = c.post("/api/feedback", json={"question": "q", "sql": "DROP TABLE customers", "correct": True})
    assert r.status_code == 400


def test_network_mode_requires_token(make_assistant):
    a, _ = make_assistant([])
    app = create_app(a, host="0.0.0.0", port=8765)
    c = TestClient(app, base_url="http://192.168.1.5:8765")
    assert app.state.token
    assert c.get("/api/health").status_code == 401
    assert c.get("/api/health", headers={"x-ldbc-token": "wrong"}).status_code == 401
    assert c.get("/api/health", headers={"x-ldbc-token": app.state.token}).status_code == 200


@pytest.mark.parametrize("url", ["http://10.0.0.5:11434", "http://api.example.com", "http://169.254.1.1"])
def test_llm_refuses_remote_hosts(url):
    with pytest.raises(LLMError, match="not this machine"):
        OllamaClient(url, "m")


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"])
def test_llm_accepts_loopback(url):
    OllamaClient(url, "m").close()


def test_llm_ignores_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corp-proxy:8080")
    client = OllamaClient("http://127.0.0.1:11434", "m")
    assert client._http._trust_env is False
    client.close()


def test_llm_not_running_message():
    client = OllamaClient("http://127.0.0.1:9", "m")  # nothing listens on port 9
    with pytest.raises(LLMError, match="not running"):
        client.chat([{"role": "user", "content": "hi"}])
    client.close()


def test_table_first_then_sentence(make_assistant):
    a, llm = make_assistant(["```sql\nSELECT COUNT(*) AS n FROM agents\n```", "There are 4 agents."],
                            explain_answers=True)
    c = TestClient(create_app(a), base_url="http://127.0.0.1:8765")
    first = c.post("/api/ask", json={"question": "How many agents?"}).json()
    assert first["ok"] and first["explain_pending"] and first["answer"] == "The answer is 4."
    assert len(llm.calls) == 1  # the sentence was not written yet: the table shows immediately
    body = {k: first[k] for k in ("question", "sql", "columns", "rows", "row_count", "truncated")}
    assert c.post("/api/explain", json=body).json() == {"answer": "There are 4 agents."}


def test_run_endpoint(make_assistant):
    a, _ = make_assistant([])
    c = TestClient(create_app(a), base_url="http://127.0.0.1:8765")
    ok = c.post("/api/run", json={"question": "agents", "sql": "SELECT name FROM agents ORDER BY name"}).json()
    assert ok["ok"] and ok["source"] == "edited" and ok["row_count"] == 4
    bad = c.post("/api/run", json={"question": "agents", "sql": "DROP TABLE agents"}).json()
    assert not bad["ok"] and "only select" in bad["error"].lower()
