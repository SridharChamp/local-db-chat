"""Command line: ask, chat, schema, serve, doctor, demo."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .config import load_config
from .db import Database, DatabaseError
from .llm import LLMError, OllamaClient
from .util import is_loopback_host

MAX_CELL = 40


def _print_table(columns: list[str], rows: list[list], limit: int = 50) -> None:
    if not columns:
        return
    shown = rows[:limit]

    def fmt(v):
        s = "NULL" if v is None else str(v).replace("\n", " ")
        return s if len(s) <= MAX_CELL else s[:MAX_CELL - 1] + "…"

    cells = [[fmt(v) for v in r] for r in shown]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    widths = [min(w, MAX_CELL) for w in widths]
    line = "  ".join(c[:w].ljust(w) for c, w in zip(columns, widths, strict=True))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows (use --json or the web UI to see all)")


def _print_answer(ans, show_sql: bool) -> None:
    if ans.error:
        print(f"Error: {ans.error}", file=sys.stderr)
        if ans.sql and show_sql:
            print(f"\nLast SQL tried:\n{ans.sql}", file=sys.stderr)
        return
    print(ans.answer)
    if ans.ok and ans.problems:
        print("Note: earlier attempts were rejected (" + "; ".join(ans.problems)
              + "). Check this answers what you asked.")
    if ans.ok and ans.columns and not (ans.row_count == 1 and len(ans.columns) == 1):
        print()
        _print_table(ans.columns, ans.rows)
    if show_sql and ans.sql:
        print(f"\nSQL:\n{ans.sql}")
    tags = [t for t, on in (("verified", ans.verified), ("cached", ans.cached)) if on]
    print(f"\n[{ans.timings_ms.get('total', 0) / 1000:.1f}s"
          + (f", {', '.join(tags)}" if tags else "")
          + (f", tables: {', '.join(ans.tables)}" if ans.tables else "") + "]")


def _assistant(args):
    from .engine import Assistant  # imported late so `ldbc --help` stays instant
    cfg = load_config(args.config)
    return cfg, Assistant(cfg)


def cmd_ask(args) -> int:
    _, assistant = _assistant(args)
    try:
        ans = assistant.ask(" ".join(args.question))
        if args.json:
            print(json.dumps(ans.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_answer(ans, show_sql=args.sql)
        return 0 if ans.ok else 1
    finally:
        assistant.close()


def cmd_chat(args) -> int:
    _, assistant = _assistant(args)
    history: list[dict] = []
    show_sql = True
    print("local-db-chat. Ask a question. Commands: :sql (toggle SQL), :new, :refresh, :tables, :quit")
    try:
        schema = assistant.schema
        print(f"Connected: {len(schema)} tables ({schema.dialect}).")
        for w in schema.warnings:
            print(f"warning: {w}")
        while True:
            try:
                line = input("\n> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in (":q", ":quit", ":exit"):
                break
            if line == ":sql":
                show_sql = not show_sql
                print(f"SQL display {'on' if show_sql else 'off'}.")
                continue
            if line == ":new":
                history.clear()
                print("New conversation.")
                continue
            if line == ":refresh":
                print(f"Schema reloaded: {len(assistant.refresh_schema())} tables.")
                continue
            if line == ":tables":
                for t in assistant.schema.tables.values():
                    print(f"  {t.full_name}: {', '.join(c.name for c in t.visible_columns)}")
                continue
            ans = assistant.ask(line, history)
            _print_answer(ans, show_sql)
            if ans.ok and ans.sql:
                history.append({"question": line, "sql": ans.sql})
                del history[:-4]
    except KeyboardInterrupt:
        print()
    finally:
        assistant.close()
    return 0


def cmd_schema(args) -> int:
    _, assistant = _assistant(args)
    try:
        from .schema import render_table
        schema = assistant.schema
        for w in schema.warnings:
            print(f"-- warning: {w}")
        for t in schema.tables.values():
            print(render_table(t, schema.dialect, assistant.samples.get(t) if args.samples else None))
            print()
        hidden = sum(1 for t in schema.tables.values() for c in t.columns if c.sensitive)
        print(f"-- {len(schema)} tables; {hidden} protected column(s) hidden from the model.")
        return 0
    finally:
        assistant.close()


def cmd_serve(args) -> int:
    import uvicorn

    from .engine import Assistant
    from .web.app import create_app
    config_path = args.config or "config.yaml"
    cfg = load_config(args.config)
    host = args.host or cfg.web_host
    port = args.port or cfg.web_port
    assistant = None
    if cfg.database_url:
        try:
            assistant = Assistant(cfg)
            schema = assistant.schema
            print(f"Database: {assistant.db.display_url} ({len(schema)} tables)")
            try:
                assistant.llm.warm_up()
                print(f"Model ready: {cfg.model}")
            except LLMError as exc:
                print(f"warning: {exc}")
        except (DatabaseError, LLMError) as exc:
            print(f"warning: {exc}\nStarting the setup screen so you can fix the connection.")
            if assistant is not None:
                assistant.close()
            assistant = None
    else:
        print("No database configured yet: the browser will show the setup screen.")
    app = create_app(assistant, cfg=cfg, config_path=config_path, host=host, port=port)
    url = f"http://{'localhost' if is_loopback_host(host) else host}:{port}/"
    if app.state.token:
        print("\nWARNING: listening on a network address. Anyone who can reach it and has the "
              "token can query the database.")
        url += f"#token={app.state.token}"
    print(f"\nOpen {url}\n")
    if not args.no_browser and is_loopback_host(host):
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    finally:
        if app.state.assistant is not None:
            app.state.assistant.close()
    return 0


def cmd_setup(args) -> int:
    import getpass

    from .setup import KINDS, ConnectionFields, SetupError, build_url, check_driver, save_database_url, try_connection
    config_path = args.config or "config.yaml"
    cfg = load_config(args.config)
    print("local-db-chat setup. Everything stays on this computer.\n")

    # 1. Model
    try:
        llm = OllamaClient(cfg.ollama_url, cfg.model, allow_remote=cfg.allow_remote_llm)
        if llm.has_model():
            print(f"[ok] Model {cfg.model} is installed.")
        else:
            answer = input(f"The model {cfg.model} is not downloaded yet (a few GB). Download it now? [Y/n] ")
            if answer.strip().lower() in ("", "y", "yes"):
                last = {"pct": -1}

                def progress(status, completed, total):
                    pct = int(completed * 100 / total) if total else -1
                    if pct != last["pct"]:
                        last["pct"] = pct
                        shown = f"{pct:3d}%" if pct >= 0 else "    "
                        print(f"\r  {shown} {status[:60]:<60}", end="", flush=True)
                llm.pull(cfg.model, progress)
                print(f"\n[ok] Model {cfg.model} downloaded.")
        llm.close()
    except LLMError as exc:
        print(f"[!] {exc}\n    You can finish the database part now and download the model later.")

    # 2. Database
    kinds = list(KINDS)
    print("\nDatabase type:")
    for i, k in enumerate(kinds, 1):
        print(f"  {i}. {KINDS[k]['label']}")
    while True:
        choice = input(f"Choose 1-{len(kinds)}: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(kinds):
            kind = kinds[int(choice) - 1]
            break
    try:
        check_driver(kind)
    except SetupError as exc:
        print(f"[!] {exc}")
        return 1

    while True:
        if kind == "sqlite":
            fields = ConnectionFields(kind, path=input("Path to the .db file: "))
        else:
            default_port = KINDS[kind]["port"]
            port = input(f"Port [{default_port}]: ").strip()
            fields = ConnectionFields(
                kind,
                host=input("Host: "),
                port=int(port) if port.isdigit() else default_port,
                database=input("Database name: "),
                user=input("User (read-only recommended): "),
                password=getpass.getpass("Password (hidden): "),
                ssl=kind != "mssql" and input("Require TLS? [y/N] ").strip().lower() in ("y", "yes"),
                trust_certificate=kind == "mssql"
                and input("Trust a self-signed server certificate? [y/N] ").strip().lower() in ("y", "yes"),
            )
        try:
            url = build_url(fields)
        except SetupError as exc:
            print(f"[!] {exc}")
            continue
        print("Testing the connection…")
        result = try_connection(url, cfg)
        print(("[ok] " if result.ok else "[!] ") + result.message)
        if result.ok:
            break
        if input("Try again? [Y/n] ").strip().lower() in ("n", "no"):
            return 1

    path = save_database_url(config_path, url)
    print(f"\nSaved to {path.resolve()} (contains the password: keep it private).")
    print("Next: ldbc serve")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    ok = True

    def report(good: bool, text: str):
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {text}")

    try:
        db = Database(cfg.database_url, timeout_seconds=cfg.timeout_seconds, max_rows=cfg.max_rows)
        db.check_connection()
        report(True, f"Database reachable: {db.display_url}")
        from .schema import introspect
        schema = introspect(db, schemas=cfg.schemas, include_tables=cfg.include_tables,
                            exclude_tables=cfg.exclude_tables, sensitive_patterns=cfg.sensitive_columns)
        report(len(schema) > 0, f"{len(schema)} tables found")
        for w in schema.warnings:
            print(f"       warning: {w}")
        hidden = [f"{t.full_name}.{c.name}" for t in schema.tables.values() for c in t.columns if c.sensitive]
        print(f"       protected columns: {', '.join(hidden) if hidden else 'none'}")
        db.dispose()
    except DatabaseError as exc:
        report(False, str(exc))

    try:
        llm = OllamaClient(cfg.ollama_url, cfg.model, embed_model=cfg.embed_model,
                           allow_remote=cfg.allow_remote_llm)
        models = llm.list_models()
        report(True, f"Ollama reachable at {cfg.ollama_url} (local only: "
                     f"{'yes' if not cfg.allow_remote_llm else 'NO, allow_remote_llm is on'})")
        def installed(name):
            return any(m == name or m.split(":")[0] == name or m == f"{name}:latest" for m in models)
        report(installed(cfg.model), f"Model '{cfg.model}' installed"
               + ("" if installed(cfg.model) else f": run 'ollama pull {cfg.model}'"))
        if cfg.embed_model:
            report(installed(cfg.embed_model), f"Embedding model '{cfg.embed_model}' installed")
        llm.close()
    except LLMError as exc:
        report(False, str(exc))

    print("\nAll good." if ok else "\nFix the items marked FAIL, then run 'ldbc doctor' again.")
    return 0 if ok else 1


def cmd_demo(args) -> int:
    from .demo import build, write_config
    try:
        path = build(args.path, overwrite=args.force)
    except FileExistsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Demo database created: {path.resolve()}")
    written = write_config(path)
    print("Wrote config.yaml for it." if written else "config.yaml already exists; left unchanged.")
    print("Next: ldbc doctor, then ldbc serve")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="ldbc", description="Ask your database questions in plain "
                                     "English, using a model that runs on this computer.")
    parser.add_argument("--version", action="version", version=f"local-db-chat {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show warnings and debug logs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="ask one question")
    p.add_argument("question", nargs="+")
    p.add_argument("--sql", action="store_true", help="also print the SQL")
    p.add_argument("--json", action="store_true", help="print the full answer as JSON")
    p.set_defaults(func=cmd_ask)

    sub.add_parser("chat", help="interactive chat in the terminal").set_defaults(func=cmd_chat)

    p = sub.add_parser("schema", help="show what the model is told about the database")
    p.add_argument("--samples", action="store_true", help="include sample rows")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("serve", help="start the web chat UI")
    p.add_argument("--host", help="address to listen on (default 127.0.0.1, this computer only)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("setup", help="connect a database and download the model, step by step") \
        .set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check the database and model setup").set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="create a sample support database and config to try it out")
    p.add_argument("--path", default="demo.db")
    p.add_argument("--force", action="store_true", help="overwrite an existing demo database")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except (DatabaseError, LLMError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    except Exception as exc:  # SetupError and friends: one clear line, no traceback
        if type(exc).__name__ == "SetupError":
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
