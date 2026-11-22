"""Kıyas ölçütleri: TL mevduat getirisi.

Türkiye'de nakit de ciddi getiri sağlar; bot riske girerek mevduatı geçemiyorsa risk almaya
değmez. Mevduat getirisi, yıllık brüt orandan stopaj düşülerek günlük bileşik hesaplanır.
Faiz değiştikçe settings.yaml → deposit.rates listesine yeni oran tarihiyle eklenir; her gün
o gün geçerli oranla hesaplanır.
"""
from __future__ import annotations

from datetime import date, timedelta

from .config import DepositConfig


def rate_on(cfg: DepositConfig, day: date) -> float:
    """O gün geçerli yıllık brüt oran (en son başlangıç tarihi ≤ day olan kayıt)."""
    rate = cfg.rates[0]["gross"] if cfg.rates else 0.0
    for r in sorted(cfg.rates, key=lambda r: str(r["from"])):
        if date.fromisoformat(str(r["from"])) <= day:
            rate = r["gross"]
    return float(rate)


def net_rate(cfg: DepositConfig, day: date) -> float:
    return rate_on(cfg, day) * (1 - cfg.withholding_tax)


def deposit_return(cfg: DepositConfig, start: date, end: date) -> float:
    """start akşamı yatırılan paranın end akşamına kadarki net getirisi (günlük bileşik)."""
    growth = 1.0
    d = start
    while d < end:
        growth *= 1 + net_rate(cfg, d) / 365
        d += timedelta(days=1)
    return growth - 1
