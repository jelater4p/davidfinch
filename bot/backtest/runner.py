"""Backtest: motoru geçmiş veride gün gün çalıştırır.

Canlı paper ile birebir aynı kod (Engine + PaperBroker) kullanılır; tek fark
veritabanının bellekte olması ve günlerin döngüyle ilerlemesidir.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pandas as pd

from ..broker.paper import PaperBroker
from ..config import Settings
from ..engine import Engine, Rows, build_rows
from ..strategy.base import Strategy
from ..storage.db import Database
from .metrics import compute_metrics


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict
    params: dict
    start: date
    end: date


def run_backtest(data: dict[str, pd.DataFrame], settings: Settings, strategy: Strategy,
                 start: date, end: date, benchmark: pd.Series | None = None,
                 rows: Rows | None = None, sizing: str | None = None) -> BacktestResult:
    """data: tüm geçmiş (gösterge ısınması için start öncesini de içermeli)."""
    rows = rows if rows is not None else build_rows(data, strategy)
    db = Database(":memory:")
    try:
        broker = PaperBroker(db, settings.risk, settings.costs)
        engine = Engine(broker, settings, strategy, rows, sizing=sizing)
        days = sorted({d for r in rows.values() for d in r if start <= d <= end})
        for day in days:
            engine.run_day(day)

        equity = pd.Series(
            {pd.Timestamp(r["date"]): r["equity"] for r in db.query("SELECT date, equity FROM equity_snapshots ORDER BY date")},
            dtype=float,
        )
        snaps = db.query("SELECT positions_value / equity AS x FROM equity_snapshots")
        exposure = sum(r["x"] for r in snaps) / len(snaps) if snaps else 0.0
        trades = pd.DataFrame([dict(r) for r in db.query("SELECT * FROM trades ORDER BY id")])
        orders = pd.DataFrame([dict(r) for r in db.query("SELECT * FROM orders ORDER BY id")])
        open_positions = len(broker.positions())
    finally:
        db.close()

    bench = None
    if benchmark is not None and not equity.empty:
        bench = benchmark[(benchmark.index >= equity.index[0]) & (benchmark.index <= equity.index[-1])]
    metrics = compute_metrics(equity, trades, settings.risk.initial_capital, bench)
    metrics["open_positions_at_end"] = open_positions
    metrics["exposure"] = exposure
    return BacktestResult(equity, trades, orders, metrics, strategy.param_dict(), start, end)


def with_params(base: StrategyConfig, **changes) -> StrategyConfig:
    return replace(base, **changes)
