"""访问口令：一个共享 token 换一个会话 cookie。

定位是「本机工具的门闩」，不是多用户账号体系：
- 口令只有一条，存在 config/auth.json，可改；
- 换到会话 cookie 后，后续请求只认 cookie，口令本身不再出现在 URL 里；
- 会话放内存，进程重启即失效（重启后重新登录一次即可）。

用 hmac.compare_digest 比较而不是 ==：后者按字节短路，长度和前缀会通过
响应时间泄漏出去。本地工具风险低，但正确的写法成本为零。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "auth.json")

#: 首次运行时写入的默认口令
DEFAULT_TOKEN = "qq928820655"
DEFAULT_HOURS = 12
COOKIE_NAME = "wk_session"

_lock = threading.Lock()
_sessions: dict[str, float] = {}


def _ensure_config() -> dict[str, Any]:
    """读配置，不存在就按默认值建一份。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.isfile(CONFIG_PATH):
        data = {"token": DEFAULT_TOKEN, "session_hours": DEFAULT_HOURS}
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return data

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # 配置坏了不能把人锁在门外：退回默认口令，并保留原文件供排查
        return {"token": DEFAULT_TOKEN, "session_hours": DEFAULT_HOURS}

    if not isinstance(data, dict) or not str(data.get("token") or "").strip():
        data = {"token": DEFAULT_TOKEN, "session_hours": DEFAULT_HOURS}
    return data


def config() -> dict[str, Any]:
    data = _ensure_config()
    try:
        hours = float(data.get("session_hours") or DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    return {"token": str(data.get("token") or DEFAULT_TOKEN), "session_hours": hours}


def token_matches(given: str) -> bool:
    expected = config()["token"]
    return hmac.compare_digest(str(given or "").encode("utf-8"),
                               expected.encode("utf-8"))


def issue_session() -> str:
    sid = secrets.token_urlsafe(24)
    ttl = config()["session_hours"] * 3600
    with _lock:
        _gc_locked()
        _sessions[sid] = time.time() + ttl
    return sid


def check_session(sid: str) -> bool:
    if not sid:
        return False
    with _lock:
        expires = _sessions.get(sid)
        if not expires:
            return False
        if expires < time.time():
            _sessions.pop(sid, None)
            return False
        return True


def revoke(sid: str) -> None:
    with _lock:
        _sessions.pop(sid or "", None)


def _gc_locked() -> None:
    now = time.time()
    for key in [k for k, exp in _sessions.items() if exp < now]:
        _sessions.pop(key, None)


def session_ttl() -> int:
    return int(config()["session_hours"] * 3600)
