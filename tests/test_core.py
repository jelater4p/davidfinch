from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.broker.base import OrderRequest
from bot.broker.paper import PaperBroker
from bot.config import CostConfig, RiskConfig, load_settings
from bot.data.base import Bar, normalize
from bot.data.store import PriceStore
from bot.indicators import atr, rsi, sma
from bot.market.bist import round_to_tick, tick_size
from bot.risk.sizing import size_position
from bot.storage.db import Database

RISK = RiskConfig()
NO_COST = CostConfig(commission_rate=0, bsmv_rate=0, slippage_rate=0)
COSTS = CostConfig()


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def bar(sym, day, o, h, l, c, v=1e6):
    return Bar(sym, day, o, h, l, c, v)


# ---------------------------------------------------------------- ayarlar
def test_settings_load_and_universe():
    s = load_settings()
    assert s.risk.min_cash_ratio == 0.0
    assert s.risk.max_open_positions == 10
    assert s.risk.max_position_ratio == 0.28
    u = s.universe()
    assert "THYAO" in u and len(u) == len(set(u))


def test_risk_validation_rejects_excessive_risk():
    with pytest.raises(ValueError):
        RiskConfig(risk_per_trade=0.10).validate()
    with pytest.raises(ValueError):
        RiskConfig(min_cash_ratio=0.8, max_position_ratio=0.28).validate()


# ---------------------------------------------------------------- BIST kuralları
@pytest.mark.parametrize("price,step", [(9.99, 0.01), (20, 0.02), (62.4, 0.05), (300, 0.25), (3000, 2.5)])
def test_tick_size(price, step):
    assert tick_size(price) == step


def test_round_to_tick_is_conservative():
    assert round_to_tick(62.41, "BUY") == 62.45
    assert round_to_tick(62.44, "SELL") == 62.40
    assert round_to_tick(62.40, "BUY") == 62.40


# ---------------------------------------------------------------- göstergeler
def test_indicators_basic():
    close = pd.Series(np.arange(1, 31, dtype=float))
    assert sma(close, 5).iloc[-1] == pytest.approx(28.0)
    assert rsi(close, 14).iloc[-1] == 100.0          # hiç düşüş yok
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close})
    assert atr(df, 14).iloc[-1] == pytest.approx(2.0, rel=1e-6)


def test_normalize_drops_bad_rows():
    df = pd.DataFrame(
        {"Open": [10, 0, 11], "High": [11, 1, 12], "Low": [9, 0, 10], "Close": [10, 1, 11], "Volume": [1, 1, 1]},
        index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
    )
    out = normalize(df)
    assert list(out.index.day) == [2, 6]


# ---------------------------------------------------------------- lot hesabı
def test_sizing_risk_rule_binds():
    # Doküman örneği: 100k, %1 risk, fiyat 62, ATR 2, çarpan 2.5 -> stop 5 TL -> 200 lot
    r = size_position(equity=100_000, cash=100_000, open_positions=0, entry_price=62, atr=2,
                      atr_mult=2.5, risk=RISK, costs=NO_COST)
    assert r.approved and r.qty == 200 and r.stop_price == 57
    assert r.binding_rule == "işlem başı risk"


def test_sizing_position_cap_binds_with_high_risk():
    # %5 risk -> 1000 lot = 62k TL; tek hisse sınırı %28 = 28k -> 451 lot
    risk5 = RiskConfig(risk_per_trade=0.05)
    r = size_position(equity=100_000, cash=100_000, open_positions=0, entry_price=62, atr=2,
                      atr_mult=2.5, risk=risk5, costs=NO_COST)
    assert r.qty == 451 and r.binding_rule == "tek hisse sınırı"


def test_sizing_min_cash_binds():
    # Nakit 35k, min nakit 30k -> en fazla 5k harcanabilir -> 80 lot
    r = size_position(equity=100_000, cash=35_000, open_positions=3, entry_price=62, atr=2,
                      atr_mult=2.5, risk=RISK, costs=NO_COST)
    assert r.qty == 80 and r.binding_rule == "minimum nakit kuralı"


def test_sizing_rejects_when_full():
    r = size_position(equity=100_000, cash=100_000, open_positions=6, entry_price=62, atr=2,
                      atr_mult=2.5, risk=RISK, costs=NO_COST)
    assert not r.approved


def test_sizing_costs_reduce_qty():
    r = size_position(equity=100_000, cash=100_000, open_positions=0, entry_price=62, atr=2,
                      atr_mult=2.5, risk=RISK, costs=COSTS)
    assert 0 < r.qty < 200 and r.risk_amount <= 1_000


# ---------------------------------------------------------------- paper broker
def buy(sym="ASELS", qty=200, stop=57.0, d=date(2026, 3, 2)):
    return OrderRequest(sym, "BUY", qty, d, stop_price=stop, reason={"neden": "test"})


