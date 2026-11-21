"""Walk-forward: öğrenme döngüsünün geçmişte dürüstçe sınanması.

Tek, kesintisiz bir paper simülasyonu çalıştırılır. Her ayın ilk işlem gününde
(aylık yenilemeden hemen önce) parametreler SADECE o güne kadarki son 24 ayın
verisiyle frenli olarak yeniden seçilir; seçilen parametreler botun hiç görmediği
bir sonraki ayda kullanılır. Sonuç, sabit parametreli çalışmayla kıyaslanır.

Gösterge değerleri t gününde yalnızca t ve öncesine baktığı için (rolling/shift)
geleceğe sızıntı yoktur.
"""
from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..backtest.metrics import compute_metrics
from ..backtest.runner import run_backtest
from ..broker.paper import PaperBroker
from ..config import MomentumConfig, Settings
from ..engine import Engine
from ..storage.db import Database
from ..strategy.momentum import MomentumStrategy
from . import optimizer
from .optimizer import Decision, Evaluation, RowsCache, neighbors

# ------------------------------------------------------------- paralel değerlendirme
_W: dict = {}


def _init_worker(data, settings):
    _W["cache"] = RowsCache(data)
    _W["settings"] = settings


def _eval_job(args) -> Evaluation:
    p, start, end = args
    return optimizer.evaluate(_W["cache"], _W["settings"], p, start, end)


def _choose_parallel(pool, settings: Settings, current: MomentumConfig, start: date, end: date) -> Decision:
    cands = neighbors(current, settings.risk.max_open_positions)
    evals = list(pool.map(_eval_job, [(p, start, end) for p in cands]))
    return optimizer.decide(current, evals, start, end)


# ------------------------------------------------------------- ana döngü
@dataclass
class WalkForwardResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict
    baseline_metrics: dict
    decisions: list[tuple[date, Decision]] = field(default_factory=list)


def _months_before(day: date, months: int) -> date:
    return (pd.Timestamp(day) - pd.DateOffset(months=months)).date()


def run_walk_forward(data: dict[str, pd.DataFrame], settings: Settings, start: date, end: date,
                     initial: MomentumConfig | None = None, benchmark: pd.Series | None = None,
                     train_months: int = optimizer.TRAIN_MONTHS, workers: int | None = None,
                     progress=None) -> WalkForwardResult:
    p = initial or settings.momentum
    cache = RowsCache(data)
    strat = MomentumStrategy(p)
    rows = cache.get(p)
    strat.prepare(rows)

    db = Database(":memory:")
    broker = PaperBroker(db, settings.risk, settings.costs)
    engine = Engine(broker, settings, strat, rows)
    days = [d for d in strat.trading_days if start <= d <= end]
    decisions: list[tuple[date, Decision]] = []

    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers or mp.cpu_count(), mp_context=ctx,
                             initializer=_init_worker, initargs=(data, settings)) as pool:
        for i, day in enumerate(days):
            if strat.is_rebalance_day(day):
                train_end = days[i - 1] if i > 0 else strat._prev_trading_day(day)
                dec = _choose_parallel(pool, settings, p, _months_before(day, train_months), train_end)
                decisions.append((day, dec))
                if progress:
                    progress(day, dec)
                if dec.changed:
                    p = dec.params
                    strat = MomentumStrategy(p)
                    rows = cache.get(p)
                    strat.prepare(rows)
                    engine.strategy, engine.rows = strat, rows
            engine.run_day(day)

    equity = pd.Series({pd.Timestamp(r["date"]): r["equity"]
                        for r in db.query("SELECT date, equity FROM equity_snapshots ORDER BY date")}, dtype=float)
    trades = pd.DataFrame([dict(r) for r in db.query("SELECT * FROM trades ORDER BY id")])
    snaps = db.query("SELECT positions_value / equity AS x FROM equity_snapshots")
    db.close()

    bench = None
    if benchmark is not None and not equity.empty:
        bench = benchmark[(benchmark.index >= equity.index[0]) & (benchmark.index <= equity.index[-1])]
    metrics = compute_metrics(equity, trades, settings.risk.initial_capital, bench)
    metrics["exposure"] = sum(r["x"] for r in snaps) / len(snaps) if snaps else 0.0
    metrics["param_changes"] = sum(d.changed for _, d in decisions)

    base_strat = MomentumStrategy(initial or settings.momentum)
    base = run_backtest(data, settings, base_strat, start, end, benchmark).metrics
    return WalkForwardResult(equity, trades, metrics, base, decisions)
