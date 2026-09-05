#!/usr/bin/env bash
# إعداد أولي على VPS ثم تشغيل عبر PM2
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install chromium
# على Ubuntu/Debian غالباً تحتاج:
# playwright install-deps chromium

if [ ! -f accounts.json ]; then
  cp accounts.example.json accounts.json
  echo "أنشئ accounts.json — عبّه من الواجهة أو عدّل الملف"
fi
if [ ! -f mailboxes.json ]; then
  cp mailboxes.example.json mailboxes.json
  echo "أنشئ mailboxes.json — عبّه من الواجهة أو عدّل الملف"
fi

echo "جاهز. شغّل:"
echo "  source .venv/bin/activate"
echo "  pm2 start ecosystem.config.cjs"
echo "  pm2 save"
echo "  pm2 startup"
