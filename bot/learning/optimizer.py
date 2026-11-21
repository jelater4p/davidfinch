"""Frenli parametre ayarı (aylık).

Kurallar:
  * Arama alanı: GRID (kullanıcının onayladığı izinli aralıklar).
  * Fren 1 — adım sınırı: her parametre bir ayda en fazla bir ızgara adımı oynar
    (ör. lookback 126 -> 252 olabilir, 63 -> 252 olamaz; atr 3.0 -> 3.5 olabilir, 4.0 olamaz).
  * Fren 2 — anlamlı iyileşme: yeni aday, mevcut parametrelerden en az
    MIN_SHARPE_GAIN kadar daha iyi Sharpe vermiyorsa değişiklik yapılmaz.
  * Ölçü: eğitim penceresindeki (son TRAIN_MONTHS ay) backtest Sharpe oranı.
"""
from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, replace
from datetime import date

import pandas as pd

from ..backtest.runner import run_backtest
from ..config import MomentumConfig, Settings
from ..engine import Rows, build_rows
from ..strategy.momentum import MomentumStrategy

GRID: dict[str, list] = {
    "lookback": [63, 126, 252],
    "hold_rank": [10, 15, 20],
    "atr_stop_mult": [2.0, 2.5, 3.0, 3.5, 4.0],
}
MIN_SHARPE_GAIN = 0.20
TRAIN_MONTHS = 24
LABELS = {"lookback": "momentum penceresi", "hold_rank": "tutma sınırı", "atr_stop_mult": "ATR stop çarpanı"}


def _feature_key(p: MomentumConfig) -> tuple:
    # Göstergeler sadece bunlara bağlı; hold_rank / atr_stop_mult değişince yeniden hesap gerekmez.
    return (p.lookback, p.skip_days, p.trend_sma, p.atr_period)


class RowsCache:
    def __init__(self, data: dict[str, pd.DataFrame]):
        self.data = data
        self._cache: dict[tuple, Rows] = {}

    def get(self, p: MomentumConfig) -> Rows:
        key = _feature_key(p)
        if key not in self._cache:
            self._cache[key] = build_rows(self.data, MomentumStrategy(p))
        return self._cache[key]


def neighbors(current: MomentumConfig, max_open_positions: int) -> list[MomentumConfig]:
    """Fren 1: her parametre için mevcut değer ve ızgarada bir sağı/solu."""
    options = {}
    for name, grid in GRID.items():
        val = getattr(current, name)
        if val in grid:
            i = grid.index(val)
            options[name] = grid[max(i - 1, 0): i + 2]
        else:   # ızgara dışı başlangıç değeri: en yakın ızgara değerleri + kendisi
            nearest = sorted(grid, key=lambda g: abs(g - val))[:2]
            options[name] = sorted({val, *nearest})
    out = []
    for combo in itertools.product(*options.values()):
        p = replace(current, **dict(zip(options, combo)))
        if p.hold_rank >= max_open_positions:
            out.append(p)
    return out


@dataclass
class Evaluation:
    params: MomentumConfig
    sharpe: float
    total_return: float
    max_drawdown: float
    trades: int


def evaluate(cache: RowsCache, settings: Settings, p: MomentumConfig, start: date, end: date) -> Evaluation:
    strat = MomentumStrategy(p)
    rows = cache.get(p)
    strat.prepare(rows)
    m = run_backtest(cache.data, settings, strat, start, end, rows=rows).metrics
    return Evaluation(p, m.get("sharpe", 0.0), m.get("total_return", 0.0), m.get("max_drawdown", 0.0),
                      m.get("trades", 0))


@dataclass
class Decision:
    changed: bool
    params: MomentumConfig
    current: Evaluation
    best: Evaluation
    explanation: str
    candidates: int


def choose(cache: RowsCache, settings: Settings, current: MomentumConfig, start: date, end: date) -> Decision:
    cands = neighbors(current, settings.risk.max_open_positions)
    return decide(current, [evaluate(cache, settings, p, start, end) for p in cands], start, end)


def decide(current: MomentumConfig, evals: list[Evaluation], start: date, end: date) -> Decision:
    """Değerlendirilmiş adaylar arasından frenli seçim (Fren 2)."""
    cands = evals
    cur = next(e for e in evals if e.params == current)
    best = max(evals, key=lambda e: e.sharpe)
    window = f"{start:%Y-%m} → {end:%Y-%m}"

    if best.params == current or best.sharpe < cur.sharpe + MIN_SHARPE_GAIN:
        expl = (f"Parametreler değişmedi. {window} döneminde mevcut ayar Sharpe {cur.sharpe:.2f}; "
                f"en iyi komşu aday {best.sharpe:.2f} (gereken fark ≥ {MIN_SHARPE_GAIN}).")
        return Decision(False, current, cur, best, expl, len(cands))

    changes = [
        f"{LABELS[k]} {getattr(current, k):g} → {getattr(best.params, k):g}"
        for k in GRID if getattr(current, k) != getattr(best.params, k)
    ]
    expl = (f"{', '.join(changes)}. Gerekçe: {window} döneminde Sharpe {cur.sharpe:.2f} → {best.sharpe:.2f}, "
            f"getiri %{100 * cur.total_return:+.0f} → %{100 * best.total_return:+.0f}, "
            f"en büyük düşüş %{100 * cur.max_drawdown:.0f} → %{100 * best.max_drawdown:.0f}.")
    return Decision(True, best.params, cur, best, expl, len(cands))


def params_to_dict(p: MomentumConfig) -> dict:
    return asdict(p)
