"""Günlük karar döngüsü. Backtest ve canlı paper aynı motoru kullanır.

Bir seans günü için sıra:
  1. Broker seansı işler: bekleyen emirler açılıştan dolar, stop'lar kontrol edilir.
  2. Kapanış sonrası: açık pozisyonların iz süren stop'ları güncellenir,
     stratejinin çıkış nedeni olanlara satış emri verilir.
  3. Stratejinin giriş adayları sırayla, (ileride KAP filtresinden geçip) lot
     hesabına sokulur ve ertesi açılış için emir verilir.
  4. Günlük portföy değeri kaydedilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from .broker.base import Fill, OrderRequest
from .broker.paper import PaperBroker
from .config import Settings
from .data.base import Bar
from .risk.sizing import size_equal_weight, size_position
from .strategy.base import EntrySignal, Strategy
from .strategy.card import entry_card, exit_card

Rows = dict[str, dict[date, dict[str, Any]]]
# Haber filtresi: (sinyal) -> (izin_var_mı, kart için haber bilgisi)
NewsFilter = Callable[[EntrySignal], tuple[bool, dict[str, Any]]]


def build_rows(data: dict[str, pd.DataFrame], strategy: Strategy) -> Rows:
    rows: Rows = {}
    for sym, df in data.items():
        feat = strategy.features(df)
        rows[sym] = {ts.date(): rec for ts, rec in feat.to_dict("index").items()}
    strategy.prepare(rows)
    return rows


@dataclass
class DayResult:
    day: date
    fills: list[Fill] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)   # verilen emirlerin kartları
    skipped: list[str] = field(default_factory=list)              # elenen adaylar ve nedeni
    equity: float = 0.0


class Engine:
    def __init__(self, broker: PaperBroker, settings: Settings, strategy: Strategy,
                 rows: Rows, news_filter: NewsFilter | None = None, sizing: str | None = None,
                 allow_entries: bool = True):
        self.broker = broker
        self.settings = settings
        self.strategy = strategy
        self.rows = rows
        self.news_filter = news_filter
        self.sizing = sizing or settings.sizing
        self.allow_entries = allow_entries   # False: /durdur — yeni alım yok, stop ve çıkışlar çalışır
        self.last_close: dict[str, float] = {}   # işlem görmeyen günlerde son bilinen fiyat

    def run_day(self, day: date) -> DayResult:
        res = DayResult(day)
        today = {s: r[day] for s, r in self.rows.items() if day in r}
        bars = {s: Bar(s, day, r["open"], r["high"], r["low"], r["close"], r["volume"]) for s, r in today.items()}

        # 1) Açılış dolumları + stop'lar
        res.fills = self.broker.process_day(day, bars)
        self.last_close.update({s: r["close"] for s, r in today.items()})

        # 2) Açık pozisyonlar: iz süren stop + çıkış
        positions = self.broker.positions()
        for sym, pos in positions.items():
            if sym in today:
                stop = self.strategy.trailing_stop(today[sym], max(pos.highest_close, today[sym]["close"]))
                if stop is not None:
                    self.broker.update_stop(sym, stop)
        exits = self.strategy.exit_reasons(day, today, set(positions))
        freed_cash = 0.0
        for sym, reason in exits.items():
            pos = positions[sym]
            card = exit_card(sym, day.isoformat(), reason, today[sym]["close"], pos.avg_cost, self.strategy.name)
            self.broker.submit(OrderRequest(sym, "SELL", pos.qty, day, reason=card, tag="exit"))
            res.orders.append(card)
            freed_cash += pos.qty * today[sym]["close"]

        # 3) Yeni girişler (satışlar ertesi açılışta alımlardan önce dolar)
        equity = self.broker.equity(self.last_close)
        pending = [o for o in self.broker.pending_orders() if o["side"] == "BUY"]
        cash = self.broker.cash() + freed_cash - sum(o["qty"] * self.last_close.get(o["symbol"], 0) for o in pending)
        busy = set(positions) | {o["symbol"] for o in pending}
        since = self.strategy.cooldown_since(day)
        if since is not None:
            busy |= self.broker.stopped_since(since)   # stop olan hisse bekleme süresinde
        open_count = len(positions) - len(exits) + len(pending)

        sizer = size_equal_weight if self.sizing == "equal_weight" else size_position
        signals = self.strategy.entry_signals(day, today, busy) if self.allow_entries else []
        for sig in signals:
            if open_count >= self.settings.risk.max_open_positions:
                break
            news: dict[str, Any] | None = None
            if self.news_filter:
                allowed, news = self.news_filter(sig)
                if not allowed:
                    res.skipped.append(f"{sig.symbol}: KAP filtresi — {news.get('summary', '')}")
                    continue
            size = sizer(
                equity=equity, cash=cash, open_positions=open_count, entry_price=sig.close,
                atr=sig.atr, atr_mult=self.strategy.atr_stop_mult,
                risk=self.settings.risk, costs=self.settings.costs,
            )
            if not size.approved:
                res.skipped.append(f"{sig.symbol}: {'; '.join(size.notes)}")
                continue
            card = entry_card(sig, size, self.strategy, equity, news)
            self.broker.submit(OrderRequest(sig.symbol, "BUY", size.qty, day, stop_price=size.stop_price, reason=card))
            res.orders.append(card)
            cash -= size.notional
            open_count += 1

        # 4) Gün sonu portföy değeri
        res.equity = self.broker.snapshot(day, self.last_close)
        return res
