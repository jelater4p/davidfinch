"""SQLite kalıcı depolama.

Botun bütün durumu (nakit, pozisyonlar, emirler, işlemler, gerekçeler) burada
tutulur. VPS yeniden başlasa bile bot kaldığı yerden devam eder.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS account (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    initial_capital REAL NOT NULL,
    last_processed_date TEXT,   -- aynı seans iki kez işlenmesin
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- client_order_id benzersiz: aynı karar iki kez kaydedilemez (yeniden başlatmada çift emir olmaz)
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    order_type TEXT NOT NULL CHECK (order_type IN ('MOO','STOP')),
    stop_price REAL,
    decision_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','FILLED','REJECTED','CANCELLED')),
    reason TEXT,                -- gerekçe kartı (JSON)
    fill_date TEXT,
    fill_qty INTEGER,
    fill_price REAL,
    fees REAL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL CHECK (qty > 0),
    avg_cost REAL NOT NULL,     -- masraflar dahil birim maliyet
    stop_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    entry_order_id INTEGER NOT NULL REFERENCES orders(id),
    highest_close REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,  -- masraflar dahil
    exit_date TEXT NOT NULL,
    exit_price REAL NOT NULL,   -- masraflar düşülmüş
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    entry_order_id INTEGER REFERENCES orders(id),
    exit_order_id INTEGER REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    date TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS param_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_date TEXT NOT NULL,
    params TEXT NOT NULL,       -- JSON: o ay geçerli parametreler
    reason TEXT,
    proposal TEXT               -- JSON: teyit bekleyen öneri (yoksa NULL)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)  # işlemleri elle yönetiyoruz
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Eski veritabanlarına sonradan eklenen sütunlar."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(param_history)")}
        if "proposal" not in cols:
            self.conn.execute("ALTER TABLE param_history ADD COLUMN proposal TEXT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Ya hep ya hiç: yarım kalan bir dolum nakit/pozisyon tutarsızlığı bırakmaz."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
