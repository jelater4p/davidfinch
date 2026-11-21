"""Strateji: Momentum / güç rotasyonu (aktif strateji).

Ayda bir (ayın ilk işlem günü kapanışından sonra) tüm evren sıralanır:
  * Momentum skoru = son `lookback` günün getirisi, son `skip_days` gün hariç
    (son bir ayı atlamak kısa vadeli geri dönüş gürültüsünü azaltır; akademik standart)
  * Uygunluk: kapanış > SMA(trend_sma) ve momentum > 0

Yenileme günü:
  * Elde tutulan hisse sıralamada ilk `hold_rank` içinde ve uygunsa TUTULUR
    (tampon bölge: 10. ile 11. sıra arasında gidip gelen hisseyi boşuna alıp satmamak için)
  * Değilse ertesi açılışta SATILIR
  * Boşalan yerler en yüksek skorlu uygun hisselerle doldurulur

Aylar arasında: iz süren ATR stop korur.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..config import MomentumConfig
from ..indicators import atr, sma
from .base import EntrySignal, Row, Strategy, missing

FEATURES = ["mom", "sma", "atr"]


class MomentumStrategy(Strategy):
    name = "momentum"
    p: MomentumConfig

    def features(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.p
        out = df.copy()
        out["mom"] = out["close"].shift(p.skip_days) / out["close"].shift(p.lookback) - 1
        out["sma"] = sma(out["close"], p.trend_sma)
        out["atr"] = atr(out, p.atr_period)
        return out

    # ---------------------------------------------------------------- takvim
    def is_rebalance_day(self, day: date) -> bool:
        """Ayın ilk işlem günü mü? (önceki işlem günü başka ayda)"""
        import bisect
        i = bisect.bisect_left(self.trading_days, day)
        if i == 0:
            return False   # veri başlangıcı; önceki günü bilmiyoruz
        prev = self.trading_days[i - 1]
        return (prev.year, prev.month) != (day.year, day.month)

    def ranking(self, today: dict[str, Row]) -> list[tuple[str, Row]]:
        eligible = [
            (sym, row) for sym, row in today.items()
            if not missing(row, FEATURES) and row["close"] > row["sma"] and row["mom"] > 0
        ]
        return sorted(eligible, key=lambda x: x[1]["mom"], reverse=True)

    # ---------------------------------------------------------------- kararlar
    def exit_reasons(self, day, today, held):
        if not self.is_rebalance_day(day):
            return {}
        ranks = {sym: i + 1 for i, (sym, _) in enumerate(self.ranking(today))}
        out = {}
        for sym in held:
            if sym not in today:
                continue
            r = ranks.get(sym)
            if r is None:
                row = today[sym]
                why = (f"kapanış {self.p.trend_sma}g ortalamanın altında"
                       if not missing(row, ["sma"]) and row["close"] <= row["sma"]
                       else "momentum negatife döndü")
                out[sym] = f"Aylık yenileme: {why}"
            elif r > self.p.hold_rank:
                out[sym] = f"Aylık yenileme: sıralama {r}. oldu (tutma sınırı ilk {self.p.hold_rank})"
        return out

    def entry_signals(self, day, today, held):
        if not self.is_rebalance_day(day):
            return []
        ranked = self.ranking(today)
        n = len(ranked)
        sigs = []
        for i, (sym, row) in enumerate(ranked):
            if sym in held:
                continue
            above = row["close"] / row["sma"] - 1
            sigs.append(EntrySignal(
                symbol=sym, date=day, close=float(row["close"]), atr=float(row["atr"]),
                score=float(row["mom"]),
                reasons=[
                    f"Momentum: son {self.p.lookback} günde (son {self.p.skip_days} gün hariç) "
                    f"%{100 * row['mom']:+.1f} — uygun {n} hisse içinde {i + 1}. sırada",
                    f"Trend: kapanış {row['close']:.2f} > {self.p.trend_sma}g ortalama "
                    f"{row['sma']:.2f} (%{100 * above:.1f} üstünde)",
                    "Aylık yenileme günü: boşalan yer en güçlü adayla dolduruluyor",
                ],
                facts={"rank": i + 1, "mom": float(row["mom"])},
            ))
        return sigs

    def hold_while(self):
        return (f"Aylık sıralamada ilk {self.p.hold_rank} içinde ve "
                f"{self.p.trend_sma}g ortalamanın üstünde kaldıkça")

    def exit_if(self):
        return [
            f"İz süren stop: en yüksek kapanış - {self.p.atr_stop_mult:g} x ATR (stop asla aşağı inmez)",
            f"Ay başı yenilemede ilk {self.p.hold_rank}'ten düşerse ya da trend bozulursa ertesi açılışta sat",
            "KAP'ta ciddi olumsuz haber gelirse yeniden değerlendir",
        ]
