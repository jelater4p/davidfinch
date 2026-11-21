"""Fiyat verisinin veritabanında saklanması."""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..storage.db import Database
from .base import COLUMNS, Bar


class PriceStore:
    def __init__(self, db: Database):
        self.db = db

    def replace_history(self, symbol: str, df: pd.DataFrame) -> int:
        """Sembolün verdiği aralıktaki geçmişini yeniler.

        Düzeltilmiş fiyatlar bedelsiz/bölünme sonrası geriye dönük değiştiği için
        eklemek yerine aralığı silip yeniden yazıyoruz.
        """
        if df.empty:
            return 0
        start, end = df.index.min().date().isoformat(), df.index.max().date().isoformat()
        rows = [
            (symbol, idx.date().isoformat(), *(float(r[c]) for c in COLUMNS))
            for idx, r in df.iterrows()
        ]
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM prices WHERE symbol = ? AND date BETWEEN ? AND ?", (symbol, start, end)
            )
            conn.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def history(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        sql = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ?"
        params: list = [symbol]
        if start:
            sql += " AND date >= ?"
            params.append(start.isoformat())
        if end:
            sql += " AND date <= ?"
            params.append(end.isoformat())
        rows = self.db.query(sql + " ORDER BY date", tuple(params))
        df = pd.DataFrame([dict(r) for r in rows], columns=["date", *COLUMNS])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def bar(self, symbol: str, day: date) -> Bar | None:
        r = self.db.query_one(
            "SELECT * FROM prices WHERE symbol = ? AND date = ?", (symbol, day.isoformat())
        )
        if r is None:
            return None
        return Bar(symbol, day, r["open"], r["high"], r["low"], r["close"], r["volume"])

    def last_close(self, symbol: str, on_or_before: date) -> float | None:
        r = self.db.query_one(
            "SELECT close FROM prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, on_or_before.isoformat()),
        )
        return None if r is None else float(r["close"])

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self.db.query("SELECT DISTINCT symbol FROM prices ORDER BY symbol")]


SPLIT_THRESHOLD = 0.10   # %10'dan büyük geriye dönük fiyat değişimi = bölünme/bedelsiz (temettü düzeltmeleri küçüktür)


def detect_adjustment(old: pd.DataFrame, new: pd.DataFrame) -> float | None:
    """Yeni çekilen (düzeltilmiş) geçmiş, eskisine göre topluca ölçeklenmiş mi?

    Ortak tarihlerdeki kapanış oranlarının medyanı (yeni / eski) döner; anlamlı bir
    değişim yoksa None. Örn. %100 bedelsizde ≈ 0.5.
    """
    common = old.index.intersection(new.index)
    if len(common) < 5:
        return None
    ratio = (new.loc[common, "close"] / old.loc[common, "close"]).median()
    return float(ratio) if abs(1 - ratio) > SPLIT_THRESHOLD else None
