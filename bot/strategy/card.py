"""Gerekçe kartı: her alım/satım kararının insan tarafından okunabilir açıklaması.

Kart sözlük olarak emirle birlikte veritabanına yazılır (orders.reason),
Telegram'da ve /neden komutunda render_card ile metne çevrilir.
"""
from __future__ import annotations

from typing import Any

from ..config import StrategyConfig
from ..risk.sizing import SizingResult
from .pullback import EntrySignal


def entry_card(sig: EntrySignal, size: SizingResult, p: StrategyConfig, equity: float,
               news: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "entry",
        "symbol": sig.symbol,
        "decision_date": sig.date.isoformat(),
        "ref_price": sig.close,
        "qty": size.qty,
        "notional": size.notional,
        "portfolio_pct": 100 * size.notional / equity,
        "stop": size.stop_price,
        "risk_tl": size.risk_amount,
        "why": sig.reasons(p),
        "sizing": size.notes,
        "news": news or {"status": "KAP filtresi henüz devrede değil"},
        "hold_while": f"Kapanış {p.trend_sma}g ortalamanın üstünde kaldıkça",
        "exit_if": [
            f"İz süren stop: en yüksek kapanış - {p.atr_stop_mult:g} x ATR (stop asla aşağı inmez)",
            f"Kapanış {p.trend_sma}g ortalamanın altına inerse ertesi açılışta sat",
            "KAP'ta ciddi olumsuz haber gelirse yeniden değerlendir",
        ],
        "params": {
            "trend_sma": p.trend_sma, "rsi_entry": p.rsi_entry,
            "atr_stop_mult": p.atr_stop_mult, "volume_mult": p.volume_mult,
        },
    }


def exit_card(symbol: str, decision_date: str, reason: str, close: float,
              avg_cost: float) -> dict[str, Any]:
    return {
        "type": "exit",
        "symbol": symbol,
        "decision_date": decision_date,
        "exit_reason": reason,
        "ref_price": close,
        "unrealized_pct": 100 * (close / avg_cost - 1),
    }


def render_card(card: dict[str, Any]) -> str:
    if card.get("type") == "exit":
        return (
            f"SAT: {card['symbol']} (ref. kapanış {card['ref_price']:.2f}, "
            f"%{card['unrealized_pct']:+.1f})\nNeden: {card['exit_reason']}"
        )
    lines = [
        f"AL: {card['symbol']} @ ~{card['ref_price']:.2f} TL | {card['qty']} lot | "
        f"Portföyün %{card['portfolio_pct']:.1f}'i",
        "Neden:",
        *[f"  • {w}" for w in card["why"]],
        f"KAP/LLM: {card['news'].get('summary') or card['news'].get('status')}",
        f"Tutma koşulu: {card['hold_while']}",
        f"Satış: Stop {card['stop']:.2f} "
        f"(-%{100 * (1 - card['stop'] / card['ref_price']):.1f}, risk ≈ {card['risk_tl']:,.0f} TL)",
        *[f"  • {e}" for e in card["exit_if"]],
        "Lot hesabı:",
        *[f"  • {n}" for n in card["sizing"]],
    ]
    return "\n".join(lines)
