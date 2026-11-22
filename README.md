# davidfinch

[![Testler](https://github.com/jelater4p/davidfinch/actions/workflows/tests.yml/badge.svg)](https://github.com/jelater4p/davidfinch/actions/workflows/tests.yml)

BIST100 üzerinde paper (sanal) işlem yapan, kararlarını gerekçelendiren ve
geçmiş işlemlerinden öğrenerek parametrelerini ayarlayan trade botu.

## Durum

| Aşama | İçerik | Durum |
|---|---|---|
| 1 | Ayarlar, SQLite, Yahoo verisi, PaperBroker, risk kuralları | ✅ |
| 2 | Strateji, gerekçe kartı, backtest | ✅ |
| 3 | Walk-forward + aylık öğrenme | ✅ |
| 4 | KAP + Claude filtresi | ✅ (KAP bağlantısı VPS'te doğrulanacak) |
| 5 | Telegram bot + zamanlayıcı + canlı paper akışı | ✅ |
| 6 | Docker + VPS kurulumu ([DEPLOY.md](DEPLOY.md)) | ✅ |

## Kurulum

VPS'e kurulum için: **[DEPLOY.md](DEPLOY.md)**. Geliştirme ortamı için:

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
python -m bot walkforward --start 2022-01-01   # aylık öğrenmeyi geçmişte sına
python -m bot learn           # aylık parametre güncellemesi (canlı paper)
python -m bot kap-check       # KAP bağlantı/biçim teşhisi (VPS'te bir kez)
python -m bot news THYAO      # hissenin son bildirimleri + Claude analizi
python -m bot evening --date 2026-11-20   # akşam işini elle çalıştır (test/onarım)
python -m bot telegram-test   # Telegram bağlantısını dene
python -m bot run             # sürekli çalışan bot (VPS'te bu çalışır)
pytest                        # testler
```

## Aktif strateji: momentum / güç rotasyonu (`bot/strategy/momentum.py`)

Ayın ilk işlem günü kapanışından sonra evren sıralanır: skor = son 126 günün getirisi
(son 21 gün hariç); uygunluk = kapanış > 200g ortalama ve skor > 0. Sıralamada ilk
`hold_rank` (15) dışına düşen ya da uygunluğunu kaybeden hisse ertesi açılışta satılır,
boşalan yerler en güçlü adaylarla doldurulur. Aylar arasında iz süren ATR stop (3 x ATR) korur.
Haftanın ilk işlem günü, ay içinde boşalan yerler yine sıralamaya göre doldurulur; stop olan
hisse bir sonraki aylık yenilemeye kadar tekrar alınmaz.

Lot yöntemi (`strategy.sizing`):
- `risk` (aktif): lot = (portföy x %1) / stop mesafesi.
- `equal_weight`: her hisseye portföy / 10. İşlem başı risk sınır değil, kartta raporlanır.

## Öğrenme döngüsü (`bot/learning/`)

Her ayın ilk işlem gününde, aylık yenilemeden önce, parametreler son 24 ayın verisiyle yeniden seçilir.
- Ölçü: Sharpe oranı.
- Arama alanı: `hold_rank` {10,15,20}, `atr_stop_mult` {2.0 … 4.0}. `lookback` 126 günde sabit
  (ilk walk-forward'da 126 ↔ 252 arasında gidip gelip gürültüye tepki verdi).
- Fren 1: her parametre bir ayda en fazla bir ızgara adımı oynar.
- Fren 2: Sharpe en az 0.20 iyileşmiyorsa öneri yapılmaz.
- Fren 3: öneri ancak art arda 2 ay aynı çıkarsa uygulanır.
- Her ayın kararı gerekçesiyle `param_history` tablosuna yazılır.

`walkforward` komutu bu döngüyü geçmişte, her ay sadece o güne kadarki veriyle çalıştırır ve
sabit parametreli sonuçla kıyaslar.

İkincil strateji (`pullback`, pasif): trend içinde geri çekilmeden al.
`python -m bot backtest --strategy pullback` ile çalıştırılabilir.

## KAP filtresi (`bot/news/`)

- Kaynak arayüzü `NewsSource`: bugün `KapWebSource`, ileride Matriks aynı arayüzle takılır
  (`settings.yaml` → `news.source`).
- Her bildirim bir kez çekilir, bir kez Claude ile sınıflandırılır (yapılandırılmış JSON çıktı) ve
  SQLite'ta önbelleklenir.
- Son 7 günde **ciddi olumsuz** bildirimi olan hisse alınmaz (güven ≥ 0.6). Ciddi olumsuz şunlardır:
  SPK cezası/işlem yasağı, önemli dava, konkordato/iflas, büyük sözleşme iptali, beklenmedik zarar,
  denetçinin olumsuz görüşü, temerrüt.
- Elde tutulan hissede ciddi olumsuz haber çıkarsa uyarı üretilir; otomatik satış yapılmaz.
- Kaynak veya analiz hatası alımı engellemez, sadece uyarı üretir.
- **Uyarı:** KAP'ın resmi API'si yoktur. `KapWebSource` sitenin kendi veri adreslerini kullanır; bu,
  KAP'ın izin verdiği bir kullanım değildir ve kırılabilir. Alan eşlemesi doğrulanmadı; VPS'te
  `kap-check` ile kontrol edilmeli.
- Geçmiş bildirim verisi olmadığı için filtre backtest'e dahil değildir.
- Claude API anahtarı `.env` içindeki `ANTHROPIC_API_KEY` değişkeninden okunur.

## Canlı paper akışı (`bot/live/`)

`python -m bot run` sürekli çalışır (Europe/Istanbul, hafta içi):
- **09:45** sabah mesajı: bot çalışıyor + o gün açılışta gerçekleşecek emirler.
- **10:05–18:15** 15 dakikada bir gün içi izleme (Yahoo, ~15 dk gecikmeli). Stop'a değen ya da
  stop'un %2 yakınına gelen pozisyon için uyarı gönderir; `/portfoy` gün içi fiyatı gösterir.
  **İşlem yapmaz.** Paper satış akşam stop fiyatından kaydedilir; bu, aracı kurumdaki bekleyen bir
  stop emrinin davranışıyla aynıdır ve sonuçları backtest ile tutarlı tutar.
- **19:00** akşam işi. Kapanış verisi gelmemişse 22:00'a kadar 30 dakikada bir tekrar dener.
  1. Veri yenilenir. Elde tutulan hissede bedelsiz/bölünme varsa lot, maliyet ve stop uyarlanır.
  2. Ay başıysa frenli parametre güncellemesi yapılır.
  3. Bekleyen emirler bugünün açılış fiyatından dolar, stop'lar bugünün en düşüğüyle kontrol edilir.
  4. Ertesi günün kararları KAP filtresiyle verilir; gerekçe kartları ve gün sonu raporu Telegram'a gider.
- Kaçırılan işlem günleri (VPS kapalı kaldıysa) sırayla işlenir. Aynı gün iki kez işlenmez.

Gün sonu raporu ve `/performans`, botun getirisini BIST100 ve TL mevduatıyla kıyaslar. Mevduat
oranı `settings.yaml` → `deposit` bölümünde; faiz değişince yeni oran tarihiyle eklenir.

Telegram komutları: `/portfoy`, `/neden HISSE`, `/bekleyen`, `/islemler`, `/performans`,
`/parametreler`, `/haber HISSE`, `/durdur` (yeni alım yok; stop/çıkış çalışır), `/devam`, `/yardim`.
Bot sadece `TELEGRAM_CHAT_ID` sohbetinden gelen komutlara cevap verir.

## Sabit risk kuralları (`settings.yaml` → `risk`)

- Başlangıç sermayesi 100.000 TL
- Nakit zorunluluğu yok (paranın tamamı hisselere girebilir)
- Tek hisse en fazla portföyün %28'i
- İşlem başı risk %1 (stop olunursa en fazla ~1.000 TL kayıp)
- En fazla 10 açık pozisyon
- Lot = (portföy × risk) / (giriş − ATR stop); yukarıdaki sınırlar sırayla uygulanır

## Paper broker gerçekçilik kuralları

- Emirler karar gününden sonraki seansın açılışında dolar
- Komisyon (binde 2, temkinli) + BSMV + %0.1 kayma
- Fiyatlar BIST fiyat adımına aleyhimize yuvarlanır
- Stop: gün içi en düşük stop'a değerse stop fiyatından, gap'te açılıştan
- Dolum anında nakit/tek hisse kuralları yeniden kontrol edilir

## Bilinen sınırlamalar

- `universes/bist100.txt` yaklaşık bir listedir; güncel endeks bileşimiyle doğrulanmalı.
- Bugünkü listeyle geçmişe dönük test hayatta kalma yanlılığı (survivorship bias) içerir.
