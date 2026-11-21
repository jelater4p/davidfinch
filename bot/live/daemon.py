"""Sürekli çalışan süreç: Telegram komutlarını dinler, günlük işleri zamanında çalıştırır.

Zamanlama (Europe/Istanbul, hafta içi):
  09:45  Sabah mesajı: "çalışıyorum" + bugün açılışta gerçekleşecek emirler
  19:00  Akşam işi (kapanış verisi gelmemişse 22:00'a kadar 30 dakikada bir tekrar dener)

Son çalışma tarihleri veritabanında tutulur; süreç yeniden başlasa bile aynı iş iki kez yapılmaz.
"""
from __future__ import annotations

import logging
import os
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
BACKUP_KEEP = 14


class Daemon:
    def __init__(self, runner: LiveRunner, notifier, tz: str = "Europe/Istanbul", now=None,
                 evening_offset_min: int = 0, heartbeat_file: str | None = None, backup_dir: str | None = None):
        self.runner = runner
        # Aynı VPS'teki birden çok bot Yahoo'ya aynı anda gitmesin: akşam işi birkaç dakika kaydırılabilir.
        self.evening_at = (datetime.combine(datetime.today(), EVENING_AT)
                           + timedelta(minutes=evening_offset_min)).time()
        self.heartbeat_file = heartbeat_file
        self.backup_dir = backup_dir
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
        if now.time() >= MORNING_AT and now.time() < self.evening_at and self._state("morning_done") != today:
            self.db.set_state("morning_done", today)
            self.notifier.send(f"☀️ Günaydın, bot çalışıyor ({now:%d.%m.%Y %H:%M}).\n"
                               + self.runner.pending_preview())
        if self.evening_at <= now.time() <= EVENING_GIVE_UP and self._state("evening_done") != today:
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
            self.backup(now)

    # ------------------------------------------------------------------ bakım
    def backup(self, now: datetime) -> str | None:
        """Akşam işinden sonra veritabanının tutarlı bir kopyasını alır; son BACKUP_KEEP kopya saklanır."""
        if not self.backup_dir:
            return None
        import sqlite3
        os.makedirs(self.backup_dir, exist_ok=True)
        path = os.path.join(self.backup_dir, f"davidfinch-{now:%Y%m%d}.db")
        try:
            dst = sqlite3.connect(path)
            with dst:
                self.db.conn.backup(dst)
            dst.close()
            files = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("davidfinch-"))
            for old in files[:-BACKUP_KEEP]:
                os.remove(os.path.join(self.backup_dir, old))
            return path
        except Exception:
            log.exception("Yedekleme başarısız")
            self.notifier.send("⚠️ Günlük veritabanı yedeği alınamadı (loglara bakın).")
            return None

    def heartbeat(self) -> None:
        """Docker sağlık kontrolü bu dosyanın güncelliğine bakar."""
        if self.heartbeat_file:
            with open(self.heartbeat_file, "w") as fh:
                fh.write(self._now().isoformat())

    # ------------------------------------------------------------------ döngü
    def handle_updates(self, poll_timeout: int = 20) -> None:
        for upd in self.notifier.poll(timeout=poll_timeout):
            self.notifier.send(self.commands.handle(upd.text))

    def run_forever(self) -> None:
        self.notifier.send("🤖 davidfinch paper bot başladı. /yardim")
        while True:
            try:
                self.heartbeat()
                self.tick()
                self.handle_updates()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Döngü hatası")
                time.sleep(10)
