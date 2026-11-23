# local-db-chat

**Ask your database questions in plain English, using a language model that runs on your own computer.
No data ever leaves the machine.**

Built for support teams: "How many open tickets does Acme have?", "Which orders did Ravi place last
month?", "Top 5 products by revenue this year". Each answer comes with the exact SQL that produced it,
the result table, and a CSV download.

- **Private.** The model runs locally through [Ollama](https://ollama.com). The app refuses to send
  anything to a non-local model host and ignores proxy settings. It works with the network unplugged.
- **Learns your schema on its own.** It reads tables, views, columns, keys and comments from the
  database catalog at startup, and again whenever you press *Refresh*.
- **Read-only by design.** Every query goes through a strict SQL guard *and* a read-only database session.
- **Precise.** The answer text is built from the result rows, so it never invents numbers. Answers you
  mark *Correct* are saved and reused, so it gets more accurate the more your team uses it.
- **Fast.** The schema is read in batched catalog calls, sample rows load only for tables a question needs,
  repeated questions skip the model, and the model stays loaded between questions.
- **PostgreSQL, MySQL / MariaDB, SQL Server and SQLite.**

## Quick start (5 minutes, with a demo database)

1. Install [Ollama](https://ollama.com/download) and pull a model:
   ```bash
   ollama pull qwen2.5-coder:7b
   ```
2. Install local-db-chat (Python 3.10+):
   ```bash
   pip install git+https://github.com/SridharChamp/local-db-chat.git
   ```
3. Create the sample support database, check the setup, and start the chat:
   ```bash
   ldbc demo
   ldbc doctor
   ldbc serve
   ```
4. Open <http://localhost:8765> and ask *"How many open tickets are urgent?"*

## Use it with your own database

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` and set `database_url`:

| Database | URL format | Driver |
|---|---|---|
| PostgreSQL | `postgresql+psycopg://user:pass@host:5432/dbname` | `pip install "local-db-chat[postgres]"` |
| MySQL / MariaDB | `mysql+pymysql://user:pass@host:3306/dbname` | `pip install "local-db-chat[mysql]"` |
| SQL Server | `mssql+pyodbc://user:pass@host/dbname?driver=ODBC+Driver+18+for+SQL+Server` | `pip install "local-db-chat[mssql]"` plus the Microsoft ODBC driver |
| SQLite | `sqlite:///path/to/file.db` | built in |

You can keep the password out of the file with an environment variable:
`LDBC_DATABASE_URL=postgresql+psycopg://...`

### Create a read-only database user (strongly recommended)

The app already blocks writes in two layers, but the strongest protection is a user that *cannot* write:

```sql
-- PostgreSQL
CREATE ROLE support_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE app TO support_ro;
GRANT USAGE ON SCHEMA public TO support_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO support_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO support_ro;

-- MySQL / MariaDB
CREATE USER 'support_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'support_ro'@'%';

-- SQL Server
CREATE LOGIN support_ro WITH PASSWORD = '...';
CREATE USER support_ro FOR LOGIN support_ro;
ALTER ROLE db_datareader ADD MEMBER support_ro;
```

A read replica is even better: support queries then never compete with production traffic.

### Teach it your business words

Support staff say "churned customer". The database says `status = 'C'`. Bridge the gap in `config.yaml`:

```yaml
glossary:
  churned customer: customers.status = 'C'
  open ticket: support_tickets.status in ('open', 'in_progress')
  revenue: sum of orders.total_amount where orders.status = 'paid'

examples:
  - question: How many active customers do we have?
    sql: SELECT COUNT(*) FROM customers WHERE status = 'A'
```

In the chat, pressing **Correct** under an answer saves that question and SQL as a verified example.
The same question is then answered instantly without the model, and similar questions get it as a
worked example. Verified examples are stored in `.ldbc/learned.json`. Move the good ones into
`examples:` in your config to share them with your team.

## Commands

| Command | What it does |
|---|---|
| `ldbc serve` | Web chat at <http://localhost:8765> |
| `ldbc chat` | Chat in the terminal (`:sql`, `:new`, `:refresh`, `:tables`, `:quit`) |
| `ldbc ask "question" [--sql] [--json]` | One question; `--json` is handy for scripts |
| `ldbc schema [--samples]` | Shows exactly what the model is told about your database |
| `ldbc doctor` | Checks the database connection, protected columns, Ollama and the model |
| `ldbc demo` | Creates a sample support database and config |

## How it keeps your data safe

**Nothing leaves the machine**
- The model host must be `localhost`, `127.0.0.1` or `::1`. Anything else is refused
  unless you explicitly set `allow_remote_llm: true` for a server you own.
- HTTP proxy environment variables are ignored, so a corporate proxy can't route prompts elsewhere.
- No telemetry, no analytics, no CDN: the web UI is three local files.

**Read-only, twice**
1. *SQL guard* (`safety.py`). The query must parse and be exactly one `SELECT`/`UNION`/`INTERSECT`/`EXCEPT`.
   It refuses writes, DDL, `SELECT INTO`, `FOR UPDATE`, writes hidden in CTEs, `EXEC`, `COPY`, `PRAGMA`,
   `ATTACH`, `SET`, system catalogs, cross-database names and dangerous functions (`pg_sleep`,
   `pg_read_file`, `dblink`, `LOAD_FILE`, `BENCHMARK`, `xp_cmdshell`, `OPENROWSET`, ...). Every table and
   column must exist in the discovered schema, which also catches made-up names before they reach the database.
2. *Read-only session* (`db.py`). PostgreSQL and MySQL sessions are set to `READ ONLY`, and SQLite
   uses `query_only`. Every connection is rolled back after use. SQL Server has no session-level
   read-only switch, so use a `db_datareader` user there.

**Bounded**
- Statement timeout on every query (`timeout_seconds`), a row cap (`max_rows`), and a cap on cell size.

**Protected columns**
- Columns whose names contain `password`, `token`, `secret`, `card_number`, `cvv`, `ssn`, `otp`, `salt`,
  `hash` and similar are hidden from the model and refused in queries, including via `SELECT *`.
  Add your own with exact `table.column` names or globs in `sensitive_columns`.

**Web UI**
- Listens on `127.0.0.1` only. It checks the Host and Origin headers, which blocks DNS-rebinding and
  cross-site attacks from other browser tabs. It sends a strict Content-Security-Policy and renders all
  data as text, never as HTML.
- `ldbc serve --host 0.0.0.0` shares it on your network. An access token is then generated and required.

## Choosing a model

| Model | RAM | Notes |
|---|---|---|
| `qwen2.5-coder:7b` | ~8 GB | Good default; strong at SQL |
| `qwen2.5-coder:14b` | ~16 GB | Noticeably better on multi-table joins |
| `qwen2.5-coder:32b` | ~32 GB | Best accuracy if you have the hardware |
| `llama3.1:8b` | ~8 GB | Good general alternative |

Any Ollama chat model works. Set `model:` in `config.yaml`. For very large schemas (hundreds of tables),
also set `embed_model: nomic-embed-text` (after `ollama pull nomic-embed-text`) for better table matching.

## How it works

```
question ─▶ pick relevant tables (keywords + foreign keys, optional embeddings)
         ─▶ prompt: schema + sample rows + glossary + verified examples + today's date
         ─▶ local model writes SQL (temperature 0: same question, same SQL)
         ─▶ SQL guard ─┬─ refused ─▶ reason sent back to the model (up to max_retries)
                       └─ ok ─▶ read-only session, timeout, row cap
                                 ├─ DB error ─▶ error sent back to the model
                                 └─ rows ─▶ answer text built from the rows + table + SQL
```

## Development

```bash
git clone https://github.com/SridharChamp/local-db-chat.git
cd local-db-chat
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

The tests use a scripted stand-in for the model, so they need neither Ollama nor a GPU.

## License

[MIT](LICENSE)
