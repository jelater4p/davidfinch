"""Günlük karar döngüsü. Backtest ve canlı paper aynı motoru kullanır.

Bir seans günü için sıra:
  1. Broker seansı işler: bekleyen emirler açılıştan dolar, stop'lar kontrol edilir.
  2. Kapanış sonrası: açık pozisyonların iz süren stop'ları güncellenir,
     çıkış sinyali olanlara satış emri verilir.
  3. Yeni giriş sinyalleri trend gücüne göre sıralanır, lot hesaplanır,
     (ileride KAP filtresinden geçer) ve ertesi açılış için emir verilir.
  4. Günlük portföy değeri kaydedilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from .broker.base import Fill, OrderRequest
from .broker.paper import PaperBroker
from .config import Settings, StrategyConfig
from .data.base import Bar
from .risk.sizing import size_position
from .strategy.card import entry_card, exit_card
from .strategy.pullback import EntrySignal, compute_features, entry_signal, exit_reason, trailing_stop

Rows = dict[str, dict[date, dict[str, Any]]]
# Haber filtresi: (sinyal) -> (izin_var_mı, kart için haber bilgisi)
NewsFilter = Callable[[EntrySignal], tuple[bool, dict[str, Any]]]


def build_rows(data: dict[str, pd.DataFrame], p: StrategyConfig) -> Rows:
    rows: Rows = {}
    for sym, df in data.items():
        feat = compute_features(df, p)
        rows[sym] = {ts.date(): rec for ts, rec in feat.to_dict("index").items()}
    return rows


@dataclass
class DayResult:
    day: date
    fills: list[Fill] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)   # verilen emirlerin kartları
    skipped: list[str] = field(default_factory=list)              # elenen sinyaller ve nedeni
    equity: float = 0.0


class Engine:
    def __init__(self, broker: PaperBroker, settings: Settings, params: StrategyConfig,
                 rows: Rows, news_filter: NewsFilter | None = None):
        self.broker = broker
        self.settings = settings
        self.p = params
        self.rows = rows
        self.news_filter = news_filter
        self.last_close: dict[str, float] = {}   # işlem görmeyen günlerde son bilinen fiyat

    def run_day(self, day: date) -> DayResult:
        res = DayResult(day)
        today = {s: r[day] for s, r in self.rows.items() if day in r}
        bars = {s: Bar(s, day, r["open"], r["high"], r["low"], r["close"], r["volume"]) for s, r in today.items()}

        # 1) Açılış dolumları + stop'lar
        res.fills = self.broker.process_day(day, bars)

        # 2) Açık pozisyonlar: iz süren stop + çıkış sinyali
        positions = self.broker.positions()
        for sym, pos in positions.items():
            row = today.get(sym)
            if row is None:
                continue
            if row.get("atr") == row.get("atr"):   # NaN değilse
                self.broker.update_stop(sym, trailing_stop(max(pos.highest_close, row["close"]), row["atr"], self.p))
            reason = exit_reason(row, self.p)
            if reason:
                card = exit_card(sym, day.isoformat(), reason, row["close"], pos.avg_cost)
                self.broker.submit(OrderRequest(sym, "SELL", pos.qty, day, reason=card, tag="exit"))
                res.orders.append(card)

        # 3) Yeni girişler
        closes = {s: r["close"] for s, r in today.items()}
        self.last_close.update(closes)
        equity = self.broker.equity(self.last_close)
        pending = [o for o in self.broker.pending_orders() if o["side"] == "BUY"]
        cash = self.broker.cash() - sum(o["qty"] * closes.get(o["symbol"], 0) for o in pending)
        busy = set(positions) | {o["symbol"] for o in pending}
        open_count = len(positions) + len(pending)

        signals = [
            sig for sym, row in today.items()
            if sym not in busy and (sig := entry_signal(sym, day, row, self.p)) is not None
        ]
        signals.sort(key=lambda s: s.score, reverse=True)
        for sig in signals:
            news: dict[str, Any] | None = None
            if self.news_filter:
                allowed, news = self.news_filter(sig)
                if not allowed:
                    res.skipped.append(f"{sig.symbol}: KAP filtresi — {news.get('summary', '')}")
                    continue
            size = size_position(
                equity=equity, cash=cash, open_positions=open_count, entry_price=sig.close,
                atr=sig.atr, atr_mult=self.p.atr_stop_mult, risk=self.settings.risk, costs=self.settings.costs,
            )
            if not size.approved:
                res.skipped.append(f"{sig.symbol}: {'; '.join(size.notes)}")
                continue
            card = entry_card(sig, size, self.p, equity, news)
            self.broker.submit(OrderRequest(sig.symbol, "BUY", size.qty, day, stop_price=size.stop_price, reason=card))
            res.orders.append(card)
            cash -= size.notional
            open_count += 1

        # 4) Gün sonu portföy değeri
        res.equity = self.broker.snapshot(day, self.last_close)
        return res
