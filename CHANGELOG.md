# Changelog

## Unreleased

- Answers are written as plain sentences ("We have 90 active customers."). Every number in a sentence
  must appear in the result, with correct digit grouping, and no currency the data doesn't have.
  Otherwise the exact data-built answer is kept.
- The web UI shows the result table first and fills in the sentence a moment later.
- Questions about the structure ("How many tables are there?", "What columns does orders have?") are
  answered from the loaded schema, instantly and in full sentences.

- Faster answers: the full schema is loaded into the model once when you connect (and on start or
  refresh). Every question reuses that work, so each one only pays for itself.
- The model stops right after the SQL, with a capped reply length.
- Each answer reports the model's read and write time and token counts.
- More precise answers: short text columns list their real values (e.g. priority: low, medium, high,
  urgent), the model only adds date filters when the question names a time period, and glossary terms
  are applied as exact conditions.

- First-run setup screen in the browser: enter database details, test, save. No config file editing.
- The model downloads through the local Ollama with a progress bar.
- `ldbc setup`: the same steps in the terminal.
- `ldbc serve` opens the browser and starts even when nothing is configured yet.
- Changing the database connection is only allowed from the machine running the app.

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