def test_buy_fills_at_next_open_with_costs(db):
    b = PaperBroker(db, RISK, COSTS)
    b.submit(buy())
    # Karar günü işlenmez, sonraki gün açılıştan dolar
    assert b.process_day(date(2026, 3, 2), {"ASELS": bar("ASELS", date(2026, 3, 2), 61, 63, 60, 62)}) == []
    fills = b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 62.3, 64, 61, 63)})
    assert len(fills) == 1
    f = fills[0]
    assert f.price == round_to_tick(62.3 * 1.001, "BUY") == 62.40
    assert f.fees == pytest.approx(200 * 62.40 * 0.002 * 1.05)
    assert b.cash() == pytest.approx(100_000 - 200 * 62.40 - f.fees)
    assert b.positions()["ASELS"].qty == 200


def test_submit_is_idempotent(db):
    b = PaperBroker(db, RISK, COSTS)
    assert b.submit(buy()) == b.submit(buy())
    assert len(b.pending_orders()) == 1


def test_process_day_is_idempotent(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy())
    bars = {"ASELS": bar("ASELS", date(2026, 3, 3), 62, 64, 61, 63)}
    assert len(b.process_day(date(2026, 3, 3), bars)) == 1
    assert b.process_day(date(2026, 3, 3), bars) == []
    assert b.positions()["ASELS"].qty == 200


def test_stop_out_and_gap(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy())
    b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 62, 64, 61, 63)})
    # Gap aşağı: açılış 55 < stop 57 -> 55'ten satılır
    fills = b.process_day(date(2026, 3, 4), {"ASELS": bar("ASELS", date(2026, 3, 4), 55, 56, 54, 55.5)})
    assert fills[0].order_type == "STOP" and fills[0].price == 55
    assert "ASELS" not in b.positions()
    t = db.query_one("SELECT * FROM trades")
    assert t["exit_reason"] == "stop" and t["pnl"] == pytest.approx((55 - 62) * 200)


def test_intraday_stop_fills_at_stop(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy())
    b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 62, 64, 61, 63)})
    fills = b.process_day(date(2026, 3, 4), {"ASELS": bar("ASELS", date(2026, 3, 4), 60, 60.5, 56, 58)})
    assert fills[0].price == 57


def test_buy_rejected_if_open_below_stop(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy())
    b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 56, 58, 55, 57)})
    assert db.query_one("SELECT status FROM orders")["status"] == "REJECTED"
    assert b.cash() == 100_000


def test_fill_trims_to_position_cap(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy(qty=1000, stop=50))   # 1000 x 62 = 62k > 28k sınırı
    fills = b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 62, 64, 61, 63)})
    assert fills[0].qty == 451 and "kırpıldı" in fills[0].note


def test_min_cash_rule_enforced_across_positions(db):
    b = PaperBroker(db, RISK, NO_COST)
    d0, d1 = date(2026, 3, 2), date(2026, 3, 3)
    for sym in ["AAA", "BBB", "CCC"]:
        b.submit(OrderRequest(sym, "BUY", 2800, d0, stop_price=5, reason={}))
    bars = {s: bar(s, d1, 10, 11, 9.5, 10) for s in ["AAA", "BBB", "CCC"]}
    fills = b.process_day(d1, bars)
    # 28k + 28k sonra kalan 44k'nın 14k'sı harcanabilir -> nakit tam %30'da kalır
    assert [f.qty for f in fills] == [2800, 2800, 1400]
    assert b.cash() == pytest.approx(30_000)


def test_sell_order_and_trailing_stop(db):
    b = PaperBroker(db, RISK, NO_COST)
    b.submit(buy())
    b.process_day(date(2026, 3, 3), {"ASELS": bar("ASELS", date(2026, 3, 3), 62, 64, 61, 63)})
    b.update_stop("ASELS", 60)
    b.update_stop("ASELS", 58)            # aşağı çekilemez
    assert b.positions()["ASELS"].stop_price == 60
    b.submit(OrderRequest("ASELS", "SELL", 200, date(2026, 3, 3), reason={"exit_reason": "trend bozuldu"}))
    fills = b.process_day(date(2026, 3, 4), {"ASELS": bar("ASELS", date(2026, 3, 4), 66, 67, 65, 66)})
    assert fills[0].side == "SELL" and fills[0].price == 66
    t = db.query_one("SELECT * FROM trades")
    assert t["exit_reason"] == "trend bozuldu" and t["pnl"] == pytest.approx(800)


def test_stop_required_for_buy(db):
    b = PaperBroker(db, RISK, NO_COST)
    with pytest.raises(ValueError):
        b.submit(OrderRequest("ASELS", "BUY", 10, date(2026, 3, 2)))


def test_price_store_roundtrip(db):
    store = PriceStore(db)
    df = pd.DataFrame(
        {"open": [10.0, 11.0], "high": [11.0, 12.0], "low": [9.0, 10.0], "close": [10.5, 11.5], "volume": [1e5, 2e5]},
        index=pd.to_datetime(["2026-03-02", "2026-03-03"]),
    )
    assert store.replace_history("ASELS", df) == 2
    assert store.replace_history("ASELS", df) == 2      # tekrar yazınca çoğalmaz
    assert len(store.history("ASELS")) == 2
    assert store.last_close("ASELS", date(2026, 3, 10)) == 11.5
    assert store.bar("ASELS", date(2026, 3, 3)).open == 11.0
