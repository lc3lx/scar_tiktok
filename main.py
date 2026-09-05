import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Any, Optional
from loguru import logger
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth
from tiktok_captcha_solver import AsyncPlaywrightSolver

from email_otp import wait_for_otp
from comments_pool import take_comment, remaining_count, migrate_from_settings, peek_status

BOT_VERSION = "2026-09-05-comment-v3"

# #region agent log
_DEBUG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug-8e9bfe.log")

STOP_BOT_FLAG = False
ACTIVE_BROWSERS: List[Any] = []
_BOT_LOOP = None  # event loop الخاص بتشغيل البوت


def request_stop_bot():
    """طلب إيقاف البوت من الواجهة."""
    global STOP_BOT_FLAG
    STOP_BOT_FLAG = True
    logger.warning("تم طلب إيقاف البوت من لوحة التحكم")
    loop = _BOT_LOOP
    if loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(_close_all_browsers(), loop)
            fut.result(timeout=8)
        except Exception as e:
            logger.warning(f"إغلاق المتصفحات أثناء الإيقاف: {e}")


def should_stop() -> bool:
    return STOP_BOT_FLAG


async def _close_all_browsers():
    for browser in list(ACTIVE_BROWSERS):
        try:
            await browser.close()
        except Exception:
            pass
    ACTIVE_BROWSERS.clear()


async def unregister_browser(browser):
    try:
        if browser in ACTIVE_BROWSERS:
            ACTIVE_BROWSERS.remove(browser)
    except Exception:
        pass
    try:
        await browser.close()
    except Exception:
        pass


async def launch_browser(playwright, config: "Config"):
    """يشغّل متصفح مناسب للجهاز (Edge على ويندوز، Chrome/Chromium على VPS)."""
    kwargs = {
        "headless": config.browser_headless,
        "args": list(config.browser_args),
    }
    proxy_cfg = None
    if getattr(config, "proxy_enabled", False) and getattr(config, "proxy", ""):
        proxy_cfg = parse_proxy(config.proxy)
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg
            logger.info(f"استخدام بروكسي: {proxy_cfg.get('server')} (user={'نعم' if proxy_cfg.get('username') else 'لا'})")
        else:
            logger.warning("صيغة البروكسي غير صحيحة — سيتم التشغيل بدون بروكسي")

    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome_win = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if os.path.exists(edge):
        kwargs["executable_path"] = edge
        browser = await playwright.chromium.launch(**kwargs)
    elif os.path.exists(chrome_win):
        kwargs["executable_path"] = chrome_win
        browser = await playwright.chromium.launch(**kwargs)
    else:
        # Linux VPS: جرّب chrome ثم chromium الافتراضي من Playwright
        try:
            browser = await playwright.chromium.launch(channel="chrome", **kwargs)
        except Exception:
            browser = await playwright.chromium.launch(**kwargs)
    ACTIVE_BROWSERS.append(browser)
    return browser


