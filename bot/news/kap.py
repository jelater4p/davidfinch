"""KAP sitesi kaynağı (geçici çözüm — ileride Matriks ile değiştirilecek).

UYARI:
  * KAP'ın resmi, belgelenmiş bir API'si yoktur. Burada sitenin kendi arayüzünün
    kullandığı veri adresleri kullanılır; bu, KAP'ın izin verdiği bir kullanım
    değildir ve site değişirse kırılabilir. Kullanıcı bunu bilerek seçmiştir.
  * Saygılı kullanım: günde birkaç istek, istekler arası bekleme, önbellek.
  * Sitenin kendi kodundan: veriler www.kap.org.tr'den değil, sitenin sunucu tarafının
    kullandığı kapsitebackend.mkk.com.tr'den gelir. Bildirim sorgulama sayfası
    POST .../api/disclosure/members/byCriteria  gövde: {"fromDate","toDate","memberTypes"}
    biçimindedir (tarihler GG.AA.YYYY). Tarih aralığıyla sorgu, günün tüm bildirimlerini
    getirdiği için ana sayfa akışından (list/main) daha güvenilirdir.
  * Hangi adresin çalıştığı ve yanıt alan adları geliştirme ortamında doğrulanamadı.
    ENDPOINTS sırayla denenir, çalışan hatırlanır. VPS'te `python -m bot kap-check`
    her adayı dener ve ham yanıtı gösterir; alan eşlemesi (FIELD_CANDIDATES) esnektir.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

import requests

from .base import Disclosure, NewsSource, NewsSourceError

log = logging.getLogger(__name__)

BASE_URL = "https://www.kap.org.tr"
BACKEND_URL = "https://kapsitebackend.mkk.com.tr"     # sitenin SERVER_BASE_URL değeri
DETAIL_URL = BASE_URL + "/tr/Bildirim/{id}"
MEMBER_TYPES = ["IGS", "DDK"]    # sitenin varsayılan şirket filtresi (IGS: işlem gören şirketler)


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str          # GET | POST
    url: str
    dated: bool          # True: tarih aralığıyla sorgu (POST gövdesi)

    def body(self, since: date, until: date) -> dict | None:
        if not self.dated:
            return None
        return {"fromDate": f"{since:%d.%m.%Y}", "toDate": f"{until:%d.%m.%Y}", "memberTypes": MEMBER_TYPES}


ENDPOINTS = [
    Endpoint("sorgu (backend)", "POST", BACKEND_URL + "/tr/api/disclosure/members/byCriteria", True),
    Endpoint("sorgu (site)", "POST", BASE_URL + "/tr/api/disclosure/members/byCriteria", True),
    Endpoint("ana akış (backend)", "GET", BACKEND_URL + "/tr/api/disclosure/list/main", False),
    Endpoint("ana akış (site)", "GET", BASE_URL + "/tr/api/disclosure/list/main", False),
]
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

    def __init__(self, session: requests.Session | None = None, timeout: float = 30.0,
                 endpoints: list[Endpoint] | None = None):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.endpoints = endpoints or ENDPOINTS
        self._working: Endpoint | None = None
        self._last_request = 0.0
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}

    def _request(self, ep: Endpoint, since: date, until: date) -> Any:
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        headers = dict(HEADERS, **({"Content-Type": "application/json"} if ep.method == "POST" else {}))
        r = self.session.request(ep.method, ep.url, headers=headers, json=ep.body(since, until),
                                 timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def diagnose(self, since: date, until: date) -> list[dict]:
        """Teşhis (kap-check): her adayı dener, sonucu ve ham örneği döndürür."""
        out = []
        for ep in self.endpoints:
            info: dict[str, Any] = {"endpoint": ep}
            try:
                payload = self._request(ep, since, until)
                items = _extract_list(payload)
                info.update(ok=True, payload=payload, items=items)
            except (requests.RequestException, json.JSONDecodeError) as exc:
                info.update(ok=False, error=str(exc)[:300])
            out.append(info)
        return out

    def _items(self, since: date, until: date) -> list[dict]:
        key = (since, until)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]
        candidates = [self._working] if self._working else []
        candidates += [ep for ep in self.endpoints if ep is not self._working]
        errors = []
        for ep in candidates:
            try:
                items = _extract_list(self._request(ep, since, until))
            except (requests.RequestException, json.JSONDecodeError) as exc:
                errors.append(f"{ep.name}: {str(exc)[:120]}")
                continue
            if ep.dated or items:          # tarihli sorguda boş liste geçerli bir cevaptır
                if self._working is not ep:
                    log.info("KAP: çalışan adres %s (%s)", ep.name, ep.url)
                self._working = ep
                self._cache[key] = (time.monotonic(), items)
                return items
            errors.append(f"{ep.name}: liste bulunamadı")
        raise NewsSourceError("KAP isteği başarısız — " + " | ".join(errors))

    def fetch(self, symbols: list[str], since: date, until: date) -> list[Disclosure]:
        items = self._items(since, until)
        wanted = set(symbols)
        result = [d for d in parse_items(items, self.name)
                  if d.symbol in wanted and since <= d.published_at.date() <= until]
        log.info("KAP: %d bildirim alındı, %d tanesi ilgili", len(items), len(result))
        return result
