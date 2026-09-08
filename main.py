"""Instagram engagement bot — profile like/story/comment + reel/post comment."""
import asyncio
import json
import os
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

BOT_VERSION = "2026-09-09-instagram-v3"

# #region agent log
_DEBUG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug-8e9bfe.log")

STOP_BOT_FLAG = False
ACTIVE_BROWSERS: List[Any] = []
_BOT_LOOP = None
LOGIN_OTP_LOCK: Optional[asyncio.Lock] = None


def _dbg(hypothesis_id: str, location: str, message: str, data: dict = None, run_id: str = "run"):
    try:
        payload = {
            "sessionId": "8e9bfe",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion


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
    kwargs = {
        "headless": config.browser_headless,
        "args": list(config.browser_args),
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

    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome_win = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if os.path.exists(edge):
        kwargs["executable_path"] = edge
        browser = await playwright.chromium.launch(**kwargs)
    elif os.path.exists(chrome_win):
        kwargs["executable_path"] = chrome_win
        browser = await playwright.chromium.launch(**kwargs)
    else:
        try:
            browser = await playwright.chromium.launch(channel="chrome", **kwargs)
        except Exception:
            browser = await playwright.chromium.launch(**kwargs)
    ACTIVE_BROWSERS.append(browser)
    return browser


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
        "enable_sharing": True,  # story / share
        "watch_count": 3,
        "max_browsers": 1,
        "browser_headless": True,
        "imap_host": "imap.hostinger.com",
        "imap_port": 993,
        "otp_timeout": 90,
        "auto_otp": True,
        "dashboard_host": "0.0.0.0",
        "dashboard_port": 5050,
        "proxy_enabled": False,
        "proxy": "",
        "force_relogin": True,
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
    browser_headless: bool = True
    max_check_attempts: int = 1
    proxy_enabled: bool = False
    proxy: str = ""
    force_relogin: bool = True
    page_timeout: int = 45
    action_delay: float = 1.0
    enable_commenting: bool = True
    enable_liking: bool = True
    enable_sharing: bool = True
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
        "viewport": {"width": 1280, "height": 900},
        "ignore_https_errors": True,
        "java_script_enabled": True,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
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
        cfg.watch_count = int(s.get("watch_count", 3) or 3)
        cfg.max_browsers = max(1, int(s.get("max_browsers", 1) or 1))
        cfg.browser_headless = bool(s.get("browser_headless", True))
        cfg.force_relogin = bool(s.get("force_relogin", True))
        cfg.auto_otp = bool(s.get("auto_otp", True))
        cfg.imap_host = (s.get("imap_host") or "imap.hostinger.com").strip()
        cfg.imap_port = int(s.get("imap_port", 993) or 993)
        cfg.otp_timeout = int(s.get("otp_timeout", 90) or 90)
        # البروكسي ملغى نهائياً
        cfg.proxy_enabled = False
        cfg.proxy = ""
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
            f"ستوري={c.get('stories', 0)} | شير={c.get('shares', 0)}"
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


class InstagramBot:
    def __init__(self, page: Page, config: Config, stats: Stats):
        self.page = page
        self.config = config
        self.stats = stats

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

    async def is_liked(self) -> bool:
        for sel in [
            'svg[aria-label="Unlike"]',
            'svg[aria-label="Remove Like"]',
            '[aria-label="Unlike"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def like_current(self, account: str) -> bool:
        if not self.config.enable_liking:
            return False
        await self.dismiss_overlays()
        if await self.is_liked():
            logger.info(f"[{account}] اللايك موجود مسبقاً — تخطي")
            return True
        for sel in [
            'svg[aria-label="Like"]',
            '[aria-label="Like"]',
            'button:has(svg[aria-label="Like"])',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                # click button parent when svg
                btn = self.page.locator('button:has(svg[aria-label="Like"])').first
                target = btn if await btn.count() > 0 else loc
                await target.click(force=True, timeout=4000)
                await asyncio.sleep(1.2)
                if await self.is_liked():
                    await self.stats.increment("likes")
                    logger.success(f"[{account}] تم عمل لايك")
                    return True
            except Exception:
                continue
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
        # focus comment box
        box = None
        for sel in [
            'textarea[aria-label*="Add a comment" i]',
            'textarea[placeholder*="Add a comment" i]',
            'textarea[aria-label*="comment" i]',
            'form textarea',
            'div[contenteditable="true"][aria-label*="comment" i]',
            'div[role="textbox"]',
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    box = loc
                    break
            except Exception:
                continue

        if not box:
            # open comments if needed
            try:
                cbtn = self.page.locator('svg[aria-label="Comment"], button:has(svg[aria-label="Comment"])').first
                if await cbtn.count() > 0:
                    await cbtn.click(force=True)
                    await asyncio.sleep(1.5)
            except Exception:
                pass
            for sel in [
                'textarea[aria-label*="Add a comment" i]',
                'textarea[placeholder*="Add a comment" i]',
                'form textarea',
            ]:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    box = loc
                    break

        if not box:
            logger.error(f"[{account}] لم يجد حقل التعليق")
            return False

        try:
            await box.click(force=True)
            await asyncio.sleep(0.3)
            try:
                await box.fill(text)
            except Exception:
                await self.page.keyboard.type(text, delay=30)
            await asyncio.sleep(0.5)

            posted = False
            for sel in [
                'div[role="button"]:has-text("Post")',
                'button:has-text("Post")',
                'div[role="button"]:has-text("Publish")',
                'button:has-text("Publish")',
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

            await asyncio.sleep(2)
            await self.stats.increment("comments")
            logger.success(f"[{account}] تم نشر التعليق: {text}")
            return True
        except Exception as e:
            logger.warning(f"[{account}] فشل التعليق: {type(e).__name__}: {e}")
            return False

    async def share_to_story(self, account: str) -> bool:
        """يحاول إضافة المنشور للستوري عبر قائمة Share."""
        if not self.config.enable_sharing:
            return False
        await self.dismiss_overlays()
        try:
            share_btn = self.page.locator(
                'svg[aria-label="Share Post"], svg[aria-label="Share"], '
                'button:has(svg[aria-label="Share Post"]), button:has(svg[aria-label="Share"])'
            ).first
            if await share_btn.count() == 0 or not await share_btn.is_visible():
                logger.warning(f"[{account}] زر الشير غير ظاهر")
                return False
            btn = self.page.locator(
                'button:has(svg[aria-label="Share Post"]), button:has(svg[aria-label="Share"])'
            ).first
            target = btn if await btn.count() > 0 else share_btn
            await target.click(force=True)
            await asyncio.sleep(1.5)

            for sel in [
                'button:has-text("Add to story")',
                'span:has-text("Add to story")',
                'div[role="button"]:has-text("Add to story")',
                'button:has-text("Add to your story")',
                'span:has-text("Add to your story")',
            ]:
                opt = self.page.locator(sel).first
                if await opt.count() > 0 and await opt.is_visible():
                    await opt.click(force=True)
                    await asyncio.sleep(2)
                    # confirm share if needed
                    for conf in [
                        'button:has-text("Share")',
                        'div[role="button"]:has-text("Share")',
                        'button:has-text("Done")',
                    ]:
                        c = self.page.locator(conf).first
                        try:
                            if await c.count() > 0 and await c.is_visible():
                                await c.click(force=True)
                                break
                        except Exception:
                            pass
                    await self.stats.increment("stories")
                    logger.success(f"[{account}] تمت إضافة المنشور إلى الستوري")
                    await self.dismiss_overlays()
                    return True

            # fallback: copy link counts as share action log
            for sel in [
                'button:has-text("Copy link")',
                'span:has-text("Copy link")',
            ]:
                opt = self.page.locator(sel).first
                if await opt.count() > 0 and await opt.is_visible():
                    await opt.click(force=True)
                    await self.stats.increment("shares")
                    logger.info(f"[{account}] Add to story غير متاح على الويب — تم Copy link كبديل")
                    await self.dismiss_overlays()
                    return True

            await self.dismiss_overlays()
            # close share sheet
            await self.page.keyboard.press("Escape")
            logger.warning(f"[{account}] لم تتوفر خيار Add to story على الويب")
            return False
        except Exception as e:
            logger.warning(f"[{account}] فشل الشير/ستوري: {type(e).__name__}: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
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
                await asyncio.sleep(2.5)
                await self.dismiss_overlays()

                await self.like_current(account)
                await asyncio.sleep(1)
                if self.config.enable_commenting:
                    await self.comment_current(account)
                    await asyncio.sleep(1)
                await self.share_to_story(account)
                opened += 1
                await asyncio.sleep(2)
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
        logger.info(f"[{account}] فتح المنشور المستهدف: {url}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await self.dismiss_overlays()

        ok = False
        if self.config.enable_liking:
            ok = await self.like_current(account) or ok
        if self.config.enable_commenting:
            ok = await self.comment_current(account) or ok
        if self.config.enable_sharing:
            ok = await self.share_to_story(account) or ok
        return ok


class InstagramChecker:
    def __init__(self, config: Config, stats: Stats):
        self.config = config
        self.stats = stats
        self.file_handler = FileHandler(config)

    async def safe_goto(self, page: Page, url: str, account: str) -> bool:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2)
            return True
        except Exception as e:
            logger.warning(f"[{account}] تنبيه فتح {url}: {type(e).__name__}: {e}")
            return "instagram.com" in (page.url or "")

    async def is_logged_in(self, page: Page) -> bool:
        url = (page.url or "").lower()
        if "/accounts/login" in url or "/challenge" in url:
            return False
        for sel in [
            'svg[aria-label="Home"]',
            'a[href="/"] svg[aria-label="Home"]',
            'svg[aria-label="New post"]',
            'svg[aria-label="Search"]',
            'img[alt*="profile picture" i]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        # login form present?
        try:
            pw = page.locator('input[name="password"]').first
            if await pw.count() > 0 and await pw.is_visible():
                return False
        except Exception:
            pass
        return "instagram.com" in url and "/accounts/login" not in url

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

    async def wait_for_login_form(self, page: Page, account: str, timeout_s: int = 35):
        """ينتظر ظهور حقول الدخول مع عدة محاولات تنقل."""
        deadline = time.time() + timeout_s
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            await self.dismiss_cookie_banners(page)

            # روابط Log in على الصفحة الرئيسية
            for sel in [
                'a[href="/accounts/login/"]',
                'a[href*="/accounts/login"]',
                'button:has-text("Log in")',
                'a:has-text("Log in")',
            ]:
                try:
                    a = page.locator(sel).first
                    if await a.count() > 0 and await a.is_visible():
                        # لا تضغط إذا الحقول ظاهرة أصلاً
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

            # صفحات حظر / خطأ شائعة
            try:
                body = (await page.inner_text("body"))[:800].lower()
                if any(x in body for x in ("sorry, this page", "something went wrong", "try again later", "unavailable")):
                    logger.error(f"[{account}] صفحة خطأ/حظر من إنستغرام — غالباً البروكسي")
                    await self.log_page_state(page, account, "ig-blocked")
                    return None, None
            except Exception:
                pass

            if attempt in (1, 3, 6):
                logger.info(f"[{account}] انتظار فورم الدخول... URL={page.url}")
                # إعادة فتح صفحة الدخول
                try:
                    await page.goto(
                        "https://www.instagram.com/accounts/login/?source=auth_switcher",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(2)

            await asyncio.sleep(1.5)

        await self.log_page_state(page, account, "login-form-timeout")
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

        if await self.is_logged_in(page):
            logger.success(f"[{login}] مسجّل مسبقاً")
            return True

        await self.safe_goto(page, "https://www.instagram.com/accounts/login/", login)
        await asyncio.sleep(2)
        await self.dismiss_cookie_banners(page)

        user_input, pass_input = await self.wait_for_login_form(page, login, timeout_s=40)
        if user_input is None and pass_input is None:
            if await self.is_logged_in(page):
                return True
            logger.warning(f"[{login}] فورم الدخول غير ظاهر | URL={page.url}")
            return False

        after_ts = time.time() - 2
        try:
            await user_input.click(force=True)
            await user_input.fill("")
            await user_input.type(login, delay=25)
            await asyncio.sleep(0.4)
            await pass_input.click(force=True)
            await pass_input.fill("")
            await pass_input.type(password, delay=25)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[{login}] فشل تعبئة الفورم: {e}")
            await self.log_page_state(page, login, "fill-fail")
            return False

        submitted = False
        for sel in [
            'button[type="submit"]',
            'button:has-text("Log in")',
            'div[role="button"]:has-text("Log in")',
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5000)
                    submitted = True
                    break
            except Exception:
                try:
                    await btn.click(force=True)
                    submitted = True
                    break
                except Exception:
                    continue
        if not submitted:
            await page.keyboard.press("Enter")

        logger.info(f"[{login}] تم ضغط Log in — انتظار...")
        await asyncio.sleep(4)

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
                                logger.info(f"[{login}] حذف الجلسة القديمة")
                            except Exception:
                                pass
                    elif os.path.exists(session_file):
                        context_kwargs["storage_state"] = session_file
                        use_session = True
                        logger.info(f"[{login}] تحميل جلسة محفوظة")

                    context = await browser.new_context(**context_kwargs)
                    context.set_default_timeout(self.config.page_timeout * 1000)
                    page = await context.new_page()
                    stealth = Stealth()
                    await stealth.apply_stealth_async(page)

                    logged_in = False
                    if use_session:
                        await self.safe_goto(page, "https://www.instagram.com/", login)
                        logged_in = await self.is_logged_in(page)
                        if logged_in:
                            logger.success(f"[{login}] الجلسة ما زالت صالحة")
                        else:
                            logger.warning(f"[{login}] الجلسة منتهية")

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

                    try:
                        await context.storage_state(path=session_file)
                        logger.info(f"[{login}] حفظ الجلسة")
                    except Exception:
                        pass

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

    # البروكسي ملغى — تجاهل أي إعداد أو متغير بيئة قديم
    config.proxy_enabled = False
    config.proxy = ""

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
    logger.info("🛡️ البروكسي: ملغى")
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
