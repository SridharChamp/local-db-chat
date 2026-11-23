"""Small helpers shared across modules."""

from __future__ import annotations

import datetime as dt
import decimal
import ipaddress
import math
import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

MAX_CELL_CHARS = 1000


def redact_url(url: str) -> str:
    """Return a connection URL with the password hidden, safe for logs."""
    try:
        parsed = make_url(url)
        if parsed.password is None:
            return url  # nothing to hide; keep it readable (e.g. Windows paths)
        return parsed.render_as_string(hide_password=True)
    except Exception:
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def scrub(message: str, url: str) -> str:
    """Remove the password (if any) from an error message."""
    try:
        password = make_url(url).password
    except Exception:
        password = None
    if password:
        message = message.replace(str(password), "***")
    return message


def to_jsonable(value, max_chars: int = MAX_CELL_CHARS):
    """Convert a DB value into something JSON can carry, without losing meaning."""
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > max_chars:
            return value[:max_chars] + f"... [{len(value) - max_chars} more chars]"
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, decimal.Decimal):
        # Keep exact digits; money must not drift through float conversion.
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, max_chars) for k, v in value.items()}
    text = str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def is_loopback_url(url: str) -> bool:
    """True when the URL points at this machine only."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
