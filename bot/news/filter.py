"""KAP filtresi: son N günde CİDDİ OLUMSUZ bildirimi olan hisse alınmaz.

  * Gün başına bir kez kaynaktan çekilir (refresh), yeni bildirimler analiz edilir.
  * Kaynak veya analiz hatası alımı ENGELLEMEZ; karta ve uyarılara "kontrol edilemedi" yazılır.
  * Elde tutulan hisseler için held_alerts() Telegram uyarısı üretir (otomatik satış yok).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..strategy.base import EntrySignal
from .analyzer import NewsAnalyzer
from .base import NewsSource, NewsSourceError
from .store import NewsStore

log = logging.getLogger(__name__)


@dataclass
class NewsFilterConfig:
    lookback_days: int = 7          # takvim günü (~5 işlem günü)
    min_confidence: float = 0.6     # bu güvenin altındaki "ciddi" kararlar engellemez


@dataclass
class KapNewsFilter:
    source: NewsSource
    analyzer: NewsAnalyzer
    store: NewsStore
    config: NewsFilterConfig = field(default_factory=NewsFilterConfig)
    warnings: list[str] = field(default_factory=list)
    _refreshed: set = field(default_factory=set)

    def refresh(self, day: date, symbols: list[str]) -> None:
        """Bildirimleri çek + yenileri analiz et. Aynı gün aynı hisse için tekrar çekmez."""
        todo = [s for s in symbols if (day, s) not in self._refreshed]
        if not todo:
            return
        since = day - timedelta(days=self.config.lookback_days)
        try:
            saved = self.store.save(self.source.fetch(todo, since, day))
            log.info("KAP: %d yeni bildirim kaydedildi", saved)
        except NewsSourceError as exc:
            self.warnings.append(f"KAP kontrolü yapılamadı: {exc}")
            return   # yenilenmedi olarak kalsın; bir sonraki çağrıda tekrar denenir
        for d in self.store.unanalyzed():
            self.store.save_analysis(d, self.analyzer.analyze(d))
        self._refreshed.update((day, s) for s in todo)

    def _severe(self, symbol: str, day: date) -> tuple[list[dict], int, int]:
        rows = self.store.for_symbol(symbol, day - timedelta(days=self.config.lookback_days))
        severe = [r for r in rows if r["severe"] == 1 and (r["confidence"] or 0) >= self.config.min_confidence]
        unknown = sum(1 for r in rows if r["severe"] is None)
        return severe, len(rows), unknown

    def __call__(self, sig: EntrySignal) -> tuple[bool, dict[str, Any]]:
        self.refresh(sig.date, [sig.symbol])
        if (sig.date, sig.symbol) not in self._refreshed:
            return True, {"status": "KAP kontrolü yapılamadı (kaynak hatası) — alım engellenmedi",
                          "summary": "KAP kontrolü yapılamadı"}
        severe, total, unknown = self._severe(sig.symbol, sig.date)
        if severe:
            top = severe[0]
            summary = f"Ciddi olumsuz: {top['category']} — {top['reason']} ({top['published_at'][:10]})"
            return False, {"status": "engellendi", "summary": summary, "url": top["url"]}
        note = f"Son {self.config.lookback_days} günde {total} bildirim, ciddi olumsuz yok"
        if unknown:
            note += f" ({unknown} tanesi analiz edilemedi)"
        return True, {"status": "temiz", "summary": note}

    def held_alerts(self, day: date, symbols: list[str]) -> list[str]:
        """Elde tutulan hisselerde ciddi olumsuz bildirim uyarıları (satış kararı kullanıcıda)."""
        self.refresh(day, symbols)
        alerts = []
        for sym in symbols:
            for r in self._severe(sym, day)[0]:
                alerts.append(f"⚠️ {sym}: {r['category']} — {r['reason']} ({r['published_at'][:16]}) {r['url']}")
        return alerts
