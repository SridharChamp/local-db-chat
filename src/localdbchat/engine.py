"""The question -> SQL -> answer pipeline."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import Config
from .db import Database, DatabaseError, QueryResult
from .llm import LLMError, OllamaClient
from .memory import ExampleStore, normalise
from .meta import answer_schema_question
from .prompts import (
    CANNOT_ANSWER,
    EXPLAIN,
    WARM_UP_QUESTION,
    build_messages,
    build_system,
    render_schema,
    retry_message,
    schema_char_budget,
)
from .retrieval import TableRetriever
from .safety import UnsafeQueryError, check_sql, extract_sql
from .schema import SampleCache, SchemaInfo, introspect
from .util import redact_url

log = logging.getLogger("localdbchat")

MAX_QUESTION_CHARS = 2000
MAX_HISTORY_TURNS = 4
SQL_CACHE_SIZE = 256
MAX_SQL_TOKENS = 512          # a reply longer than this is rambling, not SQL
SQL_STOP = ["\n```\n"]        # stop right after the closing fence; never pay for explanations
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# Digit grouping that reads correctly: Western 2,799,425 or Indian 27,99,425.
_GROUPED = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?$")
_CURRENCY = ("$", "₹", "€", "£", "¥", "Rs.", "INR", "USD", "EUR")
EXPLAIN_MAX_ROWS = 20
EXPLAIN_MAX_TOKENS = 120


@dataclass
class Answer:
    question: str
    answer: str = ""
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    tables: list[str] = field(default_factory=list)
    attempts: int = 0
    cached: bool = False
    verified: bool = False   # SQL came from a person-confirmed example
    cannot_answer: bool = False
    error: str = ""
    problems: list[str] = field(default_factory=list)  # why earlier attempts were refused
    timings_ms: dict[str, int] = field(default_factory=dict)
    # How the model spent its time: prompt_tokens it had to read (small when the
    # schema was already preloaded), read_ms, output_tokens, write_ms.
    model: dict[str, int] = field(default_factory=dict)
    schema_in_prompt: str = ""  # "full" (preloaded, reused) or "selected" (picked per question)
    source: str = ""            # "schema" when answered from the known structure, no query run
    articulated: bool = False   # answer text written by the model and checked against the data

    @property
    def ok(self) -> bool:
        return not self.error and not self.cannot_answer

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class Assistant:
    def __init__(self, cfg: Config, *, db: Database | None = None, llm: OllamaClient | None = None):
        self.cfg = cfg
        self.db = db or Database(cfg.database_url, timeout_seconds=cfg.timeout_seconds,
                                 max_rows=cfg.max_rows)
        self.llm = llm or OllamaClient(cfg.ollama_url, cfg.model, embed_model=cfg.embed_model,
                                       timeout=cfg.llm_timeout_seconds, num_ctx=cfg.num_ctx,
                                       keep_alive=cfg.keep_alive, allow_remote=cfg.allow_remote_llm)
        self.examples = ExampleStore(learned_file_for(cfg), static=cfg.examples,
                                     max_items=cfg.max_learned_examples)
        self.samples = SampleCache(self.db, cfg.sample_rows)
        self._schema: SchemaInfo | None = None
        self._retriever: TableRetriever | None = None
        self._prefix: str | None = None  # fixed rules + full schema, identical for every question
        self._glossary: dict[str, str] = {}  # the configured glossary, minus terms for other databases
        self.preloaded = False
        self._schema_lock = threading.Lock()
        self._sql_cache: OrderedDict[str, tuple[str, list[str]]] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ----------------------------------------------------------------- schema
    @property
    def schema(self) -> SchemaInfo:
        if self._schema is None:
            self.refresh_schema()
        return self._schema  # type: ignore[return-value]

    def refresh_schema(self) -> SchemaInfo:
        with self._schema_lock:
            schema = introspect(self.db, schemas=self.cfg.schemas,
                                include_tables=self.cfg.include_tables,
                                exclude_tables=self.cfg.exclude_tables,
                                sensitive_patterns=self.cfg.sensitive_columns)
            glossary = glossary_for(self.cfg.glossary, schema)
            retriever = TableRetriever(schema, glossary, self.llm)
            # Swap atomically; requests in flight keep the old objects.
            self.samples.clear()
            self._glossary = glossary
            prefix = self._build_prefix(schema)
            self._schema, self._retriever, self._prefix = schema, retriever, prefix
            self.preloaded = False
            with self._cache_lock:
                self._sql_cache.clear()
            for w in schema.warnings:
                log.warning(w)
            return schema

    def _build_prefix(self, schema: SchemaInfo) -> str | None:
        """The whole schema as one fixed block, if it fits the model's context window."""
        if len(schema) == 0:
            return None
        # Most-connected tables first: when space is short, they keep their sample rows.
        ordered = sorted(schema.tables.values(), key=lambda t: (-len(schema.neighbours(t)), t.key))
        budget = schema_char_budget(self.cfg.num_ctx, self._glossary)
        text = render_schema(schema, ordered, self.samples, budget, require_all=True)
        if text is None:
            log.warning("The schema (%d tables) is too big to preload into a %d-token context; relevant "
                        "tables will be picked per question. Raise num_ctx to preload it all.",
                        len(schema), self.cfg.num_ctx)
            return None
        return build_system(schema, text, self._glossary)

    @property
    def schema_preloadable(self) -> bool:
        _ = self.schema  # load it first; before that there is nothing to judge
        return self._prefix is not None

    def preload(self) -> dict:
        """Have the model read the schema now, so questions only pay for themselves.

        Ollama keeps the processed start of the last prompt. Every question starts
        with this same block, so after this call each question skips re-reading it.
        """
        _ = self.schema
        prefix = self._prefix
        if prefix is None:
            return {}
        reply = self.llm.chat(build_messages(prefix, WARM_UP_QUESTION, examples=[], history=[]),
                              max_tokens=1, allow_empty=True)
        self.preloaded = True
        return dict(getattr(reply, "stats", {}) or {})

    # ------------------------------------------------------------------- ask
    def ask(self, question: str, history: list[dict] | None = None, *, explain: bool | None = None) -> Answer:
        """Answer a question. explain=False skips the written sentence (the web UI asks for it
        separately so the table shows first); None follows the explain_answers setting."""
        started = time.perf_counter()
        question = (question or "").strip()
        ans = Answer(question=question)
        if not question:
            ans.error = "Please type a question."
            return ans
        if len(question) > MAX_QUESTION_CHARS:
            ans.error = f"The question is too long (max {MAX_QUESTION_CHARS} characters)."
            return ans
        history = _clean_history(history)

        try:
            schema = self.schema
            retriever = self._retriever
            if len(schema) == 0:
                ans.error = "No tables are available. " + " ".join(schema.warnings)
                return ans
            self._answer(ans, schema, retriever, history)
            want = self.cfg.explain_answers if explain is None else explain
            if want and ans.ok and ans.sql and ans.source != "schema":
                t = time.perf_counter()
                self._explain(ans)
                ans.timings_ms["explain"] = _ms(t)
        except (DatabaseError, LLMError) as exc:
            ans.error = str(exc)
        except Exception:  # never let one bad question take the service down
            log.exception("Unexpected error answering %r", question)
            ans.error = "Something went wrong while answering. The details are in the server log."
        ans.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        return ans

    def _answer(self, ans: Answer, schema: SchemaInfo, retriever: TableRetriever,
                history: list[dict]) -> None:
        cache_key = _cache_key(ans.question, history)

        # 0. Questions about the structure itself ("how many tables?") come straight from
        #    the loaded schema: exact, instant, and no system catalog is ever queried.
        meta = answer_schema_question(ans.question, schema)
        if meta is not None:
            ans.answer, ans.columns, ans.rows = meta.answer, meta.columns, meta.rows
            ans.row_count, ans.source = len(meta.rows), "schema"
            return

        # 1. A person-verified SQL for this exact question wins: fastest and most precise.
        if not history:
            verified = self.examples.exact(ans.question)
            if verified and self._try_known(ans, schema, verified["sql"], verified=True):
                return

        # 2. Same question asked before in this session: reuse the validated SQL, run fresh.
        with self._cache_lock:
            hit = self._sql_cache.get(cache_key)
            if hit:
                self._sql_cache.move_to_end(cache_key)
        if hit and self._try_known(ans, schema, hit[0], verified=False):
            ans.cached = True
            return

        # 3. Generate with the model, feeding errors back so it can correct itself.
        t0 = time.perf_counter()
        prefix = self._prefix
        if prefix is not None:
            system, ans.schema_in_prompt = prefix, "full"
        else:
            tables = retriever.select(ans.question, self.cfg.max_tables_in_prompt)
            budget = schema_char_budget(self.cfg.num_ctx, self._glossary)
            system = build_system(schema, render_schema(schema, tables, self.samples, budget) or "",
                                  self._glossary)
            ans.schema_in_prompt = "selected"
        examples = [] if history else [e for e in self.examples.similar(ans.question, 6)
                                       if _fits(e["sql"], schema, self.cfg.max_rows)][:3]
        messages = build_messages(system, ans.question, examples=examples, history=history)
        ans.timings_ms["prompt"] = _ms(t0)

        last_problem = ""
        llm_ms = db_ms = 0
        for attempt in range(1, self.cfg.max_retries + 2):
            ans.attempts = attempt
            t1 = time.perf_counter()
            reply = self.llm.chat(messages, max_tokens=MAX_SQL_TOKENS, stop=SQL_STOP)
            llm_ms += _ms(t1)
            for key, value in (getattr(reply, "stats", None) or {}).items():
                ans.model[key] = ans.model.get(key, 0) + value
            messages.append({"role": "assistant", "content": reply})

            raw_sql = extract_sql(reply)
            refusal = next((t for t in (reply.strip(), raw_sql) if t.upper().startswith(CANNOT_ANSWER)), None)
            if refusal is not None:
                ans.cannot_answer = True
                reason = refusal[len(CANNOT_ANSWER):].lstrip(" :").strip().rstrip(".!")
                if reason.startswith("<") and reason.endswith(">"):
                    reason = ""  # the model copied a placeholder; no real reason given
                ans.answer = ("I can't answer that from this database"
                              + (f": {reason}." if reason else ".")
                              + " Try rephrasing, or add the term to the glossary.")
                break

            try:
                sql, used = check_sql(raw_sql, schema, max_rows=self.cfg.max_rows)
            except UnsafeQueryError as exc:
                last_problem = str(exc)
                ans.problems.append(last_problem)
                ans.sql = raw_sql
                messages.append(retry_message(last_problem))
                continue

            ans.sql, ans.tables = sql, used
            t2 = time.perf_counter()
            try:
                result = self.db.run(sql)
            except DatabaseError as exc:
                db_ms += _ms(t2)
                if exc.connection:
                    raise
                last_problem = str(exc)
                ans.problems.append(last_problem)
                messages.append(retry_message(
                    last_problem + (" Make the query simpler or more selective." if exc.timeout else "")))
                continue
            db_ms += _ms(t2)
            self._fill(ans, result)
            with self._cache_lock:
                self._sql_cache[cache_key] = (sql, used)
                self._sql_cache.move_to_end(cache_key)
                while len(self._sql_cache) > SQL_CACHE_SIZE:
                    self._sql_cache.popitem(last=False)
            break
        else:
            ans.error = f"Could not build a working query after {ans.attempts} tries. Last problem: {last_problem}"

        ans.timings_ms["llm"] = llm_ms
        ans.timings_ms["db"] = db_ms


    def _try_known(self, ans: Answer, schema: SchemaInfo, sql: str, *, verified: bool) -> bool:
        """Run SQL we already trust. False if the schema moved on and it no longer fits."""
        try:
            safe, used = check_sql(sql, schema, max_rows=self.cfg.max_rows)
            t = time.perf_counter()
            result = self.db.run(safe)
            ans.timings_ms["db"] = _ms(t)
        except UnsafeQueryError:
            return False
        except DatabaseError as exc:
            if exc.connection:
                raise
            return False
        ans.sql, ans.tables, ans.verified, ans.attempts = safe, used, verified, 0
        self._fill(ans, result)
        return True

    def _fill(self, ans: Answer, result: QueryResult) -> None:
        ans.columns, ans.rows = result.columns, result.rows
        ans.row_count, ans.truncated = result.row_count, result.truncated
        ans.answer = describe(result, self.cfg.max_rows)

    def articulate(self, question: str, sql: str, columns: list[str], rows: list[list],
                   row_count: int, truncated: bool) -> str | None:
        """One or two plain sentences answering the question from these rows.

        Returns None when the model's sentence cannot be trusted: any number or
        currency in it that is not in the result, question or SQL rejects it, and
        the exact data-built answer stays. A wrong number is worse than no prose.
        """
        shown = rows[:EXPLAIN_MAX_ROWS]
        prompt = EXPLAIN.format(
            question=question[:MAX_QUESTION_CHARS], sql=sql[:4000], columns=", ".join(columns[:30]),
            count=row_count, more=", more exist" if truncated else "",
            rows="\n".join(" | ".join("NULL" if v is None else str(v)[:80] for v in r[:30]) for r in shown)
            or "(no rows)")
        prefix = self._prefix
        if prefix is not None:
            # Continue the conversation that starts with the preloaded schema block. A separate
            # prompt would push that block out of Ollama's cache and slow down the next question.
            messages = build_messages(prefix, question, examples=[], history=[])
            messages += [{"role": "assistant", "content": f"```sql\n{sql}\n```"},
                         {"role": "user", "content": prompt}]
        else:
            messages = [{"role": "user", "content": prompt}]
        try:
            text = self.llm.chat(messages, max_tokens=EXPLAIN_MAX_TOKENS)
        except LLMError:
            return None
        text = " ".join(str(text).split()).strip().strip('"')
        if not text or len(text) > 600 or text.upper().startswith(("SELECT", "WITH", "```", CANNOT_ANSWER)):
            return None
        return text if is_grounded(text, question, sql, rows, row_count) else None

    def _explain(self, ans: Answer) -> None:
        # The sentence is optional polish: whatever goes wrong here, the answer already found stays.
        try:
            text = self.articulate(ans.question, ans.sql, ans.columns, ans.rows, ans.row_count, ans.truncated)
        except Exception:
            log.exception("Could not write the answer sentence for %r; keeping the exact answer", ans.question)
            return
        if text:
            ans.answer, ans.articulated = text, True

    # -------------------------------------------------------------- feedback
    def confirm(self, question: str, sql: str) -> None:
        """A person says this SQL answers this question correctly."""
        safe, _ = check_sql(sql, self.schema, max_rows=self.cfg.max_rows)
        self.examples.add(question, safe)

    def reject(self, question: str) -> None:
        self.examples.remove(question)
        key_prefix = normalise(question) + "|"
        with self._cache_lock:
            for key in [k for k in self._sql_cache if k.startswith(key_prefix)]:
                del self._sql_cache[key]

    def close(self) -> None:
        self.llm.close()
        self.db.dispose()


