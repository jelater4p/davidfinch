import json

import pytest
from datetime import date, datetime
from types import SimpleNamespace

from bot.news import ClaudeAnalyzer, Disclosure, KapNewsFilter, NewsStore, NewsSourceError
from bot.news.analyzer import Analysis, NewsAnalyzer
from bot.news.base import NewsSource
from bot.news.filter import NewsFilterConfig
from bot.news.kap import _extract_list, parse_items
from bot.storage.db import Database
from bot.strategy.base import EntrySignal

DAY = date(2026, 11, 20)


def disc(sym, did, title, when=datetime(2026, 11, 19, 18, 0)):
    return Disclosure("fake", did, sym, when, title, subject="Özel Durum Açıklaması")


class FakeSource(NewsSource):
    name = "fake"

    def __init__(self, items, fail=False):
        self.items, self.fail, self.calls = items, fail, 0

    def fetch(self, symbols, since, until):
        self.calls += 1
        if self.fail:
            raise NewsSourceError("bağlantı yok")
        return [d for d in self.items if d.symbol in symbols]


class KeywordAnalyzer(NewsAnalyzer):
    def __init__(self):
        self.calls = 0

    def analyze(self, d):
        self.calls += 1
        if "konkordato" in d.title.lower():
            return Analysis(True, "konkordato_veya_iflas", "Konkordato başvurusu", 0.95, "fake")
        if "dava" in d.title.lower():
            return Analysis(True, "onemli_dava", "Belirsiz dava", 0.4, "fake")   # düşük güven
        return Analysis(False, "yok", "Rutin bildirim", 0.9, "fake")


def sig(sym):
    return EntrySignal(sym, DAY, 10.0, 0.5, 1.0)


def make_filter(items, fail=False):
    db = Database(":memory:")
    src, an = FakeSource(items, fail), KeywordAnalyzer()
    return KapNewsFilter(src, an, NewsStore(db), NewsFilterConfig(lookback_days=7, min_confidence=0.6)), src, an


def test_filter_blocks_only_serious_negative():
    f, _, _ = make_filter([disc("AAA", "1", "Konkordato başvurusu hk."),
                           disc("BBB", "2", "Genel kurul toplantısı"),
                           disc("CCC", "3", "Dava açılması hakkında")])
    ok, info = f(sig("AAA"))
    assert not ok and "konkordato" in info["summary"]
    ok, info = f(sig("BBB"))
    assert ok and info["status"] == "temiz"
    ok, _ = f(sig("CCC"))          # ciddi ama güven 0.4 < 0.6 → engellemez
    assert ok


def test_old_disclosures_outside_window_ignored():
    f, _, _ = make_filter([disc("AAA", "1", "Konkordato", when=datetime(2026, 10, 1, 10, 0))])
    assert f(sig("AAA"))[0]


def test_source_error_does_not_block_and_warns():
    f, _, _ = make_filter([], fail=True)
    ok, info = f(sig("AAA"))
    assert ok and "yapılamadı" in info["status"] and f.warnings


def test_each_disclosure_analyzed_once_and_refresh_cached_per_day():
    f, src, an = make_filter([disc("AAA", "1", "Genel kurul")])
    f(sig("AAA"))
    f(sig("AAA"))
    assert src.calls == 1 and an.calls == 1


def test_held_alerts():
    f, _, _ = make_filter([disc("AAA", "1", "Konkordato başvurusu")])
    alerts = f.held_alerts(DAY, ["AAA", "BBB"])
    assert len(alerts) == 1 and alerts[0].startswith("⚠️ AAA")


# ---------------------------------------------------------------- KAP ayrıştırma
def test_kap_parse_flexible_fields():
    payload = {"data": [
        {"disclosureIndex": 123, "stockCodes": "THYAO, PGSUS", "publishDate": "20.11.2026 18:05:00",
         "title": "Türk Hava Yolları", "subject": "Özel Durum Açıklaması", "summary": "Uçak alımı"},
        {"basic": {"disclosureIndex": 124, "stockCode": "ASELS", "publishDate": "20.11.26 17:00",
                   "subject": "Finansal Rapor"}},
        {"title": "kimliksiz kayıt"},
    ]}
    items = parse_items(_extract_list(payload))
    assert [(d.symbol, d.disclosure_id) for d in items] == [("THYAO", "123"), ("PGSUS", "123"), ("ASELS", "124")]
    assert items[0].published_at == datetime(2026, 11, 20, 18, 5)
    assert items[0].url.endswith("/Bildirim/123")


