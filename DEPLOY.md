# VPS kurulum rehberi (Ubuntu 24.04)

Bu rehber botu sıfırdan bir VPS'e kurar. Komutları sırayla kopyalayıp yapıştırın.
`<...>` ile gösterilen yerleri kendi bilgilerinizle değiştirin.

**Gerekenler**
- KVM sanallaştırmalı, Ubuntu 24.04 kurulu bir VPS ve sağlayıcının verdiği IP adresi ile root şifresi
- Telegram bot token'ı ve chat ID
- Claude API anahtarı (KAP filtresi için; yoksa bot filtresiz çalışır)

---

## 1. VPS'e bağlanma

Windows'ta PowerShell, Mac/Linux'ta Terminal açın:

```bash
ssh root@<VPS_IP>
```

İlk bağlantıda "Are you sure you want to continue connecting" sorusuna `yes` yazın, ardından root şifresini girin.
Şifre yazarken ekranda bir şey görünmez; bu normaldir.

## 2. Sistemi güncelleme ve temel güvenlik

```bash
apt update && apt upgrade -y
```

Root yerine kullanılacak bir kullanıcı oluşturun (şifre sorar):

```bash
adduser bot
```

```bash
usermod -aG sudo bot
```

Güvenlik duvarı: sadece SSH'a izin ver. Bot dışarıya port açmaz; Telegram'a kendisi bağlanır.

```bash
ufw allow OpenSSH && ufw --force enable
```

3 GB RAM'de ay başı öğrenmesi sırasında güvenlik payı olsun diye 2 GB swap ekleyin:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Yeni kullanıcıya geçin. Bundan sonraki her şey bu kullanıcıyla yapılır:

```bash
su - bot
```

## 3. Docker kurulumu

```bash
sudo apt-get install -y ca-certificates curl git
```

```bash
sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

```bash
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Kullanıcınızın `sudo` yazmadan Docker kullanabilmesi için:

```bash
sudo usermod -aG docker bot
```

Bu ayarın geçerli olması için oturumu kapatıp tekrar açın:

```bash
exit
```

```bash
su - bot
```

Kontrol: aşağıdaki komut "Hello from Docker!" yazmalı.

```bash
docker run --rm hello-world
```

## 4. Botu indirme

```bash
git clone https://github.com/jelater4p/davidfinch.git && cd davidfinch
```

> Depo private ise GitHub kullanıcı adınızı ve şifre yerine bir "Personal access token" ister.

## 5. Bot klasörünü hazırlama

```bash
./deploy/new-bot.sh momentum
```

Gizli bilgileri girin:

