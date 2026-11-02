"""token 解析：JWT 载荷、到期判定、区域（realm）推断、凭据形态分类。

规则与参考项目的口径保持一致（见 ardeyouxipianyi/workbuddy2api-hub
wb_accounts.py 的 realm_evidence / normalise_import_row）：

- 区域先看 token 的 issuer，再看 domain。域名是客户端随手记下的字段，
  可能过期或属于另一边（同一台机器切区登录过），token 才是真正要拿去请求的。
- 静态 API Key（ck_/pt_ 前缀）不是 JWT，不能按 JWT 校验，只在国内版出口有效。
- 加密信封（$wbEncrypted）不是明文 token，无法解码。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

# 与参考项目一致的区域标记
CN_MARKERS = ("copilot.tencent.com", "codebuddy.cn", "workbuddy.cn")
INTL_MARKERS = ("workbuddy.ai", "codebuddy.ai")

# 各区域的规范域名（domain 写着另一区域时用来纠正）
REALM_DOMAIN = {"cn": "copilot.tencent.com", "intl": "www.workbuddy.ai"}
REALM_LABEL = {"cn": "国内版", "intl": "国际版"}


def b64url_decode(segment: str) -> bytes:
    """JWT 段解码：补齐 base64 padding，URL-safe 字母表。"""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def jwt_claims(token: str) -> dict[str, Any]:
    """取 JWT payload。不是 JWT 就返回空字典，不抛异常。

    调用方大量用于「顺便看一眼」，抛异常会逼出无意义的 try/except。
    """
    text = str(token or "").strip()
    if text.count(".") != 2:
        return {}
    try:
        payload = b64url_decode(text.split(".")[1])
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def jwt_issuer(token: str) -> str:
    return str(jwt_claims(token).get("iss") or "")


def jwt_uid(token: str) -> str:
    """从 JWT 里取 uid。不同版本用的 claim 名不一样，按常见顺序找。"""
    claims = jwt_claims(token)
    for key in ("uid", "user_id", "userId", "sub", "open_id", "openid"):
        value = claims.get(key)
        if value:
            return str(value)
    return ""


def jwt_exp(token: str) -> int:
    return int(jwt_claims(token).get("exp") or 0)


def realm_evidence(token: str, domain: str = "") -> str:
    """从 token / 域名看区域；看不出返回空串（不瞎猜）。"""
    iss = jwt_issuer(token).lower()
    for marker in CN_MARKERS:
        if marker in iss:
            return "cn"
    for marker in INTL_MARKERS:
        if marker in iss:
            return "intl"

    dom = str(domain or "").lower()
    for marker in CN_MARKERS:
        if marker in dom:
            return "cn"
    for marker in INTL_MARKERS:
        if marker in dom:
            return "intl"
    return ""


def detect_realm(token: str, domain: str = "", api_key: bool = False) -> str:
    """推断账号区域。

    静态 Key 强制 cn：这类 key 只在国内版出口有效，放进国际版列表里
    每个请求都会打空。其余情况看不出证据时按 intl 兜底——与参考项目一致。
    """
    if api_key or str(token or "").startswith(("ck_", "pt_")):
        return "cn"
    return realm_evidence(token, domain) or "intl"


def domain_for_realm(realm: str, domain: str = "") -> str:
    """把域名对齐到区域：域名属于另一区域时换成该区域的规范域名。

    domain 会进 X-Domain 头。区域既然以 token 为准，就不能让一个过期域名
    把请求带回另一边的出口。
    """
    dom = str(domain or "").strip()
    if not dom:
        return REALM_DOMAIN.get(realm, "")
    other = "intl" if realm == "cn" else "cn"
    if not any(marker in dom.lower() for marker in _markers(other)):
        return dom
    return REALM_DOMAIN.get(realm, dom)


def _markers(realm: str) -> tuple[str, ...]:
    return CN_MARKERS if realm == "cn" else INTL_MARKERS


def is_expired(expires_at: int, skew: int = 0) -> bool:
    """到期判定。expires_at <= 0 视为「未知」，不算过期。

    未知到期时间当成过期会让一批本来能用的账号被标红，反过来又会让人
    忽略真正的过期账号——所以单独归到「未知」状态。
    """
    if not expires_at or expires_at <= 0:
        return False
    return time.time() + skew >= expires_at


def token_state(access_token: str, expires_at: int, refresh_token: str = "") -> str:
    """凭据状态分类，供界面配色与统计使用。

    取值：empty / envelope / api_key / expired / expiring / valid / unknown
    """
    token = str(access_token or "").strip()
    if not token:
        return "empty"
    if token.startswith("{"):
        return "envelope"
    if token.startswith(("ck_", "pt_")):
        return "api_key"
    if expires_at and expires_at > 0:
        now = time.time()
        if now >= expires_at:
            # 有 RT 说明还能救回来，没有就是彻底失效
            return "expired" if not refresh_token else "refreshable"
        if expires_at - now < 24 * 3600:
            return "expiring"
        return "valid"
    # 没有 expiresAt：JWT 可以自己算，算不出来就未知
    exp = jwt_exp(token)
    if exp:
        return "expired" if time.time() >= exp else "valid"
    return "unknown"


STATE_LABEL = {
    "empty": "无凭据",
    "envelope": "加密信封",
    "api_key": "静态 Key",
    "expired": "已过期",
    "refreshable": "已过期(可续期)",
    "expiring": "24h 内到期",
    "valid": "有效",
    "unknown": "到期未知",
}


def describe(token: str, expires_at: int, refresh_token: str = "") -> dict[str, Any]:
    """一次算齐界面要展示的凭据元信息。"""
    claims = jwt_claims(token)
    state = token_state(token, expires_at, refresh_token)
    exp = expires_at or jwt_exp(token)
    return {
        "state": state,
        "label": STATE_LABEL.get(state, state),
        "is_jwt": bool(claims),
        "issuer": str(claims.get("iss") or ""),
        "expires_at": exp,
        "expires_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "",
        "realm": detect_realm(token, "", False),
    }