_GENERIC_LABELS = {"n", "count", "cnt", "total", "value", "result", "count(*)", "num", "number", "sum"}


def describe(result: QueryResult, max_rows: int) -> str:
    """A plain answer built only from the rows, so it can never invent a number."""
    if result.row_count == 0:
        return "Nothing matched your question."
    if result.row_count == 1 and len(result.columns) == 1:
        value = result.rows[0][0]
        shown = "none" if value is None else value
        if result.columns[0].strip().lower() in _GENERIC_LABELS:
            return f"The answer is {shown}."
        return f"{_label(result.columns[0])}: {shown}"
    if result.row_count == 1:
        return " · ".join(f"{_label(c)}: {'none' if v is None else v}"
                          for c, v in zip(result.columns, result.rows[0], strict=True))
    if result.truncated:
        return (f"Here are the first {max_rows} results. There are more, so ask a narrower question "
                "to see them all.")
    return f"Here are the {result.row_count} matching results."


def is_grounded(text: str, question: str, sql: str, rows: list[list], row_count: int) -> bool:
    """Every number and currency symbol in `text` must come from the data, question or SQL."""
    sources = [question, sql, str(row_count), str(len(rows))]
    sources += [str(v) for row in rows for v in row]
    allowed = {_norm_num(n) for src in sources for n in _NUMBER.findall(src)}
    for n in _NUMBER.findall(text):
        n = n.rstrip(",")  # a comma ending a clause is punctuation, not part of the number
        if "," in n and not _GROUPED.match(n):
            return False  # misplaced separators ("279,9425") misstate the number
        if _norm_num(n) not in allowed:
            return False
    joined = " ".join(sources)
    return all(sym not in text or sym in joined for sym in _CURRENCY)


