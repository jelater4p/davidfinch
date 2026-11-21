"""Komut satırı: python -m bot <komut>

  fetch   Evrendeki hisselerin günlük verisini çekip veritabanına yazar
  status  Cüzdan durumunu gösterir
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from .broker.paper import PaperBroker
from .config import load_settings
from .data.store import PriceStore
from .data.yahoo import YahooSource
from .storage.db import Database


def cmd_fetch(args, settings, db) -> None:
    symbols = args.symbols or settings.universe()
    end = date.today()
    start = end - timedelta(days=settings.history_days)
    data = YahooSource().fetch_daily(symbols, start, end)
    store = PriceStore(db)
    total = sum(store.replace_history(sym, df) for sym, df in data.items())
    missing = sorted(set(symbols) - set(data))
    print(f"{len(data)}/{len(symbols)} hisse, {total} satır yazıldı.")
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m bot")
    parser.add_argument("--settings", help="settings.yaml yolu")
    sub = parser.add_subparsers(dest="command", required=True)
    p_fetch = sub.add_parser("fetch", help="Günlük veriyi çek")
    p_fetch.add_argument("symbols", nargs="*", help="Boşsa tüm evren")
    sub.add_parser("status", help="Cüzdan durumu")
    args = parser.parse_args()

    settings = load_settings(args.settings)
    db = Database(settings.db_path)
    try:
        {"fetch": cmd_fetch, "status": cmd_status}[args.command](args, settings, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
