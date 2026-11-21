"""Haber/bildirim kaynağı arayüzü.

Filtre ve analiz katmanı sadece bu arayüzü bilir. Bugün KapWebSource (KAP sitesi),
ileride MatriksNewsSource aynı arayüzü uygular; geri kalan kod değişmez.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Disclosure:
    source: str            # "kap_web", "matriks", ...
    disclosure_id: str     # kaynağın benzersiz kimliği (önbellek anahtarı)
    symbol: str            # BIST kodu (THYAO)
    published_at: datetime
    title: str
    subject: str = ""      # KAP konu başlığı (ör. "Özel Durum Açıklaması")
    summary: str = ""
    url: str = ""
    body: str = ""         # varsa tam metin

    @property
    def key(self) -> str:
        return f"{self.source}:{self.disclosure_id}"


class NewsSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, symbols: list[str], since: date, until: date) -> list[Disclosure]:
        """[since, until] aralığında verilen hisselere ait bildirimleri döndürür."""


class NewsSourceError(RuntimeError):
    """Kaynağa ulaşılamadı / yanıt anlaşılamadı. Bot durmaz, uyarı verir."""
