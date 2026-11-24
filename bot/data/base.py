"""Veri kaynağı arayüzü.

Strateji kodu sadece bu arayüzü bilir. Bugün YahooSource, yarın MatriksSource
takılır; geri kalan kod değişmez.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    """Gün içi anlık görüntü (Yahoo'da ~15 dk gecikmeli)."""
    symbol: str
    time: str        # son mumun zamanı, ISO
    last: float
    day_low: float
    day_high: float
    open: float | None = None   # günün ilk mumunun açılışı (sabah dolumu için)


class DataSource(ABC):
    @abstractmethod
    def fetch_daily(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Her sembol için tarih indeksli, COLUMNS sütunlu günlük mum verisi döndürür.

        Fiyatlar bölünme/bedelsiz için düzeltilmiş olmalıdır.
        """

    def fetch_intraday(self, symbols: list[str]) -> dict[str, Quote]:
        """Bugünün gün içi son fiyatı ve gün içi en düşük/en yüksek. Desteklenmiyorsa boş döner."""
        return {}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Sütun adlarını küçült, eksik/bozuk satırları at, tarihe göre sırala."""
    df = df.rename(columns=str.lower)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Eksik sütunlar: {missing}")
    df = df[COLUMNS].astype(float)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Fiyatı sıfır/eksik olan veya high<low gibi tutarsız satırlar
    valid = (df[["open", "high", "low", "close"]] > 0).all(axis=1) & (df["high"] >= df["low"])
    return df[valid & df.notna().all(axis=1)]
