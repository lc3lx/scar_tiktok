"""لوحة تحكم ويب لبوت تعليقات TikTok — جاهزة لـ VPS."""
import asyncio
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from loguru import logger

from main import (
    Config,
    load_accounts_json,
    load_mailboxes,
    load_settings,
    request_stop_bot,
    run_bot,
    save_accounts_json,
    save_mailboxes,
    save_settings,
    should_stop,
)
from comments_pool import peek_status, set_comments

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "tiktok_checker.log"

app = Flask(__name__)
bot_state = {
    "running": False,
    "last_result": None,
    "error": None,
    "thread": None,
}


def account_display(acc: dict) -> dict:
    return {
        "email": acc.get("email", ""),
        "mailbox": acc.get("mailbox", ""),
        "has_mailbox": bool(acc.get("mailbox")),
    }


def setup_logging():
    logger.remove()
    logger.add(str(LOG_FILE), rotation="10 MB", level="INFO")
    logger.add(
        lambda msg: print(msg, end=""),
        colorize=True,
        level="INFO",
        format="{time:HH:mm:ss} | <level>{message}</level>",
    )


def bot_worker():
    setup_logging()
    bot_state["running"] = True
    bot_state["error"] = None
    bot_state["last_result"] = None
    try:
        config = Config.from_settings()
        result = asyncio.run(run_bot(config))
        bot_state["last_result"] = result
    except Exception as e:
        logger.exception("فشل تشغيل البوت")
        bot_state["error"] = str(e)
        bot_state["last_result"] = {"ok": False, "error": str(e)}
    finally:
        bot_state["running"] = False
        bot_state["thread"] = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    settings = load_settings()
    accounts = [account_display(a) for a in load_accounts_json()]
    mailboxes = [
        {"email": m.get("email", ""), "label": m.get("label", ""), "has_password": bool(m.get("password"))}
        for m in load_mailboxes()
    ]
    log_tail = ""
    if LOG_FILE.exists():
        try:
            text = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
            log_tail = "\n".join(text.splitlines()[-60:])
        except Exception:
            pass
    return jsonify({
        "running": bot_state["running"],
        "stopping": bool(bot_state["running"] and should_stop()),
        "last_result": bot_state["last_result"],
        "error": bot_state["error"],
        "settings": settings,
        "accounts": accounts,
        "mailboxes": mailboxes,
        "account_count": len(accounts),
        "comments": peek_status(),
        "logs": log_tail,
    })


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not bot_state["running"]:
        return jsonify({"ok": True, "message": "البوت متوقف مسبقاً"})
    request_stop_bot()
    return jsonify({"ok": True, "message": "تم إرسال أمر الإيقاف — سيتم إغلاق المتصفحات"})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(load_settings())

    data = request.get_json(force=True) or {}
    comments = data.get("comment_texts", [])
    if isinstance(comments, str):
        comments = [c.strip() for c in comments.split("\n") if c.strip()]

    replace_comments = bool(data.get("replace_comments", True))
    if "comment_texts" in data or "comments_append" in data:
        append_raw = data.get("comments_append")
        if append_raw is not None:
            if isinstance(append_raw, str):
                append_list = [c.strip() for c in append_raw.split("\n") if c.strip()]
            else:
                append_list = [c.strip() for c in (append_raw or []) if c and str(c).strip()]
            set_comments(append_list, replace=False)
        elif comments is not None:
            set_comments(comments, replace=replace_comments)

    patch = {
        "target_video_url": (data.get("target_video_url") or "").strip(),
        "profile_url": (data.get("profile_url") or "").strip(),
        "bot_mode": (data.get("bot_mode") or "watch").strip(),
        "comment_texts": peek_status()["pending"],
        "comment_all_in_order": bool(data.get("comment_all_in_order", True)),
        "enable_liking": bool(data.get("enable_liking", True)),
        "enable_commenting": bool(data.get("enable_commenting", True)),
        "enable_sharing": bool(data.get("enable_sharing", True)),
        "watch_count": int(data.get("watch_count", 0) or 0),
        "max_browsers": max(1, int(data.get("max_browsers", 1) or 1)),
        "browser_headless": bool(data.get("browser_headless", True)),
        "proxy_enabled": bool(data.get("proxy_enabled", False)),
        "proxy": (data.get("proxy") or "").strip(),
        "force_relogin": bool(data.get("force_relogin", True)),
        "auto_otp": bool(data.get("auto_otp", True)),
        "imap_host": (data.get("imap_host") or "imap.hostinger.com").strip(),
        "imap_port": int(data.get("imap_port", 993) or 993),
        "otp_timeout": int(data.get("otp_timeout", 90) or 90),
        "dashboard_host": (data.get("dashboard_host") or "0.0.0.0").strip(),
        "dashboard_port": int(data.get("dashboard_port", 5050) or 5050),
    }
    save_settings(patch)
    return jsonify({"ok": True, "settings": load_settings(), "comments": peek_status()})


@app.route("/api/comments", methods=["GET", "POST"])
def api_comments():
    if request.method == "GET":
        return jsonify(peek_status())

    data = request.get_json(force=True) or {}
    raw = data.get("comments", data.get("comment_texts", ""))
    if isinstance(raw, str):
        comments = [c.strip() for c in raw.split("\n") if c.strip()]
    else:
        comments = [str(c).strip() for c in (raw or []) if str(c).strip()]
    replace = bool(data.get("replace", False))
    stats = set_comments(comments, replace=replace)
    s = load_settings()
    s["comment_texts"] = peek_status()["pending"]
    save_settings(s)
    return jsonify({"ok": True, **stats, **peek_status()})


