# scar_tiktok

بوت TikTok قابل للتحكم بالكامل من لوحة ويب — جاهز للرفع على VPS للزبون.

## ماذا يتحكم الزبون من الواجهة؟

- رابط البروفايل / الفيديو ووضع التشغيل
- لايك / شير / تعليق / OTP / Headless
- عدد المتصفحات ومهلة OTP و IMAP
- حسابات Hostinger وحسابات TikTok
- مجمع التعليقات
- **تشغيل** و **إيقاف** البوت + مشاهدة اللوقز مباشرة

## تشغيل على VPS

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium   # على Linux إن لزم
```

أنشئ الملفات محلياً (لا تُرفع للـ Git):

```bash
cp accounts.example.json accounts.json
cp mailboxes.example.json mailboxes.json
```

ثم:

```bash
python app.py
```

اللوحة تفتح على كل الواجهات افتراضياً: `http://IP:5050`

يمكنك تغيير المنفذ:

```bash
PORT=8080 python app.py
```

افتح البورت في الجدار الناري/Security Group.

## ملاحظة

الإعدادات تُحفظ في `settings.json` تلقائياً عند التعديل من الواجهة — لا حاجة لتعديل ملفات يدوياً بعد التثبيت الأولي للحسابات.