```bash
nano bots/momentum/.env
```

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
ANTHROPIC_API_KEY=sk-ant-...
```

Kaydetmek için `Ctrl+O`, `Enter`; çıkmak için `Ctrl+X`.

Ayarlar (`bots/momentum/settings.yaml`) varsayılan olarak bugüne kadar konuştuğumuz değerlerle gelir.
Komisyon oranınızı öğrendiyseniz `costs.commission_rate` satırını güncelleyin.

## 6. İmajı derleme

```bash
docker compose build
```

## 7. Bağlantı testleri

Telegram: telefonunuza "✅ Telegram bağlantısı çalışıyor" mesajı gelmeli.

```bash
docker compose run --rm momentum python -m bot telegram-test
```

KAP: çıktının tamamını bana gönderin, gerekirse veri okuma kısmını buna göre düzeltirim.

```bash
docker compose run --rm momentum python -m bot kap-check
```

İlk veri çekimi: yaklaşık 9 yıllık geçmiş. Aylık öğrenme döngüsü 24 aylık geçmişe ihtiyaç duyar. Birkaç dakika sürer.

```bash
docker compose run --rm momentum python -m bot fetch --days 3300
```

Claude ile bir hissenin son bildirimlerini deneyin:

```bash
docker compose run --rm momentum python -m bot news THYAO
```

## 8. Botu başlatma

```bash
docker compose up -d
```

Telegram'a "🤖 davidfinch paper bot başladı" mesajı gelir. Bota `/yardim` yazın.

Bot ilk akşam işini bir sonraki işlem günü 19:00'da yapar. İlk alımlar ayın ilk işlem gününü
(aylık yenileme) ya da haftanın ilk işlem gününü (boş yer doldurma) bekler.

---

## Günlük kullanım

| Ne | Komut |
|---|---|
| Bot çalışıyor mu? | `docker compose ps` (STATUS sütununda `healthy` görünmeli) |
| Son loglar | `docker compose logs --tail 100 momentum` |
| Canlı log izleme | `docker compose logs -f momentum` (çıkış: `Ctrl+C`) |
| Cüzdan durumu | `docker compose exec momentum python -m bot status` |
| Sabah dolumunu elle çalıştır | `docker compose exec momentum python -m bot morning-fill --send` |
| Yeniden başlatma | `docker compose restart momentum` |
| Durdurma | `docker compose stop momentum` |

Bot çökerse veya VPS yeniden başlarsa Docker botu otomatik olarak yeniden başlatır.

## Güncelleme (yeni sürüm geldiğinde)

```bash
cd ~/davidfinch && git pull && docker compose up -d --build
```

Veritabanınız ve ayarlarınız `bots/` klasöründe durur; güncellemeden etkilenmez.

## Yedekler

- Her akşam işinden sonra `bots/momentum/data/backups/` klasörüne veritabanının bir kopyası alınır.
- Son 14 gün saklanır.

Yedekleri kendi bilgisayarınıza indirmek için (kendi bilgisayarınızda çalıştırın):

```bash
scp -r bot@<VPS_IP>:~/davidfinch/bots/momentum/data/backups ./davidfinch-yedek
```

Bir yedeği geri yüklemek için:

```bash
docker compose stop momentum && cp bots/momentum/data/backups/davidfinch-<TARİH>.db bots/momentum/data/davidfinch.db && docker compose start momentum
```

## İkinci bir bot ekleme

1. BotFather'dan yeni bir bot oluşturun (yeni token).
2. Bot klasörünü hazırlayın:

```bash
./deploy/new-bot.sh momentum-risk2
```

3. `bots/momentum-risk2/.env` dosyasını doldurun. `settings.yaml` içinde istediğinizi değiştirin (ör. `risk_per_trade: 0.02`).
4. `docker-compose.yml` içindeki yorum satırı haline getirilmiş `momentum-risk2` bloğunu açın. `EVENING_OFFSET_MIN` değeri `10` olsun ki iki bot Yahoo'ya aynı anda gitmesin.
5. Veriyi çekin ve botu başlatın:

```bash
docker compose run --rm momentum-risk2 python -m bot fetch --days 3300 && docker compose up -d
```

3 GB RAM ile 2-3 bot rahat çalışır. Her bot boşta yaklaşık 60 MB bellek kullanır, ay başı öğrenmesinde kısa süreliğine daha fazla.

## Sorun giderme

| Belirti | Ne yapmalı |
|---|---|
| Telegram mesajı gelmiyor | `docker compose logs momentum` çıktısında "Telegram" geçen satırlara bakın. Token ve chat ID'yi kontrol edin. Botunuza en az bir kez `/start` yazmış olmalısınız |
| "KAP kontrolü yapılamadı" uyarısı | KAP siteyi değiştirmiş olabilir. `kap-check` çıktısını gönderin. Bot bu sırada filtresiz çalışmaya devam eder |
| "kapanış verisi henüz yok" | Yahoo geç güncellemiştir; bot 22:00'a kadar tekrar dener. Resmi tatillerde normaldir |
| `docker compose ps` içinde `unhealthy` | Bot 30 dakikadır kalp atışı yazmamış. `docker compose restart momentum` deneyin ve logları gönderin |
| Disk doluyor | `docker system prune -f` eski imajları temizler |
