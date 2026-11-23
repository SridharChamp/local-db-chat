# Contributing

Thanks for helping. Bug reports, new database dialects, better prompts and documentation are all welcome.

## Set up

```bash
git clone https://github.com/SridharChamp/local-db-chat.git
cd local-db-chat
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
```

The unit tests use a scripted stand-in for the model and a generated SQLite database. You don't need
Ollama, a GPU or a database server to run them.

### Testing against real database servers

`tests/test_integration.py` runs against real PostgreSQL, MySQL/MariaDB and SQL Server when you give it
connection URLs. The user must be allowed to create tables:

```bash
docker run -d --name ldbc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ldbc -p 5432:5432 postgres:16
export LDBC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ldbc
pip install -e ".[postgres]"
pytest tests/test_integration.py -rs
```

CI runs these against all four servers on every pull request.

## Ground rules

- **Safety first.** Any change to `safety.py` or `db.py` needs tests showing both what is now allowed
  and what is still refused. When in doubt, refuse.
- **Nothing leaves the machine.** No telemetry, no external calls, no CDN assets in the web UI.
- **Never invent data.** Answer text must be traceable to the result rows.
- **Keep memory bounded.** Every cache and list that grows with use needs a size limit.
- Keep pull requests focused, and add a line to `CHANGELOG.md`.

## Adding a database dialect

1. Map its SQLAlchemy backend name to a sqlglot dialect in `db.py` (`SQLGLOT_DIALECTS`).
2. Add session-level read-only and timeout settings in `Database._install_session_guards`.
3. Add its dangerous functions to `BLOCKED_FUNCTIONS` in `safety.py`.
4. Add dialect hints to `DIALECT_NOTES` in `prompts.py`.
5. Add allowed and refused cases to `tests/test_dialects.py`, and a service to the CI workflow.