def _dbg(hypothesis_id: str, location: str, message: str, data: dict = None, run_id: str = "pre-fix"):
    try:
        import json as _json
        import time as _time
        payload = {
            "sessionId": "8e9bfe",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(_time.time() * 1000),
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
MAILBOXES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mailboxes.json")


def load_mailboxes() -> List[Dict]:
    if not os.path.exists(MAILBOXES_FILE):
        return []
    try:
        with open(MAILBOXES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"فشل قراءة mailboxes.json: {e}")
        return []


def save_mailboxes(mailboxes: List[Dict]) -> None:
    with open(MAILBOXES_FILE, "w", encoding="utf-8") as f:
        json.dump(mailboxes, f, ensure_ascii=False, indent=2)


def load_accounts_json() -> List[Dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"فشل قراءة accounts.json: {e}")
        return []


def save_accounts_json(accounts: List[Dict]) -> None:
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def resolve_mailbox(mailbox_email: str) -> Optional[Dict]:
    mailbox_email = (mailbox_email or "").strip().lower()
    for m in load_mailboxes():
        if (m.get("email") or "").strip().lower() == mailbox_email:
            return m
    return None


def parse_proxy(raw: str) -> Optional[Dict[str, str]]:
    """يفهم صيغ: host:port:user:pass أو http://user:pass@host:port أو host:port"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
        # http://user:pass@host:port
        try:
            from urllib.parse import urlparse
            u = urlparse(raw)
            if not u.hostname or not u.port:
                return None
            out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
            if u.username:
                out["username"] = u.username
            if u.password:
                out["password"] = u.password
            return out
        except Exception:
            return None
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        return {"server": f"http://{host}:{port}"}
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])  # password قد يحتوي :
        return {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": password,
        }
    return None


def load_settings() -> dict:
    defaults = {
        "target_video_url": "",
        "profile_url": "",
        "bot_mode": "watch",  # comment | watch
        "comment_texts": [],
        "comment_all_in_order": True,
        "enable_liking": True,
        "enable_commenting": True,
        "enable_sharing": True,
        "watch_count": 0,  # 0 = لا نهائي
        "max_browsers": 1,
        "browser_headless": True,
        "imap_host": "imap.hostinger.com",
        "imap_port": 993,
        "otp_timeout": 90,
        "auto_otp": True,
        "dashboard_host": "0.0.0.0",
        "dashboard_port": 5050,
        "proxy_enabled": False,
        "proxy": "",  # host:port:user:pass
        "force_relogin": True,  # تجاهل الجلسات القديمة وإعادة تسجيل الدخول
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update(data)
        except Exception as e:
            logger.warning(f"فشل قراءة settings.json: {e}")
    return defaults


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)


@dataclass
class Config:
    """Класс для управления настройками скрипта"""
    sadcaptcha_api_key: str = ""

    # ===== إعدادات الفيديو المستهدف =====
    target_video_url: str = "https://www.tiktok.com/@scaralphaai/video/7680704444190821654"
    # رابط بروفايل الشخص للمراقبة المستمرة
    profile_url: str = "https://www.tiktok.com/@scaralphaai"
    # comment = تعليق على فيديو محدد | watch = مشاهدة وقلب فيديوهات الحساب
    bot_mode: str = "watch"

    # ===== قائمة التعليقات =====
    comment_all_in_order: bool = True
    comment_texts: List[str] = field(default_factory=lambda: [
        "بطل",
        "وحش",
    ])

    # Пути к файлам
    accounts_filename: str = "acc.txt"
    output_dir: str = "accounts"
    log_filename: str = "tiktok_checker.log"

    # Параметры браузера
    max_browsers: int = 2
    browser_headless: bool = False
    max_check_attempts: int = 1
    proxy_enabled: bool = False
    proxy: str = ""  # host:port:user:pass
    force_relogin: bool = True

    # Таймауты (в секундах)
    page_timeout: int = 45
    action_delay: float = 1.0
    comment_delay: float = 2.0

    # Включение/отключение действий
    enable_commenting: bool = True
    enable_reply_commenting: bool = False
    enable_liking: bool = True
    enable_sharing: bool = True
    enable_next_video: bool = True

    # مشاهدة مستمرة: 0 = لا نهائي
    watch_count: int = 0

    enable_comment_loop: bool = False
    comment_loop_count: int = 1
    comment_loop_delay: int = 3

    comment_text: str = "🔥🔥🔥"

    enable_hanging: bool = False
    hang_check_interval: int = 60

    # Hostinger IMAP OTP
    auto_otp: bool = True
    imap_host: str = "imap.hostinger.com"
    imap_port: int = 993
    otp_timeout: int = 90

    # Аргументы для запуска браузера
    browser_args: List[str] = field(default_factory=lambda: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-extensions',
        '--disable-setuid-sandbox',
        '--disable-infobars',
        '--disable-default-apps',
        '--no-first-run',
        '--disable-blink-features=AutomationControlled',
    ])

    # Настройки контекста браузера
    browser_context_options: Dict[str, Any] = field(default_factory=lambda: {
        'viewport': {'width': 1280, 'height': 800},
        'ignore_https_errors': True,
        'java_script_enabled': True,
        'locale': 'en-US',
        'timezone_id': 'America/New_York',
        'user_agent': (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        'extra_http_headers': {
            'Accept-Language': 'en-US,en;q=0.9',
        },
    })

    @classmethod
    def from_settings(cls) -> "Config":
        s = load_settings()
        cfg = cls()
        cfg.target_video_url = s.get("target_video_url", cfg.target_video_url)
        cfg.profile_url = s.get("profile_url", cfg.profile_url)
        # لو profile فاضي أو رابط فيديو ناقص — استخرج الحساب من target_video_url
        raw_profile = (cfg.profile_url or "").strip()
        raw_video = (cfg.target_video_url or "").strip()
        m = re.search(r"tiktok\.com/@([^/?]+)", raw_profile or raw_video or "", re.IGNORECASE)
        if m:
            cfg.profile_url = f"https://www.tiktok.com/@{m.group(1)}"
        cfg.bot_mode = s.get("bot_mode", cfg.bot_mode)
        cfg.comment_texts = s.get("comment_texts", cfg.comment_texts) or cfg.comment_texts
        migrate_from_settings(cfg.comment_texts)
        cfg.comment_all_in_order = bool(s.get("comment_all_in_order", True))
        cfg.enable_liking = bool(s.get("enable_liking", True))
        cfg.enable_commenting = bool(s.get("enable_commenting", True))
        cfg.enable_sharing = bool(s.get("enable_sharing", True))
        cfg.watch_count = int(s.get("watch_count", 0))
        cfg.max_browsers = int(s.get("max_browsers", 2))
        cfg.browser_headless = bool(s.get("browser_headless", False))
        cfg.proxy_enabled = bool(s.get("proxy_enabled", False))
        cfg.proxy = (s.get("proxy") or "").strip()
        cfg.force_relogin = bool(s.get("force_relogin", True))
        cfg.auto_otp = bool(s.get("auto_otp", True))
        cfg.imap_host = s.get("imap_host", cfg.imap_host)
        cfg.imap_port = int(s.get("imap_port", 993))
        cfg.otp_timeout = int(s.get("otp_timeout", 90))
        return cfg


class Stats:
    """Класс для отслеживания статистики по действиям"""

    def __init__(self):
        self.counters = {
            'total_accounts': 0,
            'processed': 0,
            'successful': 0,
            'failed': 0,
            'errors': 0,
            'comments': 0,
            'replies': 0,
            'likes': 0,
            'shares': 0,
            'watched': 0,
            'next_videos': 0,
            'comment_loops': 0,  # Количество выполненных циклов комментирования
            'comments_per_video': {},  # Статистика по комментариям на каждое видео
        }
        self.start_time = datetime.now()
        self.lock = asyncio.Lock()

    async def increment(self, key: str, value: int = 1):
        """Безопасно увеличивает счетчик"""
        async with self.lock:
            self.counters[key] = self.counters.get(key, 0) + value

    async def get_report(self) -> str:
        """Генерирует строку с текущей статистикой"""
        async with self.lock:
            runtime = datetime.now() - self.start_time
            report = f"Статистика:\n"
            report += f"Время работы: {runtime}\n"
            report += f"Обработано: {self.counters['processed']}/{self.counters['total_accounts']} | "
            report += f"Успешно: {self.counters['successful']} | "
            report += f"Неуспешно: {self.counters['failed']} | "
            report += f"Ошибки: {self.counters['errors']}\n"

            if any(self.counters.get(k, 0) > 0 for k in ['comments', 'replies', 'likes', 'next_videos', 'shares', 'watched']):
                report += f"Действия: "
                report += f"Комментарии: {self.counters.get('comments', 0)} | "
                report += f"Ответы: {self.counters.get('replies', 0)} | "
                report += f"Лайки: {self.counters.get('likes', 0)} | "
                report += f"شير: {self.counters.get('shares', 0)} | "
                report += f"مشاهدات: {self.counters.get('watched', 0)} | "
                report += f"Переходы: {self.counters.get('next_videos', 0)}"

            if self.counters.get('comment_loops', 0) > 0:
                report += f"\nЦиклы комментирования: {self.counters.get('comment_loops', 0)}"

            # Статистика по видео
            if self.counters.get('comments_per_video', {}):
                report += "\nСтатистика по видео:"
                for video_id, count in self.counters.get('comments_per_video', {}).items():
                    report += f"\n - {video_id}: {count} комментариев"

            return report


class FileHandler:
    """Класс для работы с файлами учетных записей"""

    def __init__(self, config: Config):
        self.config = config
        os.makedirs(config.output_dir, exist_ok=True)

    def save_account(self, email: str, password: str, cookies: List[Dict]) -> bool:
        """Сохраняет информацию об успешном входе в аккаунт"""
        safe_filename = f"{self.config.output_dir}/{email.replace(':', '_')}.txt"

        try:
            with open(safe_filename, 'w', encoding='utf-8') as f:
                f.write(f"{email}:{password}\n")
                f.write("Успешный вход - скрипт находится в режиме ожидания")

            logger.info(f"Аккаунт {email} - ВАЛИДНЫЙ ✓ | Сохранен в {safe_filename}")
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения аккаунта {email}: {type(e).__name__}: {str(e)}")
            return False

    def read_accounts(self) -> List[Dict]:
        """يقرأ الحسابات من accounts.json (مع ربط Hostinger) أو acc.txt كاحتياط."""
        accounts = []
        mailboxes = { (m.get("email") or "").lower(): m for m in load_mailboxes() }

        json_accounts = load_accounts_json()
        if json_accounts:
            for item in json_accounts:
                email = (item.get("email") or "").strip()
                password = (item.get("password") or "").strip()
                if not email or not password:
                    continue
                mailbox_email = (item.get("mailbox") or "").strip().lower()
                mailbox = mailboxes.get(mailbox_email) if mailbox_email else None
                if not mailbox and len(mailboxes) == 1:
                    # لو في صندوق واحد فقط، استخدمه تلقائياً
                    mailbox = next(iter(mailboxes.values()))
                    mailbox_email = mailbox.get("email", "")
                accounts.append({
                    "email": email,
                    "password": password,
                    "mailbox": mailbox_email or (mailbox or {}).get("email", ""),
                    "mailbox_email": (mailbox or {}).get("email", mailbox_email),
                    "email_password": (mailbox or {}).get("password") or password,
                })
            logger.info(f"تم تحميل {len(accounts)} حساب من accounts.json")
            return accounts

        # توافق مع acc.txt القديم
        try:
            with open(self.config.accounts_filename, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or ':' not in line:
                        continue
                    parts = line.split(':')
                    email = parts[0]
                    if len(parts) >= 3:
                        email_password = parts[-1]
                        password = ':'.join(parts[1:-1])
                    else:
                        password = parts[1]
                        email_password = password
                    # لو في mailbox افتراضي
                    if mailboxes and len(mailboxes) == 1:
                        mb = next(iter(mailboxes.values()))
                        accounts.append({
                            "email": email,
                            "password": password,
                            "mailbox": mb.get("email", ""),
                            "mailbox_email": mb.get("email", ""),
                            "email_password": mb.get("password") or email_password,
                        })
                    else:
                        accounts.append({
                            "email": email,
                            "password": password,
                            "mailbox": email,
                            "mailbox_email": email,
                            "email_password": email_password,
                        })
            logger.info(f"Загружено {len(accounts)} аккаунтов из {self.config.accounts_filename}")
        except Exception as e:
            logger.error(f"Ошибка чтения аккаунтов: {type(e).__name__}: {str(e)}")
        return accounts

    def session_path(self, email: str) -> str:
        safe = email.replace(':', '_').replace('@', '_at_')
        return os.path.join(self.config.output_dir, f"{safe}_session.json")

    def has_session(self, email: str) -> bool:
        return os.path.exists(self.session_path(email))


class TikTokActions:
    """Класс для выполнения действий на TikTok"""

    def __init__(self, page: Page, config: Config, stats: Stats):
        self.page = page
        self.config = config
        self.stats = stats
        self.current_video_id = "unknown"  # Идентификатор текущего видео для отслеживания
        import random
        self.random = random

    def get_comment_text(self, account_email: str = "") -> Optional[str]:
        """يأخذ تعليقاً من المجمع ويحذفه حتى لا يستخدمه حساب آخر."""
        text = take_comment(account_email or "")
        if text:
            return text
        logger.warning(f"[{account_email}] مجمع التعليقات فارغ")
        return None

    async def find_first(self, selectors: List[str], timeout_ms: int = 8000):
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return loc
            except Exception:
                continue
        return None

    async def dismiss_overlays(self):
        for sel in [
            'button[data-e2e="modal-close-inner-button"]',
            'button[aria-label="Close"]',
            '[data-e2e="browse-close"]',
            'div[class*="DivCloseWrapper"]',
            'button:has-text("Refresh")',
            'button:has-text("Try again")',
            'button:has-text("OK")',
            'button:has-text("Got it")',
            'button:has-text("Continue")',
            'div[role="button"]:has-text("Refresh")',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1500)
                    await asyncio.sleep(0.5)
            except Exception:
                continue
        # أغلق أي dialog بنص refresh
        try:
            await self.page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                    const hit = btns.find(b => /refresh|try again|reload|ok|got it/i.test((b.innerText||'').trim()));
                    if (hit) hit.click();
                }"""
            )
        except Exception:
            pass

    async def has_playback_error(self) -> bool:
        try:
            body = await self.page.inner_text("body")
            # #region agent log
            snippet = ""
            for pat in ("trouble playing", "Please refresh", "try again", "We're having"):
                i = body.lower().find(pat.lower())
                if i >= 0:
                    snippet = body[max(0, i - 40): i + 80].replace("\n", " ")
                    break
            matched = bool(re.search(
                r"trouble playing|Please refresh|try again|can't play|unable to play|تعذر|حدّث|حدث خطأ",
                body,
                re.IGNORECASE,
            ))
            _dbg("A", "main.py:has_playback_error", "playback_error_scan", {
                "matched": matched,
                "snippet": snippet[:160],
                "body_len": len(body),
                "url": self.page.url,
            })
            # #endregion
            return matched
        except Exception as e:
            # #region agent log
            _dbg("A", "main.py:has_playback_error", "playback_error_exception", {"error": str(e)})
            # #endregion
            return False

    async def ensure_video_plays(self, email: str, video_url: str = None, max_retries: int = 3) -> bool:
        """يفتح/يشغّل الفيديو ويعمل refresh لو ظهر خطأ التشغيل."""
        for attempt in range(1, max_retries + 1):
            await self.dismiss_overlays()

            # #region agent log
            vid_meta = await self.page.evaluate(
                """() => {
                    const vids = Array.from(document.querySelectorAll('video'));
                    return {
                        count: vids.length,
                        infos: vids.slice(0,3).map(v => ({
                            paused: v.paused,
                            readyState: v.readyState,
                            networkState: v.networkState,
                            currentTime: v.currentTime,
                            duration: v.duration || 0,
                            src: (v.currentSrc || v.src || '').slice(0,120),
                            error: v.error ? v.error.code : null,
                            width: v.videoWidth,
                            height: v.videoHeight
                        }))
                    };
                }"""
            )
            _dbg("B", "main.py:ensure_video_plays", "pre_attempt_video_meta", {
                "attempt": attempt,
                "email": email,
                "url": self.page.url,
                "video_meta": vid_meta,
            })
            # #endregion

            if await self.has_playback_error():
                logger.warning(f"[{email}] خطأ تشغيل الفيديو — refresh محاولة {attempt}/{max_retries}")
                if video_url:
                    await self.page.goto(video_url, wait_until="domcontentloaded", timeout=45000)
                else:
                    await self.page.reload(wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(4)
                await self.dismiss_overlays()

            ok = await self.play_video(email)
            await asyncio.sleep(2)

            # تحقق إن الفيديو فعلاً يتقدم
            progress = await self.page.evaluate(
                """async () => {
                    const v = document.querySelector('video');
                    if (!v) return {ok:false, reason:'no-video'};
                    let playErr = null;
                    try { v.muted = true; await v.play(); } catch(e) { playErr = String(e); }
                    const t1 = v.currentTime || 0;
                    await new Promise(r => setTimeout(r, 1500));
                    const t2 = v.currentTime || 0;
                    return {
                        ok: !v.paused && (t2 > t1 || t2 > 0.2),
                        paused: v.paused,
                        t1, t2,
                        dur: v.duration || 0,
                        readyState: v.readyState,
                        networkState: v.networkState,
                        error: v.error ? v.error.code : null,
                        playErr,
                        src: (v.currentSrc || v.src || '').slice(0,120)
                    };
                }"""
            )
            err_after = await self.has_playback_error()
            # #region agent log
            _dbg("D", "main.py:ensure_video_plays", "progress_check", {
                "attempt": attempt,
                "play_video_ok": ok,
                "progress": progress,
                "error_after": err_after,
            })
            # #endregion
            if progress and progress.get("ok") and not err_after:
                logger.success(f"[{email}] تشغيل مؤكد (t={progress.get('t2'):.1f})")
                return True

            logger.warning(f"[{email}] التشغيل غير مؤكد — إعادة تحميل ({attempt}/{max_retries})")
            if video_url:
                await self.page.goto(video_url, wait_until="domcontentloaded", timeout=45000)
            else:
                await self.page.reload(wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)

        # #region agent log
        _dbg("C", "main.py:ensure_video_plays", "all_retries_failed", {
            "email": email,
            "url": self.page.url,
            "video_url": video_url,
        })
        # #endregion
        return False

    async def play_video(self, email: str) -> bool:
        logger.info(f"[{email}] تشغيل الفيديو...")
        try:
            await self.dismiss_overlays()
            if await self.has_playback_error():
                logger.warning(f"[{email}] رسالة refresh ظاهرة قبل التشغيل")
                # #region agent log
                _dbg("A", "main.py:play_video", "abort_due_playback_error_banner", {"url": self.page.url})
                # #endregion
                return False

            video = self.page.locator("video").first
            await video.wait_for(state="attached", timeout=20000)

            for i in range(4):
                await self.dismiss_overlays()
                try:
                    await video.click(force=True, timeout=2000)
                except Exception:
                    pass
                playing = await self.page.evaluate(
                    """async () => {
                        const v = document.querySelector('video');
                        if (!v) return {ok:false, reason:'no-video'};
                        try {
                            v.playsInline = true;
                            v.currentTime = Math.max(v.currentTime, 0);
                            await v.play();
                            // Attempt to unmute if playing
                            try { v.muted = false; } catch(e) {}
                            
                            // Scroll a bit to simulate human behavior
                            window.scrollBy(0, 100);
                            setTimeout(() => window.scrollBy(0, -100), 1000);
                            
                            return {ok: !v.paused, paused: v.paused, muted: v.muted, readyState: v.readyState, error: v.error ? v.error.code : null, src:(v.currentSrc||v.src||'').slice(0,80)};
                        } catch (e) {
                            try { v.muted = true; await v.play(); return {ok:!v.paused, muted: v.muted, playCatch:String(e)}; } catch(e2) { return {ok:false, playCatch:String(e), playCatch2:String(e2)}; }
                        }
                    }"""
                )
                # #region agent log
                _dbg("B", "main.py:play_video", "play_attempt", {"i": i, "playing": playing})
                # #endregion
                ok_flag = playing.get("ok") if isinstance(playing, dict) else bool(playing)
                if ok_flag and not await self.has_playback_error():
                    logger.success(f"[{email}] الفيديو شغال فعلياً")
                    return True
                await asyncio.sleep(1)

            try:
                await self.page.keyboard.press("Space")
                await asyncio.sleep(0.8)
            except Exception:
                pass
            return not await self.has_playback_error()
        except Exception as e:
            logger.warning(f"[{email}] تعذر تشغيل الفيديو: {type(e).__name__}: {e}")
            # #region agent log
            _dbg("E", "main.py:play_video", "play_exception", {"error": f"{type(e).__name__}: {e}"})
            # #endregion
            return False

    async def like_video(self, email: str, target_username: str = None) -> bool:
        """لايك فقط على فيديو الحساب المستهدف."""
        if not self.config.enable_liking:
            return False
        if target_username and not self.is_target_creator_video(target_username):
            logger.warning(f"[{email}] تخطي لايك — مو فيديو @{target_username}")
            return False

        try:
            # أزرار اللايك بجانب المشغّل فقط (مو You may like)
            like_btn = await self.find_first([
                'div[class*="DivActionItemContainer"] strong[data-e2e="like-count"]',
                'div[class*="DivActionItemContainer"] [data-e2e="like-icon"]',
                'strong[data-e2e="browse-like-count"]',
                'strong[data-e2e="like-count"]',
                '[data-e2e="browse-like-icon"]',
                '[data-e2e="like-icon"]',
            ], timeout_ms=6000)

            if like_btn:
                await like_btn.click(force=True)
                await asyncio.sleep(self.config.action_delay)
                logger.success(f"[{email}] لايك على @{target_username or 'الفيديو'}")
                await self.stats.increment('likes')
                return True

            logger.warning(f"[{email}] لم يجد زر اللايك")
            return False
        except Exception as e:
            logger.error(f"خطأ لايك: {type(e).__name__}: {e}")
            return False

    async def share_video(self, email: str, target_username: str = None) -> bool:
        """شير فقط على فيديو الحساب المستهدف."""
        if not self.config.enable_sharing:
            return False
        if target_username and not self.is_target_creator_video(target_username):
            logger.warning(f"[{email}] تخطي شير — مو فيديو @{target_username}")
            return False
        try:
            await self.dismiss_overlays()
            share_btn = await self.find_first([
                'div[class*="DivActionItemContainer"] [data-e2e="share-icon"]',
                '[data-e2e="share-icon"]',
                '[data-e2e="browse-share-icon"]',
                'strong[data-e2e="share-count"]',
                'strong[data-e2e="browse-share-count"]',
            ], timeout_ms=6000)
            if not share_btn:
                logger.warning(f"[{email}] لم يجد زر الشير")
                return False

            await share_btn.click(force=True)
            await asyncio.sleep(1.2)

            copied = False
            for sel in [
                'button:has-text("Copy link")',
                '[data-e2e="share-copy-link"]',
                'div[role="button"]:has-text("Copy link")',
                'button:has-text("نسخ الرابط")',
            ]:
                opt = self.page.locator(sel).first
                try:
                    if await opt.count() > 0 and await opt.is_visible():
                        await opt.click(force=True)
                        copied = True
                        await asyncio.sleep(0.6)
                        break
                except Exception:
                    continue

            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await self.dismiss_overlays()
            await self.stats.increment('shares')
            logger.success(f"[{email}] شير على @{target_username or 'الفيديو'}{' (نسخ)' if copied else ''}")
            return True
        except Exception as e:
            logger.warning(f"[{email}] فشل الشير: {type(e).__name__}: {e}")
            return False

    async def open_comments(self, email: str) -> bool:
        logger.info(f"[{email}] فتح قسم التعليقات...")
        await self.dismiss_overlays()

        # #region agent log
        _dbg("H2", "main.py:open_comments", "enter", {"email": email, "url": self.page.url, "build": BOT_VERSION}, run_id="pre-fix")
        # #endregion

        if await self._comment_editor():
            # #region agent log
            _dbg("H3", "main.py:open_comments", "editor_already_present", {"email": email}, run_id="pre-fix")
            # #endregion
            return True

        # نقر مباشر عبر JS على شريط الإجراءات يمين الفيديو
        clicked = None
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const prefer = [
                      '[data-e2e="browse-comment-icon"]',
                      '[data-e2e="comment-icon"]',
                      'span[data-e2e="comment-icon"]',
                      'strong[data-e2e="browse-comment-count"]',
                      'strong[data-e2e="comment-count"]',
                    ];
                    for (const s of prefer) {
                      const el = document.querySelector(s);
                      if (el) { el.click(); return s; }
                    }
                    // أي عنصر عليه أيقونة تعليق
                    const all = Array.from(document.querySelectorAll('[data-e2e]'));
                    const hit = all.find(e => (e.getAttribute('data-e2e')||'').toLowerCase().includes('comment'));
                    if (hit) { hit.click(); return hit.getAttribute('data-e2e'); }
                    return null;
                }"""
            )
            logger.info(f"[{email}] نقر تعليقات JS: {clicked}")
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[{email}] JS comment click: {e}")

        # #region agent log
        _dbg("H2", "main.py:open_comments", "js_click_result", {"email": email, "clicked": clicked}, run_id="pre-fix")
        # #endregion

        icon_sel = [
            '[data-e2e="browse-comment-icon"]',
            '[data-e2e="comment-icon"]',
            'span[data-e2e="comment-icon"]',
            'div[data-e2e="comment-icon"]',
            'button[data-e2e="comment-icon"]',
            'strong[data-e2e="comment-count"]',
            'strong[data-e2e="browse-comment-count"]',
        ]
        pw_clicked = False
        if not await self._comment_editor():
            btn = await self.find_first(icon_sel, timeout_ms=5000)
            if btn:
                try:
                    await btn.click(force=True, timeout=4000)
                    pw_clicked = True
                except Exception:
                    pass
                await asyncio.sleep(3)

        # #region agent log
        _dbg("H2", "main.py:open_comments", "pw_click", {"email": email, "pw_clicked": pw_clicked}, run_id="pre-fix")
        # #endregion

        # انتظر ظهور الحقل حتى 15 ثانية
        for wait_i in range(15):
            if await self._comment_editor():
                # #region agent log
                _dbg("H3", "main.py:open_comments", "editor_found_after_wait", {"email": email, "wait_i": wait_i}, run_id="pre-fix")
                # #endregion
                return True
            await asyncio.sleep(1)

        await self._log_comment_dom(email)
        # #region agent log
        _dbg("H4", "main.py:open_comments", "failed_no_editor", {"email": email, "url": self.page.url}, run_id="pre-fix")
        # #endregion
        logger.warning(f"[{email}] لم يجد زر أو حقل التعليقات")
        return False

    async def _log_comment_dom(self, email: str) -> None:
        try:
            info = await self.page.evaluate(
                """() => ({
                    url: location.href,
                    editables: Array.from(document.querySelectorAll('[contenteditable], textarea')).slice(0,12).map(e => ({
                      tag: e.tagName,
                      e2e: e.closest('[data-e2e]')?.getAttribute('data-e2e') || e.getAttribute('data-e2e'),
                      visible: !!(e.offsetParent || e.getClientRects().length),
                      lexical: e.getAttribute('data-lexical-editor'),
                      cls: (e.className||'').toString().slice(0,80)
                    })),
                    commentNodes: Array.from(document.querySelectorAll('[data-e2e*="comment"]')).slice(0,25).map(e => ({
                      e2e: e.getAttribute('data-e2e'),
                      tag: e.tagName,
                      visible: !!(e.offsetParent || e.getClientRects().length),
                      text: (e.innerText||'').trim().slice(0,40)
                    }))
                })"""
            )
            logger.warning(f"[{email}] comment-dom: {info}")
            # #region agent log
            _dbg("CMT", "main.py:_log_comment_dom", "dom_snapshot", {"email": email, "info": info}, run_id="post-fix")
            # #endregion
        except Exception as e:
            logger.warning(f"[{email}] comment-dom failed: {e}")

    async def _comment_editor(self):
        """يرجع locator لحقل كتابة التعليق إن وُجد."""
        selectors = [
            '[data-lexical-editor="true"][contenteditable="true"]',
            '[data-e2e="comment-input"] [contenteditable="true"]',
            '[data-e2e="comment-input"] [data-lexical-editor="true"]',
            'div[data-e2e="comment-text"] [contenteditable="true"]',
            '[data-e2e="comment-text"]',
            'div[data-e2e="comment-input"] div[contenteditable="true"]',
            'div.public-DraftEditor-content[contenteditable="true"]',
            '.DraftEditor-root [contenteditable="true"]',
            '[data-e2e="comment-input"]',
            'div[contenteditable="true"][role="textbox"]',
            'textarea[placeholder*="comment" i]',
            'textarea[placeholder*="Add comment" i]',
            'div[contenteditable="true"]',
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                n = await loc.count()
                if n <= 0:
                    continue
                for i in range(min(n, 8)):
                    item = loc.nth(i)
                    try:
                        if await item.is_visible():
                            return item
                    except Exception:
                        continue
                # حتى لو مو visible حسب Playwright، جرّب الأخير
                return loc.last
            except Exception:
                continue
        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const eds = Array.from(document.querySelectorAll('[contenteditable="true"], textarea'));
                    return eds.reverse().find(e => (e.offsetParent || e.getClientRects().length)) || null;
                }"""
            )
            el = handle.as_element()
            if el:
                return el
        except Exception:
            pass
        return None

    async def update_video_id(self):
        """Обновляет идентификатор текущего видео, используя URL или другие данные"""
        try:
            # Попытка получить ID видео из URL или других элементов страницы
            current_url = self.page.url
            if "video/" in current_url:
                # Извлекаем ID видео из URL
                self.current_video_id = current_url.split("video/")[1].split("?")[0]
            else:
                # Используем временную метку, если не можем получить реальный ID
                self.current_video_id = f"video_{datetime.now().strftime('%H%M%S')}"

            # Инициализируем счетчик комментариев для этого видео, если его еще нет
            if self.current_video_id not in self.stats.counters['comments_per_video']:
                self.stats.counters['comments_per_video'][self.current_video_id] = 0

        except Exception as e:
            logger.warning(f"Не удалось определить ID видео: {e}")
            self.current_video_id = f"unknown_{datetime.now().strftime('%H%M%S')}"

    async def post_comment(self, email: str, comment_text: str = None) -> bool:
        """Оставляет комментарий под текущим видео"""
        if not self.config.enable_commenting:
            return False

        try:
            await self.update_video_id()
            if comment_text is None:
                comment_text = self.get_comment_text(email)
            if not comment_text:
                logger.warning(f"[{email}] لا يوجد تعليق متاح — أضف تعليقات من اللوحة")
                return False

            await self.open_comments(email)
            await asyncio.sleep(1.2)

            editor = await self._comment_editor()
            if not editor:
                # #region agent log
                _dbg("CMT", "main.py:post_comment", "no_editor", {
                    "email": email,
                    "url": self.page.url,
                }, run_id="post-fix")
                # #endregion
                logger.error(f"[{email}] لم يجد حقل كتابة التعليق")
                return False

            try:
                await editor.click(timeout=3000)
            except Exception:
                try:
                    await editor.click(force=True, timeout=3000)
                except Exception:
                    pass
            await asyncio.sleep(0.3)

            # إدخال النص بعدة طرق
            typed = False
            try:
                await editor.fill(comment_text)
                typed = True
            except Exception:
                pass
            if not typed:
                try:
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.type(comment_text, delay=40)
                    typed = True
                except Exception:
                    pass
            if not typed:
                try:
                    await self.page.evaluate(
                        """(text) => {
                            const eds = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                            const ed = eds.reverse().find(e => e.offsetParent !== null) || eds[eds.length-1];
                            if (!ed) return false;
                            ed.focus();
                            ed.innerText = text;
                            ed.dispatchEvent(new InputEvent('input', {bubbles:true, data:text}));
                            return true;
                        }""",
                        comment_text,
                    )
                    typed = True
                except Exception as e:
                    logger.warning(f"[{email}] فشل إدخال نص التعليق: {e}")
                    return False

            await asyncio.sleep(self.config.action_delay)

            posted = False
            for sel in [
                '[data-e2e="comment-post"]',
                'div[data-e2e="comment-post"]',
                'button[data-e2e="comment-post"]',
                'button:has-text("Post")',
                'div[role="button"]:has-text("Post")',
                'button:has-text("نشر")',
            ]:
                btn = self.page.locator(sel).first
                try:
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(force=True)
                        posted = True
                        break
                except Exception:
                    continue
            if not posted:
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(0.3)
                # أحياناً Enter وحده ما يكفي — Ctrl+Enter
                try:
                    await self.page.keyboard.press("Control+Enter")
                except Exception:
                    pass
            await asyncio.sleep(self.config.comment_delay)

            await self.stats.increment('comments')
            self.stats.counters['comments_per_video'][self.current_video_id] = self.stats.counters[
                                                                                   'comments_per_video'].get(
                self.current_video_id, 0) + 1

            # #region agent log
            _dbg("CMT", "main.py:post_comment", "posted", {
                "email": email,
                "text_len": len(comment_text),
                "posted_btn": posted,
            }, run_id="post-fix")
            # #endregion
            logger.success(f"[{email}] تم نشر التعليق: {comment_text}")
            return True
        except Exception as e:
            logger.error(f"Ошибка при оставлении комментария: {type(e).__name__}: {str(e)}")
            return False

    async def reply_to_comment(self, email: str) -> bool:
        """Отвечает на существующий комментарий"""
        if not self.config.enable_reply_commenting:
            return False

        try:
            # Находим кнопку ответа на первый комментарий
            reply_button = self.page.locator('span[data-e2e="comment-reply-1"]').first
            await reply_button.click()
            await asyncio.sleep(self.config.comment_delay)

            # После нажатия кнопки "Ответить" используем ПОСЛЕДНЕЕ поле ввода (которое появилось для ответа)
            reply_input = self.page.locator('div[data-e2e="comment-input"]').last
            await reply_input.click()
            await asyncio.sleep(self.config.action_delay)

            comment_text = self.get_comment_text(email)
            if not comment_text:
                return False
            await self.page.keyboard.type(comment_text)
            await asyncio.sleep(self.config.action_delay)

            await self.page.keyboard.press('Enter')
            await asyncio.sleep(self.config.comment_delay)

            logger.success(f"Успешно оставлен ответ на комментарий для {email}")
            await self.stats.increment('replies')
            return True
        except Exception as e:
            logger.warning(f"Не удалось ответить на комментарий: {type(e).__name__}: {str(e)}")
            return False

            return False

    async def wait_until_video_ends(self, email: str, max_wait: int = 180, video_url: str = None) -> bool:
        """يشاهد الفيديو فعلياً حتى النهاية (مهم لعدّ المشاهدات)."""
        logger.info(f"[{email}] مشاهدة الفيديو حتى النهاية...")
        if not await self.ensure_video_plays(email, video_url=video_url):
            logger.warning(f"[{email}] فشل تشغيل الفيديو بعد عدة محاولات")
            return False
        started = time.time()
        last_t = -1.0
        stuck = 0
        min_watch = 15  # أقل مدة مشاهدة قبل الانتقال

        while time.time() - started < max_wait:
            if should_stop():
                logger.info(f"[{email}] إيقاف المشاهدة بأمر الواجهة")
                return False
            if await self.has_playback_error():
                logger.warning(f"[{email}] ظهر خطأ تشغيل أثناء المشاهدة — refresh")
                if not await self.ensure_video_plays(email, video_url=video_url):
                    return False

            await self.dismiss_overlays()
            info = await self.page.evaluate(
                """async () => {
                    const v = document.querySelector('video');
                    if (!v) return null;
                    try {
                        if (v.paused) { await v.play(); }
                        try { v.muted = false; } catch(e) {}
                    } catch(e) {}
                    
                    // Random small scroll
                    if (Math.random() > 0.7) {
                        window.scrollBy(0, Math.random() > 0.5 ? 20 : -20);
                    }
                    
                    return {
                        current: v.currentTime || 0,
                        duration: v.duration || 0,
                        ended: !!v.ended,
                        paused: !!v.paused
                    };
                }"""
            )
            if not info:
                await self.ensure_video_plays(email, video_url=video_url)
                await asyncio.sleep(1)
                continue

            cur = float(info.get("current") or 0)
            dur = float(info.get("duration") or 0)
            watched = time.time() - started

            if info.get("ended") and watched >= min_watch:
                logger.success(f"[{email}] انتهى الفيديو بعد {watched:.0f}ث")
                return True
            if dur > 1 and cur >= max(dur - 0.8, dur * 0.95) and watched >= min_watch:
                logger.success(f"[{email}] شوهد كاملاً ({cur:.1f}/{dur:.1f})")
                return True

            if abs(cur - last_t) < 0.08:
                stuck += 1
                if stuck % 5 == 0:
                    await self.play_video(email)
            else:
                stuck = 0
            last_t = cur

            if stuck >= 25 and watched >= min_watch:
                logger.warning(f"[{email}] الفيديو عالق بعد مشاهدة {watched:.0f}ث — التالي")
                return True

            await asyncio.sleep(1)

        logger.warning(f"[{email}] تجاوز وقت مشاهدة الفيديو ({max_wait}ث)")
        return True

    async def scroll_to_next(self, email: str) -> bool:
        """سكرول / انتقال للفيديو التالي."""
        prev_url = self.page.url
        prev_id = None
        try:
            prev_id = await self.page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    return v ? (v.currentSrc || v.src || '') : '';
                }"""
            )
        except Exception:
            pass

        # 1) زر السهم
        for sel in [
            'button[data-e2e="arrow-right"]',
            'button[data-e2e="arrow-down"]',
            '[data-e2e="arrow-right"]',
            '.css-1s9jpf8-ButtonBasicButtonContainer-StyledVideoSwitch',
        ]:
            btn = self.page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True)
                    await asyncio.sleep(1.5)
                    await self.stats.increment('next_videos')
                    logger.success(f"[{email}] سكرول للفيديو التالي (زر)")
                    return True
            except Exception:
                continue

        # 2) سهم لوحة المفاتيح
        try:
            await self.page.keyboard.press("ArrowDown")
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # 3) عجلة الماوس
        try:
            await self.page.mouse.wheel(0, 1200)
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # تحقق إن تغير شيء
        try:
            new_id = await self.page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    return v ? (v.currentSrc || v.src || '') : '';
                }"""
            )
            if (self.page.url != prev_url) or (new_id and new_id != prev_id):
                await self.stats.increment('next_videos')
                logger.success(f"[{email}] سكرول للفيديو التالي")
                return True
        except Exception:
            pass

        logger.warning(f"[{email}] تعذر الانتقال للفيديو التالي")
        return False

    async def open_profile_first_video(self, email: str, profile_url: str) -> bool:
        """يفتح بروفايل الشخص ويضغط أول فيديو."""
        logger.info(f"[{email}] فتح بروفايل للمراقبة: {profile_url}")
        await self.page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await self.dismiss_overlays()

        # إن كنا أصلاً على فيديو
        if "/video/" in self.page.url:
            return True

        selectors = [
            'div[data-e2e="user-post-item"] a',
            'a[href*="/video/"]',
            '[data-e2e="user-post-item"]',
        ]
        for sel in selectors:
            item = self.page.locator(sel).first
            try:
                await item.wait_for(state="visible", timeout=8000)
                href = await item.get_attribute("href")
                if href and "/video/" in href:
                    if not href.startswith("http"):
                        href = "https://www.tiktok.com" + href
                    await self.page.goto(href, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(3)
                    return True
                await item.click(force=True)
                await asyncio.sleep(3)
                if "/video/" in self.page.url or await self.page.locator("video").count() > 0:
                    return True
            except Exception:
                continue

        logger.error(f"[{email}] لم يجد فيديوهات على البروفايل")
        return False

    def extract_username(self, url: str) -> str:
        """يستخرج @username من رابط بروفايل أو فيديو."""
        if not url:
            return ""
        m = re.search(r"tiktok\.com/@([^/?]+)", url, re.IGNORECASE)
        return (m.group(1) if m else "").lower().lstrip("@")

    def normalize_profile_url(self, url: str) -> str:
        """يحول أي رابط حساب/فيديو إلى بروفايل نظيف."""
        username = self.extract_username(url)
        if not username:
            return (url or "").strip()
        return f"https://www.tiktok.com/@{username}"

    def is_target_creator_video(self, target_username: str) -> bool:
        if not target_username:
            return False
        url = (self.page.url or "").lower()
        return f"/@{target_username.lower()}" in url and "/video/" in url

    async def collect_profile_videos(self, email: str, profile_url: str, limit: int = 40) -> List[str]:
        """يجمع روابط فيديوهات البروفايل فقط."""
        profile_url = self.normalize_profile_url(profile_url)
        await self.page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await self.dismiss_overlays()

        username = self.extract_username(profile_url)
        links = []
        seen = set()

        for _ in range(10):
            hrefs = await self.page.eval_on_selector_all(
                'a[href*="/video/"]',
                "els => els.map(e => e.href || e.getAttribute('href') || '')",
            )
            for href in hrefs or []:
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.tiktok.com" + href
                href = href.split("?")[0].rstrip("/")
                # تجاهل روابط ناقصة مثل .../video/
                if not re.search(r"/video/\d+", href):
                    continue
                if username and f"/@{username}/video/" not in href.lower():
                    continue
                if href not in seen:
                    seen.add(href)
                    links.append(href)
            if limit and len(links) >= limit:
                break
            await self.page.mouse.wheel(0, 1800)
            await asyncio.sleep(1.2)

        logger.info(f"[{email}] تم جمع {len(links)} فيديو من @{username}")
        return links[:limit] if limit else links

    async def watch_profile_loop(self, email: str, captcha_solver=None) -> None:
        """يشاهد فيديوهات حساب محدد فقط ويقلبها: مشاهدة → لايك → شير → التالي."""
        raw = (self.config.profile_url or "").strip() or (self.config.target_video_url or "").strip()
        if not raw:
            logger.error(f"[{email}] لا يوجد رابط بروفايل/فيديو للمراقبة")
            return

        username = self.extract_username(raw)
        if not username:
            logger.error(f"[{email}] رابط غير صالح: {raw}")
            return
        profile_url = self.normalize_profile_url(raw)
        logger.info(f"[{email}] قلب فيديوهات @{username} فقط من {profile_url}")

        max_n = int(self.config.watch_count or 0)
        n = 0
        visited = set()
        commented_once = False

        while max_n <= 0 or n < max_n:
            if should_stop():
                logger.info(f"[{email}] توقف البوت بسبب أمر الإيقاف")
                break
            videos = await self.collect_profile_videos(
                email,
                profile_url,
                limit=80 if max_n <= 0 else max(max_n + 5, 20),
            )
            if not videos:
                logger.warning(f"[{email}] لا توجد فيديوهات على @{username}")
                break

            progressed = False
            for video_url in videos:
                if should_stop():
                    break
                if max_n > 0 and n >= max_n:
                    break
                vid_id = video_url.rstrip("/").split("/")[-1]
                if not vid_id.isdigit() or vid_id in visited:
                    continue
                visited.add(vid_id)
                n += 1
                progressed = True

                logger.info(f"[{email}] فيديو #{n} لـ @{username}: {video_url}")
                
                # استخدام API الـ History الخاص بالمتصفح بدل الانتقال المباشر إذا أمكن
                await self.page.evaluate(f"window.history.pushState(null, '', '{video_url}');")
                await self.page.goto(video_url, wait_until="domcontentloaded", timeout=45000, referer=profile_url)
                await asyncio.sleep(4)
                await self.dismiss_overlays()

                # لازم نكون على فيديو الحساب المستهدف فقط (مو You may like)
                if not self.is_target_creator_video(username):
                    logger.warning(f"[{email}] تخطي — الرابط مو لـ @{username}: {self.page.url}")
                    continue

                try:
                    if captcha_solver:
                        await captcha_solver.solve_captcha_if_present()
                except Exception:
                    pass

                played = await self.wait_until_video_ends(email, video_url=video_url)
                if not played:
                    logger.warning(f"[{email}] تخطي الفيديو بسبب فشل التشغيل")
                    continue

                # تأكيد مرة ثانية قبل أي تفاعل
                if not self.is_target_creator_video(username):
                    logger.warning(f"[{email}] خرجنا من حساب @{username} — بدون لايك/تعليق")
                    continue

                await self.stats.increment('watched')
                await self.like_video(email, target_username=username)
                await self.share_video(email, target_username=username)
                # تعليق واحد فقط لكل حساب من المجمع (يُحذف بعد الاستخدام)
                if self.config.enable_commenting and not commented_once:
                    ok = await self.post_comment(email)
                    if ok:
                        commented_once = True

                await asyncio.sleep(1.5)

            if max_n > 0 and n >= max_n:
                break
            if not progressed:
                logger.info(f"[{email}] خلصت فيديوهات @{username} المتاحة")
                break
            logger.info(f"[{email}] إعادة تحميل بروفايل @{username}...")
            await asyncio.sleep(2)

        logger.success(
            f"[{email}] انتهت المراقبة على @{username} — مشاهدات {self.stats.counters.get('watched', 0)} | "
            f"لايك {self.stats.counters.get('likes', 0)} | شير {self.stats.counters.get('shares', 0)}"
        )

    async def next_video(self, email: str, captcha_solver) -> bool:
        """Переходит к следующему видео"""
        if not self.config.enable_next_video:
            return False

        try:
            logger.info(f"Пытаемся найти и нажать на кнопку Следующее видео для {email}")

            # Пробуем найти кнопку по data-e2e="arrow-right"
            next_video_button = self.page.locator('button[data-e2e="arrow-right"]')

            # Проверяем, найдена ли кнопка
            if await next_video_button.count() > 0:
                await next_video_button.click()
                await asyncio.sleep(self.config.action_delay)
                logger.success(f"Успешно нажали на кнопку Следующее видео для {email}")
                await self.stats.increment('next_videos')
                if captcha_solver:
                    try:
                        await captcha_solver.solve_captcha_if_present()
                    except Exception:
                        pass
                await self.update_video_id()  # Обновляем ID видео после перехода
                return True
            else:
                # Альтернативный поиск по CSS классу, если первый способ не сработал
                next_video_button_alt = self.page.locator('.css-1s9jpf8-ButtonBasicButtonContainer-StyledVideoSwitch')
                if await next_video_button_alt.count() > 0:
                    await next_video_button_alt.click()
                    await asyncio.sleep(self.config.action_delay)
                    logger.success(f"Успешно нажали на кнопку Следующее видео (по CSS классу) для {email}")
                    await self.stats.increment('next_videos')
                    await self.update_video_id()  # Обновляем ID видео после перехода
                    if captcha_solver:
                        try:
                            await captcha_solver.solve_captcha_if_present()
                        except Exception:
                            pass

                    return True
                else:
                    logger.warning(f"Не удалось найти кнопку Следующее видео")
                    return False

        except Exception as e:
            logger.error(f"Ошибка при нажатии на кнопку Следующее видео: {type(e).__name__}: {str(e)}")
            return False

    async def run_comment_loop(self, email: str, captcha_solver):
        """Выполняет циклическое комментирование"""
        if not self.config.enable_comment_loop:
            return

        loop_count = 0
        max_loops = self.config.comment_loop_count
        comments_opened = False

        try:
            # Находим и открываем комментарии только в самом начале
            try:
                comments_section = self.page.locator('div[data-e2e="comment-input"]')
                if await comments_section.count() == 0:
                    comments_button = self.page.locator('span[data-e2e="comment-icon"]').first
                    await comments_button.click()
                    if captcha_solver:
                        try:
                            await captcha_solver.solve_captcha_if_present()
                        except Exception:
                            pass
                    await asyncio.sleep(self.config.comment_delay)
                    comments_opened = True
                    logger.info(f"Комментарии успешно открыты для {email}")
                else:
                    comments_opened = True
                    logger.info(f"Комментарии уже открыты для {email}")
            except Exception as e:
                logger.warning(f"Не удалось открыть секцию комментариев: {e}")
                return

            # Основной цикл комментирования
            while max_loops == 0 or loop_count < max_loops:
                # СНАЧАЛА пытаемся ответить на существующий комментарий, если это разрешено
                if self.config.enable_reply_commenting:
                    try:
                        reply_success = await self.reply_to_comment(email)
                        if reply_success:
                            logger.success(f"Успешно ответили на комментарий в цикле {loop_count + 1}")
                    except Exception as e:
                        logger.warning(f"Ошибка при ответе на комментарий: {type(e).__name__}: {str(e)}")

                # ЗАТЕМ оставляем свой комментарий
                comment_success = await self.post_comment(email)

                if comment_success:
                    loop_count += 1
                    await self.stats.increment('comment_loops')
                    logger.info(
                        f"Цикл комментирования {loop_count}{' из ' + str(max_loops) if max_loops > 0 else ''} завершен")

                    # Ставим лайк, если это разрешено
                    if self.config.enable_liking:
                        await self.like_video(email)

                    # Переходим к следующему видео после цикла, если это разрешено
                    if self.config.enable_next_video:
                        next_success = await self.next_video(email, captcha_solver)
                        if not next_success:
                            logger.warning("Не удалось перейти к следующему видео, продолжаем с текущим")

                    # Задержка между циклами
                    if max_loops == 0 or loop_count < max_loops:
                        logger.info(f"Ожидание {self.config.comment_loop_delay} секунд перед следующим циклом")
                        await asyncio.sleep(self.config.comment_loop_delay)
                else:
                    logger.warning(f"Не удалось оставить комментарий в цикле {loop_count + 1}")
                    # Пробуем переключиться на следующее видео
                    if await self.next_video(email, captcha_solver):
                        logger.info("Перешли к следующему видео после неудачной попытки комментирования")
                    else:
                        logger.error("Не удалось найти новое видео для комментирования")
                        break

        except Exception as e:
            logger.error(f"Ошибка в цикле комментирования: {type(e).__name__}: {str(e)}")

        logger.info(f"Цикл комментирования завершен. Всего комментариев: {self.stats.counters['comments']}")

class TikTokChecker:
    """Основной класс для проверки аккаунтов TikTok"""

    def __init__(self, config: Config, stats: Stats):
        self.config = config
        self.stats = stats
        self.file_handler = FileHandler(config)
        self.successful_logins = []  # Отслеживание успешных входов в браузер

    async def safe_goto(self, page: Page, url: str, email: str) -> bool:
        """يفتح الرابط بدون انتظار حدث load الكامل (TikTok غالباً ما يكمله)."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)
            return True
        except Exception as e:
            logger.warning(f"[{email}] تنبيه أثناء فتح {url}: {type(e).__name__}: {e}")
            # الصفحة غالباً تكون ظهرت أصلاً رغم الـ timeout
            await asyncio.sleep(3)
            return "tiktok.com" in (page.url or "")

    @staticmethod
    def clean_tiktok_url(url: str) -> str:
        """يشيل query/hash من رابط تيك توك."""
        url = (url or "").strip()
        if not url:
            return url
        return url.split("?")[0].split("#")[0].rstrip("/")

    def extract_video_id(self, url: str) -> Optional[str]:
        m = re.search(r"/video/(\d+)", url or "")
        return m.group(1) if m else None

    async def open_target_video(self, page: Page, email: str, target: str) -> bool:
        """يفتح فيديو مستهدف مع تجاوز تحويلات /about الشائعة على VPS."""
        target = self.clean_tiktok_url(target)
        vid = self.extract_video_id(target)
        username_m = re.search(r"tiktok\.com/@([^/?]+)", target or "", re.IGNORECASE)
        username = username_m.group(1) if username_m else None

        # #region agent log
        _dbg("NAV", "main.py:open_target_video", "nav_start", {
            "email": email,
            "target": target,
            "vid": vid,
            "username": username,
        }, run_id="post-fix")
        # #endregion

        # محاولة 1: رابط الفيديو المباشر النظيف
        await self.safe_goto(page, target, email)
        await asyncio.sleep(2)
        if "/video/" in (page.url or "") and "about" not in (page.url or ""):
            # #region agent log
            _dbg("NAV", "main.py:open_target_video", "nav_ok_direct", {"url": page.url}, run_id="post-fix")
            # #endregion
            return True

        logger.warning(f"[{email}] تحويل غير متوقع بعد رابط مباشر: {page.url} — محاولة عبر البروفايل")

        # محاولة 2: افتح البروفايل ثم اضغط على نفس الفيديو
        if username and vid:
            profile = f"https://www.tiktok.com/@{username}"
            await self.safe_goto(page, profile, email)
            await asyncio.sleep(2)
            # #region agent log
            _dbg("NAV", "main.py:open_target_video", "profile_page", {"url": page.url}, run_id="post-fix")
            # #endregion
            if "about" in (page.url or "") and "/@" not in (page.url or ""):
                logger.error(f"[{email}] تيك توك حجب الوصول (about page). غالباً IP السيرفر محظور.")
                return False

            link = page.locator(f'a[href*="/video/{vid}"]').first
            if await link.count() > 0:
                try:
                    await link.click(timeout=8000)
                except Exception:
                    href = await link.get_attribute("href")
                    if href:
                        if not href.startswith("http"):
                            href = "https://www.tiktok.com" + href
                        await self.safe_goto(page, self.clean_tiktok_url(href), email)
                await asyncio.sleep(3)
                if "/video/" in (page.url or ""):
                    # #region agent log
                    _dbg("NAV", "main.py:open_target_video", "nav_ok_profile_click", {"url": page.url}, run_id="post-fix")
                    # #endregion
                    return True

            # محاولة 3: goto مباشر من البروفايل مرة ثانية
            await self.safe_goto(page, target, email)
            await asyncio.sleep(2)
            if "/video/" in (page.url or ""):
                return True

        # #region agent log
        _dbg("NAV", "main.py:open_target_video", "nav_failed", {"url": page.url}, run_id="post-fix")
        # #endregion
        logger.error(f"[{email}] فشل فتح الفيديو. الصفحة النهائية: {page.url}")
        return False

    async def safe_solve_captcha(self, captcha_solver, email: str) -> None:
        """يحل الكابتشا إن وُجد مفتاح API صالح؛ لا يوقف البوت عند فشل الخدمة."""
        if not captcha_solver:
            return
        key = (self.config.sadcaptcha_api_key or "").strip()
        if not key or key in ("SADCAPCHA_API_KEY", "YOUR_API_KEY", "changeme"):
            return
        try:
            await captcha_solver.solve_captcha_if_present()
        except Exception as e:
            logger.warning(f"[{email}] تخطي حل الكابتشا: {type(e).__name__}: {e}")

    async def is_logged_in(self, page: Page) -> bool:
        """يتحقق إذا المستخدم مسجّل دخول فعلاً."""
        url = page.url or ""
        if "/login" in url:
            return False
        login_form = page.locator('input[type="password"]')
        if await login_form.count() > 0 and await login_form.first.is_visible():
            return False
        logged_in_selectors = [
            '[data-e2e="profile-icon"]',
            '[data-e2e="nav-profile"]',
            'a[href*="/@"]',
            '[data-e2e="top-digg-icon"]',
            '[data-e2e="like-icon"]',
            '[data-e2e="comment-icon"]',
        ]
        for sel in logged_in_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return "tiktok.com" in url and "/login" not in url and "verify" not in url

    async def has_code_input(self, page: Page) -> bool:
        selectors = [
            'input[name="verifyCode"]',
            '.verification-code-input',
            'input[placeholder*="Digit code" i]',
            'input[placeholder*="code" i]',
            'input[placeholder*="Code"]',
            'input[maxlength="6"]',
            'input[autocomplete="one-time-code"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def needs_otp(self, page: Page) -> bool:
        url = (page.url or "").lower()
        if "verify" in url or "otp" in url:
            return True
        if await self.has_code_input(page):
            return True
        if await self.has_verify_method_picker(page):
            return True
        return False

    async def has_verify_method_picker(self, page: Page) -> bool:
        """شاشة Verify it's really you فقط (مو أي Email بالصفحة)."""
        try:
            title = page.locator("text=/Verify it.?s really you/i").first
            if await title.count() > 0 and await title.is_visible():
                return True
        except Exception:
            pass
        try:
            body = (await page.inner_text("body"))[:2000]
            if "Verify it's really you" in body or "Verify it’s really you" in body:
                if "Email" in body and ("***" in body or "@" in body):
                    return True
        except Exception:
            pass
        return False

    async def click_email_verification_method(self, page: Page, email: str) -> bool:
        """يضغط خيار Email مرة واحدة لإرسال الرمز."""
        if not await self.has_verify_method_picker(page):
            return False

        logger.info(f"[{email}] شاشة Verify — الضغط على Email لإرسال الكود...")
        domain = (email or "").split("@")[-1] if "@" in (email or "") else ""

        # اضغط صف Email الذي فيه الإيميل المخفي
        clicked = await page.evaluate(
            """(domain) => {
                const nodes = Array.from(document.querySelectorAll('div,button,li,a,span'));
                const hit = nodes.find(n => {
                    const t = (n.innerText || '').replace(/\\s+/g,' ').trim();
                    if (!t) return false;
                    const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                    const hasEmailWord = lines.some(l => l === 'Email' || l.startsWith('Email'));
                    const hasMasked = /@/.test(t) && (/\\*/.test(t) || (domain && t.toLowerCase().includes(domain.toLowerCase())));
                    return hasEmailWord && hasMasked && t.length < 120;
                });
                if (!hit) {
                    const fallback = nodes.find(n => {
                        const t = (n.innerText || '').trim();
                        return t === 'Email' || (t.startsWith('Email') && t.includes('@'));
                    });
                    if (!fallback) return false;
                    (fallback.closest('button,div[role="button"],li,a,div') || fallback).click();
                    return true;
                }
                (hit.closest('button,div[role="button"],li,a,div') || hit).click();
                return true;
            }""",
            domain,
        )
        if clicked:
            await asyncio.sleep(3)
            logger.success(f"[{email}] تم اختيار Email — بانتظار وصول الكود")
            return True

        logger.warning(f"[{email}] لم يجد خيار Email للضغط")
        return False

    async def fill_otp(self, page: Page, code: str, email: str) -> bool:
        selectors = [
            'input[name="verifyCode"]',
            '.verification-code-input',
            'input[placeholder*="code" i]',
            'input[placeholder*="Code"]',
            'input[maxlength="6"]',
            'input[maxlength="4"]',
            'input[type="tel"]',
            'input[type="text"]',
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await loc.click(force=True)
                await loc.fill("")
                await loc.type(code, delay=80)
                await asyncio.sleep(0.8)

                clicked_next = False
                for btn_sel in [
                    'button:has-text("Next")',
                    'button[data-e2e="email-verification-submit"]',
                    'button[type="submit"]',
                    'button:has-text("Verify")',
                    'button:has-text("Continue")',
                    'button:has-text("Submit")',
                    'div[role="button"]:has-text("Next")',
                ]:
                    btn = page.locator(btn_sel).first
                    try:
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click(force=True)
                            clicked_next = True
                            logger.info(f"[{email}] تم الضغط على Next بعد OTP")
                            break
                    except Exception:
                        continue
                if not clicked_next:
                    await page.keyboard.press("Enter")
                    logger.info(f"[{email}] تم إرسال OTP بـ Enter")

                logger.success(f"[{email}] تم إدخال OTP تلقائياً: {code}")
                await asyncio.sleep(3)

                # لو لسا ظاهر Next جرب مرة ثانية
                try:
                    nxt = page.locator('button:has-text("Next")').first
                    if await nxt.count() > 0 and await nxt.is_visible():
                        await nxt.click(force=True)
                        await asyncio.sleep(2)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        logger.warning(f"[{email}] وجد OTP لكن تعذر إدخاله في الصفحة")
        return False

    async def wait_for_login(self, page: Page, email: str, email_password: str = None, max_wait: int = 180, mailbox_login: str = None) -> bool:
        """ينتظر اكتمال الدخول مع OTP من Hostinger."""
        after_ts = time.time() - 5
        waited = 0
        otp_tried = False
        email_clicked = False
        mailbox_login = mailbox_login or email

        logger.warning(
            f"[{email}] انتظار الدخول (OTP من Hostinger: {mailbox_login}) — حتى {max_wait}ث..."
        )

        while waited < max_wait:
            if await self.is_logged_in(page):
                logger.success(f"[{email}] تم تسجيل الدخول بنجاح")
                return True

            # 1) شاشة اختيار الطريقة → اضغط Email مرة واحدة فقط
            if not email_clicked and await self.has_verify_method_picker(page):
                if await self.click_email_verification_method(page, email):
                    email_clicked = True
                    after_ts = time.time() - 2
                    await asyncio.sleep(3)

            # 2) بعد اختيار Email أو ظهور حقل الكود → جلب OTP من Hostinger
            if (
                self.config.auto_otp
                and not otp_tried
                and email_password
                and (email_clicked or await self.has_code_input(page))
            ):
                # انتظر ظهور حقل الكود قليلاً
                for _ in range(10):
                    if await self.has_code_input(page) or await self.is_logged_in(page):
                        break
                    await asyncio.sleep(1)
                    waited += 1

                if await self.is_logged_in(page):
                    return True

                logger.info(f"[{email}] جلب OTP من Hostinger ({mailbox_login}) لحساب {email}...")
                code = await asyncio.to_thread(
                    wait_for_otp,
                    mailbox_login,
                    email_password,
                    timeout=min(self.config.otp_timeout, max(30, max_wait - waited)),
                    poll_interval=4,
                    imap_host=self.config.imap_host,
                    imap_port=self.config.imap_port,
                    after_ts=after_ts,
                    for_account=email,
                )
                otp_tried = True
                if code:
                    await self.fill_otp(page, code, email)
                    await asyncio.sleep(3)
                    if await self.is_logged_in(page):
                        logger.success(f"[{email}] تم تسجيل الدخول بعد OTP")
                        return True
                else:
                    logger.warning(f"[{email}] لم يُجلب OTP من Hostinger — أكمل يدوياً إن لزم")

            await asyncio.sleep(3)
            waited += 3
            if waited % 15 == 0:
                logger.info(f"[{email}] لا يزال ينتظر الدخول... ({waited}/{max_wait}ث) | URL: {page.url}")

        logger.warning(f"[{email}] انتهى وقت انتظار الدخول ({max_wait}ث) | URL: {page.url}")
        return False

    async def save_session(self, context, email: str):
        path = self.file_handler.session_path(email)
        try:
            await context.storage_state(path=path)
            logger.info(f"[{email}] تم حفظ الجلسة في {path}")
        except Exception as e:
            logger.warning(f"[{email}] تعذر حفظ الجلسة: {e}")

    async def check_account(self, account: Dict) -> bool:
        """Проверяет один аккаунт TikTok"""
        email = account['email']
        password = account['password']
        email_password = account.get('email_password') or password
        mailbox_email = account.get('mailbox_email') or account.get('mailbox') or email

        for attempt in range(1, self.config.max_check_attempts + 1):
            if attempt > 1:
                logger.info(f"Повторная попытка {attempt}/{self.config.max_check_attempts} для {email}")

            browser = None
            context = None

            try:
                async with async_playwright() as p:
                    browser = await launch_browser(p, self.config)

                    session_file = self.file_handler.session_path(email)
                    context_kwargs = dict(self.config.browser_context_options)

                    use_session = False
                    # على VPS مع بروكسي: الجلسات من اللوكل غالباً تسبب مشاكل
                    force = bool(self.config.force_relogin)
                    if force:
                        if os.path.exists(session_file):
                            try:
                                os.remove(session_file)
                                logger.info(f"[{email}] تم حذف الجلسة القديمة — سيتم تسجيل دخول جديد")
                            except Exception as e:
                                logger.warning(f"[{email}] تعذر حذف الجلسة: {e}")
                    elif os.path.exists(session_file):
                        context_kwargs["storage_state"] = session_file
                        use_session = True
                        logger.info(f"[{email}] تم تحميل جلسة محفوظة")

                    context = await browser.new_context(**context_kwargs)
                    context.set_default_timeout(self.config.page_timeout * 1000)

                    page = await context.new_page()

                    stealth = Stealth()
                    await stealth.apply_stealth_async(page)

                    captcha_solver = None
                    key = (self.config.sadcaptcha_api_key or "").strip()
                    if key and key not in ("SADCAPCHA_API_KEY", "YOUR_API_KEY", "changeme"):
                        captcha_solver = AsyncPlaywrightSolver(
                            page=page,
                            sadcaptcha_api_key=key,
                            mouse_step_size=2,
                            mouse_step_delay_ms=5
                        )
                    else:
                        logger.info(f"[{email}] بدون مفتاح Captcha — سيتم التخطي")

                    logged_in = False
                    if use_session and os.path.exists(session_file):
                        await self.safe_goto(page, 'https://www.tiktok.com', email)
                        logged_in = await self.is_logged_in(page)
                        if logged_in:
                            logger.success(f"[{email}] الجلسة المحفوظة ما زالت صالحة")
                        else:
                            logger.warning(f"[{email}] الجلسة منتهية — تسجيل دخول جديد")

                    if not logged_in:
                        # Загрузка страницы логина
                        await self.safe_goto(page, 'https://www.tiktok.com/login/phone-or-email/email', email)

                        # Локаторы элементов формы
                        email_input = page.locator('input[type="text"]')
                        password_input = page.locator('input[type="password"]')
                        login_button = page.locator('button[data-e2e="login-button"], button[type="submit"]')

                        # Проверка существования элементов формы
                        if await email_input.count() == 0 or await password_input.count() == 0 or await login_button.count() == 0:
                            logger.warning(f"Не удалось загрузить форму входа для {email} — انتظر الدخول اليدوي")
                            logged_in = await self.wait_for_login(page, email, email_password, mailbox_login=mailbox_email)
                        else:
                            # Заполнение формы входа
                            await email_input.fill(email)
                            await asyncio.sleep(self.config.action_delay)
                            await password_input.fill(password)
                            await asyncio.sleep(self.config.action_delay)

                            # Нажатие кнопки входа
                            await login_button.click()
                            await asyncio.sleep(self.config.action_delay)

                            await self.safe_solve_captcha(captcha_solver, email)

                            await asyncio.sleep(5)
                            # لو ظهرت شاشة اختيار طريقة التحقق — اضغط Email فوراً
                            if await self.has_verify_method_picker(page):
                                await self.click_email_verification_method(page, email)
                            logged_in = await self.is_logged_in(page)
                            if not logged_in:
                                logged_in = await self.wait_for_login(page, email, email_password, mailbox_login=mailbox_email)

                    if not logged_in:
                        logger.warning(f"Аккаунт {email} - НЕВАЛИДНЫЙ ✗ | URL: {page.url}")
                        await self.stats.increment('failed')
                        return False

                    await self.save_session(context, email)

                    actions = TikTokActions(page, self.config, self.stats)
                    mode = (self.config.bot_mode or "comment").strip().lower()

                    if mode in ("watch", "watch_comment"):
                        # ===== وضع مشاهدة مستمرة لحساب شخص =====
                        success = self.file_handler.save_account(email, password, [])
                        if success:
                            await self.stats.increment('successful')
                            logger.success(f"Успешный вход в аккаунт {email}")
                        try:
                            await actions.watch_profile_loop(email, captcha_solver)
                        except Exception as e:
                            logger.error(f"[{email}] خطأ في وضع المراقبة: {type(e).__name__}: {e}")

                        try:
                            report = await self.stats.get_report()
                            logger.info(f"Текущая статистика действий:\n{report}")
                        except Exception as e:
                            logger.error(f"Ошибка при формировании отчета: {e}")

                        if self.config.enable_hanging:
                            self.successful_logins.append((browser, context))
                            return True
                        await context.close()
                        await unregister_browser(browser)
                        return True

                    # ===== وضع التعليق على فيديو محدد =====
                    target = self.clean_tiktok_url(self.config.target_video_url.strip())
                    is_short = any(x in target for x in ["vm.tiktok.com", "vt.tiktok.com", "/t/"])
                    is_video = "/video/" in target or is_short
                    is_profile = not is_video

                    if is_profile:
                        logger.info(f"[{email}] رابط بروفايل — فتح الصفحة للحصول على أول فيديو...")
                        await self.safe_goto(page, target, email)
                        await self.safe_solve_captcha(captcha_solver, email)
                        await asyncio.sleep(2)
                        first_video = page.locator('a[href*="/video/"]').first
                        if await first_video.count() > 0:
                            href = await first_video.get_attribute("href")
                            if href and not href.startswith("http"):
                                href = "https://www.tiktok.com" + href
                            href = self.clean_tiktok_url(href)
                            logger.info(f"[{email}] وجد أول فيديو: {href}")
                            opened = await self.open_target_video(page, email, href)
                        else:
                            logger.warning(f"[{email}] لم يجد فيديو على البروفايل، سيحاول النقر على أول مقطع...")
                            video_thumb = page.locator('div[data-e2e="user-post-item"] a').first
                            if await video_thumb.count() > 0:
                                await video_thumb.click()
                                await asyncio.sleep(4)
                            opened = "/video/" in (page.url or "")
                    else:
                        logger.info(f"[{email}] الانتقال إلى الفيديو المستهدف: {target}")
                        opened = await self.open_target_video(page, email, target)
                        logger.info(f"[{email}] بعد التحويل: {page.url}")

                    await self.safe_solve_captcha(captcha_solver, email)

                    # التحقق من أن الصفحة حُمّلت بشكل صحيح
                    if "login" in page.url:
                        logger.warning(f"[{email}] تم تحويله لصفحة الدخول بعد فتح الفيديو")
                        await self.stats.increment('failed')
                        return False

                    logger.info(f"[{email}] الصفحة الحالية: {page.url}")

                    if not opened or "/video/" not in page.url:
                        logger.error(
                            f"[{email}] لم نصل لصفحة الفيديو (حظر/تحويل). "
                            f"جرّب Proxy سكني أو عطّل Headless. URL={page.url}"
                        )
                        await self.stats.increment('failed')
                        await context.close()
                        await unregister_browser(browser)
                        return False

                    # Сохранение информации об аккаунте
                    success = self.file_handler.save_account(email, password, [])

                    if success:
                        logger.success(f"Успешный вход в аккаунт {email}")
                        await self.stats.increment('successful')

                        await self.safe_solve_captcha(captcha_solver, email)
                        await asyncio.sleep(self.config.comment_delay)

                        try:
                            await actions.play_video(email)
                        except Exception as e:
                            logger.error(f"[{email}] فشل تشغيل الفيديو: {type(e).__name__}: {e}")

                        if self.config.enable_liking:
                            try:
                                await actions.like_video(email)
                            except Exception as e:
                                logger.error(f"[{email}] فشل اللايك: {type(e).__name__}: {e}")

                        if self.config.enable_commenting:
                            try:
                                await actions.post_comment(email)
                            except Exception as e:
                                logger.error(f"[{email}] فشل التعليق: {type(e).__name__}: {e}")

                        # Формируем отчет о действиях
                        try:
                            report = await self.stats.get_report()
                            logger.info(f"Текущая статистика действий:\n{report}")
                        except Exception as e:
                            logger.error(f"Ошибка при формировании отчета: {e}")

                        # Сохраняем браузер для "висения"
                        if self.config.enable_hanging:
                            self.successful_logins.append((browser, context))
                            return True
                        else:
                            await context.close()
                            await unregister_browser(browser)
                            return True

                    await context.close()
                    await unregister_browser(browser)
                    return False

            except Exception as e:
                logger.error(f"Ошибка проверки {email}: {type(e).__name__}: {str(e)}")

                if browser and not context:
                    try:
                        await unregister_browser(browser)
                    except:
                        pass

                if attempt == self.config.max_check_attempts:
                    logger.warning(f"Аккаунт {email} - ОШИБКА ✗")
                    await self.stats.increment('errors')
                    return False

                await asyncio.sleep(1)

        return False


class AccountProcessor:
    """Класс для обработки группы аккаунтов"""

    def __init__(self, accounts: List[Dict], config: Config):
        self.accounts = accounts
        self.config = config
        self.stats = Stats()
        self.checker = TikTokChecker(config, self.stats)
        self.next_index = 0
        self.lock = asyncio.Lock()

    async def worker(self, worker_id: int, semaphore: asyncio.Semaphore):
        """Обработчик для одного параллельного потока проверки"""
        while True:
            if should_stop():
                logger.info(f"Worker {worker_id} stopping due to STOP_BOT_FLAG")
                break
            async with self.lock:
                if self.next_index >= len(self.accounts):
                    break

                account_index = self.next_index
                self.next_index += 1
                current_account = self.accounts[account_index]
                current_account['index'] = account_index + 1

            async with semaphore:
                email = current_account['email']
                logger.info(f"[{account_index + 1}/{len(self.accounts)}] Проверка {email}")

                try:
                    await self.checker.check_account(current_account)

                    async with self.lock:
                        await self.stats.increment('processed')

                        if self.stats.counters['processed'] % 5 == 0 or self.stats.counters['processed'] == len(
                                self.accounts):
                            report = await self.stats.get_report()
                            logger.info(report)

                except Exception as e:
                    logger.error(f"Критическая ошибка проверки {email}: {type(e).__name__}: {str(e)}")
                    async with self.lock:
                        await self.stats.increment('processed')
                        await self.stats.increment('errors')

    async def process_all(self):
        """Обрабатывает все аккаунты с параллельным выполнением"""
        if not self.accounts:
            logger.warning("Нет аккаунтов для проверки")
            return

        # Обновляем статистику
        await self.stats.increment('total_accounts', len(self.accounts))

        logger.info(f"Начинаем проверку {len(self.accounts)} аккаунтов")

        # Создаем семафор для ограничения параллельных браузеров
        semaphore = asyncio.Semaphore(self.config.max_browsers)

        # Создаем и запускаем задачи работников
        tasks = []
        for worker_id in range(min(self.config.max_browsers, len(self.accounts))):
            task = asyncio.create_task(self.worker(worker_id + 1, semaphore))
            tasks.append(task)

        # Ожидаем завершения всех задач
        await asyncio.gather(*tasks)

        report = await self.stats.get_report()
        logger.success("Проверка аккаунтов завершена!")
        logger.success(report)

        # Поддерживаем "висящие" сессии, если они есть
        if self.checker.successful_logins and self.config.enable_hanging:
            logger.info(
                f"Успешный вход в {len(self.checker.successful_logins)} аккаунтов. Скрипт находится в режиме ожидания...")
            try:
                while True:
                    if should_stop():
                        logger.info("إيقاف وضع الانتظار بأمر الواجهة")
                        break
                    logger.info("Скрипт продолжает работу... Сессии браузера активны.")
                    await asyncio.sleep(self.config.hang_check_interval)
            except KeyboardInterrupt:
                logger.info("Получен сигнал остановки. Закрываем браузеры...")
            for browser, context in self.checker.successful_logins:
                try:
                    await context.close()
                    await unregister_browser(browser)
                except:
                    pass


async def run_bot(config: Config = None) -> dict:
    """تشغيل البوت من الواجهة أو من سطر الأوامر."""
    global STOP_BOT_FLAG, _BOT_LOOP
    STOP_BOT_FLAG = False
    _BOT_LOOP = asyncio.get_running_loop()

    if config is None:
        config = Config.from_settings()

    # بروكسي من متغير البيئة إن وُجد (مفيد على VPS)
    env_proxy = (os.environ.get("TIKTOK_PROXY") or os.environ.get("PROXY") or "").strip()
    if env_proxy:
        config.proxy = env_proxy
        config.proxy_enabled = True

    logger.info("=" * 60)
    logger.info(f"TikTok Comment Bot | build={BOT_VERSION}")
    logger.info("=" * 60)
    logger.info(f"🎯 الفيديو المستهدف: {config.target_video_url}")
    logger.info(f"👤 بروفايل المراقبة: {config.profile_url}")
    logger.info(f"🎮 الوضع: {config.bot_mode}")
    logger.info(f"💬 تعليقات متبقية في المجمع: {remaining_count()}")
    logger.info(f"👥 أقصى عدد متصفحات متوازية: {config.max_browsers}")
    logger.info(f"📧 OTP تلقائي من Hostinger: {'نعم' if config.auto_otp else 'لا'}")
    if config.proxy_enabled and config.proxy:
        parsed = parse_proxy(config.proxy)
        if parsed:
            logger.info(f"🛡️ البروكسي: مفعّل → {parsed.get('server')}")
        else:
            logger.error("🛡️ البروكسي مفعّل لكن الصيغة خاطئة!")
    else:
        logger.warning("🛡️ البروكسي: غير مفعّل — على VPS غالباً راح يحوّلك تيك توك لـ /about")
    logger.info("=" * 60)

    if config.bot_mode in ("watch", "watch_comment"):
        if not (config.profile_url or config.target_video_url):
            logger.error("لم يتم تحديد رابط بروفايل للمراقبة")
            return {"ok": False, "error": "no_profile_url"}
    elif not config.target_video_url:
        logger.error("لم يتم تحديد رابط فيديو")
        return {"ok": False, "error": "no_video_url"}

    file_handler = FileHandler(config)
    accounts = file_handler.read_accounts()
    if not accounts:
        logger.error("لا توجد حسابات في acc.txt")
        return {"ok": False, "error": "no_accounts"}

    processor = AccountProcessor(accounts, config)
    await processor.process_all()
    report = await processor.stats.get_report()
    await _close_all_browsers()
    _BOT_LOOP = None
    return {"ok": True, "report": report, "stats": processor.stats.counters, "stopped": should_stop()}


async def main():
    """Основная функция скрипта"""
    logger.remove()
    logger.add("tiktok_checker.log", rotation="10 MB", level="INFO")
    logger.add(
        lambda msg: print(msg, end=""),
        colorize=True,
        level="INFO",
        format="{time:HH:mm:ss} | <level>{message}</level>"
    )
    await run_bot()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен пользователем")