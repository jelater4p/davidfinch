"""Broker arayüzü.

Bugün PaperBroker, ileride gerçek aracı kurum entegrasyonu aynı arayüzü uygular.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str                # 'BUY' | 'SELL'
    qty: int
    decision_date: date      # kararın verildiği gün (kapanış sonrası)
    stop_price: float | None = None   # alımda: açılacak pozisyonun ilk stop'u
    reason: dict[str, Any] = field(default_factory=dict)   # gerekçe kartı
    tag: str = "entry"       # client_order_id'nin parçası (entry, exit, ...)

    @property
    def client_order_id(self) -> str:
        return f"{self.decision_date.isoformat()}:{self.symbol}:{self.side}:{self.tag}"


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_cost: float
    stop_price: float
    entry_date: date
    highest_close: float


@dataclass(frozen=True)
class Fill:
    order_id: int
    symbol: str
    side: str
    qty: int
    price: float
    fees: float
    date: date
    order_type: str
    note: str = ""


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def submit(self, req: OrderRequest) -> int:
        """Emri kuyruğa alır (bir sonraki seans açılışında işlenir). Emir id döner.

        Aynı client_order_id ile ikinci kez çağrılırsa mevcut emrin id'sini döner.
        """

    @abstractmethod
    def update_stop(self, symbol: str, new_stop: float) -> None: ...

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash() + sum(p.qty * prices.get(s, p.avg_cost) for s, p in self.positions().items())
