from datetime import date

from bot.config import MomentumConfig, Settings
from bot.learning.live import already_updated, current_params, monthly_update
from bot.learning.optimizer import Evaluation, decide, neighbors
from bot.learning.walkforward import run_walk_forward
from bot.storage.db import Database
from test_strategy import make_series

BASE = MomentumConfig(lookback=126, hold_rank=15, atr_stop_mult=3.0)


def test_neighbors_respect_step_brake():
    cands = neighbors(BASE, max_open_positions=10)
    assert BASE in cands
    assert {c.lookback for c in cands} == {63, 126, 252}
    assert {c.atr_stop_mult for c in cands} == {2.5, 3.0, 3.5}      # 2.0 / 4.0 tek adımda olmaz
    assert len(cands) == 27


def test_neighbors_drop_hold_rank_below_max_positions():
    p = MomentumConfig(lookback=63, hold_rank=10, atr_stop_mult=2.0)
    cands = neighbors(p, max_open_positions=15)
    assert all(c.hold_rank >= 15 for c in cands)


def _ev(p, sharpe):
    return Evaluation(p, sharpe, 0.1, -0.1, 10)


def test_decide_requires_meaningful_gain():
    other = MomentumConfig(lookback=252, hold_rank=15, atr_stop_mult=3.0)
    small = decide(BASE, [_ev(BASE, 1.0), _ev(other, 1.1)], date(2024, 1, 1), date(2025, 12, 31))
    assert not small.changed and small.params == BASE and "değişmedi" in small.explanation
    big = decide(BASE, [_ev(BASE, 1.0), _ev(other, 1.5)], date(2024, 1, 1), date(2025, 12, 31))
    assert big.changed and big.params == other
    assert "momentum penceresi 126 → 252" in big.explanation and "Sharpe 1.00 → 1.50" in big.explanation


def _data(n=12, length=700):
    return {f"S{i}": make_series(n=length, seed=i, drift=0.002 + 0.0004 * i, pullbacks=False)
            for i in range(n)}


def test_monthly_update_writes_history_once_per_month():
    db = Database(":memory:")
    s = Settings()
    assert current_params(db, s) == s.momentum
    d = date(2026, 6, 1)
    data = _data()
    first = monthly_update(db, s, data, d)
    assert first is not None and already_updated(db, d)
    assert monthly_update(db, s, data, d) is None                 # aynı ay ikinci kez çalışmaz
    row = db.query_one("SELECT reason FROM param_history")
    assert row["reason"] == first.explanation
    assert current_params(db, s) == first.params


def test_walk_forward_runs_and_reports():
    s = Settings(momentum=MomentumConfig(lookback=63, skip_days=5, trend_sma=50, hold_rank=10))
    res = run_walk_forward(_data(length=500), s, date(2025, 3, 1), date(2025, 8, 1), workers=2, train_months=6)
    assert len(res.decisions) >= 4
    assert not res.equity.empty and "sharpe" in res.metrics and "sharpe" in res.baseline_metrics
