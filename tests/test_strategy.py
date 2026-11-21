from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest.metrics import format_report, max_drawdown
from bot.backtest.runner import run_backtest, with_params
from bot.config import MomentumConfig, Settings, StrategyConfig
from bot.strategy import MomentumStrategy, PullbackStrategy
from bot.strategy.card import render_card
from bot.strategy.pullback import compute_features, entry_signal, exit_reason

P = StrategyConfig(trend_sma=50, rsi_entry=40, atr_stop_mult=2.5, volume_mult=1.2)


def make_series(n=300, seed=1, drift=0.004, pullbacks=True):
    """Yükselen trend + dönemsel 5 günlük geri çekilmeler + dönüş günlerinde hacim artışı."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.005, n)
    vol = np.full(n, 1e6)
    if pullbacks:
        for start in range(80, n - 10, 40):
            rets[start:start + 5] = -0.015
            rets[start + 5] = 0.03
            vol[start + 5] = 2e6
    close = 100 * np.exp(np.cumsum(rets))
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({
        "open": close * (1 - 0.002), "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": vol,
    }, index=idx)


def test_entry_signal_fires_on_pullback_bounce():
    feat = compute_features(make_series(), P)
    hits = [ts for ts, row in feat.iterrows() if entry_signal("X", ts.date(), row.to_dict(), P)]
    assert hits, "geri çekilme + dönüş gününde sinyal bekleniyordu"
    row = feat.loc[hits[0]].to_dict()
    sig = entry_signal("X", hits[0].date(), row, P)
    assert row["close"] > row["sma"] and row["rsi_min3"] < 40
    assert len(sig.reasons) == 4


def test_no_signal_in_downtrend():
    df = make_series(drift=-0.003)
    feat = compute_features(df, P)
    assert not any(entry_signal("X", ts.date(), r.to_dict(), P) for ts, r in feat.iterrows())


def test_exit_reason_when_below_sma():
    assert exit_reason({"close": 9.0, "sma": 10.0}, P).startswith("Trend bozuldu")
    assert exit_reason({"close": 11.0, "sma": 10.0}, P) is None
    assert exit_reason({"close": 11.0, "sma": float("nan")}, P) is None


def test_backtest_end_to_end_produces_trades_and_cards():
    data = {f"S{i}": make_series(seed=i) for i in range(4)}
    settings = Settings(active_strategy="pullback", sizing="risk", pullback=P)
    res = run_backtest(data, settings, PullbackStrategy(P), date(2024, 4, 1), date(2025, 2, 1))
    assert res.metrics["trades"] > 0
    assert not res.equity.empty
    # Nakit kuralı hiçbir gün ihlal edilmemeli: pozisyon değeri <= %70 (+ fiyat hareketi payı)
    assert res.metrics["exposure"] <= 0.70
    buys = res.orders[(res.orders["side"] == "BUY") & (res.orders["status"] == "FILLED")]
    import json
    text = render_card(json.loads(buys.iloc[0]["reason"]))
    assert text.startswith("AL:") and "Stop" in text and "Lot hesabı" in text
    assert "Backtest" in format_report(res.metrics, "Backtest")


def test_backtest_is_deterministic():
    data = {f"S{i}": make_series(seed=i) for i in range(3)}
    s = Settings(pullback=P, sizing="risk")
    a = run_backtest(data, s, PullbackStrategy(P), date(2024, 4, 1), date(2025, 2, 1)).metrics
    b = run_backtest(data, s, PullbackStrategy(P), date(2024, 4, 1), date(2025, 2, 1)).metrics
    assert a["final"] == pytest.approx(b["final"])


def test_max_drawdown():
    assert max_drawdown(pd.Series([100, 120, 90, 130])) == pytest.approx(-0.25)


def test_with_params():
    assert with_params(P, rsi_entry=30).rsi_entry == 30 and P.rsi_entry == 40


# ---------------------------------------------------------------- momentum
M = MomentumConfig(lookback=60, skip_days=5, trend_sma=50, hold_rank=12, atr_stop_mult=3.0)


def test_momentum_rebalance_day_detection():
    strat = MomentumStrategy(M)
    strat.trading_days = [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)]
    assert strat.is_rebalance_day(date(2024, 2, 1))
    assert not strat.is_rebalance_day(date(2024, 2, 2))
    assert not strat.is_rebalance_day(date(2024, 1, 30))   # önceki gün bilinmiyor


def test_momentum_ranks_strongest_and_excludes_downtrend():
    data = {"UP_FAST": make_series(drift=0.006, pullbacks=False, seed=1),
            "UP_SLOW": make_series(drift=0.002, pullbacks=False, seed=2),
            "DOWN": make_series(drift=-0.004, pullbacks=False, seed=3)}
    strat = MomentumStrategy(M)
    from bot.engine import build_rows
    rows = build_rows(data, strat)
    day = next(d for d in strat.trading_days if d.month == 9 and strat.is_rebalance_day(d))
    today = {s: r[day] for s, r in rows.items()}
    sigs = strat.entry_signals(day, today, set())
    assert [s.symbol for s in sigs] == ["UP_FAST", "UP_SLOW"]
    assert "1. sırada" in sigs[0].reasons[0]
    assert strat.exit_reasons(day, today, {"DOWN"})["DOWN"].startswith("Aylık yenileme")
    tight = MomentumStrategy(MomentumConfig(lookback=60, skip_days=5, trend_sma=50, hold_rank=1))
    tight.trading_days = strat.trading_days
    assert "sıralama 2." in tight.exit_reasons(day, today, {"UP_SLOW"})["UP_SLOW"]


def test_hold_rank_must_cover_max_positions():
    from bot.strategy import make_strategy
    with pytest.raises(ValueError):
        make_strategy(Settings(momentum=MomentumConfig(hold_rank=5)))


def test_momentum_equal_weight_backtest_is_mostly_invested():
    data = {f"S{i}": make_series(seed=i, drift=0.003 + 0.0005 * i, pullbacks=False) for i in range(12)}
    s = Settings(sizing="equal_weight", momentum=M)
    res = run_backtest(data, s, MomentumStrategy(M), date(2024, 5, 1), date(2025, 2, 1))
    assert res.metrics["exposure"] > 0.8
    buys = res.orders[(res.orders["side"] == "BUY") & (res.orders["status"] == "FILLED")]
    import json
    card = json.loads(buys.iloc[0]["reason"])
    assert card["strategy"] == "momentum" and 9 < card["portfolio_pct"] <= 10.0
