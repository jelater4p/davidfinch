# davidfinch

BIST100 üzerinde paper (sanal) işlem yapan, kararlarını gerekçelendiren ve
geçmiş işlemlerinden öğrenerek parametrelerini ayarlayan trade botu.

## Durum

| Aşama | İçerik | Durum |
|---|---|---|
| 1 | Ayarlar, SQLite, Yahoo verisi, PaperBroker, risk kuralları | ✅ |
| 2 | Strateji, gerekçe kartı, backtest | ✅ |
| 3 | Walk-forward + aylık öğrenme | ✅ |
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
python -m bot walkforward --start 2022-01-01   # aylık öğrenmeyi geçmişte sına
python -m bot learn           # aylık parametre güncellemesi (canlı paper)
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
- Arama alanı: `lookback` {63,126,252}, `hold_rank` {10,15,20}, `atr_stop_mult` {2.0 … 4.0}.
- Fren 1: her parametre bir ayda en fazla bir ızgara adımı oynar.
- Fren 2: Sharpe en az 0.20 iyileşmiyorsa değişiklik yapılmaz.
- Her ayın kararı gerekçesiyle `param_history` tablosuna yazılır.

`walkforward` komutu bu döngüyü geçmişte, her ay sadece o güne kadarki veriyle çalıştırır ve
sabit parametreli sonuçla kıyaslar.

İkincil strateji (`pullback`, pasif): trend içinde geri çekilmeden al.
`python -m bot backtest --strategy pullback` ile çalıştırılabilir.

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

- `data/bist100.txt` yaklaşık bir listedir; güncel endeks bileşimiyle doğrulanmalı.
- Bugünkü listeyle geçmişe dönük test hayatta kalma yanlılığı (survivorship bias) içerir.
- Paper pozisyon taşınırken bedelsiz/bölünme olursa lot ayarlaması henüz yok (2. aşamada).
