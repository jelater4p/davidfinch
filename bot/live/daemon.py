"""Sürekli çalışan süreç: Telegram komutlarını dinler, günlük işleri zamanında çalıştırır.

Zamanlama (Europe/Istanbul, hafta içi):
  09:45  Sabah mesajı: "çalışıyorum" + bugün açılışta gerçekleşecek emirler
  19:00  Akşam işi (kapanış verisi gelmemişse 22:00'a kadar 30 dakikada bir tekrar dener)

Son çalışma tarihleri veritabanında tutulur; süreç yeniden başlasa bile aynı iş iki kez yapılmaz.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .commands import CommandHandler
from .runner import LiveRunner

log = logging.getLogger(__name__)

MORNING_AT = dtime(9, 45)
EVENING_AT = dtime(19, 0)
EVENING_GIVE_UP = dtime(22, 0)
EVENING_RETRY = timedelta(minutes=30)


class Daemon:
    def __init__(self, runner: LiveRunner, notifier, tz: str = "Europe/Istanbul", now=None):
        self.runner = runner
        self.notifier = notifier
        self.tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self.tz))
        self.commands = CommandHandler(runner, today=lambda: self._now().date())
        self.db = runner.db

    # ------------------------------------------------------------------ zamanlama
    def _state(self, key: str) -> str | None:
        return self.db.get_state(key)

    def tick(self) -> None:
        """Zamanı gelen işleri çalıştırır. Döngüde birkaç saniyede bir çağrılır."""
        now = self._now()
        today = now.date().isoformat()
        if now.weekday() >= 5:
            return
        if now.time() >= MORNING_AT and now.time() < EVENING_AT and self._state("morning_done") != today:
            self.db.set_state("morning_done", today)
            self.notifier.send(f"☀️ Günaydın, bot çalışıyor ({now:%d.%m.%Y %H:%M}).\n"
                               + self.runner.pending_preview())
        if EVENING_AT <= now.time() <= EVENING_GIVE_UP and self._state("evening_done") != today:
            last_try = self._state("evening_last_try")
            if last_try and now - datetime.fromisoformat(last_try) < EVENING_RETRY:
                return
            self.db.set_state("evening_last_try", now.isoformat())
            self._run_evening(now)

    def _run_evening(self, now: datetime) -> None:
        try:
            res = self.runner.evening(now.date())
        except Exception:
            log.exception("Akşam işi hata verdi")
            self.notifier.send("❌ Akşam işi hata verdi:\n" + traceback.format_exc()[-1500:])
            return
        if res.status == "no_data":
            if (now + EVENING_RETRY).time() > EVENING_GIVE_UP:
                self.db.set_state("evening_done", now.date().isoformat())
                self.notifier.send(f"ℹ️ {res.message} Bugün işlem yapılmadı.")
            return
        self.db.set_state("evening_done", now.date().isoformat())
        if res.status == "ok":
            self.notifier.send(res.message + "\n\n" + "\n\n".join(res.sections))

    # ------------------------------------------------------------------ döngü
    def handle_updates(self, poll_timeout: int = 20) -> None:
        for upd in self.notifier.poll(timeout=poll_timeout):
            self.notifier.send(self.commands.handle(upd.text))

    def run_forever(self) -> None:
        self.notifier.send("🤖 davidfinch paper bot başladı. /yardim")
        while True:
            try:
                self.tick()
                self.handle_updates()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Döngü hatası")
                time.sleep(10)
