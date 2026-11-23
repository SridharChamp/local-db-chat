# Changelog

## 0.1.0

First public release.

- Ask questions in plain English through a web chat UI or the command line.
- Uses a local model through Ollama; refuses non-local model hosts and ignores proxy settings.
- Reads the schema (tables, views, columns, keys, comments) from the database catalog.
- PostgreSQL, MySQL/MariaDB, SQL Server and SQLite.
- Two layers of read-only protection: a strict SQL guard and read-only database sessions.
- Protected columns are hidden from the model and refused in queries.
- Query timeouts, row caps, self-correcting retries.
- Verified answers: mark an answer correct to reuse it and improve similar questions.
- Answer text built from the result rows, so it never invents numbers.
