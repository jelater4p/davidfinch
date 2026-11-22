"""İşlem bildirimleri: her gerçekleşen alış ve satış için ayrı, okunabilir mesaj.

Alış:  gerçekleşen fiyat, lot, tutar, neden alındı, beklenti (en kötü durum = stop,
       ne zamana kadar tutulacağı, geçmiş benzer işlemlerin istatistiği).
Satış: alış → satış fiyatı, masraflar dahil % ve TL kâr/zarar, tutma süresi, neden çıkıldı.
"""
from __future__ import annotations

import json
from datetime import date

from ..broker.base import Fill
from ..config import ExpectationConfig
from ..storage.db import Database


def expectation_line(db: Database, exp: ExpectationConfig) -> str:
    rows = db.query("SELECT pnl, pnl_pct, entry_date, exit_date FROM trades")
    if len(rows) >= exp.min_own_trades:
        wins = [r["pnl_pct"] for r in rows if r["pnl"] > 0]
        losses = [r["pnl_pct"] for r in rows if r["pnl"] <= 0]
        hold = [(date.fromisoformat(r["exit_date"]) - date.fromisoformat(r["entry_date"])).days for r in rows]
        win_rate, avg_win = len(wins) / len(rows), (sum(wins) / len(wins) if wins else 0.0)
        avg_loss, avg_hold = (sum(losses) / len(losses) if losses else 0.0), sum(hold) / len(hold)
        source = f"botun kendi {len(rows)} işlemi"
    else:
        win_rate, avg_win, avg_loss, avg_hold = exp.win_rate, exp.avg_win_pct, exp.avg_loss_pct, exp.avg_hold_days
        source = exp.source
    return (f"Geçmiş benzer işlemler ({source}): kazanma oranı %{100 * win_rate:.0f}, "
            f"ort. kazanç %{100 * avg_win:+.1f}, ort. kayıp %{100 * avg_loss:+.1f}, ort. süre {avg_hold:.0f} gün")


def entry_note(db: Database, fill: Fill, equity: float, exp: ExpectationConfig) -> str:
    order = db.query_one("SELECT * FROM orders WHERE id = ?", (fill.order_id,))
    card = json.loads(order["reason"] or "{}")
    stop = order["stop_price"]
    notional = fill.qty * fill.price
    max_loss = fill.qty * (fill.price - stop) + fill.fees if stop else None
    lines = [
        f"🟢 ALINDI: {fill.symbol}  ({fill.date:%d.%m.%Y} açılış)",
        f"Alış: {fill.price:.2f} TL × {fill.qty} lot = {notional:,.0f} TL "
        f"(portföy payı %{100 * notional / equity:.1f}, masraf {fill.fees:.2f} TL)",
    ]
    if fill.note:
        lines.append(f"Not: {fill.note}")
    why = card.get("why") or []
    if why:
        lines.append("Neden:")
        lines += [f"  • {w}" for w in why]
    news = (card.get("news") or {}).get("summary")
    if news:
        lines.append(f"  • KAP: {news}")
    lines.append("Beklenti:")
    if stop:
        lines.append(f"  • En kötü durum: stop {stop:.2f} → %{100 * (stop / fill.price - 1):+.1f} "
                     f"(≈{max_loss:,.0f} TL kayıp)")
    lines.append(f"  • Hedef fiyat yok; {card.get('hold_while', 'trend sürdükçe').lower()} tutulur. "
                 "Fiyat yükseldikçe stop yukarı çekilir ve kâr kilitlenir")
    lines.append(f"  • {expectation_line(db, exp)}")
    return "\n".join(lines)


def _exit_reason_text(trade, entry_fill_price: float | None, gap_note: str | None) -> str:
    reason = trade["exit_reason"]
    if reason != "stop":
        return reason
    stop, high = trade["stop_price"], trade["highest_close"]
    if stop and entry_fill_price and stop > entry_fill_price:
        text = f"İz süren stop ({stop:.2f}) tetiklendi, kâr kilitlendi"
    else:
        text = f"Zarar durdur (stop {stop:.2f}) tetiklendi" if stop else "Stop tetiklendi"
    if high:
        text += f" — pozisyonun en yüksek kapanışı {high:.2f} idi"
    if gap_note:
        text += "; açılış stop'un altında olduğu için açılış fiyatından satıldı"
    return text


def exit_note(db: Database, fill: Fill) -> str | None:
    t = db.query_one("SELECT * FROM trades WHERE exit_order_id = ? ORDER BY id DESC LIMIT 1", (fill.order_id,))
    if t is None:
        return None
    entry = db.query_one("SELECT fill_price FROM orders WHERE id = ?", (t["entry_order_id"],))
    entry_price = entry["fill_price"] if entry else None
    days = (date.fromisoformat(t["exit_date"]) - date.fromisoformat(t["entry_date"])).days
    icon = "🔴" if t["pnl"] < 0 else "🔵"
    head = f"{icon} SATILDI: {fill.symbol} — %{100 * t['pnl_pct']:+.1f} ({t['pnl']:+,.0f} TL)"
    prices = (f"Alış {entry_price:.2f} → Satış {fill.price:.2f}" if entry_price else f"Satış {fill.price:.2f}")
    return "\n".join([
        head,
        f"{prices} | {fill.qty} lot | {days} gün | {fill.date:%d.%m.%Y}",
        f"Neden: {_exit_reason_text(t, entry_price, fill.note or None)}",
        "(% ve TL, alış-satış komisyonu, BSMV ve kayma dahil net sonuçtur)",
    ])