@app.route("/api/mailboxes", methods=["GET", "POST", "DELETE"])
def api_mailboxes():
    if request.method == "GET":
        return jsonify({
            "mailboxes": [
                {"email": m.get("email", ""), "label": m.get("label", ""), "has_password": bool(m.get("password"))}
                for m in load_mailboxes()
            ]
        })

    if request.method == "DELETE":
        data = request.get_json(force=True) or {}
        email = (data.get("email") or "").strip().lower()
        mailboxes = [m for m in load_mailboxes() if (m.get("email") or "").lower() != email]
        save_mailboxes(mailboxes)
        return jsonify({"ok": True, "mailboxes": mailboxes})

    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    label = (data.get("label") or "").strip() or email
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password required"}), 400

    mailboxes = load_mailboxes()
    updated = False
    for i, m in enumerate(mailboxes):
        if (m.get("email") or "").lower() == email.lower():
            mailboxes[i] = {"email": email, "password": password, "label": label}
            updated = True
            break
    if not updated:
        mailboxes.append({"email": email, "password": password, "label": label})
    save_mailboxes(mailboxes)
    return jsonify({"ok": True, "mailboxes": mailboxes})


@app.route("/api/accounts", methods=["GET", "POST", "DELETE"])
def api_accounts():
    if request.method == "GET":
        return jsonify({"accounts": [account_display(a) for a in load_accounts_json()]})

    if request.method == "DELETE":
        data = request.get_json(force=True) or {}
        email = (data.get("email") or "").strip().lower()
        accounts = [a for a in load_accounts_json() if (a.get("email") or "").lower() != email]
        save_accounts_json(accounts)
        return jsonify({"ok": True, "accounts": [account_display(a) for a in accounts]})

    data = request.get_json(force=True) or {}
    mailbox = (data.get("mailbox") or "").strip()

    if "bulk" in data:
        raw = data.get("bulk") or ""
        if not mailbox:
            return jsonify({"ok": False, "error": "mailbox required"}), 400
        if not resolve_mailbox_exists(mailbox):
            return jsonify({"ok": False, "error": "mailbox_not_found"}), 400

        replace = bool(data.get("replace", False))
        accounts = [] if replace else load_accounts_json()
        existing = {(a.get("email") or "").lower(): i for i, a in enumerate(accounts)}

        for line in raw.splitlines():
            line = line.strip()
            if not line or ":" not in line or line.startswith("#"):
                continue
            parts = line.split(":", 1)
            email = parts[0].strip()
            password = parts[1].strip()
            if not email or not password:
                continue
            entry = {"email": email, "password": password, "mailbox": mailbox}
            key = email.lower()
            if key in existing:
                accounts[existing[key]] = entry
            else:
                existing[key] = len(accounts)
                accounts.append(entry)

        save_accounts_json(accounts)
        return jsonify({"ok": True, "accounts": [account_display(a) for a in accounts]})

    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password required"}), 400
    if not mailbox:
        return jsonify({"ok": False, "error": "mailbox required"}), 400
    if not resolve_mailbox_exists(mailbox):
        return jsonify({"ok": False, "error": "mailbox_not_found"}), 400

    entry = {"email": email, "password": password, "mailbox": mailbox}
    accounts = load_accounts_json()
    updated = False
    for i, a in enumerate(accounts):
        if (a.get("email") or "").lower() == email.lower():
            accounts[i] = entry
            updated = True
            break
    if not updated:
        accounts.append(entry)
    save_accounts_json(accounts)
    return jsonify({"ok": True, "accounts": [account_display(a) for a in accounts]})


def resolve_mailbox_exists(email: str) -> bool:
    email = email.lower()
    return any((m.get("email") or "").lower() == email for m in load_mailboxes())


@app.route("/api/start", methods=["POST"])
def api_start():
    if bot_state["running"]:
        return jsonify({"ok": False, "error": "already_running"}), 409

    settings = load_settings()
    mode = settings.get("bot_mode", "watch")
    if mode in ("watch", "watch_comment"):
        if not settings.get("profile_url") and not settings.get("target_video_url"):
            return jsonify({"ok": False, "error": "no_profile_url"}), 400
    elif not settings.get("target_video_url"):
        return jsonify({"ok": False, "error": "no_video_url"}), 400
    if not load_accounts_json():
        return jsonify({"ok": False, "error": "no_accounts"}), 400

    t = threading.Thread(target=bot_worker, daemon=True)
    bot_state["thread"] = t
    t.start()
    return jsonify({"ok": True, "message": "started"})


@app.route("/api/logs")
def api_logs():
    if not LOG_FILE.exists():
        return jsonify({"logs": ""})
    text = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    return jsonify({"logs": "\n".join(text.splitlines()[-80:])})


if __name__ == "__main__":
    setup_logging()
    settings = load_settings()
    host = os.environ.get("HOST") or settings.get("dashboard_host") or "0.0.0.0"
    port = int(os.environ.get("PORT") or settings.get("dashboard_port") or 5050)
    print("=" * 50)
    print("  TikTok Bot Dashboard")
    print(f"  http://{host}:{port}")
    print("  (VPS: افتح البورت في الجدار الناري)")
    print("=" * 50)
    app.run(host=host, port=port, debug=False, use_reloader=False)
