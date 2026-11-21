from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest.metrics import format_report, max_drawdown
from bot.backtest.runner import run_backtest, with_params
from bot.config import Settings, StrategyConfig
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
    assert len(sig.reasons(P)) == 4


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
    settings = Settings(strategy=P)
    res = run_backtest(data, settings, P, date(2024, 4, 1), date(2025, 2, 1))
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
    s = Settings(strategy=P)
    a = run_backtest(data, s, P, date(2024, 4, 1), date(2025, 2, 1)).metrics
    b = run_backtest(data, s, P, date(2024, 4, 1), date(2025, 2, 1)).metrics
    assert a["final"] == pytest.approx(b["final"])


def test_max_drawdown():
    assert max_drawdown(pd.Series([100, 120, 90, 130])) == pytest.approx(-0.25)


def test_with_params():
    assert with_params(P, rsi_entry=30).rsi_entry == 30 and P.rsi_entry == 40
