"""Telegram komutları. Her komut metin döndürür; gönderimi daemon yapar."""
from __future__ import annotations

import json
from datetime import date

from ..backtest.metrics import max_drawdown
from ..learning.live import current_params, last_proposal
from ..strategy.card import render_card
from .runner import LiveRunner

HELP = """Komutlar:
/portfoy — açık pozisyonlar, kâr/zarar, stop seviyeleri
/neden HISSE — o hisseyi neden aldığım, ne zamana kadar tutacağım, ne olursa satacağım
/bekleyen — yarın açılışta gerçekleşecek emirler
/islemler [N] — son N kapanan işlem (varsayılan 10)
/performans — toplam getiri, BIST100 kıyası, en büyük düşüş, kazanma oranı
/parametreler — aktif parametreler ve son öğrenme kararı
/haber HISSE — son KAP bildirimleri ve Claude değerlendirmesi
/durdur — yeni alımları durdur (stop ve çıkışlar çalışmaya devam eder)
/devam — yeni alımlara devam
/yardim — bu liste"""


class CommandHandler:
    def __init__(self, runner: LiveRunner, today=date.today):
        self.r = runner
        self.today = today

    def handle(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return HELP
        cmd = parts[0].lower().split("@")[0]   # /portfoy@BotAdi biçimini de kabul et
        args = parts[1:]
        table = {
            "/portfoy": self.portfoy, "/neden": self.neden, "/bekleyen": self.bekleyen,
            "/islemler": self.islemler, "/performans": self.performans, "/parametreler": self.parametreler,
            "/haber": self.haber, "/durdur": self.durdur, "/devam": self.devam,
            "/yardim": lambda a: HELP, "/start": lambda a: HELP, "/help": lambda a: HELP,
        }
        fn = table.get(cmd)
        if fn is None:
            return "Bilinmeyen komut.\n\n" + HELP
        try:
            return fn(args)
        except Exception as exc:   # komut hatası botu düşürmesin
            return f"Komut çalıştırılamadı: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    def portfoy(self, args) -> str:
        positions = self.r.broker.positions()
        if not positions:
            return "Açık pozisyon yok.\n\n" + self.r.summary(self.today())
        lines = []
        for s, p in sorted(positions.items()):
            last = self.r.store.last_close(s, self.today()) or p.avg_cost
            days = (self.today() - p.entry_date).days
            lines.append(f"{s}: {p.qty} lot | maliyet {p.avg_cost:.2f} → {last:.2f} "
                         f"(%{100 * (last / p.avg_cost - 1):+.1f}) | stop {p.stop_price:.2f} | {days} gün")
        return "\n".join(lines) + "\n\n" + self.r.summary(self.today())

    def neden(self, args) -> str:
        if not args:
            return "Kullanım: /neden HISSE (ör. /neden ASELS)"
        sym = args[0].upper()
        row = self.r.db.query_one(
            "SELECT * FROM orders WHERE symbol = ? AND side = 'BUY' AND status IN ('FILLED','PENDING') "
            "ORDER BY id DESC LIMIT 1", (sym,))
        if row is None:
            return f"{sym} için alım kaydı yok."
        card = render_card(json.loads(row["reason"]))
        pos = self.r.broker.positions().get(sym)
        if pos:
            last = self.r.store.last_close(sym, self.today()) or pos.avg_cost
            status = (f"\n\nŞu an: {pos.qty} lot, maliyet {pos.avg_cost:.2f}, son {last:.2f} "
                      f"(%{100 * (last / pos.avg_cost - 1):+.1f}), güncel stop {pos.stop_price:.2f}")
        elif row["status"] == "PENDING":
            status = "\n\nDurum: emir bekliyor (yarın açılışta)."
        else:
            t = self.r.db.query_one("SELECT * FROM trades WHERE entry_order_id = ? ORDER BY id DESC LIMIT 1",
                                    (row["id"],))
            status = (f"\n\nKapandı: {t['exit_date']} @ {t['exit_price']:.2f}, {t['pnl']:+,.0f} TL "
                      f"(%{100 * t['pnl_pct']:+.1f}) — {t['exit_reason']}") if t else ""
        return f"[{row['decision_date']} kararı]\n{card}{status}"

    def bekleyen(self, args) -> str:
        return self.r.pending_preview().replace("Bugün açılışta", "Sıradaki açılışta")

    def islemler(self, args) -> str:
        n = int(args[0]) if args and args[0].isdigit() else 10
        rows = self.r.db.query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,))
        if not rows:
            return "Henüz kapanmış işlem yok."
        return "\n".join(
            f"{t['exit_date']} {t['symbol']}: {t['entry_price']:.2f} → {t['exit_price']:.2f} "
            f"{t['pnl']:+,.0f} TL (%{100 * t['pnl_pct']:+.1f}) — {t['exit_reason']}" for t in rows)

    def performans(self, args) -> str:
        import pandas as pd
        snaps = self.r.db.query("SELECT date, equity FROM equity_snapshots ORDER BY date")
        if not snaps:
            return "Henüz kayıt yok."
        eq = pd.Series([s["equity"] for s in snaps])
        trades = self.r.db.query("SELECT pnl FROM trades")
        wins = sum(1 for t in trades if t["pnl"] > 0)
        lines = [self.r.summary(self.today()),
                 f"En büyük düşüş: %{100 * max_drawdown(eq):.1f}",
                 f"Kapanan işlem: {len(trades)}" + (f", kazanma oranı %{100 * wins / len(trades):.0f}" if trades else ""),
                 f"Başlangıç: {snaps[0]['date']} ({len(snaps)} işlem günü)"]
        return "\n".join(lines)

    def parametreler(self, args) -> str:
        p = current_params(self.r.db, self.r.settings)
        lines = [f"Momentum penceresi: {p.lookback} gün (sabit)",
                 f"Tutma sınırı: ilk {p.hold_rank}",
                 f"ATR stop çarpanı: {p.atr_stop_mult:g}",
                 f"İşlem başı risk: %{100 * self.r.settings.risk.risk_per_trade:g} | "
                 f"En fazla {self.r.settings.risk.max_open_positions} pozisyon"]
        last = self.r.db.query_one("SELECT effective_date, reason FROM param_history ORDER BY id DESC LIMIT 1")
        if last:
            lines.append(f"\nSon karar ({last['effective_date']}): {last['reason']}")
        if last_proposal(self.r.db):
            lines.append("Teyit bekleyen öneri var (gelecek ay aynı çıkarsa uygulanacak).")
        return "\n".join(lines)

    def haber(self, args) -> str:
        if not args:
            return "Kullanım: /haber HISSE"
        flt = self.r.news_filter
        if flt is None:
            return "KAP filtresi kapalı."
        from datetime import timedelta
        sym = args[0].upper()
        flt.refresh(self.today(), [sym])
        rows = flt.store.for_symbol(sym, self.today() - timedelta(days=flt.config.lookback_days))
        if not rows:
            return f"{sym}: son {flt.config.lookback_days} günde bildirim yok." + (
                "\n⚠️ " + flt.warnings[-1] if flt.warnings else "")
        out = []
        for r in rows[:10]:
            flag = {1: "🔴 CİDDİ OLUMSUZ", 0: "⚪ normal", None: "❔ analiz yok"}[r["severe"]]
            out.append(f"[{r['published_at'][:16]}] {flag} {r['subject'] or ''} — {r['title'] or ''}")
            if r["reason"]:
                out.append(f"   {r['reason']}")
        return "\n".join(out)

    def durdur(self, args) -> str:
        self.r.set_paused(True)
        return "⏸ Yeni alımlar durduruldu. Stop'lar ve satış kararları çalışmaya devam ediyor. /devam ile açabilirsin."

    def devam(self, args) -> str:
        self.r.set_paused(False)
        return "▶️ Yeni alımlar açıldı."
