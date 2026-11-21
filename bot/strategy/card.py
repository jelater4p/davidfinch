"""Gerekçe kartı: her alım/satım kararının insan tarafından okunabilir açıklaması.

Kart sözlük olarak emirle birlikte veritabanına yazılır (orders.reason),
Telegram'da ve /neden komutunda render_card ile metne çevrilir.
"""
from __future__ import annotations

from typing import Any

from ..risk.sizing import SizingResult
from .base import EntrySignal, Strategy


def entry_card(sig: EntrySignal, size: SizingResult, strategy: Strategy, equity: float,
               news: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "entry",
        "strategy": strategy.name,
        "symbol": sig.symbol,
        "decision_date": sig.date.isoformat(),
        "ref_price": sig.close,
        "qty": size.qty,
        "notional": size.notional,
        "portfolio_pct": 100 * size.notional / equity,
        "stop": size.stop_price,
        "risk_tl": size.risk_amount,
        "why": sig.reasons,
        "sizing": size.notes,
        "news": news or {"status": "KAP filtresi henüz devrede değil"},
        "hold_while": strategy.hold_while(),
        "exit_if": strategy.exit_if(),
        "params": strategy.param_dict(),
    }


def exit_card(symbol: str, decision_date: str, reason: str, close: float,
              avg_cost: float, strategy: str = "") -> dict[str, Any]:
    return {
        "type": "exit",
        "strategy": strategy,
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
        f"Portföyün %{card['portfolio_pct']:.1f}'i  [{card.get('strategy', '')}]",
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
