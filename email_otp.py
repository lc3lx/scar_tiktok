"""جلب كود التحقق من بريد Hostinger عبر IMAP."""
import email
import imaplib
import re
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from loguru import logger

# Hostinger default IMAP
DEFAULT_IMAP_HOST = "imap.hostinger.com"
DEFAULT_IMAP_PORT = 993

OTP_PATTERNS = [
    r"(?:verification|verify|security)\s*code[:\s]*([0-9]{4,8})",
    r"(?:code|رمز|كود)[:\s]*([0-9]{4,8})",
    r"\b([0-9]{6})\b",
    r"\b([0-9]{4})\b",
]

TIKTOK_SENDERS = ("tiktok", "noreply@", "account@", "mail.tiktok")
IMAP_FOLDERS = ("INBOX", "Junk", "Spam", "Junk E-mail", "Bulk Mail")

# أكواد استُخدمت مسبقاً حتى لا يأخذ حساب ثانٍ نفس الرمز من صندوق مشترك
_USED_OTPS: dict = {}  # code -> unix ts


def mark_otp_used(code: str) -> None:
    if code:
        _USED_OTPS[str(code)] = time.time()
        # نظّف الأقدم من 15 دقيقة
        cutoff = time.time() - 900
        for k, ts in list(_USED_OTPS.items()):
            if ts < cutoff:
                _USED_OTPS.pop(k, None)


def _is_used(code: str) -> bool:
    ts = _USED_OTPS.get(str(code))
    return bool(ts and time.time() - ts < 900)


def _decode_mime(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="ignore"))
        else:
            out.append(data)
    return "".join(out)


def _extract_body(msg) -> str:
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    texts.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            texts.append(payload.decode(charset, errors="ignore"))
        except Exception:
            pass
    return "\n".join(texts)


def extract_otp(text: str) -> Optional[str]:
    if not text:
        return None
    # امسح HTML tags تقريباً
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)
    for pattern in OTP_PATTERNS:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            code = m.group(1)
            # تجاهل أرقام طويلة شائعة (تواريخ/IDs)
            if 4 <= len(code) <= 8:
                return code
    return None


def fetch_tiktok_otp(
    email_addr: str,
    email_password: str,
    *,
    imap_host: str = DEFAULT_IMAP_HOST,
    imap_port: int = DEFAULT_IMAP_PORT,
    after_ts: Optional[float] = None,
    max_age_seconds: int = 300,
    for_account: Optional[str] = None,
) -> Optional[str]:
    """
    يقرأ أحدث رسائل TikTok من صندوق الوارد ويرجع كود OTP.
    email_addr/email_password: بيانات دخول Hostinger IMAP (صندوق الميل).
    for_account: إيميل حساب TikTok (لو الميل مشترك لعدة حسابات على نفس الصندوق).
    after_ts: لا يقبل رموز من رسائل أقدم من هذا الوقت (unix).
    """
    if after_ts is None:
        after_ts = time.time() - 30
    target = (for_account or email_addr or "").lower()

    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(email_addr, email_password)

        best_code = None
        best_score = -1

        for folder in IMAP_FOLDERS:
            try:
                status, _ = mail.select(folder)
                if status != "OK":
                    continue
            except Exception:
                continue

            status, data = mail.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                continue

            ids = data[0].split()
            for msg_id in reversed(ids[-40:]):
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                sender = _decode_mime(msg.get("From", "")).lower()
                subject = _decode_mime(msg.get("Subject", ""))
                to_hdr = _decode_mime(msg.get("To", "")).lower()
                cc_hdr = _decode_mime(msg.get("Cc", "")).lower()
                delivered = _decode_mime(msg.get("Delivered-To", "")).lower()
                date_hdr = msg.get("Date")

                if not any(s in sender for s in TIKTOK_SENDERS) and "tiktok" not in subject.lower():
                    continue

                try:
                    msg_dt = parsedate_to_datetime(date_hdr)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    msg_ts = msg_dt.timestamp()
                except Exception:
                    msg_ts = time.time()

                if msg_ts < after_ts - 5:
                    continue
                if time.time() - msg_ts > max_age_seconds:
                    continue

                body = _extract_body(msg)
                combined = f"{subject}\n{to_hdr}\n{cc_hdr}\n{delivered}\n{body}"
                code = extract_otp(combined)
                if not code or _is_used(code):
                    continue

                score = 1
                if folder != "INBOX":
                    score = 2
                if target and target in combined.lower():
                    score = 10
                elif target and target.split("@")[0] in combined.lower():
                    score = 5

                if score > best_score:
                    best_score = score
                    best_code = code
                    if score >= 10:
                        break
            if best_score >= 10:
                break

        mail.logout()
        if best_code:
            logger.success(
                f"[mailbox:{email_addr}] وجد OTP"
                + (f" لحساب {for_account}" if for_account else "")
                + f": {best_code}"
            )
        return best_code
    except imaplib.IMAP4.error as e:
        logger.error(f"[{email_addr}] خطأ IMAP Hostinger: {e}")
    except Exception as e:
        logger.error(f"[{email_addr}] فشل جلب OTP: {type(e).__name__}: {e}")
    return None


def wait_for_otp(
    email_addr: str,
    email_password: str,
    *,
    timeout: int = 90,
    poll_interval: int = 5,
    imap_host: str = DEFAULT_IMAP_HOST,
    imap_port: int = DEFAULT_IMAP_PORT,
    after_ts: Optional[float] = None,
    for_account: Optional[str] = None,
) -> Optional[str]:
    """ينتظر وصول كود OTP في صندوق Hostinger."""
    if after_ts is None:
        after_ts = time.time() - 10
    deadline = time.time() + timeout
    label = for_account or email_addr
    logger.info(f"[{label}] انتظار OTP من Hostinger ({email_addr}) حتى {timeout}ث...")
    while time.time() < deadline:
        code = fetch_tiktok_otp(
            email_addr,
            email_password,
            imap_host=imap_host,
            imap_port=imap_port,
            after_ts=after_ts,
            for_account=for_account or email_addr,
        )
        if code:
            return code
        time.sleep(poll_interval)
    logger.warning(f"[{label}] لم يصل OTP خلال {timeout} ثانية")
    return None
