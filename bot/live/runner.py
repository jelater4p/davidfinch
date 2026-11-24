"""Canlı paper çalışma: her işlem günü akşamı tek iş.

evening(day) sırası:
  1. Veriyi yenile; elde tutulan hisselerde bedelsiz/bölünme varsa pozisyonu uyarla
     (stop kontrolünden ÖNCE — yoksa yarıya inen fiyat sahte stop tetikler).
  2. Ay başıysa aylık parametre güncellemesi (frenli; ayda bir kez).
  3. Motor: bekleyen emirler bugünün açılış fiyatından dolar, stop'lar bugünün
     en düşüğüyle kontrol edilir, ertesi gün için kararlar verilir (KAP filtresiyle).
  4. Elde tutulan hisseler için KAP uyarıları, günlük rapor.

Aynı gün iki kez çalışırsa ikinci çalışma hiçbir şey yapmaz (broker.last_processed_date).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..broker.paper import PaperBroker
from ..config import Settings
from ..data.base import DataSource
from ..data.store import PriceStore, detect_adjustment
from ..engine import Engine, build_rows
from ..learning.live import already_updated, current_params, monthly_update
from ..storage.db import Database
from ..strategy.card import render_card
from ..strategy.momentum import MomentumStrategy

log = logging.getLogger(__name__)
PAUSED_KEY = "paused"


@dataclass
class EveningResult:
    status: str                       # "ok" | "no_data" | "already_done"
    message: str = ""
    sections: list[str] = field(default_factory=list)
    trade_messages: list[str] = field(default_factory=list)   # her alış/satış için ayrı mesaj


class LiveRunner:
    def __init__(self, settings: Settings, db: Database, source: DataSource, news_filter=None):
        self.settings = settings
        self.db = db
        self.source = source
        self.news_filter = news_filter
        self.store = PriceStore(db)
        self.broker = PaperBroker(db, settings.risk, settings.costs)

    # ------------------------------------------------------------------ durum
    def paused(self) -> bool:
        return self.db.get_state(PAUSED_KEY, "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.db.set_state(PAUSED_KEY, "1" if value else "0")

    # ------------------------------------------------------------------ veri
    def refresh_data(self, day: date) -> tuple[bool, list[str]]:
        """Veriyi çeker. (bugünün verisi var mı, bölünme mesajları) döner."""
        symbols = self.settings.universe() + [self.settings.benchmark]
        start = day - timedelta(days=self.settings.history_days)
        fresh = self.source.fetch_daily(symbols, start, day)
        held = self.broker.positions()
        notes = []
        for sym, df in fresh.items():
            if sym in held:
                old = self.store.history(sym, start=start, end=day - timedelta(days=1))
                factor = detect_adjustment(old, df)
                if factor:
                    pos = held[sym]
                    self.broker.apply_split(sym, factor)
                    notes.append(f"🔁 {sym}: fiyat geçmişi x{factor:.3f} düzeltildi (bedelsiz/bölünme). "
                                 f"Lot {pos.qty} → {round(pos.qty / factor)}, stop {pos.stop_price:.2f} → "
                                 f"{pos.stop_price * factor:.2f}")
            self.store.replace_history(sym, df)
        bench = fresh.get(self.settings.benchmark)
        has_today = bench is not None and not bench.empty and bench.index[-1].date() == day
        return has_today, notes

    def _load(self, day: date) -> dict[str, pd.DataFrame]:
        data = {}
        for sym in self.settings.universe():
            df = self.store.history(sym, end=day)
            if len(df) > 60:
                data[sym] = df
        return data

    # ------------------------------------------------------------------ akşam işi
    def evening(self, day: date, fetch: bool = True) -> EveningResult:
        last = self.broker.last_processed_date()
        if last is not None and day <= last:
            return EveningResult("already_done", f"{day} zaten işlendi.")

        notes: list[str] = []
        if fetch:
            has_today, notes = self.refresh_data(day)
            if not has_today:
                return EveningResult("no_data", f"{day} için kapanış verisi henüz yok (tatil olabilir).")

        data = self._load(day)
        params = current_params(self.db, self.settings)
        strat = MomentumStrategy(params)
        rows = build_rows(data, strat)
        if day not in strat.trading_days:
            return EveningResult("no_data", f"{day} için hisse verisi yok.")

        sections = list(notes)
        # Kaçırılan işlem günleri (VPS kapalı kaldıysa) sırayla işlenir; stop'lar atlanmaz.
        days = [d for d in strat.trading_days if (last is None or d > last) and d <= day]
        if last is None:
            days = [day]
        if len(days) > 1:
            sections.append(f"⏪ Kaçırılan {len(days) - 1} işlem günü de işlendi: "
                            + ", ".join(f"{d:%d.%m}" for d in days[:-1]))
        all_fills, all_orders, all_skipped = [], [], []
        for d in days:
            if strat.is_rebalance_day(d) and not already_updated(self.db, d):
                dec = monthly_update(self.db, self.settings, {s: df[df.index <= pd.Timestamp(d)]
                                                              for s, df in data.items()}, d)
                if dec is not None:
                    sections.append(("🧠 Aylık parametre kararı:\n" if dec.changed else "🧠 Aylık kontrol:\n")
                                    + dec.explanation)
                    if dec.changed:
                        strat = MomentumStrategy(dec.params)
                        rows = build_rows(data, strat)
            engine = Engine(self.broker, self.settings, strat, rows, news_filter=self.news_filter,
                            allow_entries=not self.paused())
            for sym, r in rows.items():   # işlem görmeyen hisseler için son bilinen fiyat
                past = [x for x in r if x < d]
                if past:
                    engine.last_close[sym] = r[max(past)]["close"]
            res = engine.run_day(d)
            all_fills += res.fills
            all_orders = res.orders          # sadece son günün emirleri hâlâ bekliyor
            all_skipped = res.skipped

        trade_messages = self._trade_messages(all_fills)
        for card in all_orders:
            sections.append(("📝 Yarın açılışta:\n" if card.get("type") == "entry" else "📤 Yarın açılışta:\n")
                            + render_card(card))
        if all_skipped:
            sections.append("⏭ Elenen adaylar:\n" + "\n".join(f"• {s}" for s in all_skipped[:15]))
        if self.news_filter is not None:
            alerts = self.news_filter.held_alerts(day, list(self.broker.positions()))
            if alerts:
                sections.append("🚨 Elde tutulan hisselerde KAP uyarısı (satış kararı sende):\n" + "\n".join(alerts))
            if self.news_filter.warnings:
                sections.append("⚠️ " + "\n⚠️ ".join(dict.fromkeys(self.news_filter.warnings)))
                self.news_filter.warnings.clear()
        if self.paused():
            sections.append("⏸ Bot durdurulmuş durumda: yeni alım yapılmıyor (stop ve çıkışlar çalışıyor). /devam")
        sections.append(self.summary(day))
        return EveningResult("ok", f"📊 {day:%d.%m.%Y} gün sonu", sections, trade_messages)

    def _trade_messages(self, fills) -> list[str]:
        """Her gerçekleşen işlem için ayrı mesaj (alışta beklenti, satışta sonuç ve neden)."""
        from .trade_notes import entry_note, exit_note

        out = []
        for f in fills:
            if f.side == "BUY":
                snap = self.db.query_one("SELECT equity FROM equity_snapshots WHERE date = ?", (f.date.isoformat(),))
                equity = snap["equity"] if snap else self.settings.risk.initial_capital
                out.append(entry_note(self.db, f, equity, self.settings.expectations))
            else:
                note = exit_note(self.db, f)
                if note:
                    out.append(note)
        return out

    # ------------------------------------------------------------------ sabah dolumu
    def morning_fill(self, day: date) -> list[str]:
        """Bekleyen emirleri bugünün açılış fiyatından (Yahoo gün içi, ~15 dk gecikmeli) gerçekleştirir.

        Akşam işiyle aynı fiyat (açılış) ve aynı kurallar kullanılır; sadece kayıt sabaha çekilir.
        Fiyatı gelmeyen hissenin emri beklemede kalır, akşam işi onu her zamanki gibi doldurur.
        """
        from .trade_notes import entry_note, exit_note

        pending = self.broker.pending_orders()
        if not pending:
            return []
        quotes = self.source.fetch_intraday(sorted({o["symbol"] for o in pending}))
        opens = {s: q.open for s, q in quotes.items()
                 if q.open and q.time[:10] == day.isoformat()}     # sadece bugünün mumları
        fills = self.broker.fill_at_open(day, opens)
        prices = {s: self.store.last_close(s, day) or 0.0 for s in self.broker.positions()}
        prices.update(opens)
        equity = self.broker.equity(prices)
        msgs = []
        for f in fills:
            if f.side == "BUY":
                msgs.append(entry_note(self.db, f, equity, self.settings.expectations))
            else:
                note = exit_note(self.db, f)
                if note:
                    msgs.append(note)
        return msgs

    # ------------------------------------------------------------------ rapor
    def summary(self, day: date) -> str:
        positions = self.broker.positions()
        closes = {s: self.store.last_close(s, day) or p.avg_cost for s, p in positions.items()}
        equity = self.broker.equity(closes)
        cash = self.broker.cash()
        initial = self.settings.risk.initial_capital
        prev = self.db.query_one("SELECT equity FROM equity_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
                                 (day.isoformat(),))
        first = self.db.query_one("SELECT date FROM equity_snapshots ORDER BY date LIMIT 1")
        lines = [f"💼 Portföy: {equity:,.0f} TL (başlangıçtan %{100 * (equity / initial - 1):+.1f})"]
        if prev:
            lines.append(f"   Günlük: %{100 * (equity / prev['equity'] - 1):+.2f}")
        if first:
            from ..benchmarks import deposit_return, net_rate

            start = date.fromisoformat(first["date"])
            b0 = self.store.last_close(self.settings.benchmark, start)
            b1 = self.store.last_close(self.settings.benchmark, day)
            if b0 and b1:
                lines.append(f"   BIST100 aynı dönem: %{100 * (b1 / b0 - 1):+.1f}")
            # Para, ilk işlem gününden bir gün önce mevduata yatırılmış gibi
            dep = deposit_return(self.settings.deposit, start - timedelta(days=1), day)
            bot = equity / initial - 1
            lines.append(f"   Mevduat aynı dönem: %{100 * dep:+.1f} (net yıllık %"
                         f"{100 * net_rate(self.settings.deposit, day):.1f}) → bot farkı "
                         f"{100 * (bot - dep):+.1f} puan")
        lines.append(f"   Nakit: {cash:,.0f} TL (%{100 * cash / equity:.0f}) | Pozisyon: {len(positions)}"
                     f"/{self.settings.risk.max_open_positions}")
        return "\n".join(lines)

    def pending_preview(self) -> str:
        pending = self.broker.pending_orders()
        if not pending:
            return "Bugün açılışta bekleyen emir yok."
        lines = ["Bugün açılışta gerçekleşecek emirler:"]
        for o in pending:
            card = json.loads(o["reason"] or "{}")
            why = card.get("exit_reason") or (card.get("why") or [""])[0]
            lines.append(f"• {'AL' if o['side'] == 'BUY' else 'SAT'} {o['symbol']} {o['qty']} lot — {why}")
        return "\n".join(lines)
