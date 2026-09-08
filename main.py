"""Instagram engagement bot — profile like/story/comment + reel/post comment."""
import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

from email_otp import wait_for_otp, mark_otp_used
from comments_pool import take_comment, remaining_count, migrate_from_settings, peek_status

BOT_VERSION = "2026-09-09-instagram-v12"

# بروكسي افتراضي — مفعّل دائماً إلا إذا غيّرته من اللوحة
DEFAULT_PROXY = "178.93.74.74:46459:ilIXTcXCyPrJyYm:7LMX2TY1odthIoK"
_XVFB_PROC = None

STOP_BOT_FLAG = False
ACTIVE_BROWSERS: List[Any] = []
_BOT_LOOP = None
LOGIN_OTP_LOCK: Optional[asyncio.Lock] = None


def _dbg(*_args, **_kwargs):
    """debug stub — لا يفعل شيئاً (كان يسبب NameError بعد الحذف)."""
    return None


def ensure_virtual_display() -> None:
    """على Linux بدون شاشة: شغّل Xvfb لفتح كروم نظامي (headed)."""
    global _XVFB_PROC
    if os.name == "nt":
        return
    if os.environ.get("DISPLAY"):
        logger.info(f"DISPLAY موجود: {os.environ['DISPLAY']}")
        return
    display = os.environ.get("IG_DISPLAY", ":99")
    import shutil
    import subprocess

    xvfb = shutil.which("Xvfb")
    if not xvfb:
        logger.error(
            "Xvfb غير مثبّت — نفّذ: sudo apt-get install -y xvfb "
            "ثم أعد تشغيل البوت (مطلوب لفتح متصفح نظامي على السيرفر)"
        )
        os.environ["DISPLAY"] = display
        return
    try:
        _XVFB_PROC = subprocess.Popen(
            [xvfb, display, "-screen", "0", "1920x1080x24", "-ac", "+extension", "RANDR"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = display
        time.sleep(0.8)
        logger.success(f"تم تشغيل Xvfb على {display} — المتصفح سيُفتح بواجهة نظامية")
    except Exception as e:
        logger.error(f"فشل تشغيل Xvfb: {e}")
        os.environ["DISPLAY"] = display


def find_system_chrome() -> Optional[str]:
    """مسار كروم/كروميوم النظامي على Linux أو Windows."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def request_stop_bot():
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


def get_login_otp_lock() -> asyncio.Lock:
    global LOGIN_OTP_LOCK
    if LOGIN_OTP_LOCK is None:
        LOGIN_OTP_LOCK = asyncio.Lock()
    return LOGIN_OTP_LOCK


def parse_proxy(raw: str) -> Optional[Dict[str, str]]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
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
    parts = raw.split(":")
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": password,
        }
    return None


async def launch_browser(playwright, config: "Config"):
    # على السيرفر: متصفح نظامي بواجهة (headed) عبر Xvfb
    if os.name != "nt" and not config.browser_headless:
        ensure_virtual_display()

    args = list(config.browser_args)
    if config.browser_headless:
        args = [a for a in args if not a.startswith("--headless")]
        args.append("--headless=new")
    else:
        args = [a for a in args if not a.startswith("--headless")]

    kwargs = {
        "headless": bool(config.browser_headless),
        "args": args,
    }
    if getattr(config, "proxy_enabled", False) and getattr(config, "proxy", ""):
        proxy_cfg = parse_proxy(config.proxy)
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg
            logger.info(
                f"استخدام بروكسي: {proxy_cfg.get('server')} "
                f"(user={'نعم' if proxy_cfg.get('username') else 'لا'})"
            )
        else:
            logger.warning("صيغة البروكسي غير صحيحة — بدون بروكسي")

    chrome = find_system_chrome()
    if chrome:
        kwargs["executable_path"] = chrome
        logger.info(
            f"متصفح نظامي: {chrome} | "
            f"وضع={'headless' if config.browser_headless else 'واجهة (headed)'}"
        )
        browser = await playwright.chromium.launch(**kwargs)
    else:
        logger.warning("لم يُعثر على Chrome النظامي — استخدام Chromium من Playwright")
        try:
            browser = await playwright.chromium.launch(channel="chrome", **kwargs)
        except Exception:
            browser = await playwright.chromium.launch(**kwargs)
    ACTIVE_BROWSERS.append(browser)
    return browser


async def prepare_page(page: Page) -> None:
    """إخفاء علامات الأتمتة قدر الإمكان."""
    try:
        await page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )
    except Exception:
        pass


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


def load_settings() -> dict:
    defaults = {
        "target_video_url": "",
        "profile_url": "",
        "bot_mode": "full",  # profile | video | full
        "comment_texts": [],
        "comment_all_in_order": True,
        "enable_liking": True,
        "enable_commenting": True,
        "enable_sharing": True,  # story
        "enable_repost": True,
        "watch_count": 3,
        "max_browsers": 1,
        "browser_headless": False,  # متصفح نظامي بواجهة على VPS
        "imap_host": "imap.hostinger.com",
        "imap_port": 993,
        "otp_timeout": 90,
        "auto_otp": True,
        "dashboard_host": "0.0.0.0",
        "dashboard_port": 5050,
        "proxy_enabled": True,
        "proxy": DEFAULT_PROXY,
        "force_relogin": False,
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
    sadcaptcha_api_key: str = ""
    target_video_url: str = ""
    profile_url: str = ""
    bot_mode: str = "full"
    comment_all_in_order: bool = True
    comment_texts: List[str] = field(default_factory=list)
    max_browsers: int = 1
    browser_headless: bool = False  # واجهة نظامية (headed) افتراضياً
    max_check_attempts: int = 1
    proxy_enabled: bool = True
    proxy: str = DEFAULT_PROXY
    force_relogin: bool = False
    page_timeout: int = 45
    action_delay: float = 1.0
    enable_commenting: bool = True
    enable_liking: bool = True
    enable_sharing: bool = True
    enable_repost: bool = True
    watch_count: int = 3
    auto_otp: bool = True
    imap_host: str = "imap.hostinger.com"
    imap_port: int = 993
    otp_timeout: int = 90
    enable_hanging: bool = False
    hang_check_interval: int = 60
    browser_args: List[str] = field(default_factory=lambda: [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--disable-blink-features=AutomationControlled",
    ])
    browser_context_options: Dict[str, Any] = field(default_factory=lambda: {
        "viewport": {"width": 1365, "height": 900},
        "ignore_https_errors": True,
        "java_script_enabled": True,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "color_scheme": "light",
        "device_scale_factor": 1,
        "has_touch": False,
        "is_mobile": False,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        },
    })

    @classmethod
    def from_settings(cls) -> "Config":
        s = load_settings()
        cfg = cls()
        cfg.target_video_url = (s.get("target_video_url") or "").strip()
        cfg.profile_url = (s.get("profile_url") or "").strip()
        cfg.bot_mode = (s.get("bot_mode") or "full").strip().lower()
        # migrate old tiktok modes
        if cfg.bot_mode in ("watch", "watch_comment"):
            cfg.bot_mode = "profile"
        if cfg.bot_mode == "comment":
            cfg.bot_mode = "video"
        cfg.comment_texts = s.get("comment_texts") or []
        cfg.comment_all_in_order = bool(s.get("comment_all_in_order", True))
        cfg.enable_liking = bool(s.get("enable_liking", True))
        cfg.enable_commenting = bool(s.get("enable_commenting", True))
        cfg.enable_sharing = bool(s.get("enable_sharing", True))
        cfg.enable_repost = bool(s.get("enable_repost", True))
        cfg.watch_count = int(s.get("watch_count", 3) or 3)
        cfg.max_browsers = max(1, int(s.get("max_browsers", 1) or 1))
        cfg.browser_headless = bool(s.get("browser_headless", False))
        # على Linux: افتح متصفح نظامي بواجهة (إلا إذا IG_HEADLESS=1)
        if os.name != "nt" and os.environ.get("IG_HEADLESS", "").strip() not in ("1", "true", "yes"):
            cfg.browser_headless = False
        cfg.proxy = (s.get("proxy") or "").strip() or DEFAULT_PROXY
        # إذا في بروكسي → شغّالو (إلا إذا صراحة proxy_enabled=false وبلا قيمة)
        if "proxy_enabled" in s:
            cfg.proxy_enabled = bool(s.get("proxy_enabled"))
        else:
            cfg.proxy_enabled = True
        if cfg.proxy and s.get("proxy_enabled") is not False:
            cfg.proxy_enabled = True
        cfg.force_relogin = bool(s.get("force_relogin", False))
        cfg.auto_otp = bool(s.get("auto_otp", True))
        cfg.imap_host = (s.get("imap_host") or "imap.hostinger.com").strip()
        cfg.imap_port = int(s.get("imap_port", 993) or 993)
        cfg.otp_timeout = int(s.get("otp_timeout", 90) or 90)
        return cfg


@dataclass
class Stats:
    start_time: datetime = field(default_factory=datetime.now)
    counters: Dict[str, Any] = field(default_factory=lambda: {
        "total_accounts": 0,
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "errors": 0,
        "likes": 0,
        "comments": 0,
        "stories": 0,
        "reposts": 0,
        "shares": 0,
    })

    async def increment(self, key: str, amount: int = 1):
        self.counters[key] = self.counters.get(key, 0) + amount

    async def get_report(self) -> str:
        elapsed = datetime.now() - self.start_time
        c = self.counters
        return (
            f"إحصائيات:\n"
            f"وقت العمل: {elapsed}\n"
            f"معالج: {c.get('processed', 0)}/{c.get('total_accounts', 0)} | "
            f"نجاح: {c.get('successful', 0)} | فشل: {c.get('failed', 0)} | أخطاء: {c.get('errors', 0)}\n"
            f"إجراءات: تعليقات={c.get('comments', 0)} | لايك={c.get('likes', 0)} | "
            f"ريبوست={c.get('reposts', 0)} | ستوري={c.get('stories', 0)} | شير={c.get('shares', 0)}"
        )


class FileHandler:
    def __init__(self, config: Config):
        self.config = config
        self.output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
        os.makedirs(self.output_dir, exist_ok=True)

    def session_path(self, login: str) -> str:
        safe = re.sub(r"[^\w.@+-]+", "_", login.replace("@", "_at_"))
        return os.path.join(self.output_dir, f"{safe}_session.json")

    def save_account(self, login: str, password: str) -> bool:
        path = os.path.join(self.output_dir, f"{login}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{login}:{password}\n")
            logger.info(f"حساب {login} — صالح ✓ | محفوظ في {path}")
            return True
        except Exception as e:
            logger.warning(f"فشل حفظ الحساب: {e}")
            return False


def read_accounts(config: Config) -> List[Dict]:
    accounts = []
    mailboxes = {(m.get("email") or "").lower(): m for m in load_mailboxes()}
    for item in load_accounts_json():
        login = (item.get("email") or item.get("username") or "").strip()
        password = (item.get("password") or "").strip()
        if not login or not password:
            continue
        mailbox_email = (item.get("mailbox") or item.get("mailbox_email") or "").strip()
        mb = mailboxes.get(mailbox_email.lower()) if mailbox_email else None
        accounts.append({
            "email": login,
            "password": password,
            "mailbox_email": mailbox_email or login,
            "email_password": (mb or {}).get("password") or password,
        })
    logger.info(f"تم تحميل {len(accounts)} حساب من accounts.json")
    return accounts


def normalize_ig_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("?")[0].split("#")[0].rstrip("/")
    if url.startswith("@"):
        return f"https://www.instagram.com/{url[1:]}"
    if "instagram.com" not in url and re.match(r"^[\w.]+$", url):
        return f"https://www.instagram.com/{url}"
    return url


def extract_username(url: str) -> Optional[str]:
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", url or "")
    if not m:
        return None
    user = m.group(1)
    if user.lower() in ("p", "reel", "reels", "stories", "explore", "accounts", "direct"):
        return None
    return user


class Human:
    """حركات وتأخيرات شبيهة بالإنسان لتقليل كشف الأتمتة."""

    def __init__(self, page: Page):
        self.page = page

    async def pause(self, lo: float = 0.7, hi: float = 2.2) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def think(self) -> None:
        """توقف قصير كأن المستخدم يقرأ."""
        await self.pause(1.5, 4.0)

    async def read_post(self) -> None:
        await self.pause(2.5, 6.0)

    async def between_actions(self) -> None:
        await self.pause(1.8, 4.5)

    async def between_posts(self) -> None:
        await self.pause(4.0, 11.0)

    async def move_around(self) -> None:
        try:
            vp = self.page.viewport_size or {"width": 1280, "height": 900}
            for _ in range(random.randint(1, 3)):
                x = random.randint(80, max(120, vp["width"] - 80))
                y = random.randint(80, max(120, vp["height"] - 80))
                await self.page.mouse.move(x, y, steps=random.randint(8, 22))
                await self.pause(0.15, 0.55)
        except Exception:
            pass

    async def scroll_feed(self) -> None:
        try:
            delta = random.randint(180, 520) * random.choice([1, 1, -1])
            await self.page.mouse.wheel(0, delta)
            await self.pause(0.4, 1.2)
            if random.random() < 0.4:
                await self.page.mouse.wheel(0, -abs(delta) // 3)
                await self.pause(0.3, 0.8)
        except Exception:
            pass

    async def human_click(self, locator, force: bool = False) -> bool:
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
            await self.pause(0.2, 0.7)
            box = await locator.bounding_box()
            if box and box.get("width", 0) > 2 and box.get("height", 0) > 2:
                x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
                y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
                await self.page.mouse.move(x, y, steps=random.randint(6, 18))
                await self.pause(0.08, 0.35)
                await self.page.mouse.click(x, y, delay=random.randint(40, 120))
                return True
            if force:
                await locator.click(force=True, timeout=4000)
            else:
                await locator.click(timeout=4000)
            return True
        except Exception:
            try:
                await locator.click(force=True, timeout=3000)
                return True
            except Exception:
                return False

    async def human_type(self, text: str) -> None:
        for i, ch in enumerate(text):
            await self.page.keyboard.type(ch, delay=random.randint(45, 160))
            if random.random() < 0.07:
                await self.pause(0.2, 0.7)
            if i > 0 and i % random.randint(8, 14) == 0:
                await self.pause(0.15, 0.45)


class InstagramBot:
    def __init__(self, page: Page, config: Config, stats: Stats):
        self.page = page
        self.config = config
        self.stats = stats
        self.human = Human(page)

    async def dismiss_overlays(self):
        for sel in [
            'button:has-text("Allow all cookies")',
            'button:has-text("Accept all")',
            'button:has-text("Allow all")',
            'button:has-text("Not Now")',
            'button:has-text("Not now")',
            'button:has-text("Turn On")',  # notifications — skip by Not Now preferred
            'div[role="dialog"] button:has-text("Not Now")',
            'svg[aria-label="Close"]',
        ]:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    # لا تضغط Turn On للإشعارات
                    txt = ""
                    try:
                        txt = (await btn.inner_text() or "").lower()
                    except Exception:
                        pass
                    if "turn on" in txt:
                        continue
                    await btn.click(force=True, timeout=2000)
                    await asyncio.sleep(0.6)
            except Exception:
                continue
        # Close via aria-label
        try:
            close = self.page.locator('[aria-label="Close"], svg[aria-label="Close"]').first
            if await close.count() > 0 and await close.is_visible():
                parent = close.locator("xpath=ancestor::button[1]")
                if await parent.count() > 0:
                    await parent.click(force=True)
                else:
                    await close.click(force=True)
                await asyncio.sleep(0.5)
        except Exception:
            pass

    async def wait_for_post_ready(self, account: str, timeout_s: int = 20) -> bool:
        """ينتظر ظهور منشور صورة أو فيديو/ريل."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await self.dismiss_overlays()
            ready = await self.page.evaluate(
                """() => {
                    const hasMedia = !!(
                      document.querySelector('article video, article img, main video, main img[srcset], div[role="dialog"] video, div[role="dialog"] img')
                    );
                    const hasActions = !!(
                      document.querySelector('svg[aria-label="Like"], svg[aria-label="Unlike"], svg[aria-label="Comment"], svg[aria-label="Share"], svg[aria-label="Share Post"]')
                    );
                    return {hasMedia, hasActions, url: location.href};
                }"""
            )
            if ready.get("hasMedia") or ready.get("hasActions"):
                logger.info(
                    f"[{account}] المنشور جاهز (media={ready.get('hasMedia')} actions={ready.get('hasActions')})"
                )
                return True
            await asyncio.sleep(1)
        logger.warning(f"[{account}] المنشور لم يكتمل تحميله | URL={self.page.url}")
        return False

    async def dump_action_dom(self, account: str, tag: str) -> None:
        try:
            info = await self.page.evaluate(
                """() => {
                    const labels = Array.from(document.querySelectorAll('[aria-label]'))
                      .slice(0, 40)
                      .map(e => e.getAttribute('aria-label'));
                    const areas = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]'))
                      .slice(0, 10)
                      .map(e => ({
                        tag: e.tagName,
                        aria: e.getAttribute('aria-label'),
                        ph: e.getAttribute('placeholder'),
                        visible: !!(e.offsetParent || e.getClientRects().length)
                      }));
                    return {
                      url: location.href,
                      labels: [...new Set(labels)],
                      inputs: areas,
                      hasVideo: !!document.querySelector('video'),
                      hasImg: !!document.querySelector('article img, main img'),
                      body: (document.body?.innerText||'').replace(/\\s+/g,' ').slice(0,350)
                    };
                }"""
            )
            logger.warning(f"[{account}] {tag}: {info}")
            # #region agent log
            _dbg("ACT", "main.py:dump_action_dom", tag, {"account": account, "info": info})
            # #endregion
        except Exception as e:
            logger.warning(f"[{account}] {tag} dump failed: {e}")

    async def is_liked(self) -> bool:
        liked = await self.page.evaluate(
            """() => {
                const sels = [
                  'svg[aria-label="Unlike"]',
                  'svg[aria-label="Remove Like"]',
                  '[aria-label="Unlike"]',
                  '[aria-label="Remove Like"]',
                ];
                for (const s of sels) {
                  const el = document.querySelector(s);
                  if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                  }
                }
                // أحياناً الزر أحمر بدون Unlike واضح — مسار بديل
                const path = document.querySelector('span svg[aria-label="Unlike"], section svg[aria-label="Unlike"]');
                return !!path;
            }"""
        )
        return bool(liked)

    async def like_current(self, account: str) -> bool:
        if not self.config.enable_liking:
            return False
        await self.dismiss_overlays()
        await self.wait_for_post_ready(account, timeout_s=12)
        await self.human.move_around()
        await self.human.scroll_feed()
        await self.human.read_post()

        if await self.is_liked():
            logger.info(f"[{account}] اللايك موجود مسبقاً — تخطي")
            return True

        for sel in [
            'main button:has(svg[aria-label="Like"])',
            'article button:has(svg[aria-label="Like"])',
            'div[role="dialog"] button:has(svg[aria-label="Like"])',
            'section button:has(svg[aria-label="Like"])',
            'button:has(svg[aria-label="Like"])',
            'svg[aria-label="Like"]',
            '[aria-label="Like"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                if await self.human.human_click(loc):
                    await self.human.pause(1.0, 2.2)
                    if await self.is_liked():
                        await self.stats.increment("likes")
                        logger.success(f"[{account}] تم عمل لايك")
                        return True
            except Exception:
                continue

        try:
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('svg[aria-label="Like"], [aria-label="Like"]'));
                    const el = nodes.find(n => {
                      const r = n.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    });
                    if (!el) return false;
                    const btn = el.closest('button,div[role="button"],span') || el;
                    btn.click();
                    return true;
                }"""
            )
            await self.human.pause(1.0, 2.0)
            if clicked and await self.is_liked():
                await self.stats.increment("likes")
                logger.success(f"[{account}] تم عمل لايك (JS)")
                return True
        except Exception:
            pass

        try:
            media = self.page.locator(
                'article video, article img, main video, div[role="dialog"] video, div[role="dialog"] img'
            ).first
            if await media.count() > 0 and await media.is_visible():
                box = await media.bounding_box()
                if box:
                    x = box["x"] + box["width"] * random.uniform(0.4, 0.6)
                    y = box["y"] + box["height"] * random.uniform(0.4, 0.6)
                    await self.page.mouse.move(x, y, steps=random.randint(8, 16))
                    await self.human.pause(0.2, 0.5)
                    await self.page.mouse.dblclick(x, y, delay=random.randint(50, 120))
                    await self.human.pause(1.2, 2.5)
                    if await self.is_liked():
                        await self.stats.increment("likes")
                        logger.success(f"[{account}] تم عمل لايك (double-click على الوسائط)")
                        return True
        except Exception:
            pass

        await self.dump_action_dom(account, "like-fail")
        logger.warning(f"[{account}] تعذر عمل لايك")
        return False

    async def comment_current(self, account: str, text: str = None) -> bool:
        if not self.config.enable_commenting:
            return False
        if not text:
            text = take_comment(account)
        if not text:
            logger.warning(f"[{account}] لا يوجد تعليق في المجمع")
            return False

        await self.dismiss_overlays()
        await self.wait_for_post_ready(account, timeout_s=10)
        await self.human.between_actions()
        await self.human.move_around()

        async def find_box():
            sels = [
                'textarea[aria-label*="Add a comment" i]',
                'textarea[placeholder*="Add a comment" i]',
                'textarea[aria-label*="comment" i]',
                'textarea[placeholder*="comment" i]',
                'form textarea',
                'div[contenteditable="true"][aria-label*="comment" i]',
                'div[contenteditable="true"][role="textbox"]',
                'p[contenteditable="true"]',
                '[role="textbox"]',
            ]
            for sel in sels:
                try:
                    loc = self.page.locator(sel)
                    n = await loc.count()
                    for i in range(min(n, 5)):
                        item = loc.nth(i)
                        if await item.is_visible():
                            return item
                except Exception:
                    continue
            return None

        box = await find_box()
        if not box:
            for sel in [
                'button:has(svg[aria-label="Comment"])',
                'svg[aria-label="Comment"]',
                '[aria-label="Comment"]',
            ]:
                try:
                    cbtn = self.page.locator(sel).first
                    if await cbtn.count() > 0 and await cbtn.is_visible():
                        await self.human.human_click(cbtn)
                        await self.human.pause(1.2, 2.5)
                        break
                except Exception:
                    continue
            box = await find_box()

        if not box:
            try:
                focused = await self.page.evaluate(
                    """() => {
                        const eds = Array.from(document.querySelectorAll(
                          'textarea, [contenteditable="true"], [role="textbox"]'
                        ));
                        const el = eds.find(e => {
                          const r = e.getBoundingClientRect();
                          return r.width > 40 && r.height > 10;
                        });
                        if (!el) return false;
                        el.focus();
                        el.click();
                        return true;
                    }"""
                )
                if focused:
                    await self.human.pause(0.4, 0.9)
                    box = await find_box()
            except Exception:
                pass

        if not box:
            await self.dump_action_dom(account, "comment-fail")
            logger.error(f"[{account}] لم يجد حقل التعليق")
            return False

        try:
            await self.human.human_click(box)
            await self.human.pause(0.3, 0.8)
            try:
                await box.fill("")
            except Exception:
                pass
            await self.human.human_type(text)
            await self.human.pause(0.6, 1.4)

            posted = False
            for sel in [
                'form div[role="button"]:has-text("Post")',
                'div[role="button"]:has-text("Post")',
                'button:has-text("Post")',
                'div[role="button"]:has-text("Publish")',
                'button:has-text("Publish")',
                '[role="button"]:has-text("Post")',
            ]:
                btn = self.page.locator(sel).first
                try:
                    if await btn.count() > 0 and await btn.is_visible():
                        disabled = await btn.get_attribute("aria-disabled")
                        if disabled == "true":
                            continue
                        await self.human.human_click(btn)
                        posted = True
                        break
                except Exception:
                    continue
            if not posted:
                await self.human.pause(0.2, 0.5)
                await self.page.keyboard.press("Enter")
                posted = True

            await self.human.pause(2.0, 4.0)
            await self.stats.increment("comments")
            logger.success(f"[{account}] تم نشر التعليق: {text}")
            return True
        except Exception as e:
            logger.warning(f"[{account}] فشل التعليق: {type(e).__name__}: {e}")
            await self.dump_action_dom(account, "comment-exception")
            return False

    async def _open_share_sheet(self, account: str) -> bool:
        await self.dismiss_overlays()
        share_btn = self.page.locator(
            'button:has(svg[aria-label="Share Post"]), button:has(svg[aria-label="Share"]), '
            'svg[aria-label="Share Post"], svg[aria-label="Share"]'
        ).first
        try:
            if await share_btn.count() == 0 or not await share_btn.is_visible():
                opened = await self.page.evaluate(
                    """() => {
                        const el = document.querySelector(
                          'svg[aria-label="Share Post"], svg[aria-label="Share"], [aria-label="Share Post"], [aria-label="Share"]'
                        );
                        if (!el) return false;
                        (el.closest('button,div[role="button"]') || el).click();
                        return true;
                    }"""
                )
                if not opened:
                    logger.warning(f"[{account}] زر الشير غير ظاهر")
                    return False
            else:
                await share_btn.click(force=True)
            await asyncio.sleep(1.8)
            return True
        except Exception as e:
            logger.warning(f"[{account}] فشل فتح قائمة الشير: {e}")
            return False

    async def _click_share_option(self, labels: list) -> bool:
        for label in labels:
            for sel in [
                f'button:has-text("{label}")',
                f'span:has-text("{label}")',
                f'div[role="button"]:has-text("{label}")',
                f'[role="menuitem"]:has-text("{label}")',
            ]:
                try:
                    opt = self.page.locator(sel).first
                    if await opt.count() > 0 and await opt.is_visible():
                        await opt.click(force=True)
                        await asyncio.sleep(1.5)
                        return True
                except Exception:
                    continue
        try:
            clicked = await self.page.evaluate(
                """(labels) => {
                    const nodes = Array.from(document.querySelectorAll('button,div[role="button"],span,div'));
                    for (const label of labels) {
                      const hit = nodes.find(n => {
                        const t = (n.innerText || '').replace(/\\s+/g,' ').trim();
                        return t === label || t.startsWith(label);
                      });
                      if (hit) {
                        const t = hit.closest('button,div[role="button"],div') || hit;
                        const r = t.getBoundingClientRect();
                        if (r.height > 0 && r.height < 120) { t.click(); return label; }
                        hit.click();
                        return label;
                      }
                    }
                    return null;
                }""",
                labels,
            )
            if clicked:
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass
        return False

    async def _confirm_share_dialog(self) -> None:
        for conf in [
            'button:has-text("Repost")',
            'div[role="button"]:has-text("Repost")',
            'button:has-text("Share")',
            'div[role="button"]:has-text("Share")',
            'button:has-text("Done")',
            'button:has-text("OK")',
        ]:
            c = self.page.locator(conf).first
            try:
                if await c.count() > 0 and await c.is_visible():
                    await c.click(force=True)
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                continue

    async def repost_current(self, account: str) -> bool:
        """ريبوست المنشور إلى الحساب."""
        if not getattr(self.config, "enable_repost", True):
            return False
        if not await self._open_share_sheet(account):
            return False

        ok = await self._click_share_option(["Repost", "Repost to", "Repost this"])
        if ok:
            await self._confirm_share_dialog()
            await self._click_share_option(["Repost"])
            await self._confirm_share_dialog()
            await self.stats.increment("reposts")
            logger.success(f"[{account}] تم عمل Repost للمنشور")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await self.dismiss_overlays()
            return True

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await self.dismiss_overlays()
        logger.warning(f"[{account}] خيار Repost غير ظاهر على الويب لهذا المنشور")
        return False

    async def share_to_story(self, account: str) -> bool:
        """إضافة المنشور للستوري عبر قائمة Share."""
        if not self.config.enable_sharing:
            return False
        if not await self._open_share_sheet(account):
            return False

        ok = await self._click_share_option([
            "Add to story",
            "Add to your story",
            "Add post to your story",
        ])
        if ok:
            await self._confirm_share_dialog()
            await self.stats.increment("stories")
            logger.success(f"[{account}] تمت إضافة المنشور إلى الستوري")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await self.dismiss_overlays()
            return True

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await self.dismiss_overlays()
        logger.warning(f"[{account}] Add to story غير متاح على الويب")
        return False

    async def engage_profile(self, account: str) -> bool:
        profile = normalize_ig_url(self.config.profile_url)
        if not profile:
            logger.error(f"[{account}] رابط البروفايل فارغ")
            return False

        logger.info(f"[{account}] فتح البروفايل: {profile}")
        await self.page.goto(profile, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await self.dismiss_overlays()

        # محاولة التفاعل مع الستوري إن وُجدت حلقة ستوري
        if self.config.enable_sharing:
            await self._try_view_story(account)

        limit = self.config.watch_count if self.config.watch_count > 0 else 3
        posts = self.page.locator('a[href*="/p/"], a[href*="/reel/"]')
        count = await posts.count()
        if count == 0:
            logger.warning(f"[{account}] لا توجد منشورات ظاهرة على البروفايل")
            return False

        logger.info(f"[{account}] منشورات ظاهرة: {count} — سنعالج حتى {limit}")
        opened = 0
        for i in range(min(count, max(limit * 2, limit))):
            if should_stop() or opened >= limit:
                break
            try:
                # أعد تحميل البروفايل كل منشور لروابط ثابتة
                if i > 0:
                    await self.page.goto(profile, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(2)
                    await self.dismiss_overlays()
                    posts = self.page.locator('a[href*="/p/"], a[href*="/reel/"]')

                post = posts.nth(i)
                if await post.count() == 0:
                    continue
                href = await post.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.instagram.com" + href
                logger.info(f"[{account}] فتح منشور: {href}")
                await self.page.goto(href, wait_until="domcontentloaded", timeout=45000)
                await self.human.pause(2.0, 4.5)
                await self.dismiss_overlays()
                await self.human.read_post()

                await self.like_current(account)
                await self.human.between_actions()
                if self.config.enable_commenting:
                    await self.comment_current(account)
                    await self.human.between_actions()
                if getattr(self.config, "enable_repost", True):
                    await self.repost_current(account)
                    await self.human.between_actions()
                await self.share_to_story(account)
                opened += 1
                await self.human.between_posts()
            except Exception as e:
                logger.warning(f"[{account}] خطأ منشور #{i}: {type(e).__name__}: {e}")
                continue

        return opened > 0

    async def _try_view_story(self, account: str):
        try:
            # story ring often on profile header canvas/img with aria
            story = self.page.locator(
                'header canvas, header img[alt*="profile picture" i], '
                'div[role="button"]:has(canvas), header span:has(canvas)'
            ).first
            # better: click the profile pic that opens story if colorful ring
            header_btn = self.page.locator('header section header div[role="button"]').first
            if await header_btn.count() > 0 and await header_btn.is_visible():
                await header_btn.click(force=True)
                await asyncio.sleep(2)
                # if story viewer opened
                viewer = self.page.locator('div[role="dialog"], section[aria-label*="Story" i]').first
                body = (await self.page.inner_text("body"))[:500].lower()
                if "story" in body or await viewer.count() > 0:
                    logger.info(f"[{account}] فتح ستوري البروفايل")
                    # optional like on story
                    like = self.page.locator('svg[aria-label="Like"], [aria-label="Like"]').first
                    if await like.count() > 0 and await like.is_visible():
                        try:
                            await like.click(force=True)
                            await self.stats.increment("stories")
                            logger.success(f"[{account}] لايك على الستوري")
                        except Exception:
                            pass
                    await asyncio.sleep(2)
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.debug(f"[{account}] لا ستوري أو تعذر فتحها: {e}")

    async def engage_video(self, account: str) -> bool:
        url = normalize_ig_url(self.config.target_video_url)
        if not url:
            logger.error(f"[{account}] رابط الفيديو/الريل فارغ")
            return False
        # احتفظ بالمسار فقط — إنستغرام يفتح /p/ و /reel/ للصور والفيديو
        logger.info(f"[{account}] فتح المنشور المستهدف: {url}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self.human.pause(2.5, 5.0)
        await self.dismiss_overlays()
        await self.human.move_around()
        await self.wait_for_post_ready(account, timeout_s=20)
        await self.human.read_post()

        liked = False
        commented = False
        reposted = False
        story = False
        if self.config.enable_liking:
            liked = await self.like_current(account)
            await self.human.between_actions()
        if self.config.enable_commenting:
            commented = await self.comment_current(account)
            await self.human.between_actions()
        if getattr(self.config, "enable_repost", True):
            reposted = await self.repost_current(account)
            await self.human.between_actions()
        if self.config.enable_sharing:
            story = await self.share_to_story(account)

        ok = liked or commented or reposted or story
        logger.info(
            f"[{account}] نتيجة المنشور: like={liked} comment={commented} "
            f"repost={reposted} story={story}"
        )
        return ok


class InstagramChecker:
    def __init__(self, config: Config, stats: Stats):
        self.config = config
        self.stats = stats
        self.file_handler = FileHandler(config)

    async def page_is_rate_limited(self, page: Page) -> bool:
        url = (page.url or "").lower()
        if "chrome-error://" in url:
            return True
        try:
            body = (await page.inner_text("body"))[:800].lower()
        except Exception:
            body = ""
        markers = (
            "http error 429",
            "429",
            "too many requests",
            "please wait a few minutes",
            "try again later",
            "this page isn’t working",
            "this page isn't working",
        )
        return any(m in body for m in markers)

    async def wait_out_rate_limit(self, page: Page, account: str, attempt: int = 1) -> bool:
        """عند 429: انتظر ثم أعد المحاولة بدل الفشل فوراً."""
        wait_s = min(45 + attempt * 30, 180)
        logger.error(
            f"[{account}] إنستغرام رجّع HTTP 429 (طلبات كثيرة / البروكسي محروق مؤقتاً). "
            f"انتظار {wait_s}ث ثم إعادة المحاولة..."
        )
        await asyncio.sleep(wait_s)
        for url in (
            "https://www.instagram.com/",
            "https://i.instagram.com/",
            "https://www.instagram.com/accounts/login/?hl=en",
        ):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"[{account}] إعادة بعد 429 فشلت على {url}: {e}")
            await asyncio.sleep(3 + attempt)
            if not await self.page_is_rate_limited(page) and "chrome-error://" not in (page.url or ""):
                logger.success(f"[{account}] خرجنا من 429 — الصفحة: {page.url}")
                return True
        return False

    async def safe_goto(self, page: Page, url: str, account: str) -> bool:
        last_err = None
        for attempt in range(1, 4):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)
                if await self.page_is_rate_limited(page):
                    ok = await self.wait_out_rate_limit(page, account, attempt)
                    if ok:
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as e:
                            last_err = e
                            continue
                        return "instagram.com" in (page.url or "")
                    continue
                return True
            except Exception as e:
                last_err = e
                msg = str(e)
                logger.warning(f"[{account}] تنبيه فتح {url}: {type(e).__name__}: {e}")
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" in msg or "429" in msg:
                    await self.wait_out_rate_limit(page, account, attempt)
                    continue
                if "instagram.com" in (page.url or ""):
                    return True
                await asyncio.sleep(3 * attempt)
        if last_err:
            logger.error(f"[{account}] فشل فتح {url} بعد عدة محاولات")
        return "instagram.com" in (page.url or "")

    async def is_logged_in(self, page: Page) -> bool:
        """تحقق صارم — لا نعتبر أي صفحة إنستغرام دخولاً ناجحاً."""
        url = (page.url or "").lower()
        if "/accounts/login" in url or "/accounts/emailsignup" in url or "/challenge" in url:
            return False

        # مؤشرات عدم الدخول
        for sel in [
            'input[name="username"]',
            'input[name="password"]',
            'a[href="/accounts/login/"]',
            'a[href*="/accounts/login"]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return False
            except Exception:
                continue

        try:
            login_cta = page.locator('a:has-text("Log in"), button:has-text("Log in")').first
            if await login_cta.count() > 0 and await login_cta.is_visible():
                return False
        except Exception:
            pass

        # مؤشرات دخول حقيقية فقط
        for sel in [
            'svg[aria-label="Home"]',
            'svg[aria-label="New post"]',
            'svg[aria-label="Reels"]',
            'svg[aria-label="Direct"]',
            'svg[aria-label="Messenger"]',
            'a[href*="/direct/inbox"]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def fill_otp_if_needed(
        self, page: Page, account: str, mailbox: str, mailbox_pass: str, after_ts: float
    ) -> bool:
        # challenge / security code inputs
        code_sel = [
            'input[name="verificationCode"]',
            'input[name="email"]',
            'input[aria-label*="code" i]',
            'input[placeholder*="code" i]',
            'input[name="security_code"]',
            'input[type="tel"]',
        ]
        has_input = False
        for sel in code_sel:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    has_input = True
                    break
            except Exception:
                continue

        body = ""
        try:
            body = (await page.inner_text("body"))[:2000].lower()
        except Exception:
            pass
        needs = has_input or "security code" in body or "confirmation code" in body or "enter the code" in body
        if not needs or not self.config.auto_otp or not mailbox_pass:
            return False

        logger.info(f"[{account}] جلب OTP إنستغرام من Hostinger ({mailbox})...")
        code = await asyncio.to_thread(
            wait_for_otp,
            mailbox,
            mailbox_pass,
            timeout=self.config.otp_timeout,
            poll_interval=4,
            imap_host=self.config.imap_host,
            imap_port=self.config.imap_port,
            after_ts=after_ts,
            for_account=account,
        )
        if not code:
            logger.warning(f"[{account}] لم يصل OTP")
            return False

        for sel in code_sel:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click(force=True)
                await loc.fill(code)
                mark_otp_used(code)
                for btn_sel in [
                    'button:has-text("Confirm")',
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                    'button:has-text("Submit")',
                    'button[type="submit"]',
                ]:
                    btn = page.locator(btn_sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(force=True)
                        break
                logger.success(f"[{account}] تم إدخال OTP: {code}")
                await asyncio.sleep(3)
                return True
            except Exception:
                continue
        return False

    async def log_page_state(self, page: Page, account: str, tag: str) -> None:
        try:
            info = await page.evaluate(
                """() => ({
                    url: location.href,
                    title: document.title,
                    inputs: Array.from(document.querySelectorAll('input')).slice(0,12).map(i => ({
                      name: i.name, type: i.type, ph: i.placeholder,
                      visible: !!(i.offsetParent || i.getClientRects().length)
                    })),
                    buttons: Array.from(document.querySelectorAll('button')).slice(0,10).map(b => ({
                      text: (b.innerText||'').trim().slice(0,40),
                      visible: !!(b.offsetParent || b.getClientRects().length)
                    })),
                    body: (document.body?.innerText||'').replace(/\\s+/g,' ').slice(0,450)
                })"""
            )
            logger.warning(f"[{account}] {tag}: {info}")
            # #region agent log
            _dbg("LOGIN", "main.py:log_page_state", tag, {"account": account, "info": info})
            # #endregion
        except Exception as e:
            logger.warning(f"[{account}] {tag} failed: {e} | URL={page.url}")

    async def dismiss_cookie_banners(self, page: Page) -> None:
        for sel in [
            'button:has-text("Allow all cookies")',
            'button:has-text("Allow all")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Only allow essential")',
            'button:has-text("Decline optional")',
            '[role="dialog"] button:has-text("Allow")',
        ]:
            try:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible():
                    await b.click(force=True, timeout=2000)
                    await asyncio.sleep(0.8)
            except Exception:
                continue

    async def page_looks_blank(self, page: Page) -> bool:
        try:
            info = await page.evaluate(
                """() => ({
                    textLen: (document.body?.innerText || '').trim().length,
                    inputs: document.querySelectorAll('input').length,
                    buttons: document.querySelectorAll('button').length,
                    ready: document.readyState
                })"""
            )
            return (info.get("textLen", 0) < 20 and info.get("inputs", 0) == 0)
        except Exception:
            return True

    async def recover_blank_login_page(self, page: Page, account: str) -> bool:
        """محاولات إنقاذ عندما إنستغرام يرجع صفحة فارغة على VPS."""
        logger.error(
            f"[{account}] صفحة إنستغرام فارغة (غالباً حظر IP السيرفر أو headless). جاري المحاولة..."
        )
        for i, url in enumerate([
            "https://www.instagram.com/",
            "https://www.instagram.com/accounts/login/",
            "https://www.instagram.com/accounts/login/?hl=en",
        ]):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"[{account}] تنقل #{i}: {e}")
            await asyncio.sleep(4 + i * 2)
            await self.dismiss_cookie_banners(page)
            # انتظر أي input يظهر
            try:
                await page.wait_for_selector(
                    'input[name="username"], input[name="password"], input[type="text"]',
                    timeout=12000,
                )
            except Exception:
                pass
            if not await self.page_looks_blank(page):
                logger.info(f"[{account}] الصفحة صارت تحتوي محتوى بعد المحاولة #{i+1}")
                return True
        await self.log_page_state(page, account, "blank-page-unrecoverable")
        logger.error(
            f"[{account}] إنستغرام ما زال يعرض صفحة فارغة من هذا السيرفر. "
            f"الحل: بروكسي سكني نظيف، أو شغّل المتصفح بواجهة (xvfb + headless=false)، "
            f"أو سجّل الدخول مرة من جهاز عادي واحفظ الجلسة."
        )
        return False

    async def wait_for_login_form(self, page: Page, account: str, timeout_s: int = 50):
        """ينتظر ظهور حقول الدخول مع عدة محاولات تنقل."""
        if await self.page_looks_blank(page):
            await self.recover_blank_login_page(page, account)

        deadline = time.time() + timeout_s
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            await self.dismiss_cookie_banners(page)

            if await self.page_is_rate_limited(page):
                if not await self.wait_out_rate_limit(page, account, attempt):
                    await self.log_page_state(page, account, "rate-limit-429")
                    return None, None
                continue

            if await self.page_looks_blank(page) and attempt in (2, 5):
                await self.recover_blank_login_page(page, account)

            for sel in [
                'a[href="/accounts/login/"]',
                'a[href*="/accounts/login"]',
                'button:has-text("Log in")',
                'a:has-text("Log in")',
            ]:
                try:
                    a = page.locator(sel).first
                    if await a.count() > 0 and await a.is_visible():
                        if await page.locator('input[name="username"]').count() == 0:
                            await a.click(force=True)
                            await asyncio.sleep(2)
                except Exception:
                    pass

            user_sels = [
                'input[name="username"]',
                'input[aria-label="Phone number, username, or email"]',
                'input[aria-label*="username" i]',
                'input[placeholder*="username" i]',
                'input[autocomplete="username"]',
                'input[type="text"]',
            ]
            pass_sels = [
                'input[name="password"]',
                'input[aria-label="Password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
            ]
            user_input = None
            pass_input = None
            for sel in user_sels:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        user_input = loc
                        break
                except Exception:
                    continue
            for sel in pass_sels:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        pass_input = loc
                        break
                except Exception:
                    continue

            if user_input and pass_input:
                return user_input, pass_input

            if await self.is_logged_in(page):
                return None, None

            try:
                body = (await page.inner_text("body"))[:800].lower()
                if any(x in body for x in ("sorry, this page", "something went wrong", "try again later", "unavailable")):
                    logger.error(f"[{account}] صفحة خطأ/حظر من إنستغرام")
                    await self.log_page_state(page, account, "ig-blocked")
                    return None, None
            except Exception:
                pass

            if attempt in (1, 3, 6):
                logger.info(f"[{account}] انتظار فورم الدخول... URL={page.url}")
                try:
                    await page.goto(
                        "https://www.instagram.com/accounts/login/?hl=en",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(3)

            await asyncio.sleep(1.5)

        await self.log_page_state(page, account, "login-form-timeout")
        if await self.page_looks_blank(page):
            logger.error(
                f"[{account}] السبب الجذري: صفحة فارغة من إنستغرام على IP السيرفر — "
                f"مو مشكلة يوزر/باسورد"
            )
        return None, None

    async def login(self, page: Page, account: Dict) -> bool:
        login = account["email"]
        password = account["password"]
        mailbox = account.get("mailbox_email") or login
        mailbox_pass = account.get("email_password") or password

        logger.info(f"[{login}] إحماء إنستغرام ثم صفحة الدخول...")
        await self.safe_goto(page, "https://www.instagram.com/", login)
        await self.dismiss_cookie_banners(page)
        await asyncio.sleep(2)

        if await self.page_is_rate_limited(page):
            if not await self.wait_out_rate_limit(page, login, 1):
                logger.error(
                    f"[{login}] ما زال 429 بعد الانتظار — وقف التشغيل 10–20 دقيقة، "
                    f"أو بدّل بروكسي، ولا تشغّل إجبار login كل دقيقة"
                )
                return False

        if await self.page_looks_blank(page):
            ok = await self.recover_blank_login_page(page, login)
            if not ok:
                return False

        if await self.is_logged_in(page):
            logger.success(f"[{login}] مسجّل مسبقاً")
            return True

        await self.safe_goto(page, "https://www.instagram.com/accounts/login/?hl=en", login)
        await asyncio.sleep(3)
        await self.dismiss_cookie_banners(page)

        if await self.page_is_rate_limited(page):
            if not await self.wait_out_rate_limit(page, login, 2):
                return False

        user_input, pass_input = await self.wait_for_login_form(page, login, timeout_s=55)
        if user_input is None and pass_input is None:
            if await self.is_logged_in(page):
                return True
            if await self.page_is_rate_limited(page):
                logger.error(f"[{login}] فورم الدخول محجوب بـ 429 — انتظر ثم أعد المحاولة")
            logger.warning(f"[{login}] فورم الدخول غير ظاهر | URL={page.url}")
            return False

        after_ts = time.time() - 2
        try:
            human = Human(page)
            await human.move_around()
            await human.human_click(user_input)
            await human.pause(0.3, 0.8)
            await user_input.fill("")
            await human.human_type(login)
            await human.pause(0.4, 1.0)
            await human.human_click(pass_input)
            await human.pause(0.2, 0.6)
            await pass_input.fill("")
            await human.human_type(password)
            await human.pause(0.5, 1.2)
        except Exception as e:
            logger.warning(f"[{login}] فشل تعبئة الفورم: {e}")
            await self.log_page_state(page, login, "fill-fail")
            return False

        submitted = False
        human = Human(page)
        for sel in [
            'button[type="submit"]',
            'button:has-text("Log in")',
            'div[role="button"]:has-text("Log in")',
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible():
                    await human.human_click(btn)
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            await page.keyboard.press("Enter")

        logger.info(f"[{login}] تم ضغط Log in — انتظار...")
        await human.pause(3.0, 6.0)

        for _ in range(4):
            for sel in [
                'button:has-text("Not Now")',
                'button:has-text("Not now")',
            ]:
                try:
                    b = page.locator(sel).first
                    if await b.count() > 0 and await b.is_visible():
                        await b.click(force=True)
                        await asyncio.sleep(1)
                except Exception:
                    pass

        await self.fill_otp_if_needed(page, login, mailbox, mailbox_pass, after_ts)

        for i in range(30):
            if await self.is_logged_in(page):
                for sel in ['button:has-text("Not Now")', 'button:has-text("Not now")']:
                    try:
                        b = page.locator(sel).first
                        if await b.count() > 0 and await b.is_visible():
                            await b.click(force=True)
                    except Exception:
                        pass
                logger.success(f"[{login}] تم تسجيل الدخول")
                return True
            await self.fill_otp_if_needed(page, login, mailbox, mailbox_pass, after_ts)
            # خطأ كلمة مرور / حظر
            try:
                body = (await page.inner_text("body"))[:1200].lower()
                if "sorry, your password was incorrect" in body or "incorrect" in body and "password" in body:
                    logger.error(f"[{login}] كلمة المرور غير صحيحة")
                    return False
                if "try again later" in body or "suspicious" in body:
                    logger.error(f"[{login}] إنستغرام رفض الدخول مؤقتاً (بروكسي/حظر)")
                    await self.log_page_state(page, login, "login-rejected")
                    return False
            except Exception:
                pass
            if i % 5 == 0:
                logger.info(f"[{login}] انتظار اكتمال الدخول... ({i*2}ث) URL={page.url}")
            await asyncio.sleep(2)

        await self.log_page_state(page, login, "login-timeout")
        logger.warning(f"[{login}] فشل تسجيل الدخول | URL={page.url}")
        return False

    async def check_account(self, account: Dict) -> bool:
        login = account["email"]
        password = account["password"]

        for attempt in range(1, self.config.max_check_attempts + 1):
            if should_stop():
                return False
            browser = None
            try:
                async with async_playwright() as p:
                    browser = await launch_browser(p, self.config)
                    session_file = self.file_handler.session_path(login)
                    context_kwargs = dict(self.config.browser_context_options)

                    use_session = False
                    if self.config.force_relogin:
                        if os.path.exists(session_file):
                            try:
                                os.remove(session_file)
                                logger.info(f"[{login}] فرض إعادة دخول — تم حذف الجلسة القديمة")
                            except Exception:
                                pass
                    elif os.path.exists(session_file):
                        context_kwargs["storage_state"] = session_file
                        use_session = True
                        logger.info(f"[{login}] استخدام جلسة محفوظة (بدون login جديد)")

                    context = await browser.new_context(**context_kwargs)
                    context.set_default_timeout(self.config.page_timeout * 1000)
                    page = await context.new_page()
                    await prepare_page(page)
                    stealth = Stealth()
                    await stealth.apply_stealth_async(page)

                    logged_in = False
                    if use_session:
                        await self.safe_goto(page, "https://www.instagram.com/", login)
                        await asyncio.sleep(2)
                        logged_in = await self.is_logged_in(page)
                        if logged_in:
                            logger.success(f"[{login}] الجلسة صالحة — تخطي تسجيل الدخول")
                        else:
                            logger.warning(f"[{login}] الجلسة منتهية — سيتم login مرة واحدة ثم الحفظ")

                    if not logged_in:
                        lock = get_login_otp_lock() if self.config.auto_otp else None
                        if lock:
                            async with lock:
                                logged_in = await self.login(page, account)
                        else:
                            logged_in = await self.login(page, account)

                    if not logged_in:
                        await self.stats.increment("failed")
                        logger.warning(f"[{login}] غير صالح ✗")
                        await context.close()
                        await unregister_browser(browser)
                        return False

                    # احفظ الجلسة فوراً بعد الدخول الناجح
                    try:
                        await context.storage_state(path=session_file)
                        logger.success(f"[{login}] تم حفظ الجلسة → {session_file}")
                    except Exception as e:
                        logger.warning(f"[{login}] تعذر حفظ الجلسة: {e}")

                    self.file_handler.save_account(login, password)
                    bot = InstagramBot(page, self.config, self.stats)
                    mode = (self.config.bot_mode or "full").lower()
                    ok = False

                    try:
                        if mode in ("profile", "full"):
                            ok = await bot.engage_profile(login) or ok
                        if mode in ("video", "full"):
                            if self.config.target_video_url:
                                ok = await bot.engage_video(login) or ok
                            elif mode == "video":
                                logger.error(f"[{login}] وضع video بدون رابط منشور")
                    except Exception as e:
                        logger.error(f"[{login}] خطأ أثناء التفاعل: {type(e).__name__}: {e}")
                        await self.stats.increment("errors")

                    # تحديث الجلسة بعد النشاط
                    try:
                        await context.storage_state(path=session_file)
                        logger.info(f"[{login}] تحديث الجلسة المحفوظة")
                    except Exception:
                        pass

                    if ok:
                        await self.stats.increment("successful")
                        logger.success(f"[{login}] اكتملت الإجراءات بنجاح")
                    else:
                        await self.stats.increment("failed")
                        logger.warning(f"[{login}] لم تكتمل الإجراءات")

                    report = await self.stats.get_report()
                    logger.info(report)

                    await context.close()
                    await unregister_browser(browser)
                    return ok
            except Exception as e:
                logger.error(f"[{login}] خطأ حرج: {type(e).__name__}: {e}")
                await self.stats.increment("errors")
                if browser:
                    try:
                        await unregister_browser(browser)
                    except Exception:
                        pass
            if attempt < self.config.max_check_attempts:
                await asyncio.sleep(2)
        return False


class AccountProcessor:
    def __init__(self, accounts: List[Dict], config: Config):
        self.accounts = accounts
        self.config = config
        self.stats = Stats()
        self.checker = InstagramChecker(config, self.stats)
        self.next_index = 0
        self.lock = asyncio.Lock()

    async def worker(self, worker_id: int, semaphore: asyncio.Semaphore):
        while True:
            if should_stop():
                break
            async with self.lock:
                if self.next_index >= len(self.accounts):
                    break
                idx = self.next_index
                self.next_index += 1
                account = self.accounts[idx]

            async with semaphore:
                logger.info(f"[{idx + 1}/{len(self.accounts)}] فحص {account['email']}")
                try:
                    await self.checker.check_account(account)
                    # فاصل بشري بين الحسابات
                    await asyncio.sleep(random.uniform(5.0, 14.0))
                    async with self.lock:
                        await self.stats.increment("processed")
                except Exception as e:
                    logger.error(f"خطأ {account['email']}: {type(e).__name__}: {e}")
                    async with self.lock:
                        await self.stats.increment("processed")
                        await self.stats.increment("errors")

    async def process_all(self):
        if not self.accounts:
            logger.warning("لا توجد حسابات")
            return
        await self.stats.increment("total_accounts", len(self.accounts))
        logger.info(f"بدء معالجة {len(self.accounts)} حساب")
        sem = asyncio.Semaphore(self.config.max_browsers)
        tasks = [
            asyncio.create_task(self.worker(i + 1, sem))
            for i in range(min(self.config.max_browsers, len(self.accounts)))
        ]
        await asyncio.gather(*tasks)
        report = await self.stats.get_report()
        logger.success("انتهت المعالجة")
        logger.success(report)


async def run_bot(config: Config = None) -> dict:
    global STOP_BOT_FLAG, _BOT_LOOP, LOGIN_OTP_LOCK
    STOP_BOT_FLAG = False
    _BOT_LOOP = asyncio.get_running_loop()
    LOGIN_OTP_LOCK = asyncio.Lock()

    if config is None:
        config = Config.from_settings()

    env_proxy = (
        os.environ.get("IG_PROXY")
        or os.environ.get("PROXY")
        or os.environ.get("TIKTOK_PROXY")
        or ""
    ).strip()
    if env_proxy:
        config.proxy = env_proxy
        config.proxy_enabled = True

    # إجبار تشغيل البروكسي — ما عاد يتعطل بالغلط
    if not (config.proxy or "").strip():
        config.proxy = DEFAULT_PROXY
    config.proxy_enabled = True
    # على السيرفر: متصفح نظامي headed دائماً
    if os.name != "nt" and os.environ.get("IG_HEADLESS", "").strip() not in ("1", "true", "yes"):
        config.browser_headless = False
        ensure_virtual_display()
    try:
        s = load_settings()
        s["proxy_enabled"] = True
        s["proxy"] = config.proxy
        if os.name != "nt":
            s["browser_headless"] = False
        # لا تفرض login كل تشغيل — يسبب 429
        if s.get("force_relogin"):
            s["force_relogin"] = False
        save_settings(s)
    except Exception:
        pass

    migrate_from_settings(config.comment_texts)

    logger.info("=" * 60)
    logger.info(f"Instagram Bot | build={BOT_VERSION}")
    logger.info("=" * 60)
    logger.info(f"🎯 منشور/ريل: {config.target_video_url or '(غير محدد)'}")
    logger.info(f"👤 بروفايل: {config.profile_url or '(غير محدد)'}")
    if config.profile_url and "tiktok.com" in config.profile_url.lower():
        logger.error(
            "⚠️ رابط البروفايل ما زال تيك توك! غيّره في اللوحة إلى رابط إنستغرام "
            "مثل https://www.instagram.com/username/"
        )
    logger.info(f"🎮 الوضع: {config.bot_mode}")
    logger.info(f"💬 تعليقات متبقية: {remaining_count()}")
    logger.info(f"👥 متصفحات متوازية: {config.max_browsers}")
    logger.info(f"📧 OTP تلقائي: {'نعم' if config.auto_otp else 'لا'}")
    logger.info(f"🔑 إعادة دخول إجبارية: {'نعم' if config.force_relogin else 'لا (استخدام الجلسات)'}")
    logger.info(
        f"🖥️ المتصفح: {'headless' if config.browser_headless else 'نظامي headed'} "
        f"| DISPLAY={os.environ.get('DISPLAY', '(لا)')}"
    )
    if config.proxy_enabled and config.proxy:
        parsed = parse_proxy(config.proxy)
        server = (parsed or {}).get("server", config.proxy)
        logger.info(f"🛡️ البروكسي: مفعّل → {server}")
    else:
        logger.warning("🛡️ البروكسي: معطّل — إنستغرام غالباً يحظر IP السيرفر")
    logger.info("=" * 60)

    accounts = read_accounts(config)
    if not accounts:
        logger.error("لا حسابات في accounts.json")
        return {"ok": False, "error": "no_accounts"}

    processor = AccountProcessor(accounts, config)
    await processor.process_all()
    report = await processor.stats.get_report()
    return {"ok": True, "report": report, "stats": dict(processor.stats.counters)}


if __name__ == "__main__":
    asyncio.run(run_bot())
