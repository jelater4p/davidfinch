"""Sanal (paper) broker.

Gerçekçilik kuralları:
  * Piyasa emirleri karar gününden SONRAKİ seansın açılış fiyatından dolar.
  * Alımda +kayma, satımda -kayma; fiyat BIST fiyat adımına aleyhimize yuvarlanır.
  * Komisyon + BSMV her iki yönde kesilir.
  * Stop: günün en düşüğü stop'a değerse stop fiyatından, açılış zaten stop'un
    altındaysa (gap) açılış fiyatından satılır.
  * Dolum anında sabit kurallar (min nakit, tek hisse sınırı) yeniden kontrol
    edilir; açılış fiyatı farklı geldiyse lot kırpılır.
"""
from __future__ import annotations

import json
import math
from datetime import date

from ..config import CostConfig, RiskConfig
from ..data.base import Bar
from ..market.bist import round_to_tick
from ..storage.db import Database, dumps
from .base import Broker, Fill, OrderRequest, Position

PENDING_ORDER_MAX_AGE_DAYS = 7   # tatil/işlem durdurma sonrası bayat emirler iptal


class PaperBroker(Broker):
    def __init__(self, db: Database, risk: RiskConfig, costs: CostConfig):
        self.db = db
        self.risk = risk
        self.costs = costs
        if self.db.query_one("SELECT id FROM account WHERE id = 1") is None:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO account (id, cash, initial_capital) VALUES (1, ?, ?)",
                    (risk.initial_capital, risk.initial_capital),
                )

    # ------------------------------------------------------------------ okuma
    def cash(self) -> float:
        return float(self.db.query_one("SELECT cash FROM account WHERE id = 1")["cash"])

    def last_processed_date(self) -> date | None:
        v = self.db.query_one("SELECT last_processed_date FROM account WHERE id = 1")[0]
        return date.fromisoformat(v) if v else None

    def positions(self) -> dict[str, Position]:
        return {
            r["symbol"]: Position(
                r["symbol"], r["qty"], r["avg_cost"], r["stop_price"],
                date.fromisoformat(r["entry_date"]), r["highest_close"],
            )
            for r in self.db.query("SELECT * FROM positions")
        }

    def pending_orders(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM orders WHERE status = 'PENDING' ORDER BY id")]

    def stopped_since(self, since: date) -> set[str]:
        rows = self.db.query(
            "SELECT DISTINCT symbol FROM trades WHERE exit_reason = 'stop' AND exit_date >= ?",
            (since.isoformat(),),
        )
        return {r["symbol"] for r in rows}

    # ------------------------------------------------------------------ yazma
    def submit(self, req: OrderRequest) -> int:
        if req.side not in ("BUY", "SELL"):
            raise ValueError("side BUY veya SELL olmalı")
        if req.qty <= 0:
            raise ValueError("qty pozitif olmalı")
        if req.side == "BUY" and (req.stop_price is None or req.stop_price <= 0):
            raise ValueError("Stop'suz alım emri kabul edilmez")
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO orders
                   (client_order_id, symbol, side, qty, order_type, stop_price,
                    decision_date, status, reason)
                   VALUES (?, ?, ?, ?, 'MOO', ?, ?, 'PENDING', ?)""",
                (req.client_order_id, req.symbol, req.side, req.qty, req.stop_price,
                 req.decision_date.isoformat(), dumps(req.reason)),
            )
            row = conn.execute(
                "SELECT id FROM orders WHERE client_order_id = ?", (req.client_order_id,)
            ).fetchone()
        return int(row["id"])

    def cancel(self, order_id: int, note: str = "iptal") -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE orders SET status = 'CANCELLED', note = ? WHERE id = ? AND status = 'PENDING'",
                (note, order_id),
            )

    def apply_split(self, symbol: str, price_factor: float) -> None:
        """Bedelsiz/bölünme: fiyatlar price_factor ile çarpıldı (ör. %100 bedelsizde 0.5).

        Lot 1/price_factor ile çarpılır; maliyet, stop ve en yüksek kapanış aynı oranda düşer.
        Pozisyonun TL değeri değişmez. Bekleyen emirlerin lot ve stop'ları da uyarlanır.
        """
        if price_factor <= 0:
            raise ValueError("price_factor pozitif olmalı")
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE positions SET qty = CAST(ROUND(qty / ?) AS INTEGER), avg_cost = avg_cost * ?,
                       stop_price = stop_price * ?, highest_close = highest_close * ? WHERE symbol = ?""",
                (price_factor, price_factor, price_factor, price_factor, symbol),
            )
            conn.execute(
                """UPDATE orders SET qty = MAX(1, CAST(ROUND(qty / ?) AS INTEGER)), stop_price = stop_price * ?
                   WHERE symbol = ? AND status = 'PENDING'""",
                (price_factor, price_factor, symbol),
            )

    def update_stop(self, symbol: str, new_stop: float, allow_lower: bool = False) -> None:
        """İz süren stop: varsayılan olarak stop asla aşağı çekilmez."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT stop_price FROM positions WHERE symbol = ?", (symbol,)).fetchone()
            if row is None:
                raise KeyError(f"{symbol} pozisyonu yok")
            stop = new_stop if allow_lower else max(row["stop_price"], new_stop)
            conn.execute("UPDATE positions SET stop_price = ? WHERE symbol = ?", (stop, symbol))

    # ------------------------------------------------------------ seans işleme
    def process_day(self, day: date, bars: dict[str, Bar]) -> list[Fill]:
        """Bir seansı işler: önce açılış emirleri, sonra stop kontrolü."""
        last = self.last_processed_date()
        if last is not None and day <= last:
            return []   # zaten işlendi (yeniden başlatma güvenliği)

        fills: list[Fill] = []
        with self.db.transaction() as conn:
            self._expire_stale(conn, day)
            for order in conn.execute(
                "SELECT * FROM orders WHERE status = 'PENDING' AND decision_date < ? ORDER BY id",
                (day.isoformat(),),
            ).fetchall():
                bar = bars.get(order["symbol"])
                if bar is None:
                    continue   # bugün işlem görmedi; emir beklemede kalır
                fill = (self._fill_buy if order["side"] == "BUY" else self._fill_sell)(conn, order, bar, bars)
                if fill:
                    fills.append(fill)

            for pos in conn.execute("SELECT * FROM positions").fetchall():
                bar = bars.get(pos["symbol"])
                if bar is None:
                    continue
                if bar.low <= pos["stop_price"]:
                    fills.append(self._stop_out(conn, pos, bar))
                else:
                    conn.execute(
                        "UPDATE positions SET highest_close = MAX(highest_close, ?) WHERE symbol = ?",
                        (bar.close, pos["symbol"]),
                    )

            conn.execute("UPDATE account SET last_processed_date = ? WHERE id = 1", (day.isoformat(),))
        return fills

    def snapshot(self, day: date, closes: dict[str, float]) -> float:
        cash = self.cash()
        pos_value = sum(p.qty * closes.get(s, p.avg_cost) for s, p in self.positions().items())
        equity = cash + pos_value
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity_snapshots VALUES (?, ?, ?, ?)",
                (day.isoformat(), cash, pos_value, equity),
            )
        return equity

    # ------------------------------------------------------------ iç yardımcılar
    def _expire_stale(self, conn, day: date) -> None:
        for o in conn.execute("SELECT id, decision_date FROM orders WHERE status = 'PENDING'").fetchall():
            if (day - date.fromisoformat(o["decision_date"])).days > PENDING_ORDER_MAX_AGE_DAYS:
                conn.execute(
                    "UPDATE orders SET status = 'CANCELLED', note = 'bayat emir' WHERE id = ?", (o["id"],)
                )

    def _cash(self, conn) -> float:
        return float(conn.execute("SELECT cash FROM account WHERE id = 1").fetchone()["cash"])

    def _equity_at_open(self, conn, bars: dict[str, Bar]) -> float:
        value = self._cash(conn)
        for p in conn.execute("SELECT symbol, qty, avg_cost FROM positions").fetchall():
            bar = bars.get(p["symbol"])
            value += p["qty"] * (bar.open if bar else p["avg_cost"])
        return value

    def _reject(self, conn, order_id: int, note: str) -> None:
        conn.execute("UPDATE orders SET status = 'REJECTED', note = ? WHERE id = ?", (note, order_id))

    def _fill_buy(self, conn, order, bar: Bar, bars: dict[str, Bar]) -> Fill | None:
        price = round_to_tick(bar.open * (1 + self.costs.slippage_rate), "BUY")
        if price <= order["stop_price"]:
            self._reject(conn, order["id"], f"Açılış ({bar.open:.2f}) stop seviyesinin altında")
            return None

        held = conn.execute("SELECT * FROM positions WHERE symbol = ?", (order["symbol"],)).fetchone()
        open_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        if held is None and open_count >= self.risk.max_open_positions:
            self._reject(conn, order["id"], "Açık pozisyon sınırı dolu")
            return None

        equity = self._equity_at_open(conn, bars)
        cash = self._cash(conn)
        unit_cost = price + self.costs.fee(price)
        held_value = held["qty"] * bar.open if held else 0.0
        max_by_cash = math.floor(max(cash - equity * self.risk.min_cash_ratio, 0) / unit_cost)
        max_by_pos = math.floor(max(equity * self.risk.max_position_ratio - held_value, 0) / price)
        qty = min(order["qty"], max_by_cash, max_by_pos)
        if qty < 1:
            self._reject(conn, order["id"], "Dolum anında nakit/tek hisse kuralı izin vermedi")
            return None

        fees = self.costs.fee(qty * price)
        total = qty * price + fees
        note = "" if qty == order["qty"] else f"Lot {order['qty']} → {qty} kırpıldı (açılış fiyatı farkı)"
        conn.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (total,))
        if held:
            new_qty = held["qty"] + qty
            new_cost = (held["qty"] * held["avg_cost"] + total) / new_qty
            conn.execute(
                "UPDATE positions SET qty = ?, avg_cost = ?, stop_price = MAX(stop_price, ?) WHERE symbol = ?",
                (new_qty, new_cost, order["stop_price"], order["symbol"]),
            )
        else:
            conn.execute(
                "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order["symbol"], qty, total / qty, order["stop_price"], bar.date.isoformat(),
                 order["id"], bar.close),
            )
        self._mark_filled(conn, order["id"], bar.date, qty, price, fees, note)
        return Fill(order["id"], order["symbol"], "BUY", qty, price, fees, bar.date, "MOO", note)

    def _fill_sell(self, conn, order, bar: Bar, bars: dict[str, Bar]) -> Fill | None:
        held = conn.execute("SELECT * FROM positions WHERE symbol = ?", (order["symbol"],)).fetchone()
        if held is None:
            self._reject(conn, order["id"], "Satılacak pozisyon yok (stop olmuş olabilir)")
            return None
        qty = min(order["qty"], held["qty"])
        price = round_to_tick(bar.open * (1 - self.costs.slippage_rate), "SELL")
        reason = json.loads(order["reason"] or "{}")
        fill = self._close(conn, held, qty, price, bar.date, reason.get("exit_reason", "sinyal"),
                           order["id"], "MOO")
        self._mark_filled(conn, order["id"], bar.date, qty, price, fill.fees, "")
        return fill

    def _stop_out(self, conn, pos, bar: Bar) -> Fill:
        trigger = min(bar.open, pos["stop_price"])   # gap'te açılıştan
        price = round_to_tick(trigger * (1 - self.costs.slippage_rate), "SELL")
        cur = conn.execute(
            """INSERT INTO orders (client_order_id, symbol, side, qty, order_type, stop_price,
                                   decision_date, status, reason)
               VALUES (?, ?, 'SELL', ?, 'STOP', ?, ?, 'PENDING', ?)""",
            (f"{bar.date.isoformat()}:{pos['symbol']}:SELL:stop", pos["symbol"], pos["qty"],
             pos["stop_price"], bar.date.isoformat(), dumps({"exit_reason": "stop"})),
        )
        order_id = cur.lastrowid
        note = "Açılış stop'un altında (gap)" if bar.open < pos["stop_price"] else ""
        fill = self._close(conn, pos, pos["qty"], price, bar.date, "stop", order_id, "STOP", note)
        self._mark_filled(conn, order_id, bar.date, pos["qty"], price, fill.fees, note)
        return fill

    def _close(self, conn, pos, qty: int, price: float, day: date, exit_reason: str,
               order_id: int, order_type: str, note: str = "") -> Fill:
        fees = self.costs.fee(qty * price)
        proceeds = qty * price - fees
        conn.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (proceeds,))
        exit_unit = proceeds / qty
        pnl = (exit_unit - pos["avg_cost"]) * qty
        conn.execute(
            """INSERT INTO trades (symbol, qty, entry_date, entry_price, exit_date, exit_price,
                                   pnl, pnl_pct, exit_reason, entry_order_id, exit_order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["symbol"], qty, pos["entry_date"], pos["avg_cost"], day.isoformat(), exit_unit,
             pnl, exit_unit / pos["avg_cost"] - 1, exit_reason, pos["entry_order_id"], order_id),
        )
        if qty >= pos["qty"]:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (pos["symbol"],))
        else:
            conn.execute("UPDATE positions SET qty = qty - ? WHERE symbol = ?", (qty, pos["symbol"]))
        return Fill(order_id, pos["symbol"], "SELL", qty, price, fees, day, order_type, note)

    def _mark_filled(self, conn, order_id: int, day: date, qty: int, price: float, fees: float, note: str):
        conn.execute(
            """UPDATE orders SET status = 'FILLED', fill_date = ?, fill_qty = ?, fill_price = ?,
                                 fees = ?, note = ? WHERE id = ?""",
            (day.isoformat(), qty, price, fees, note or None, order_id),
        )
