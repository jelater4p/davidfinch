"""Komut satırı: python -m bot <komut>

  fetch     Evrendeki hisselerin (ve endeksin) günlük verisini çekip veritabanına yazar
  status    Cüzdan durumunu gösterir
  backtest  Stratejiyi geçmiş veride çalıştırıp rapor üretir
  walkforward  Aylık öğrenme döngüsünü geçmişte sınar (görmediği veride)
  learn     Aylık parametre güncellemesi (canlı paper; ayda bir)
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
from .strategy import make_strategy
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
    strategy = make_strategy(settings, args.strategy)
    sizing = args.sizing or settings.sizing
    data = {}
    for sym in settings.universe():
        df = store.history(sym, end=end)
        if len(df) > 60:
            data[sym] = df
    if not data:
        raise SystemExit("Veri yok. Önce: python -m bot fetch --days 2000")
    bench_df = store.history(settings.benchmark, start=start, end=end)
    bench = bench_df["close"] if not bench_df.empty else None

    res = run_backtest(data, settings, strategy, start, end, bench, sizing=sizing)
    print(format_report(res.metrics, f"Backtest {start} → {end} | {len(data)} hisse | {strategy.name} / {sizing}"))

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


def _load_universe(settings, store, end):
    data = {}
    for sym in settings.universe():
        df = store.history(sym, end=end)
        if len(df) > 60:
            data[sym] = df
    if not data:
        raise SystemExit("Veri yok. Önce: python -m bot fetch --days 3300")
    return data


def cmd_walkforward(args, settings, db) -> None:
    from .learning.walkforward import run_walk_forward

    store = PriceStore(db)
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else date(2022, 1, 1)
    data = _load_universe(settings, store, end)
    bench_df = store.history(settings.benchmark, start=start, end=end)
    bench = bench_df["close"] if not bench_df.empty else None

    def progress(day, dec):
        mark = "DEĞİŞTİ" if dec.changed else "aynı   "
        print(f"[{day}] {mark} {dec.explanation}", flush=True)

    res = run_walk_forward(data, settings, start, end, benchmark=bench, workers=args.workers, progress=progress)
    print()
    print(format_report(res.metrics, f"Walk-forward (aylık öğrenme) {start} → {end}"))
    print(f"Parametre değişikliği: {res.metrics['param_changes']} / {len(res.decisions)} ay")
    print()
    print(format_report(res.baseline_metrics, "Kıyas: sabit başlangıç parametreleri"))

    out = Path(args.out or ROOT / "reports" / f"walkforward_{start}_{end}")
    out.mkdir(parents=True, exist_ok=True)
    res.equity.rename("equity").to_csv(out / "equity.csv")
    if not res.trades.empty:
        res.trades.to_csv(out / "trades.csv", index=False)
    with open(out / "decisions.txt", "w", encoding="utf-8") as fh:
        for day, dec in res.decisions:
            fh.write(f"[{day}] {dec.explanation}\n")
    (out / "metrics.json").write_text(json.dumps(
        {"walk_forward": res.metrics, "baseline": res.baseline_metrics}, ensure_ascii=False, indent=2, default=str))
    print(f"\nDetaylar: {out}/")


def cmd_learn(args, settings, db) -> None:
    from .learning.live import current_params, monthly_update

    store = PriceStore(db)
    today = date.today()
    dec = monthly_update(db, settings, _load_universe(settings, store, today), today, force=args.force)
    if dec is None:
        print("Bu ay güncelleme zaten yapıldı. Aktif parametreler:", current_params(db, settings))
    else:
        print(dec.explanation)


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
    p_bt.add_argument("--strategy", choices=["momentum", "pullback"], help="Varsayılan: settings")
    p_bt.add_argument("--sizing", choices=["equal_weight", "risk"], help="Varsayılan: settings")
    p_wf = sub.add_parser("walkforward", help="Aylık öğrenmeyi geçmişte sına")
    p_wf.add_argument("--start", help="Test başlangıcı (varsayılan 2022-01-01)")
    p_wf.add_argument("--end")
    p_wf.add_argument("--out")
    p_wf.add_argument("--workers", type=int, help="Paralel işlem sayısı (varsayılan: CPU sayısı)")
    p_learn = sub.add_parser("learn", help="Aylık parametre güncellemesi")
    p_learn.add_argument("--force", action="store_true", help="Bu ay yapılmış olsa da tekrar çalıştır")
    args = parser.parse_args()

    settings = load_settings(args.settings)
    db = Database(settings.db_path)
    try:
        {"fetch": cmd_fetch, "status": cmd_status, "backtest": cmd_backtest,
         "walkforward": cmd_walkforward, "learn": cmd_learn}[args.command](args, settings, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
