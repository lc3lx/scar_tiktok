#!/usr/bin/env bash
# إعداد أولي على VPS ثم تشغيل عبر PM2 (متصفح نظامي + Xvfb)
set -e
cd "$(dirname "$0")"

sudo apt-get update -y
sudo apt-get install -y xvfb fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0

# كروم نظامي إن أمكن
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium || true
fi

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium || true

# شاشة وهمية دائمة
if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1920x1080x24 -ac +extension RANDR >/tmp/xvfb99.log 2>&1 &
  echo "Xvfb started on :99"
fi
export DISPLAY=:99

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

python3 - <<'PY'
import json
p="settings.json"
s=json.load(open(p,encoding="utf-8"))
s["proxy_enabled"]=False
s["proxy"]=""
s["browser_headless"]=False
s["force_relogin"]=False
json.dump(s, open(p,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
print("settings: headed + proxy OFF")
PY

echo "جاهز. شغّل:"
echo "  pm2 delete scar-instagram scar-tiktok 2>/dev/null || true"
echo "  DISPLAY=:99 pm2 start ecosystem.config.cjs"
echo "  pm2 save"
echo "  pm2 startup"
