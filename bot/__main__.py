"""Komut satırı: python -m bot <komut>

  fetch     Evrendeki hisselerin (ve endeksin) günlük verisini çekip veritabanına yazar
  status    Cüzdan durumunu gösterir
  backtest  Stratejiyi geçmiş veride çalıştırıp rapor üretir
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from .backtest.metrics import format_report
from .backtest.runner import run_backtest
from .broker.paper import PaperBroker
from .config import ROOT, load_settings
from .data.store import PriceStore
from .data.yahoo import YahooSource
from .storage.db import Database
from .strategy.card import render_card


def cmd_fetch(args, settings, db) -> None:
    symbols = args.symbols or settings.universe()
    if not args.symbols:
        symbols = symbols + [settings.benchmark]
    end = date.today()
    start = end - timedelta(days=args.days or settings.history_days)
    data = YahooSource().fetch_daily(symbols, start, end)
    store = PriceStore(db)
    total = sum(store.replace_history(sym, df) for sym, df in data.items())
    missing = sorted(set(symbols) - set(data))
    print(f"{len(data)}/{len(symbols)} sembol, {total} satır yazıldı.")
    if missing:
        print("Veri gelmeyenler:", ", ".join(missing))


def cmd_status(args, settings, db) -> None:
    broker = PaperBroker(db, settings.risk, settings.costs)
    store = PriceStore(db)
    today = date.today()
    positions = broker.positions()
    closes = {s: store.last_close(s, today) or p.avg_cost for s, p in positions.items()}
    equity = broker.equity(closes)
    cash = broker.cash()
    print(f"Portföy: {equity:,.2f} TL | Nakit: {cash:,.2f} TL (%{100 * cash / equity:.1f})")
    for s, p in positions.items():
        pnl = (closes[s] / p.avg_cost - 1) * 100
        print(f"  {s:6} {p.qty:>6} lot  maliyet {p.avg_cost:>8.2f}  son {closes[s]:>8.2f}  "
              f"%{pnl:+.1f}  stop {p.stop_price:.2f}")
    if not positions:
        print("  Açık pozisyon yok.")


def cmd_backtest(args, settings, db) -> None:
    store = PriceStore(db)
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365 * 2)
    data = {}
    for sym in settings.universe():
        df = store.history(sym, end=end)
        if len(df) > settings.strategy.trend_sma + 20:
            data[sym] = df
    if not data:
        raise SystemExit("Veri yok. Önce: python -m bot fetch --days 2000")
    bench_df = store.history(settings.benchmark, start=start, end=end)
    bench = bench_df["close"] if not bench_df.empty else None

    res = run_backtest(data, settings, settings.strategy, start, end, bench)
    print(format_report(res.metrics, f"Backtest {start} → {end} | {len(data)} hisse"))

    out = Path(args.out or ROOT / "reports" / f"backtest_{start}_{end}")
    out.mkdir(parents=True, exist_ok=True)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    if not res.trades.empty:
        res.trades.to_csv(out / "trades.csv", index=False)
    (out / "metrics.json").write_text(json.dumps(res.metrics, ensure_ascii=False, indent=2, default=str))
    filled_buys = res.orders[(res.orders["side"] == "BUY") & (res.orders["status"] == "FILLED")] if not res.orders.empty else []
    with open(out / "cards.txt", "w", encoding="utf-8") as fh:
        for _, o in (filled_buys.iterrows() if len(filled_buys) else []):
            fh.write(f"[{o['fill_date']}] dolum {o['fill_qty']} lot @ {o['fill_price']:.2f}\n")
            fh.write(render_card(json.loads(o["reason"])) + "\n\n")
    print(f"\nDetaylar: {out}/ (equity.csv, trades.csv, cards.txt, metrics.json)")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m bot")
    parser.add_argument("--settings", help="settings.yaml yolu")
    sub = parser.add_subparsers(dest="command", required=True)
    p_fetch = sub.add_parser("fetch", help="Günlük veriyi çek")
    p_fetch.add_argument("symbols", nargs="*", help="Boşsa tüm evren + endeks")
    p_fetch.add_argument("--days", type=int, help="Kaç günlük geçmiş (varsayılan: settings)")
    sub.add_parser("status", help="Cüzdan durumu")
    p_bt = sub.add_parser("backtest", help="Geçmiş veride strateji testi")
    p_bt.add_argument("--start", help="YYYY-AA-GG (varsayılan: 2 yıl önce)")
    p_bt.add_argument("--end", help="YYYY-AA-GG (varsayılan: bugün)")
    p_bt.add_argument("--out", help="Rapor klasörü")
    args = parser.parse_args()

    settings = load_settings(args.settings)
    db = Database(settings.db_path)
    try:
        {"fetch": cmd_fetch, "status": cmd_status, "backtest": cmd_backtest}[args.command](args, settings, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
