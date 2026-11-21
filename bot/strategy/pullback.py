"""Strateji: "Trend içinde geri çekilmeden al".

Giriş (kapanış sonrası karar, ertesi açılışta işlem):
  1. Trend:        kapanış > SMA(trend_sma) ve SMA son 10 günde yükseliyor
  2. Geri çekilme: son 3 gün içinde RSI < rsi_entry olmuş
  3. Dönüş:        bugün kapanış > dünkü kapanış (düşüş durmuş)
  4. Hacim:        bugünkü hacim >= volume_mult x 20 günlük ortalama hacim

Çıkış:
  * İz süren stop: stop = max(eski stop, en yüksek kapanış - atr_stop_mult x ATR)
  * Trend bozuldu: kapanış < SMA(trend_sma) -> ertesi açılışta sat

Satırlar (row) sözlük olarak gelir: backtest'te hız için DataFrame satırı yerine dict.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

import pandas as pd

from ..config import StrategyConfig
from ..indicators import atr, rsi, sma

SMA_SLOPE_DAYS = 10
RSI_LOOKBACK_DAYS = 3
VOLUME_AVG_DAYS = 20
FEATURES = ["sma", "sma_slope", "rsi", "rsi_min3", "atr", "vol_avg", "prev_close"]


def compute_features(df: pd.DataFrame, p: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["sma"] = sma(out["close"], p.trend_sma)
    out["sma_slope"] = out["sma"] - out["sma"].shift(SMA_SLOPE_DAYS)
    out["rsi"] = rsi(out["close"], p.rsi_period)
    out["rsi_min3"] = out["rsi"].rolling(RSI_LOOKBACK_DAYS).min()
    out["atr"] = atr(out, p.atr_period)
    out["vol_avg"] = out["volume"].rolling(VOLUME_AVG_DAYS).mean().shift(1)  # bugünü hariç tut
    out["prev_close"] = out["close"].shift(1)
    return out


def _missing(row: Mapping[str, Any], keys: list[str]) -> bool:
    return any(row.get(k) is None or (isinstance(row[k], float) and math.isnan(row[k])) for k in keys)


@dataclass
class EntrySignal:
    symbol: str
    date: date
    close: float
    atr: float
    score: float                  # birden fazla sinyal varsa sıralama (trend gücü)
    facts: dict[str, float] = field(default_factory=dict)

    def reasons(self, p: StrategyConfig) -> list[str]:
        f = self.facts
        return [
            f"Trend: kapanış {f['close']:.2f} > {p.trend_sma}g ortalama {f['sma']:.2f} "
            f"(%{f['above_sma_pct']:.1f} üstünde, ortalama yükseliyor)",
            f"Geri çekilme: son {RSI_LOOKBACK_DAYS} günde RSI {f['rsi_min3']:.0f} "
            f"(eşik {p.rsi_entry:g}), bugün {f['rsi']:.0f}",
            f"Dönüş: kapanış dünden %{f['day_change_pct']:+.1f}",
            f"Hacim: 20g ortalamanın {f['vol_ratio']:.1f} katı (eşik {p.volume_mult:g}x)",
        ]


def entry_signal(sym: str, day: date, row: Mapping[str, Any], p: StrategyConfig) -> EntrySignal | None:
    if _missing(row, FEATURES) or row["vol_avg"] <= 0:
        return None
    vol_ratio = row["volume"] / row["vol_avg"]
    if not (
        row["close"] > row["sma"]
        and row["sma_slope"] > 0
        and row["rsi_min3"] < p.rsi_entry
        and row["close"] > row["prev_close"]
        and vol_ratio >= p.volume_mult
    ):
        return None
    above = row["close"] / row["sma"] - 1
    return EntrySignal(
        symbol=sym,
        date=day,
        close=float(row["close"]),
        atr=float(row["atr"]),
        score=float(above),
        facts={
            "close": float(row["close"]),
            "sma": float(row["sma"]),
            "above_sma_pct": 100 * above,
            "rsi": float(row["rsi"]),
            "rsi_min3": float(row["rsi_min3"]),
            "day_change_pct": 100 * (row["close"] / row["prev_close"] - 1),
            "vol_ratio": float(vol_ratio),
            "atr": float(row["atr"]),
        },
    )


def exit_reason(row: Mapping[str, Any], p: StrategyConfig) -> str | None:
    """Stop dışındaki çıkış sinyali (kapanışa göre)."""
    if not _missing(row, ["sma"]) and row["close"] < row["sma"]:
        return f"Trend bozuldu: kapanış {row['close']:.2f} < {p.trend_sma}g ortalama {row['sma']:.2f}"
    return None


def trailing_stop(highest_close: float, atr_value: float, p: StrategyConfig) -> float:
    return highest_close - p.atr_stop_mult * atr_value
