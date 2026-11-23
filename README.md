# local-db-chat

[![tests](https://github.com/SridharChamp/local-db-chat/actions/workflows/tests.yml/badge.svg)](https://github.com/SridharChamp/local-db-chat/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Let your support team ask the product database questions in plain English, with a language model
that runs on your own machine. No data ever leaves it.**

Support teams spend hours a day waiting on developers for "can you check this in the database?".
local-db-chat lets them ask directly: "How many open tickets does Acme have?", "Which orders did Ravi
place last month?", "Is customer 4821 suspended?". Each answer comes with the exact SQL that produced it,
the result table, and a CSV download, so a developer can check it at a glance.

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

## Quick start

1. Install [Ollama](https://ollama.com/download), the free local model runner.
2. Install local-db-chat (Python 3.10+), with the driver for your database:
   ```bash
   pip install "local-db-chat[postgres] @ git+https://github.com/SridharChamp/local-db-chat.git"
   ```
   Use `[mysql]`, `[mssql]` or `[all]` instead of `[postgres]` as needed. SQLite needs nothing extra.
3. Start it:
   ```bash
   ldbc serve
   ```
4. Your browser opens the setup screen:
   - **Local model:** press *Download* once (a few GB, through Ollama).
   - **Database:** choose the type, then enter host, port, database name, user and password.
     Press *Test connection*, then *Save and start*. The app reads your schema on its own.
5. Ask away: *"How many open tickets are urgent?"*

Prefer the terminal? `ldbc setup` does the same steps there.
Just want to try it? `ldbc demo` creates a sample support database to connect to.

### Where is the model? Do I need to train it?

No training is needed. The app uses a standard open coding model (default: `qwen2.5-coder:7b`,
Apache-2.0) and gives it *your* schema at question time. One model therefore works for every database.
The model is downloaded by Ollama from its library on first run rather than stored in this repository,
because a model is several gigabytes, and GitHub isn't built for files that size.

## Use it with your own database

The setup screen writes a `config.yaml` for you. To edit it by hand, copy
[`config.example.yaml`](config.example.yaml) to `config.yaml` and set `database_url`:

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

## Rolling it out to a support team

**Option A: on each person's computer (most private).** Install Ollama and local-db-chat on each
machine and share one `config.yaml` (minus the password; use `LDBC_DATABASE_URL`). Each person runs
`ldbc serve`. Needs about 8 GB of free RAM per machine for a 7B model.

**Option B: one shared machine on your internal network.** Run it on a server with a GPU or plenty of
RAM. The team uses it from their browsers:

```bash
ldbc serve --host 0.0.0.0 --port 8765
```

It prints a link with an access token. Share that link only inside your company network. Put it behind
your VPN or reverse proxy, and never expose it to the internet.

**Getting good answers from day one**
1. Run `ldbc schema` and read what the model will see. Add `comment`s to cryptic tables and columns
   in your database, or add terms to `glossary`.
2. Ask the 20 questions your team asks most. Mark the correct ones **Correct**. They become instant,
   exact answers.
3. Move the best verified examples into `examples:` in the shared config so everyone benefits.

**Hardware**

| Setup | Speed per question |
|---|---|
| NVIDIA GPU (8 GB+) or Apple Silicon | 1–3 seconds |
| Modern CPU only, 16 GB RAM | 10–40 seconds with a 7B model; use a 3B model for faster replies |

**Ollama's own online features.** local-db-chat never sends your data anywhere. Ollama itself can
check for updates and offers optional cloud models. To keep it fully offline, set the environment
variable `OLLAMA_NO_CLOUD=1` before starting Ollama.

## Commands

| Command | What it does |
|---|---|
| `ldbc serve` | Web chat at <http://localhost:8765>; shows the setup screen on first run |
| `ldbc setup` | Connect a database and download the model, step by step in the terminal |
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
