"""Configuration: a YAML file plus environment-variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_SENSITIVE_PATTERNS = [
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "card_number", "cardnumber", "cvv", "ssn", "pan_number", "aadhaar",
    "otp", "salt", "hash",
]


@dataclass
class Config:
    database_url: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5-coder:7b"
    embed_model: str = ""  # e.g. "nomic-embed-text"; empty = keyword matching only
    max_rows: int = 200
    timeout_seconds: int = 15
    max_retries: int = 2
    max_tables_in_prompt: int = 8
    sample_rows: int = 3
    schemas: list[str] = field(default_factory=list)  # empty = default schema
    include_tables: list[str] = field(default_factory=list)
    exclude_tables: list[str] = field(default_factory=list)
    sensitive_columns: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_PATTERNS))
    glossary: dict[str, str] = field(default_factory=dict)
    examples: list[dict[str, str]] = field(default_factory=list)
    num_ctx: int = 8192           # model context window; Ollama's own default is too small
    keep_alive: str = "30m"       # keep the model loaded between questions (speed)
    llm_timeout_seconds: int = 120
    explain_answers: bool = False  # extra model call to describe results in words
    learned_examples_file: str = ".ldbc/learned.json"
    max_learned_examples: int = 500
    allow_remote_llm: bool = False  # safety: refuse non-loopback Ollama hosts unless set
    web_host: str = "127.0.0.1"
    web_port: int = 8765


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from YAML (if given / found), then apply env overrides."""
    data: dict = {}
    candidate = Path(path) if path else Path("config.yaml")
    if candidate.exists():
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    elif path:
        raise FileNotFoundError(f"Config file not found: {candidate}")

    known = Config.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    cfg = Config(**data)

    env_map = {
        "LDBC_DATABASE_URL": "database_url",
        "LDBC_OLLAMA_URL": "ollama_url",
        "LDBC_MODEL": "model",
        "LDBC_EMBED_MODEL": "embed_model",
    }
    for env, attr in env_map.items():
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env])
    return cfg
