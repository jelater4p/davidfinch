"""Performans ölçütleri ve okunabilir rapor."""
from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1).min())


def compute_metrics(equity: pd.Series, trades: pd.DataFrame, initial: float,
                    benchmark: pd.Series | None = None) -> dict:
    m: dict = {"days": len(equity)}
    if equity.empty:
        return m
    final = float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    rets = equity.pct_change().dropna()
    m.update(
        initial=initial,
        final=final,
        total_return=final / initial - 1,
        cagr=(final / initial) ** (1 / years) - 1,
        max_drawdown=max_drawdown(equity),
        volatility=float(rets.std() * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0,
        sharpe=float(rets.mean() / rets.std() * math.sqrt(TRADING_DAYS)) if len(rets) > 1 and rets.std() > 0 else 0.0,
    )
    if benchmark is not None and len(benchmark) > 1:
        m["benchmark_return"] = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1)
        m["benchmark_max_drawdown"] = max_drawdown(benchmark)

    m["trades"] = n = len(trades)
    if n:
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        hold = (pd.to_datetime(trades["exit_date"]) - pd.to_datetime(trades["entry_date"])).dt.days
        gross_loss = -losses["pnl"].sum()
        m.update(
            win_rate=len(wins) / n,
            avg_win_pct=float(wins["pnl_pct"].mean()) if len(wins) else 0.0,
            avg_loss_pct=float(losses["pnl_pct"].mean()) if len(losses) else 0.0,
            profit_factor=float(wins["pnl"].sum() / gross_loss) if gross_loss > 0 else float("inf"),
            avg_hold_days=float(hold.mean()),
            total_pnl=float(trades["pnl"].sum()),
            exit_reasons=trades["exit_reason"].str.split(":").str[0].value_counts().to_dict(),
        )
    return m


def _pct(x: float) -> str:
    return f"%{100 * x:+.1f}"


def format_report(m: dict, title: str = "Backtest raporu") -> str:
    if "final" not in m:
        return f"{title}\nVeri yok."
    lines = [
        title,
        "=" * len(title),
        f"Gün sayısı        : {m['days']}",
        f"Başlangıç → Son   : {m['initial']:,.0f} TL → {m['final']:,.0f} TL",
        f"Toplam getiri     : {_pct(m['total_return'])}   (yıllık {_pct(m['cagr'])})",
    ]
    if "benchmark_return" in m:
        lines.append(f"BIST100 (aynı dönem): {_pct(m['benchmark_return'])}   "
                     f"(en büyük düşüş {_pct(m['benchmark_max_drawdown'])})")
    lines += [
        f"En büyük düşüş    : {_pct(m['max_drawdown'])}",
        f"Yıllık oynaklık   : %{100 * m['volatility']:.1f}   Sharpe: {m['sharpe']:.2f}",
        f"Ort. hissede olma : %{100 * m.get('exposure', 0):.0f} (kalanı nakit)",
        f"İşlem sayısı      : {m['trades']}   (dönem sonunda açık: {m.get('open_positions_at_end', 0)})",
    ]
    if m["trades"]:
        pf = m["profit_factor"]
        lines += [
            f"Kazanma oranı     : %{100 * m['win_rate']:.0f}",
            f"Ort. kazanç/kayıp : {_pct(m['avg_win_pct'])} / {_pct(m['avg_loss_pct'])}",
            f"Kâr faktörü       : {'∞' if math.isinf(pf) else f'{pf:.2f}'}   (>1 kârlı; 1.3+ iyi)",
            f"Ort. tutma süresi : {m['avg_hold_days']:.0f} gün",
            "Çıkış nedenleri   : " + ", ".join(f"{k}: {v}" for k, v in m["exit_reasons"].items()),
        ]
    lines.append("Not: TL nominal getiri; enflasyon ve mevduat faizi ile de kıyaslayın.")
    return "\n".join(lines)
