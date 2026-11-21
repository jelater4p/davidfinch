"""Borsa İstanbul pay piyasası kuralları: fiyat adımı (tick) yuvarlama.

Fiyat adımları BIST pay piyasası tablosuna göredir; Borsa İstanbul tabloyu
değiştirirse burası güncellenmelidir.
"""
from __future__ import annotations

import math

# (üst sınır, adım) — fiyat bu sınırın altındaysa ilgili adım geçerli
_TICK_TABLE = [
    (20.0, 0.01),
    (50.0, 0.02),
    (100.0, 0.05),
    (250.0, 0.10),
    (500.0, 0.25),
    (1000.0, 0.50),
    (2500.0, 1.00),
    (math.inf, 2.50),
]


def tick_size(price: float) -> float:
    for upper, step in _TICK_TABLE:
        if price < upper:
            return step
    return _TICK_TABLE[-1][1]


def round_to_tick(price: float, side: str) -> float:
    """Alımda yukarı, satımda aşağı yuvarlar (her zaman aleyhimize, temkinli)."""
    step = tick_size(price)
    units = price / step
    units = math.ceil(units - 1e-9) if side == "BUY" else math.floor(units + 1e-9)
    return round(units * step, 2)
