"""KAP sitesi kaynağı (geçici çözüm — ileride Matriks ile değiştirilecek).

UYARI:
  * KAP'ın resmi, belgelenmiş bir API'si yoktur. Burada sitenin kendi arayüzünün
    kullandığı veri adresleri kullanılır; bu, KAP'ın izin verdiği bir kullanım
    değildir ve site değişirse kırılabilir. Kullanıcı bunu bilerek seçmiştir.
  * Saygılı kullanım: günde birkaç istek, istekler arası bekleme, önbellek.
  * Yanıt alanlarının adları geliştirme ortamında doğrulanamadı. Alan eşlemesi
    esnek tutuldu (FIELD_CANDIDATES). VPS'te `python -m bot kap-check` ham yanıtı
    gösterir; eşleme gerekirse buradan güncellenir.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from typing import Any, Iterable

import requests

from .base import Disclosure, NewsSource, NewsSourceError

log = logging.getLogger(__name__)

BASE_URL = "https://www.kap.org.tr"
LIST_PATH = "/tr/api/disclosure/list/main"     # sitenin ana sayfa bildirim akışı
DETAIL_URL = BASE_URL + "/tr/Bildirim/{id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; davidfinch-paper-bot)",
    "Accept": "application/json",
    "Accept-Language": "tr",
}
MIN_SECONDS_BETWEEN_REQUESTS = 5.0
LIST_CACHE_SECONDS = 600     # aynı çalıştırmada birden çok hisse sorulsa da liste bir kez çekilir

# Her alan için olası anahtar adları (ilk bulunan kullanılır).
FIELD_CANDIDATES: dict[str, list[str]] = {
    "id": ["disclosureIndex", "disclosureId", "index", "id"],
    "symbols": ["stockCodes", "stockCode", "relatedStocks", "companyCodes"],
    "published": ["publishDate", "publishedDate", "publishDateTime", "time", "date"],
    "title": ["title", "kapTitle", "companyTitle"],
    "subject": ["subject", "disclosureSubject", "subjectName"],
    "summary": ["summary", "disclosureSummary", "shortText"],
}
DATE_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%y %H:%M"]


def _pick(item: dict, field: str) -> Any:
    for key in FIELD_CANDIDATES[field]:
        if key in item and item[key] not in (None, ""):
            return item[key]
        # iç içe yapı: {"basic": {...}, "detail": {...}}
        for nested in ("basic", "disclosureBasic", "detail"):
            sub = item.get(nested)
            if isinstance(sub, dict) and sub.get(key) not in (None, ""):
                return sub[key]
    return None


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):   # epoch ms
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value)
    if not isinstance(value, str):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _symbols(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip().upper() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [s.strip().upper() for s in value.replace(";", ",").split(",") if s.strip()]
    return []


def parse_items(items: Iterable[dict], source: str = "kap_web") -> list[Disclosure]:
    out = []
    for item in items:
        did = _pick(item, "id")
        published = _parse_date(_pick(item, "published"))
        if did is None or published is None:
            continue
        for sym in _symbols(_pick(item, "symbols")):
            out.append(Disclosure(
                source=source,
                disclosure_id=str(did),
                symbol=sym,
                published_at=published,
                title=str(_pick(item, "title") or ""),
                subject=str(_pick(item, "subject") or ""),
                summary=str(_pick(item, "summary") or ""),
                url=DETAIL_URL.format(id=did),
            ))
    return out


def _extract_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "disclosures", "content", "result"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


class KapWebSource(NewsSource):
    name = "kap_web"

    def __init__(self, session: requests.Session | None = None, timeout: float = 30.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self._last_request = 0.0
        self._list_cache: tuple[float, list[dict]] | None = None

    def _get_json(self, path: str) -> Any:
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            r = self.session.get(BASE_URL + path, headers=HEADERS, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise NewsSourceError(f"KAP isteği başarısız: {exc}") from exc

    def raw_sample(self) -> Any:
        """Teşhis: ham yanıtı döndürür (kap-check komutu için)."""
        return self._get_json(LIST_PATH)

    def _list(self) -> list[dict]:
        if self._list_cache and time.monotonic() - self._list_cache[0] < LIST_CACHE_SECONDS:
            return self._list_cache[1]
        items = _extract_list(self._get_json(LIST_PATH))
        self._list_cache = (time.monotonic(), items)
        return items

    def fetch(self, symbols: list[str], since: date, until: date) -> list[Disclosure]:
        items = self._list()
        if not items:
            raise NewsSourceError("KAP yanıtında bildirim listesi bulunamadı (format değişmiş olabilir)")
        wanted = set(symbols)
        result = [d for d in parse_items(items, self.name)
                  if d.symbol in wanted and since <= d.published_at.date() <= until]
        log.info("KAP: %d bildirim alındı, %d tanesi ilgili", len(items), len(result))
        return result
