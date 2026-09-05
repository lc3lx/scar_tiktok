"""مجمع تعليقات يُستهلك: كل حساب يأخذ تعليقاً ويُحذف حتى لا يتكرر."""
import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMMENTS_FILE = os.path.join(BASE_DIR, "comments_pool.json")
_lock = threading.Lock()


def _default_pool() -> dict:
    return {"pending": [], "used": []}


def load_pool() -> dict:
    if not os.path.exists(COMMENTS_FILE):
        return _default_pool()
    try:
        with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_pool()
        data.setdefault("pending", [])
        data.setdefault("used", [])
        return data
    except Exception as e:
        logger.warning(f"فشل قراءة comments_pool.json: {e}")
        return _default_pool()


def save_pool(data: dict) -> None:
    with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def remaining_count() -> int:
    with _lock:
        return len(load_pool().get("pending") or [])


def used_count() -> int:
    with _lock:
        return len(load_pool().get("used") or [])


def list_pending() -> List[str]:
    with _lock:
        return list(load_pool().get("pending") or [])


def set_comments(comments: List[str], replace: bool = True) -> dict:
    """إضافة/استبدال قائمة التعليقات. replace=True يمسح القديمة."""
    cleaned = []
    seen = set()
    for c in comments or []:
        t = (c or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)

    with _lock:
        pool = load_pool()
        if replace:
            pool["pending"] = cleaned
        else:
            existing = set(pool.get("pending") or [])
            for t in cleaned:
                if t not in existing:
                    pool.setdefault("pending", []).append(t)
                    existing.add(t)
        save_pool(pool)
        return {
            "pending": len(pool["pending"]),
            "used": len(pool.get("used") or []),
        }


def take_comment(account_email: str = "") -> Optional[str]:
    """ يأخذ أول تعليق متاح ويحذفه من القائمة (لا يُعاد استخدامه)."""
    with _lock:
        pool = load_pool()
        pending = pool.get("pending") or []
        if not pending:
            logger.warning(f"[{account_email}] لا توجد تعليقات متبقية في المجمع")
            return None
        text = pending.pop(0)
        pool["pending"] = pending
        pool.setdefault("used", []).append({
            "text": text,
            "account": account_email,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        # لا نكبر used بلا حدود
        if len(pool["used"]) > 5000:
            pool["used"] = pool["used"][-2000:]
        save_pool(pool)
        logger.info(
            f"[{account_email}] أُخذ تعليق من المجمع ({len(pending)} متبقي): {text[:60]}"
        )
        return text


def peek_status() -> Dict:
    with _lock:
        pool = load_pool()
        return {
            "pending_count": len(pool.get("pending") or []),
            "used_count": len(pool.get("used") or []),
            "pending": list(pool.get("pending") or []),
            "recent_used": list(reversed((pool.get("used") or [])[-10:])),
        }


def migrate_from_settings(comment_texts: List[str]) -> None:
    """لو المجمع فاضي والإعدادات فيها تعليقات — انقلها للمجمع."""
    with _lock:
        pool = load_pool()
        if pool.get("pending"):
            return
        cleaned = [c.strip() for c in (comment_texts or []) if c and c.strip()]
        if cleaned:
            pool["pending"] = cleaned
            save_pool(pool)
            logger.info(f"تم نقل {len(cleaned)} تعليق من الإعدادات إلى مجمع التعليقات")