def numbers_are_grounded(text: str, ans: Answer) -> bool:
    return is_grounded(text, ans.question, ans.sql, ans.rows, ans.row_count)


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def _label(column: str) -> str:
    text = column.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else column


def learned_file_for(cfg: Config) -> str:
    """Verified answers are kept per database, so one database's SQL never coaches another.

    The default file name gets a short fingerprint of the connection (without the password).
    An explicitly configured file is used as is.
    """
    path = cfg.learned_examples_file
    if path != Config().learned_examples_file or not cfg.database_url:
        return path
    try:
        u = make_url(cfg.database_url)
        identity = f"{u.get_backend_name()}|{u.host or ''}|{u.port or ''}|{u.database or ''}".lower()
    except Exception:
        identity = redact_url(cfg.database_url)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    p = Path(path)
    return str(p.with_name(f"{p.stem}-{digest}{p.suffix}"))


_QUALIFIED = re.compile(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b")


def glossary_for(glossary: dict[str, str], schema: SchemaInfo) -> dict[str, str]:
    """Drop glossary terms that point at tables this database doesn't have.

    A glossary written for another database ("revenue: orders.total_amount ...") would
    otherwise be followed as a rule and send the model to tables that don't exist.
    """
    kept: dict[str, str] = {}
    for term, meaning in (glossary or {}).items():
        missing = sorted({t for t, _ in _QUALIFIED.findall(str(meaning)) if schema.find(t) is None})
        if missing:
            log.warning("Glossary term %r ignored: %s not in this database.", term, ", ".join(missing))
            continue
        kept[term] = meaning
    return kept


def _fits(sql: str, schema: SchemaInfo, max_rows: int) -> bool:
    try:
        check_sql(sql, schema, max_rows=max_rows)
        return True
    except UnsafeQueryError:
        return False


def _clean_history(history) -> list[dict]:
    if not isinstance(history, list):
        return []
    cleaned = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if isinstance(turn, dict) and isinstance(turn.get("question"), str) and isinstance(turn.get("sql"), str):
            cleaned.append({"question": turn["question"][:MAX_QUESTION_CHARS], "sql": turn["sql"][:5000]})
    return cleaned


def _cache_key(question: str, history: list[dict]) -> str:
    hist = "||".join(normalise(h["question"]) + "=>" + h["sql"] for h in history)
    return normalise(question) + "|" + hist


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
