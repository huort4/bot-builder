# AIDahl Optimized (v2.0.0)

نسخهٔ بازنویسی‌شده و ماژولار ربات AIDahl.

## تغییرات اصلی نسبت به نسخه ۱

| مورد | قبل | بعد |
|------|-----|-----|
| ساختار | یک فایل ۵۱۰۰ خطی | ماژولار (config / services / handlers / keyboards) |
| کلیدهای API | plain text | اختیاری رمزنگاری با Fernet |
| Callback handler | ~۶۸۰ خط در یک تابع | جدا و خوانا |
| Type hints | ناقص | تقریباً کامل |
| خوانایی | سخت | بسیار بهتر |
| قابلیت‌ها | کامل | همان قابلیت‌ها حفظ شده |

## ساختار

```
AIDahl-Optimized/
├── main.py
├── requirements.txt
├── .env.example
├── bot/
│   ├── config.py
│   ├── db/
│   │   └── supabase.py
│   ├── services/
│   │   ├── users.py          # settings, keys, quotas
│   │   ├── conversations.py
│   │   ├── llm.py
│   │   └── manus.py
│   ├── handlers/
│   │   ├── commands.py
│   │   ├── callbacks.py
│   │   └── chat.py
│   ├── keyboards/
│   │   └── __init__.py
│   └── utils/
│       ├── helpers.py
│       └── crypto.py
├── sql/                      # همان اسکریپت‌های قبلی
└── privacy_policy.md
```

## راه‌اندازی

1. کپی `.env.example` → `.env` و پر کردن مقادیر
2. اجرای SQLها به ترتیب در Supabase
3. (اختیاری) ساخت کلید رمزنگاری:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   مقدار را در `ENCRYPTION_KEY` بگذارید.
4. نصب و اجرا:
   ```bash
   pip install -r requirements.txt
   python main.py
   ```

## دستورات

همان دستورات نسخه قبلی:
`/start` `/settings` `/keys` `/manus` `/chats` `/clear` `/usage` `/admin` `/mode` …

## نکات

- صف Manus هنوز در حافظه است (مناسب هاست تک‌کانتینری رایگان).
- برای مقیاس بالاتر می‌توان صف را به Redis منتقل کرد.
- همه قابلیت‌های اصلی (BYOK، استریم، سهمیه، Manus multi-turn، پاک‌سازی تاریخچه) حفظ شده‌اند.
