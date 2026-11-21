"""Strateji: "Trend içinde geri çekilmeden al" (ikincil strateji; şu an pasif).

Giriş (kapanış sonrası karar, ertesi açılışta işlem):
  1. Trend:        kapanış > SMA(trend_sma) ve SMA son 10 günde yükseliyor
  2. Geri çekilme: son 3 gün içinde RSI < rsi_entry olmuş
  3. Dönüş:        bugün kapanış > dünkü kapanış
  4. Hacim:        bugünkü hacim >= volume_mult x 20 günlük ortalama hacim

Çıkış: iz süren ATR stop veya kapanış < SMA(trend_sma).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..config import StrategyConfig
from ..indicators import atr, rsi, sma
from .base import EntrySignal, Row, Strategy, missing

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


def entry_signal(sym: str, day: date, row: Row, p: StrategyConfig) -> EntrySignal | None:
    if missing(row, FEATURES) or row["vol_avg"] <= 0:
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
    day_chg = row["close"] / row["prev_close"] - 1
    return EntrySignal(
        symbol=sym, date=day, close=float(row["close"]), atr=float(row["atr"]), score=float(above),
        reasons=[
            f"Trend: kapanış {row['close']:.2f} > {p.trend_sma}g ortalama {row['sma']:.2f} "
            f"(%{100 * above:.1f} üstünde, ortalama yükseliyor)",
            f"Geri çekilme: son {RSI_LOOKBACK_DAYS} günde RSI {row['rsi_min3']:.0f} "
            f"(eşik {p.rsi_entry:g}), bugün {row['rsi']:.0f}",
            f"Dönüş: kapanış dünden %{100 * day_chg:+.1f}",
            f"Hacim: 20g ortalamanın {vol_ratio:.1f} katı (eşik {p.volume_mult:g}x)",
        ],
        facts={"rsi_min3": float(row["rsi_min3"]), "vol_ratio": float(vol_ratio)},
    )


def exit_reason(row: Row, p: StrategyConfig) -> str | None:
    if not missing(row, ["sma"]) and row["close"] < row["sma"]:
        return f"Trend bozuldu: kapanış {row['close']:.2f} < {p.trend_sma}g ortalama {row['sma']:.2f}"
    return None


class PullbackStrategy(Strategy):
    name = "pullback"

    def features(self, df):
        return compute_features(df, self.p)

    def entry_signals(self, day, today, held):
        sigs = [s for sym, row in today.items() if sym not in held
                and (s := entry_signal(sym, day, row, self.p)) is not None]
        return sorted(sigs, key=lambda s: s.score, reverse=True)

    def exit_reasons(self, day, today, held):
        out = {}
        for sym in held:
            if sym in today and (r := exit_reason(today[sym], self.p)):
                out[sym] = r
        return out

    def hold_while(self):
        return f"Kapanış {self.p.trend_sma}g ortalamanın üstünde kaldıkça"

    def exit_if(self):
        return [
            f"İz süren stop: en yüksek kapanış - {self.p.atr_stop_mult:g} x ATR (stop asla aşağı inmez)",
            f"Kapanış {self.p.trend_sma}g ortalamanın altına inerse ertesi açılışta sat",
            "KAP'ta ciddi olumsuz haber gelirse yeniden değerlendir",
        ]
