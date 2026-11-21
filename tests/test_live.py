from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bot.broker.base import OrderRequest
from bot.config import MomentumConfig, Settings
from bot.data.base import DataSource
from bot.data.store import PriceStore, detect_adjustment
from bot.live.commands import CommandHandler
from bot.live.daemon import Daemon
from bot.live.runner import LiveRunner
from bot.notify.telegram import ConsoleNotifier, Telegram, chunk
from bot.storage.db import Database
from test_strategy import make_series

TZ = ZoneInfo("Europe/Istanbul")
M = MomentumConfig(lookback=60, skip_days=5, trend_sma=50, hold_rank=10)


class FakeSource(DataSource):
    def __init__(self, data):
        self.data = data

    def fetch_daily(self, symbols, start, end):
        out = {}
        for s in symbols:
            if s in self.data:
                df = self.data[s]
                out[s] = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        return out


@pytest.fixture
def setup(tmp_path):
    uni = tmp_path / "u.txt"
    syms = [f"S{i}" for i in range(12)]
    uni.write_text("\n".join(syms))
    data = {s: make_series(n=400, seed=i, drift=0.002 + 0.0004 * i, pullbacks=False) for i, s in enumerate(syms)}
    data["XU100"] = make_series(n=400, seed=99, pullbacks=False)
    s = Settings(universe_file=uni, momentum=M, history_days=800)
    db = Database(":memory:")
    runner = LiveRunner(s, db, FakeSource(data))
    days = [d.date() for d in data["XU100"].index]
    return runner, data, days, db


def test_evening_runs_once_and_reports(setup):
    runner, data, days, db = setup
    day = days[300]
    res = runner.evening(day)
    assert res.status == "ok" and "gün sonu" in res.message
    assert any("Portföy" in s for s in res.sections)
    assert runner.evening(day).status == "already_done"


def test_evening_no_data_on_holiday(setup):
    runner, _, days, _ = setup
    assert runner.evening(date(2030, 1, 1)).status == "no_data"


def test_evening_catches_up_missed_days(setup):
    runner, _, days, db = setup
    runner.evening(days[300])
    res = runner.evening(days[305])
    assert res.status == "ok"
    assert any("Kaçırılan 4 işlem günü" in s for s in res.sections)
    assert runner.broker.last_processed_date() == days[305]


def test_pause_blocks_new_entries_only(setup):
    runner, _, days, db = setup
    runner.set_paused(True)
    first_rebalance = next(d for d in days[250:] if d.day <= 3 and d.month != days[249].month)
    runner.evening(first_rebalance)
    assert runner.broker.pending_orders() == []
    runner.set_paused(False)
    assert not runner.paused()


def test_detect_adjustment():
    old = make_series(n=30, pullbacks=False)
    assert detect_adjustment(old, old) is None
    assert detect_adjustment(old, old * 0.98) is None          # temettü düzeltmesi: bölünme değil
    assert detect_adjustment(old, old * 0.5) == pytest.approx(0.5)


def test_split_adjusts_position_before_stop_check(setup):
    runner, data, days, db = setup
    d0 = days[300]
    # Elle bir pozisyon aç: S0, 100 lot (ilk akşam işinde, strateji alımlarından önce dolar)
    runner.broker.submit(OrderRequest("S0", "BUY", 100, d0, stop_price=1.0, reason={}))
    runner.evening(days[301])
    pos = runner.broker.positions()["S0"]
    # %100 bedelsiz: geçmiş fiyatlar yarıya iner
    data["S0"] = data["S0"] * 0.5
    data["S0"]["volume"] = data["S0"]["volume"] * 4
    res = runner.evening(days[302])
    new = runner.broker.positions()["S0"]
    assert new.qty == pos.qty * 2
    assert new.avg_cost == pytest.approx(pos.avg_cost * 0.5)
    assert any("bedelsiz" in s for s in res.sections)


# ---------------------------------------------------------------- daemon zamanlaması
class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def test_daemon_schedule(setup):
    runner, _, days, db = setup
    day = days[300]
    clock = Clock(datetime(day.year, day.month, day.day, 9, 50, tzinfo=TZ))
    note = ConsoleNotifier()
    d = Daemon(runner, note, now=clock)
    d.tick()
    assert len(note.sent) == 1 and "Günaydın" in note.sent[0]
    d.tick()
    assert len(note.sent) == 1                       # sabah mesajı günde bir kez
    clock.dt = clock.dt.replace(hour=19, minute=5)
    d.tick()
    assert len(note.sent) == 2 and "gün sonu" in note.sent[1]
    d.tick()
    assert len(note.sent) == 2                       # akşam işi günde bir kez


def test_daemon_retries_when_data_late_then_gives_up(setup):
    runner, _, days, db = setup
    clock = Clock(datetime(2030, 1, 2, 19, 0, tzinfo=TZ))   # veri yok
    note = ConsoleNotifier()
    d = Daemon(runner, note, now=clock)
    d.tick()
    assert note.sent == []                                # veri yok, henüz vazgeçmedi
    clock.dt = clock.dt.replace(hour=19, minute=10)
    d.tick()                                              # 30 dk dolmadı, denemez
    clock.dt = clock.dt.replace(hour=21, minute=45)
    d.tick()                                              # son deneme → vazgeç ve bildir
    assert "işlem yapılmadı" in note.sent[-1]
    assert db.get_state("evening_done") == "2030-01-02"


def test_daemon_skips_weekend(setup):
    runner, _, _, _ = setup
    note = ConsoleNotifier()
    Daemon(runner, note, now=Clock(datetime(2026, 11, 21, 19, 30, tzinfo=TZ))).tick()   # cumartesi
    assert note.sent == []


def test_commands_basic(setup):
    runner, _, days, _ = setup
    h = CommandHandler(runner, today=lambda: days[300])
    assert "Açık pozisyon yok" in h.handle("/portfoy")
    assert "Kullanım" in h.handle("/neden")
    assert "durduruldu" in h.handle("/durdur") and runner.paused()
    assert "açıldı" in h.handle("/devam@davidfinch_bot") and not runner.paused()
    assert "Bilinmeyen" in h.handle("/foo")


# ---------------------------------------------------------------- telegram
def test_chunk_splits_long_messages():
    text = "\n".join("x" * 100 for _ in range(100))
    parts = chunk(text, size=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts) == text


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, updates):
        self.updates, self.posts = updates, []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("getUpdates"):
            return FakeResp({"ok": True, "result": self.updates})
        return FakeResp({"ok": True, "result": {}})


def test_telegram_ignores_unauthorized_chats():
    sess = FakeSession([
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/portfoy"}},
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/durdur"}},   # yabancı
    ])
    tg = Telegram("TOKEN", "111", session=sess)
    ups = tg.poll()
    assert [u.text for u in ups] == ["/portfoy"]
    assert tg.offset == 3
    tg.send("merhaba")
    assert sess.posts[-1][1]["chat_id"] == "111"


def test_telegram_requires_config():
    with pytest.raises(ValueError):
        Telegram("", "")
