"""Gün içi izleme (Yahoo, ~15 dk gecikmeli): SADECE uyarı ve bilgi, işlem yapmaz.

Paper işlemler akşam işinde kaydedilir: stop'a değen pozisyon stop fiyatından satılmış sayılır
(gerçekte aracı kurumda bekleyen stop emri de böyle çalışır). Böylece paper sonuçları backtest ile
tutarlı kalır; gecikmeli gün içi fiyattan satış kaydetmek sonuçları olduğundan kötü gösterirdi.

Seans boyunca her CHECK_EVERY'de bir, elde tutulan hisseler için:
  🚨 gün içi en düşük ≤ stop       → "stop'a değdi" uyarısı (hisse başına günde bir)
  ⚠️ son fiyat stop'un %NEAR_PCT içinde → "stop'a yaklaştı" uyarısı (hisse başına günde bir)
Son fiyatlar /portfoy için saklanır.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timedelta

from ..data.base import DataSource, Quote
from ..storage.db import Database

log = logging.getLogger(__name__)

SESSION_START = dtime(10, 5)
SESSION_END = dtime(18, 15)
CHECK_EVERY = timedelta(minutes=15)
NEAR_PCT = 0.02
QUOTES_KEY = "intraday_quotes"


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


class IntradayMonitor:
    def __init__(self, db: Database, broker, source: DataSource, notifier):
        self.db = db
        self.broker = broker
        self.source = source
        self.notifier = notifier

    def due(self, now: datetime) -> bool:
        if not in_session(now):
            return False
        last = self.db.get_state("intraday_last_check")
        return last is None or now - datetime.fromisoformat(last) >= CHECK_EVERY

    def check(self, now: datetime) -> list[str]:
        """Bir kontrol turu; gönderilen uyarıları döndürür."""
        self.db.set_state("intraday_last_check", now.isoformat())
        positions = self.broker.positions()
        if not positions:
            return []
        try:
            quotes = self.source.fetch_intraday(sorted(positions))
        except Exception as exc:   # veri hatası botu düşürmesin; bir sonraki turda tekrar dener
            log.warning("Gün içi veri alınamadı: %s", exc)
            return []
        today = now.date().isoformat()
        # Seans başında gecikme yüzünden önceki günün verisi dönebilir: sadece bugünün mumları geçerli.
        quotes = {s: q for s, q in quotes.items() if q.time[:10] == today}
        self.db.set_state(QUOTES_KEY, json.dumps(
            {"date": today, "checked_at": now.strftime("%H:%M"),
             "quotes": {s: q.__dict__ for s, q in quotes.items()}}))
        alerts = []
        for sym, pos in positions.items():
            q = quotes.get(sym)
            if q is None:
                continue
            if q.day_low <= pos.stop_price:
                msg = self._once(today, sym, "hit",
                                 f"🚨 {sym} stop'a değdi: gün içi en düşük {q.day_low:.2f} ≤ stop {pos.stop_price:.2f} "
                                 f"(son {q.last:.2f}, ~15 dk gecikmeli).\n"
                                 f"Paper işlem akşam stop fiyatından ({pos.stop_price:.2f}) satılmış olarak kaydedilecek.")
            elif q.last <= pos.stop_price * (1 + NEAR_PCT):
                msg = self._once(today, sym, "near",
                                 f"⚠️ {sym} stop'a yaklaştı: son {q.last:.2f}, stop {pos.stop_price:.2f} "
                                 f"(%{100 * (q.last / pos.stop_price - 1):.1f} yukarıda, ~15 dk gecikmeli).")
            else:
                msg = None
            if msg:
                self.notifier.send(msg)
                alerts.append(msg)
        return alerts

    def _once(self, today: str, sym: str, kind: str, text: str) -> str | None:
        key = f"intraday_alert:{today}:{sym}:{kind}"
        if self.db.get_state(key):
            return None
        self.db.set_state(key, "1")
        return text


def latest_quotes(db: Database, now: datetime, max_age: timedelta = timedelta(minutes=45)) -> tuple[dict[str, Quote], str]:
    """/portfoy için: bugünün ve yeterince taze gün içi fiyatları (yoksa boş)."""
    raw = db.get_state(QUOTES_KEY)
    if not raw:
        return {}, ""
    data = json.loads(raw)
    if data.get("date") != now.date().isoformat():
        return {}, ""
    checked = datetime.combine(now.date(), dtime.fromisoformat(data["checked_at"]), tzinfo=now.tzinfo)
    if now - checked > max_age:
        return {}, ""
    return {s: Quote(**q) for s, q in data["quotes"].items()}, data["checked_at"]
