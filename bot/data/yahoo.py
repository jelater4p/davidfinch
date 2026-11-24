"""Yahoo Finance veri kaynağı (ücretsiz, günlük veri için yeterli).

BIST hisseleri Yahoo'da '.IS' ekiyle bulunur (THYAO -> THYAO.IS).
auto_adjust=True ile fiyatlar bölünme, bedelsiz ve temettüye göre düzeltilir;
aksi halde bedelsiz sermaye artırımları sahte bir çöküş gibi görünür.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from .base import DataSource, Quote, normalize

log = logging.getLogger(__name__)


class YahooSource(DataSource):
    def __init__(self, suffix: str = ".IS"):
        self.suffix = suffix

    def fetch_daily(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        import yfinance as yf  # ağır import, sadece gerektiğinde

        tickers = [s + self.suffix for s in symbols]
        raw = yf.download(
            tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance 'end' hariç tutar
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            progress=False,
            threads=True,
        )
        out: dict[str, pd.DataFrame] = {}
        for sym, ticker in zip(symbols, tickers):
            try:
                df = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = normalize(df.dropna(how="all"))
            except (KeyError, ValueError) as exc:
                log.warning("%s verisi alınamadı: %s", sym, exc)
                continue
            if df.empty:
                log.warning("%s için veri boş geldi", sym)
                continue
            out[sym] = df
        return out

    def fetch_intraday(self, symbols: list[str]) -> dict[str, Quote]:
        """Bugünün 5 dakikalık mumlarından son fiyat ve gün içi en düşük/en yüksek (~15 dk gecikmeli).

        Gün içi fiyatlar düzeltilmemiş (gerçek) fiyatlardır; bugün için günlük düzeltilmiş
        fiyatla aynıdır.
        """
        import yfinance as yf

        if not symbols:
            return {}
        tickers = [s + self.suffix for s in symbols]
        raw = yf.download(tickers, period="1d", interval="5m", auto_adjust=False,
                          group_by="ticker", progress=False, threads=True)
        out: dict[str, Quote] = {}
        for sym, ticker in zip(symbols, tickers):
            try:
                df = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower).dropna(subset=["close"])
            except KeyError:
                continue
            if df.empty:
                continue
            out[sym] = Quote(sym, df.index[-1].isoformat(), float(df["close"].iloc[-1]),
                             float(df["low"].min()), float(df["high"].max()), float(df["open"].iloc[0]))
        return out