# ---------------------------------------------------------------- Claude analizci (sahte istemci)
class FakeClient:
    def __init__(self, response=None, exc=None):
        self.kwargs = None
        self._response, self._exc = response, exc
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self._exc:
            raise self._exc
        return self._response


def _resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, model="claude-opus-5-5",
                           content=[SimpleNamespace(type="text", text=text)])


def test_claude_analyzer_parses_structured_output():
    body = json.dumps({"ciddi_olumsuz": True, "kategori": "konkordato_veya_iflas",
                       "gerekce": "Konkordato başvurusu yapıldı.", "guven": 0.9})
    client = FakeClient(_resp(body))
    a = ClaudeAnalyzer(client=client).analyze(disc("AAA", "1", "Konkordato"))
    assert a.severe is True and a.category == "konkordato_veya_iflas" and a.confidence == 0.9
    kw = client.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert "AAA" in kw["messages"][0]["content"]


def test_claude_analyzer_refusal_and_bad_json_are_unknown():
    assert ClaudeAnalyzer(client=FakeClient(_resp("", stop="refusal"))).analyze(disc("A", "1", "x")).severe is None
    assert ClaudeAnalyzer(client=FakeClient(_resp("bozuk"))).analyze(disc("A", "1", "x")).severe is None


def test_claude_analyzer_connection_error_is_unknown():
    import anthropic
    import httpx2
    exc = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    a = ClaudeAnalyzer(client=FakeClient(exc=exc)).analyze(disc("A", "1", "x"))
    assert a.severe is None and "APIConnectionError" in a.reason


def test_unknown_analysis_is_retried_next_refresh():
    db = Database(":memory:")
    store = NewsStore(db)
    d = disc("AAA", "1", "x")
    store.save([d])
    store.save_analysis(d, Analysis(None, "yok", "hata", 0.0, "m"))
    assert [x.key for x in store.unanalyzed()] == [d.key]


# ---------------------------------------------------------------- KAP adres yedekleme
class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._data


class _KapSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self.routes.get((method, url), _Resp(405))


def test_kap_falls_back_and_remembers_working_endpoint(monkeypatch):
    from bot.news import kap
    monkeypatch.setattr(kap, "MIN_SECONDS_BETWEEN_REQUESTS", 0)
    item = {"disclosureIndex": 7, "stockCodes": "THYAO", "publishDate": "20.11.2026 18:05:00",
            "subject": "Özel Durum Açıklaması", "title": "THY"}
    good = kap.ENDPOINTS[2]   # ana akış (backend) çalışıyor, sorgular 405 veriyor
    sess = _KapSession({(good.method, good.url): _Resp(200, {"data": [item]})})
    src = kap.KapWebSource(session=sess)
    got = src.fetch(["THYAO"], date(2026, 11, 18), date(2026, 11, 20))
    assert [d.symbol for d in got] == ["THYAO"]
    assert src._working == good and len(sess.calls) == 3
    # İkinci çağrıda (farklı tarih) önce çalışan adres denenir
    src.fetch(["THYAO"], date(2026, 11, 19), date(2026, 11, 20))
    assert sess.calls[3][1] == good.url


def test_kap_dated_query_sends_date_body_and_accepts_empty(monkeypatch):
    from bot.news import kap
    monkeypatch.setattr(kap, "MIN_SECONDS_BETWEEN_REQUESTS", 0)
    ep = kap.ENDPOINTS[0]
    sess = _KapSession({(ep.method, ep.url): _Resp(200, [])})
    assert kap.KapWebSource(session=sess).fetch(["THYAO"], date(2026, 11, 18), date(2026, 11, 20)) == []
    assert sess.calls[0][2] == {"fromDate": "18.11.2026", "toDate": "20.11.2026", "memberTypes": ["IGS", "DDK"]}


def test_kap_all_endpoints_fail_raises(monkeypatch):
    from bot.news import kap
    monkeypatch.setattr(kap, "MIN_SECONDS_BETWEEN_REQUESTS", 0)
    with pytest.raises(NewsSourceError):
        kap.KapWebSource(session=_KapSession({})).fetch(["X"], date(2026, 11, 18), date(2026, 11, 20))
