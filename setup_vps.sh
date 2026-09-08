#!/usr/bin/env bash
# إعداد أولي على VPS ثم تشغيل عبر PM2
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install chromium

if [ ! -f accounts.json ]; then
  cp accounts.example.json accounts.json
  echo "أنشئ accounts.json — عبّه من الواجهة"
fi
if [ ! -f mailboxes.json ]; then
  cp mailboxes.example.json mailboxes.json
  echo "أنشئ mailboxes.json — عبّه من الواجهة"
fi
if [ ! -f settings.json ]; then
  cp settings.example.json settings.json
fi

echo "جاهز. شغّل:"
echo "  pm2 delete scar-tiktok 2>/dev/null || true"
echo "  pm2 start ecosystem.config.cjs"
echo "  pm2 save"
echo "  pm2 startup"
