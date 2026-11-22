"""Komut satırı: python -m bot <komut>

  fetch     Evrendeki hisselerin (ve endeksin) günlük verisini çekip veritabanına yazar
  status    Cüzdan durumunu gösterir
  backtest  Stratejiyi geçmiş veride çalıştırıp rapor üretir
  walkforward  Aylık öğrenme döngüsünü geçmişte sınar (görmediği veride)
  learn     Aylık parametre güncellemesi (canlı paper; ayda bir)
  kap-check KAP bağlantısını ve yanıt biçimini teşhis eder (VPS'te bir kez çalıştırın)
  news      Bir hissenin son bildirimlerini çeker, Claude ile analiz edip gösterir
  run       Sürekli çalışan bot: Telegram komutları + günlük zamanlanmış işler (VPS'te bu çalışır)
  evening   Akşam işini elle bir kez çalıştırır (test/onarım için)
  telegram-test  Telegram'a deneme mesajı gönderir
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


def cmd_kap_check(args, settings, db) -> None:
    from .news.kap import KapWebSource, parse_items

    until = date.today()
    since = until - timedelta(days=2)
    print(f"KAP adresleri deneniyor ({since} → {until}); istekler arası 5 sn beklenir...\n")
    any_ok = False
    for res in KapWebSource().diagnose(since, until):
        ep = res["endpoint"]
        print(f"== {ep.name}: {ep.method} {ep.url}")
        if not res["ok"]:
            print(f"   HATA: {res['error']}\n")
            continue
        payload, items = res["payload"], res["items"]
        print(f"   Yanıt tipi: {type(payload).__name__}, liste uzunluğu: {len(items)}")
        if isinstance(payload, dict):
            print("   Üst seviye anahtarlar:", list(payload)[:20])
        if items:
            print("   İlk kaydın anahtarları:", list(items[0])[:30])
            print("   İlk kayıt:", json.dumps(items[0], ensure_ascii=False)[:1200])
        else:
            print("   Ham yanıt:", json.dumps(payload, ensure_ascii=False)[:600])
        parsed = parse_items(items)
        print(f"   Çözümlenen bildirim (hisse bazında): {len(parsed)}")
        for d in parsed[:5]:
            print(f"     {d.published_at:%Y-%m-%d %H:%M} {d.symbol:6} {d.subject[:35]:35} {d.title[:45]}")
        if parsed:
            any_ok = True
            dates = sorted(d.published_at for d in parsed)
            print(f"   Kapsanan aralık: {dates[0]} → {dates[-1]}")
        print()
    print("SONUÇ:", "En az bir adres çalışıyor." if any_ok else
          "Hiçbir adresten bildirim çözümlenemedi; bu çıktıyı geliştiriciye gönderin.")


def cmd_news(args, settings, db) -> None:
    from .news import make_news_filter

    flt = make_news_filter(settings, db)
    if flt is None:
        raise SystemExit("news.enabled = false")
    today = date.today()
    flt.refresh(today, [args.symbol.upper()])
    for w in flt.warnings:
        print(w)
    rows = flt.store.for_symbol(args.symbol.upper(), today - timedelta(days=settings.news.lookback_days))
    if not rows:
        print("Son günlerde bildirim yok.")
    for r in rows:
        flag = {1: "CİDDİ OLUMSUZ", 0: "normal", None: "analiz yok"}[r["severe"]]
        print(f"[{r['published_at'][:16]}] {flag:13} {r['subject'] or ''} — {r['title'] or ''}")
        if r["reason"]:
            print(f"    {r['category']} (güven {r['confidence']:.2f}): {r['reason']}")


def _notifier():
    import os

    from .notify.telegram import ConsoleNotifier, Telegram

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return Telegram(token, chat)
    print("UYARI: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yok — mesajlar konsola yazılacak.")
    return ConsoleNotifier()


def _runner(settings, db):
    import os

    from .live.runner import LiveRunner
    from .news import make_news_filter

    news = None
    if settings.news.enabled:
        if os.environ.get("ANTHROPIC_API_KEY"):
            news = make_news_filter(settings, db)
        else:
            print("UYARI: ANTHROPIC_API_KEY yok — KAP filtresi devre dışı.")
    return LiveRunner(settings, db, YahooSource(), news)


def cmd_run(args, settings, db) -> None:
    from .live.daemon import Daemon

    import os

    data_dir = os.path.dirname(os.path.abspath(settings.db_path))
    Daemon(_runner(settings, db), _notifier(), tz=settings.timezone,
           evening_offset_min=int(os.environ.get("EVENING_OFFSET_MIN", "0")),
           heartbeat_file=os.path.join(data_dir, "heartbeat"),
           backup_dir=os.path.join(data_dir, "backups")).run_forever()


def cmd_evening(args, settings, db) -> None:
    day = date.fromisoformat(args.date) if args.date else date.today()
    res = _runner(settings, db).evening(day, fetch=not args.no_fetch)
    text = res.message + ("\n\n" + "\n\n".join(res.sections) if res.sections else "")
    if args.send:
        _notifier().send(text)
    print(text)


def cmd_telegram_test(args, settings, db) -> None:
    from .notify.telegram import Telegram

    notifier = _notifier()
    if not isinstance(notifier, Telegram):
        raise SystemExit("Telegram yapılandırılmamış: .env içinde TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID olmalı.")
    if notifier.send("✅ davidfinch: Telegram bağlantısı çalışıyor."):
        print("Gönderildi. Telefonunuzu kontrol edin.")
    else:
        raise SystemExit("GÖNDERİLEMEDİ — yukarıdaki hata satırına bakın.")


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
    sub.add_parser("kap-check", help="KAP bağlantı teşhisi")
    p_news = sub.add_parser("news", help="Hisse bildirimlerini analiz et")
    p_news.add_argument("symbol")
    sub.add_parser("run", help="Sürekli çalışan bot (Telegram + zamanlayıcı)")
    p_ev = sub.add_parser("evening", help="Akşam işini elle çalıştır")
    p_ev.add_argument("--date", help="YYYY-AA-GG (varsayılan bugün)")
    p_ev.add_argument("--no-fetch", action="store_true", help="Veri çekme, veritabanındakini kullan")
    p_ev.add_argument("--send", action="store_true", help="Sonucu Telegram'a da gönder")
    sub.add_parser("telegram-test", help="Telegram deneme mesajı")
    args = parser.parse_args()
    if args.command in ("run", "evening"):
        logging.getLogger().setLevel(logging.INFO)

    settings = load_settings(args.settings)
    db = Database(settings.db_path)
    try:
        {"fetch": cmd_fetch, "status": cmd_status, "backtest": cmd_backtest,
         "walkforward": cmd_walkforward, "learn": cmd_learn,
         "kap-check": cmd_kap_check, "news": cmd_news,
         "run": cmd_run, "evening": cmd_evening, "telegram-test": cmd_telegram_test}[args.command](args, settings, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
