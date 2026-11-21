#!/usr/bin/env bash
# Yeni bot klasörü hazırlar: bots/<ad>/{settings.yaml,.env,data/}
# Kullanım: ./deploy/new-bot.sh momentum
set -euo pipefail
NAME="${1:?Kullanım: $0 <bot-adı>}"
DIR="bots/$NAME"
if [ -e "$DIR" ]; then
  echo "$DIR zaten var, dokunulmadı."; exit 1
fi
mkdir -p "$DIR/data"
cp settings.yaml "$DIR/settings.yaml"
cat > "$DIR/.env" <<ENV
# $NAME botunun gizli bilgileri — bu dosyayı kimseyle paylaşmayın
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
ENV
chmod 600 "$DIR/.env"
# Konteyner kullanıcısı (uid 1000) sadece data/ klasörüne yazar; .env sizde kalır.
sudo chown -R 1000:1000 "$DIR/data" 2>/dev/null || chown -R 1000:1000 "$DIR/data"
echo "Hazır: $DIR"
echo "1) $DIR/.env dosyasını doldurun:   nano $DIR/.env"
echo "2) İsterseniz ayarları değiştirin: nano $DIR/settings.yaml"
