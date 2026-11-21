# davidfinch

BIST100 üzerinde paper (sanal) işlem yapan, kararlarını gerekçelendiren ve
geçmiş işlemlerinden öğrenerek parametrelerini ayarlayan trade botu.

## Durum

| Aşama | İçerik | Durum |
|---|---|---|
| 1 | Ayarlar, SQLite, Yahoo verisi, PaperBroker, risk kuralları | ✅ |
| 2 | Strateji, gerekçe kartı, backtest | ✅ |
| 3 | Walk-forward + haftalık öğrenme | ⏳ |
| 4 | KAP + Claude filtresi | ⏳ |
| 5 | Telegram bot + zamanlayıcı | ⏳ |
| 6 | Docker + VPS kurulumu | ⏳ |

## Kurulum

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

## Kullanım

```bash
python -m bot fetch           # tüm BIST100 günlük verisini çek
python -m bot fetch THYAO     # tek hisse
python -m bot fetch --days 2200   # backtest için ~6 yıllık geçmiş
python -m bot status          # cüzdan durumu
python -m bot backtest --start 2022-01-01   # rapor: reports/ klasörüne
pytest                        # testler
```

## Strateji: trend içinde geri çekilmeden al

Giriş (kapanış sonrası karar, ertesi açılışta işlem): kapanış > SMA(trend) ve SMA yükseliyor,
son 3 günde RSI < eşik, bugün kapanış > dünkü kapanış, hacim >= çarpan x 20g ortalama.
Çıkış: iz süren ATR stop veya kapanış < SMA(trend).

Öğrenilen parametreler: `trend_sma` {50,100,200}, `rsi_entry` 30–50, `atr_stop_mult` 1.5–3.5,
`volume_mult` 1.0–2.0. Varsayılanlar (100 / 40 / 2.5 / 1.2) tüm dönem verisine bakılarak seçildi;
bu iyimser bir seçimdir, dürüst değerlendirme 3. aşamadaki walk-forward ile yapılacak.

## Sabit risk kuralları (`settings.yaml` → `risk`)

- Başlangıç sermayesi 100.000 TL
- En az %30 nakit
- Tek hisse en fazla portföyün %28'i
- İşlem başı risk %1 (stop olunursa en fazla ~1.000 TL kayıp)
- En fazla 6 açık pozisyon
- Lot = (portföy × risk) / (giriş − ATR stop); yukarıdaki sınırlar sırayla uygulanır

## Paper broker gerçekçilik kuralları

- Emirler karar gününden sonraki seansın açılışında dolar
- Komisyon (binde 2, temkinli) + BSMV + %0.1 kayma
- Fiyatlar BIST fiyat adımına aleyhimize yuvarlanır
- Stop: gün içi en düşük stop'a değerse stop fiyatından, gap'te açılıştan
- Dolum anında nakit/tek hisse kuralları yeniden kontrol edilir

## Bilinen sınırlamalar

- `data/bist100.txt` yaklaşık bir listedir; güncel endeks bileşimiyle doğrulanmalı.
- Bugünkü listeyle geçmişe dönük test hayatta kalma yanlılığı (survivorship bias) içerir.
- Paper pozisyon taşınırken bedelsiz/bölünme olursa lot ayarlaması henüz yok (2. aşamada).
