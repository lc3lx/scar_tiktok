# scar_tiktok

بوت TikTok لمشاهدة فيديوهات حساب محدد، لايك، شير، وتعليق — مع لوحة تحكم ويب و OTP تلقائي من Hostinger.

## التشغيل السريع

```bash
pip install -r requirements.txt
playwright install chromium
```

انسخ ملفات الإعداد:

```bash
copy accounts.example.json accounts.json
copy mailboxes.example.json mailboxes.json
```

عدّل `accounts.json` و `mailboxes.json` و `settings.json`، ثم:

```bash
python app.py
```

افتح: http://127.0.0.1:5050

أو شغّل مباشرة:

```bash
python main.py
```

## ملاحظات أمان

لا ترفع `accounts.json` أو `mailboxes.json` أو مجلد `accounts/` (جلسات وكلمات سر) إلى GitHub.
