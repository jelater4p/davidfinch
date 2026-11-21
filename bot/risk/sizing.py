"""Pozisyon büyüklüğü (lot) hesabı ve sabit risk kuralları.

Lot = (portföy x işlem başı risk) / (giriş - stop)
Sonra sırayla şu sınırlar uygulanır; hangisi bağlayıcı olduysa gerekçeye yazılır:
  1) Tek hisse en fazla portföyün max_position_ratio'su
  2) İşlem sonrası nakit en az portföyün min_cash_ratio'su
  3) Açık pozisyon sayısı max_open_positions'ı aşamaz
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import CostConfig, RiskConfig


@dataclass
class SizingResult:
    approved: bool
    qty: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0      # stop olunursa beklenen kayıp (masraflar dahil yaklaşık)
    binding_rule: str = ""
    notes: list[str] = field(default_factory=list)


def size_position(
    *,
    equity: float,
    cash: float,
    open_positions: int,
    entry_price: float,
    atr: float,
    atr_mult: float,
    risk: RiskConfig,
    costs: CostConfig,
) -> SizingResult:
    notes: list[str] = []

    if open_positions >= risk.max_open_positions:
        return SizingResult(False, notes=[f"Açık pozisyon sınırı dolu ({risk.max_open_positions})"])
    if entry_price <= 0 or atr <= 0 or equity <= 0:
        return SizingResult(False, notes=["Geçersiz fiyat/ATR/portföy değeri"])

    stop_distance = atr * atr_mult
    stop_price = entry_price - stop_distance
    if stop_price <= 0:
        return SizingResult(False, notes=["Stop seviyesi sıfırın altında kalıyor"])

    # Stop olunursa birim başına kayıp: fiyat farkı + kayma + iki yönlü masraf
    per_share_loss = (
        stop_distance
        + entry_price * costs.slippage_rate
        + stop_price * costs.slippage_rate
        + costs.fee(entry_price)
        + costs.fee(stop_price)
    )
    risk_budget = equity * risk.risk_per_trade
    candidates = {
        "işlem başı risk": math.floor(risk_budget / per_share_loss),
    }

    max_notional = equity * risk.max_position_ratio
    candidates["tek hisse sınırı"] = math.floor(max_notional / entry_price)

    # İşlem sonrası nakit >= portföy x min_cash_ratio (alım masrafı dahil)
    spendable = cash - equity * risk.min_cash_ratio
    unit_cost = entry_price * (1 + costs.slippage_rate) + costs.fee(entry_price)
    candidates["minimum nakit kuralı"] = math.floor(max(spendable, 0) / unit_cost)

    binding_rule = min(candidates, key=candidates.get)
    qty = candidates[binding_rule]
    if qty < 1:
        return SizingResult(
            False,
            entry_price=entry_price,
            stop_price=stop_price,
            binding_rule=binding_rule,
            notes=[f"'{binding_rule}' nedeniyle 1 lot bile alınamıyor"],
        )

    notional = qty * entry_price
    risk_amount = qty * per_share_loss
    notes.append(
        f"Stop = giriş {entry_price:.2f} - {atr_mult} x ATR {atr:.2f} = {stop_price:.2f}"
    )
    notes.append(
        f"Bağlayıcı kural: {binding_rule} → {qty} lot "
        f"(portföyün %{100 * notional / equity:.1f}'i, risk ≈ {risk_amount:,.0f} TL "
        f"= %{100 * risk_amount / equity:.2f})"
    )
    return SizingResult(
        True,
        qty=qty,
        entry_price=entry_price,
        stop_price=stop_price,
        notional=notional,
        risk_amount=risk_amount,
        binding_rule=binding_rule,
        notes=notes,
    )
