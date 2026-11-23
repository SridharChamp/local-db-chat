"""Builds a small, realistic support database to try the tool without your own data."""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path

FIRST = ["Asha", "Ravi", "Meera", "John", "Priya", "Arjun", "Sara", "Karthik", "Divya", "Imran",
         "Lakshmi", "Vikram", "Neha", "Rahul", "Anita", "David", "Fatima", "Suresh", "Kavya", "Tom"]
LAST = ["Iyer", "Kumar", "Sharma", "Smith", "Reddy", "Nair", "Khan", "Patel", "Das", "Menon"]
COMPANIES = ["Acme Corp", "Globex", "Initech", "Umbrella Ltd", "Stark Traders", "Wayne Retail",
             "Hooli", "Vandelay Imports"]
CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Kochi"]
PRODUCTS = [("Starter Plan", 499.00), ("Pro Plan", 1499.00), ("Enterprise Plan", 4999.00),
            ("Extra Storage 100GB", 299.00), ("Priority Support", 999.00), ("Onboarding Session", 2500.00)]
SUBJECTS = ["Cannot log in", "Invoice amount wrong", "App is slow", "Export to Excel fails",
            "Password reset email not received", "Need to upgrade plan", "Data not syncing",
            "Refund request", "Feature request: dark mode", "Error 500 on dashboard"]

SCHEMA = """
CREATE TABLE customers (
  customer_id   INTEGER PRIMARY KEY,
  full_name     TEXT NOT NULL,
  email         TEXT NOT NULL,
  company       TEXT,
  city          TEXT,
  status        TEXT NOT NULL CHECK (status IN ('A','S','C')),
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE products (
  product_id  INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  price       NUMERIC NOT NULL
);
CREATE TABLE orders (
  order_id     INTEGER PRIMARY KEY,
  customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
  order_date   TEXT NOT NULL,
  status       TEXT NOT NULL,
  total_amount NUMERIC NOT NULL
);
CREATE TABLE order_items (
  order_item_id INTEGER PRIMARY KEY,
  order_id      INTEGER NOT NULL REFERENCES orders(order_id),
  product_id    INTEGER NOT NULL REFERENCES products(product_id),
  quantity      INTEGER NOT NULL,
  unit_price    NUMERIC NOT NULL
);
CREATE TABLE agents (
  agent_id  INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  team      TEXT NOT NULL
);
CREATE TABLE support_tickets (
  ticket_id   INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
  agent_id    INTEGER REFERENCES agents(agent_id),
  subject     TEXT NOT NULL,
  priority    TEXT NOT NULL,
  status      TEXT NOT NULL,
  opened_at   TEXT NOT NULL,
  closed_at   TEXT
);
"""

DEMO_CONFIG = """# local-db-chat demo configuration
database_url: sqlite:///{db}
model: qwen2.5-coder:7b

# Business words your support team uses, and what they mean in this database.
glossary:
  active customer: customers.status = 'A'
  suspended customer: customers.status = 'S'
  churned customer: customers.status = 'C'
  open ticket: support_tickets.status in ('open', 'in_progress')
  revenue: sum of orders.total_amount where orders.status = 'paid'

examples:
  - question: How many active customers do we have?
    sql: SELECT COUNT(*) AS active_customers FROM customers WHERE status = 'A'
"""


def build(path: str | Path = "demo.db", *, seed: int = 42, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"{path} already exists (use --force to rebuild it).")
        path.unlink()
    rnd = random.Random(seed)
    today = dt.date.today()
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        customers = []
        for cid in range(1, 121):
            name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            created = today - dt.timedelta(days=rnd.randint(30, 900))
            customers.append((cid, name, f"{name.lower().replace(' ', '.')}{cid}@example.com",
                              rnd.choice(COMPANIES), rnd.choice(CITIES),
                              rnd.choices("ASC", weights=[80, 8, 12])[0],
                              f"pbkdf2$demo${rnd.getrandbits(64):x}", created.isoformat()))
        con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)
        con.executemany("INSERT INTO products VALUES (?,?,?)",
                        [(i + 1, n, p) for i, (n, p) in enumerate(PRODUCTS)])
        agents = [(1, "Kiran", "Billing"), (2, "Sneha", "Technical"), (3, "Joseph", "Technical"),
                  (4, "Aisha", "Onboarding")]
        con.executemany("INSERT INTO agents VALUES (?,?,?)", agents)

        order_id = item_id = 0
        orders, items = [], []
        for cid, *_ in customers:
            for _ in range(rnd.randint(0, 6)):
                order_id += 1
                odate = today - dt.timedelta(days=rnd.randint(0, 400))
                lines, total = [], 0.0
                for _ in range(rnd.randint(1, 3)):
                    item_id += 1
                    pid = rnd.randint(1, len(PRODUCTS))
                    qty = rnd.randint(1, 3)
                    price = PRODUCTS[pid - 1][1]
                    lines.append((item_id, order_id, pid, qty, price))
                    total += qty * price
                items.extend(lines)
                orders.append((order_id, cid, odate.isoformat(),
                               rnd.choices(["paid", "refunded", "pending"], weights=[85, 5, 10])[0],
                               round(total, 2)))
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)

        tickets = []
        for tid in range(1, 301):
            opened = dt.datetime.combine(today, dt.time(9)) - dt.timedelta(hours=rnd.randint(1, 24 * 180))
            status = rnd.choices(["open", "in_progress", "resolved", "closed"], weights=[15, 10, 35, 40])[0]
            closed = (opened + dt.timedelta(hours=rnd.randint(1, 96))).isoformat(sep=" ", timespec="minutes") \
                if status in ("resolved", "closed") else None
            tickets.append((tid, rnd.randint(1, len(customers)),
                            rnd.choice([None, 1, 2, 3, 4]) if status == "open" else rnd.randint(1, 4),
                            rnd.choice(SUBJECTS), rnd.choices(["low", "medium", "high", "urgent"],
                                                              weights=[30, 40, 22, 8])[0],
                            status, opened.isoformat(sep=" ", timespec="minutes"), closed))
        con.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?)", tickets)
        con.commit()
    finally:
        con.close()
    return path


def write_config(db_path: Path, config_path: str | Path = "config.yaml") -> Path | None:
    config_path = Path(config_path)
    if config_path.exists():
        return None
    config_path.write_text(DEMO_CONFIG.format(db=db_path.resolve().as_posix()), encoding="utf-8")
    return config_path
