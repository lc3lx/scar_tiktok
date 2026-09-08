# Instagram Engagement Bot

بوت إنستغرام مع لوحة تحكم Flask — جاهز لـ VPS عبر PM2.

## ماذا يفعل؟

- تسجيل دخول بحسابات Instagram (مع OTP من Hostinger عند الحاجة)
- فتح **بروفايل**: لايك للمنشورات إن لم يكن معجباً، تعليق من المجمع، محاولة Add to story
- فتح **منشور/ريل محدد**: لايك + تعليق + ستوري/شير
- إيقاف فوري من الواجهة

## أوضاع التشغيل

| الوضع | الوصف |
|--------|--------|
| `full` | بروفايل + منشور محدد |
| `profile` | بروفايل فقط |
| `video` | منشور/ريل محدد فقط |

## تشغيل محلي

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp settings.example.json settings.json
cp accounts.example.json accounts.json
cp mailboxes.example.json mailboxes.json
python app.py
```

افتح: http://127.0.0.1:5050

## VPS + PM2

```bash
cd /home/web/tik
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# أوقف النسخة القديمة إن وُجدت
pm2 delete scar-tiktok 2>/dev/null || true

pm2 start ecosystem.config.cjs
pm2 save
```

الواجهة: `http://YOUR_VPS_IP:5050`

## ملاحظات

- خيار **Add to story** أحياناً غير متاح على ويب إنستغرام؛ البوت يحاول ثم يستخدم Copy link كبديل ويسجّل ذلك.
- يُفضّل `max_browsers=1` مع صندوق OTP مشترك.
- لا ترفع `accounts.json` / `mailboxes.json` / `settings.json` إلى Git.
