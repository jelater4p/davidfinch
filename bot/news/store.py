"""Bildirim ve analiz önbelleği (SQLite). Her bildirim bir kez çekilir, bir kez analiz edilir."""
from __future__ import annotations

from datetime import date, datetime

from ..storage.db import Database
from .analyzer import Analysis
from .base import Disclosure

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    key TEXT NOT NULL,               -- kaynak:id
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    title TEXT, subject TEXT, summary TEXT, url TEXT, body TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (key, symbol)        -- bir bildirim birden çok hisseyi ilgilendirebilir
);
CREATE TABLE IF NOT EXISTS disclosure_analysis (
    key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    severe INTEGER,                  -- 1/0, NULL = bilinmiyor
    category TEXT, reason TEXT, confidence REAL, model TEXT,
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (key, symbol)
);
"""


class NewsStore:
    def __init__(self, db: Database):
        self.db = db
        self.db.conn.executescript(SCHEMA)

    def save(self, items: list[Disclosure]) -> int:
        with self.db.transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO disclosures
                   (key, symbol, source, published_at, title, subject, summary, url, body)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(d.key, d.symbol, d.source, d.published_at.isoformat(), d.title, d.subject,
                  d.summary, d.url, d.body) for d in items],
            )
            return conn.total_changes - before

    def unanalyzed(self) -> list[Disclosure]:
        rows = self.db.query(
            """SELECT d.* FROM disclosures d
               LEFT JOIN disclosure_analysis a ON a.key = d.key AND a.symbol = d.symbol
               WHERE a.key IS NULL OR a.severe IS NULL ORDER BY d.published_at"""
        )
        return [self._row(r) for r in rows]

    def save_analysis(self, d: Disclosure, a: Analysis) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO disclosure_analysis
                   (key, symbol, severe, category, reason, confidence, model) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (d.key, d.symbol, None if a.severe is None else int(a.severe), a.category, a.reason,
                 a.confidence, a.model),
            )

    def for_symbol(self, symbol: str, since: date) -> list[dict]:
        rows = self.db.query(
            """SELECT d.symbol, d.published_at, d.title, d.subject, d.url,
                      a.severe, a.category, a.reason, a.confidence
               FROM disclosures d LEFT JOIN disclosure_analysis a ON a.key = d.key AND a.symbol = d.symbol
               WHERE d.symbol = ? AND d.published_at >= ? ORDER BY d.published_at DESC""",
            (symbol, since.isoformat()),
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _row(r) -> Disclosure:
        return Disclosure(
            source=r["source"], disclosure_id=r["key"].split(":", 1)[1], symbol=r["symbol"],
            published_at=datetime.fromisoformat(r["published_at"]), title=r["title"] or "",
            subject=r["subject"] or "", summary=r["summary"] or "", url=r["url"] or "", body=r["body"] or "",
        )
