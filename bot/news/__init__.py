from ..config import Settings
from ..storage.db import Database
from .analyzer import Analysis, ClaudeAnalyzer, NewsAnalyzer
from .base import Disclosure, NewsSource, NewsSourceError
from .filter import KapNewsFilter, NewsFilterConfig
from .store import NewsStore


def make_source(settings: Settings) -> NewsSource:
    if settings.news.source == "kap_web":
        from .kap import KapWebSource
        return KapWebSource()
    raise ValueError(f"Bilinmeyen haber kaynağı: {settings.news.source} (Matriks henüz eklenmedi)")


def make_news_filter(settings: Settings, db: Database) -> KapNewsFilter | None:
    if not settings.news.enabled:
        return None
    return KapNewsFilter(
        source=make_source(settings),
        analyzer=ClaudeAnalyzer(model=settings.news.model, effort=settings.news.effort),
        store=NewsStore(db),
        config=NewsFilterConfig(settings.news.lookback_days, settings.news.min_confidence),
    )
