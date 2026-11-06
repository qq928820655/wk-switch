"""访问口令：一个共享 token 换一个会话 cookie。

定位是「本机工具的门闩」，不是多用户账号体系：
- 口令只有一条，存在 config/auth.json，可改；
- 换到会话 cookie 后，后续请求只认 cookie，口令本身不再出现在 URL 里；
- 会话落盘到 data/sessions.json。**这一点是升级重启逼出来的**：原先只放
  内存，重启后新进程是空的，用户刚点完「一键升级」就被踢回登录页，看起来
  像升级把会话搞坏了。落盘的是随机 sid + 过期时间，不含口令本身，权限与
  config/auth.json 同级。

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
_loaded = False
SESSIONS_PATH = os.path.join(ROOT, "data", "sessions.json")


def _load_locked() -> None:
    """第一次用到会话时从磁盘补一次。调用方持锁。

    只在启动后第一次调用真正读盘：之后内存就是权威，避免每个请求都碰文件。
    文件坏了当成空——会话丢失最多是重新登录，不该让服务起不来。
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    for sid, expires in data.items():
        try:
            exp = float(expires)
        except (TypeError, ValueError):
            continue
        if exp > now:
            _sessions[str(sid)] = exp


def _save_locked() -> None:
    """把会话表写回磁盘。调用方持锁。

    写失败不影响本次登录（内存里还有效），只是重启后需要重新登录一次——
    这比因为目录不可写而拒绝登录要好。
    """
    path = SESSIONS_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sessions, f)
        os.replace(tmp, path)
    except OSError:
        pass


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
        _load_locked()
        _gc_locked()
        _sessions[sid] = time.time() + ttl
        _save_locked()
    return sid


def check_session(sid: str) -> bool:
    if not sid:
        return False
    with _lock:
        _load_locked()
        expires = _sessions.get(sid)
        if not expires:
            return False
        if expires < time.time():
            _sessions.pop(sid, None)
            _save_locked()
            return False
        return True


def revoke(sid: str) -> None:
    with _lock:
        _load_locked()
        if _sessions.pop(sid or "", None) is not None:
            _save_locked()


def _gc_locked() -> None:
    now = time.time()
    stale = [k for k, exp in _sessions.items() if exp < now]
    for key in stale:
        _sessions.pop(key, None)
    if stale:
        _save_locked()


def session_ttl() -> int:
    return int(config()["session_hours"] * 3600)
