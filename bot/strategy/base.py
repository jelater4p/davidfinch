"""Strateji arayüzü. Motor (engine) sadece bu arayüzü bilir.

Her strateji şunları sağlar:
  * features():      ham fiyat verisinden gösterge sütunları
  * entry_signals(): kapanış sonrası alım adayları (sıralı, gerekçeli)
  * exit_reasons():  stop dışındaki satış nedenleri
  * trailing_stop(): pozisyonun güncel stop seviyesi
  * hold_while/exit_if/param_dict: gerekçe kartı metinleri
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping

import pandas as pd

Row = Mapping[str, Any]


def missing(row: Row, keys: list[str]) -> bool:
    return any(row.get(k) is None or (isinstance(row[k], float) and math.isnan(row[k])) for k in keys)


@dataclass
class EntrySignal:
    symbol: str
    date: date
    close: float
    atr: float
    score: float                       # büyük olan önce
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, float] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"

    def __init__(self, params):
        self.p = params
        self.trading_days: list[date] = []

    def prepare(self, rows: dict[str, dict[date, dict]]) -> None:
        """Tüm işlem günlerini öğrenir (ay başı yenileme gibi takvim kararları için)."""
        self.trading_days = sorted({d for r in rows.values() for d in r})

    def cooldown_since(self, day: date) -> date | None:
        """Bu tarihten beri stop olan hisseler tekrar alınmaz (None: bekleme yok)."""
        return None

    @property
    def atr_stop_mult(self) -> float:
        return self.p.atr_stop_mult

    @abstractmethod
    def features(self, df: pd.DataFrame) -> pd.DataFrame: ...

    @abstractmethod
    def entry_signals(self, day: date, today: dict[str, Row], held: set[str]) -> list[EntrySignal]: ...

    @abstractmethod
    def exit_reasons(self, day: date, today: dict[str, Row], held: set[str]) -> dict[str, str]: ...

    def trailing_stop(self, row: Row, highest_close: float) -> float | None:
        if missing(row, ["atr"]):
            return None
        return highest_close - self.p.atr_stop_mult * row["atr"]

    @abstractmethod
    def hold_while(self) -> str: ...

    @abstractmethod
    def exit_if(self) -> list[str]: ...

    def param_dict(self) -> dict[str, Any]:
        return asdict(self.p)
