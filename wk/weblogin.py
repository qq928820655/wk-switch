"""从官方网址登录账户，把凭据和资料落进本地账户库。

协议照 ardeyouxipianyi/workbuddy2api-hub 的最新 `wb_accounts.py`
（start_login / poll_login）实现，三段：

1. `POST /v2/plugin/auth/state?platform=CLI`
   → `{data: {state, authUrl}}`。authUrl 是官方登录页，用户在浏览器里完成授权。
2. `GET  /v2/plugin/auth/token?state=...` 反复轮询
   → `code=11217` 还没登录；`code=0` 时 `data` 带 accessToken / refreshToken /
   expiresAt / domain。
3. `GET  /v2/plugin/login/account?state=...` + `Authorization: Bearer <AT>`
   → `data` 里的昵称、邮箱等账户资料。

拿到第 3 步的资料（以及 JWT 里的 uid）后组装 `Account`，交给 `store.upsert`
按 (uid, realm) 写库，所以重复扫码同一账号是更新而不是多出一行。

会话只放内存，TTL 10 分钟，与 hub 一致：这是「点一下 → 去浏览器登录 → 回来」
的一次性流程，落盘没有意义，还会把未完成的 state 留到下次启动。所有会话
字典操作都在锁内，复杂度 O(1)；过期清理 O(n)，n 是并发登录数（个位数）。

出口代理是显式参数：不填就装空 ProxyHandler 直连，**不读 HTTPS_PROXY**。
本机常驻着别的账号时，环境变量代理或默认直连都可能把这批登录关联到本地
IP 上；设置页的「固定验证代理」由 server 侧传进来，这里不做隐式兜底。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import clock, probe, store, token as tok
from .model import Account, normalize_epoch

#: 与 hub wb_accounts.REALM_CONFIGS / wb_identity.DESKTOP_VERSION_* 对齐。
#: UA 是官方客户端的版本串，网关靠它识别渠道；写死成 curl 会被挡在门外。
REALM_CONFIGS: dict[str, dict[str, str]] = {
    "intl": {
        "name": "国际版 (Global)",
        "base": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "ua": "WorkBuddy/5.9.4",
    },
    "cn": {
        "name": "国内版 (China)",
        "base": "https://copilot.tencent.com",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "ua": "WorkBuddy/5.8.3",
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"

#: 上游「用户还没在浏览器里点完」的业务码
LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
DEFAULT_PLATFORM = "CLI"
DEFAULT_TIMEOUT = 25.0

SOURCE = "oauth"


class WebLoginError(RuntimeError):
    """发起登录失败（地址/参数/上游拒绝）。poll 的网络错误不抛，转 pending。"""


def realm_config(realm: str) -> dict[str, str]:
    cfg = REALM_CONFIGS.get(str(realm or "").strip().lower())
    if cfg is None:
        raise WebLoginError("版本只能是 intl 或 cn")
    return cfg


def normalize_proxy(value: Any) -> str:
    """代理地址收拾成可直接交给 ProxyHandler 的形式。

    只放行 http/https：urllib 收到 socks5 会抛 `unknown url type`，与其运行时
    炸不如在这里判掉。与 recheck.normalize_url 不同——那个会把 user:pass 丢掉，
    代理槽可能带认证，这里保留原串。
    """
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if "://" not in text:
        text = "http://" + text
    if not text.startswith(("http://", "https://")):
        raise WebLoginError("代理只支持 http:// 或 https://")
    return text.rstrip("/")


def _headers(cfg: dict[str, str]) -> dict[str, str]:
    """官方插件渠道的固定头。登录三条请求共用同一份。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": cfg["ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
    }


def _request_json(url: str, method: str = "GET", headers: dict | None = None,
                  body: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
                  proxy: str = "") -> tuple[int, dict]:
    """发一个 JSON 请求，返回 (状态码, 解析后的 body)。

    非 2xx 也读 body：网关有时用 200 + code、有时用 4xx + JSON 表达失败，
    只看状态码会把两种混成一种。解析不了就留一段原文，方便界面显示上游说了什么。
    """
    data = (json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    # 复用 probe 的 opener：它显式装空 ProxyHandler，不吃环境变量。
    opener = probe._opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            payload = {"_raw": str(payload)[:1000]}
    except (ValueError, UnicodeDecodeError):
        payload = {"_raw": raw[:1000].decode("utf-8", "replace")}
    return status, payload


def _pick(data: dict, *keys: str) -> Any:
    """从上游资料里按别名取第一个非空值。上游版本之间字段名并不统一。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", 0, [], {}):
            return value
    return None


def _upstream_text(payload: dict) -> str:
    bits = [str(payload.get(k) or "") for k in ("msg", "message", "_raw", "error")]
    return " ".join(b for b in bits if b).strip()[:300]


def build_account(info: dict, data: dict, profile: dict) -> Account:
    """把 token 响应 + 账户资料组装成 Account。

    uid 以 JWT 为准：上游资料里的 id 可能是企业 id、会话 id，拿它当主键会让
    同一个号在库里裂成两行。JWT 解不出时才退回资料字段。
    """
    token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not token:
        raise WebLoginError("上游没给 accessToken")

    realm = str(info.get("realm") or "intl").lower()
    cfg = realm_config(realm)
    uid = tok.jwt_uid(token) or str(_pick(profile, "uid", "userId", "user_id", "id") or "").strip()
    if not uid:
        # 与 store.identity_of 的兜底口径一致：token 指纹，重复登录命中同一行
        import hashlib
        uid = "sha256-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    acc = Account()
    acc.uid = uid
    acc.nickname = str(_pick(profile, "nickname", "name", "displayName",
                             "userName", "username") or "").strip() or uid[:8]
    acc.email = str(_pick(profile, "email", "mail", "userEmail") or "").strip()
    acc.phone = str(_pick(profile, "phone", "mobile", "phoneNumber") or "").strip()
    acc.access_token = token
    acc.refresh_token = str(data.get("refreshToken") or data.get("refresh_token") or "").strip()
    acc.token_type = str(data.get("tokenType") or data.get("token_type") or "Bearer").strip()
    acc.expires_at = (normalize_epoch(data.get("expiresAt") or data.get("expires_at"))
                      or tok.jwt_exp(token))
    acc.refresh_expires_at = normalize_epoch(
        data.get("refreshExpiresAt") or data.get("refresh_expires_at"))
    acc.domain = str(data.get("domain") or _pick(profile, "domain", "host")
                     or cfg["domain"]).strip()
    acc.realm = realm
    acc.enterprise_id = str(_pick(profile, "enterpriseId", "enterprise_id",
                                  "tenantId", "corpId") or "").strip()
    acc.tier = str(_pick(profile, "tier", "plan", "level", "subscription") or "").strip()
    acc.source = SOURCE
    acc.platform = str(info.get("platform") or DEFAULT_PLATFORM)
    acc.product = "WorkBuddy"
    acc.proxy = normalize_proxy(info.get("proxy"))
    acc.enabled = True
    acc.created_at = normalize_epoch(_pick(profile, "createdAt", "created_at", "loginAt")) \
        or int(time.time())
    # 原始资料留档：上游以后加字段也不会静默丢，导出时随 payload 带走
    acc.profile_raw = profile
    acc.auth_raw = {"state": info.get("state"), "realm": realm,
                    "platform": acc.platform, "tokenType": acc.token_type}
    return acc


def account_view(acc: Account, row_id: int = 0) -> dict:
    """回给界面的账户摘要。token 一律不回显。"""
    return {
        "id": row_id,
        "uid": acc.uid,
        "name": acc.nickname or acc.uid[:8],
        "nickname": acc.nickname,
        "email": acc.email,
        "phone": acc.phone,
        "realm": acc.realm,
        "realm_label": tok.REALM_LABEL.get(acc.realm, acc.realm),
        "domain": acc.domain,
        "tier": acc.tier,
        "expires_at": acc.expires_at,
        "expires": clock.stamp(acc.expires_at, "%Y-%m-%d") if acc.expires_at else "",
        "source": acc.source,
        "has_rt": bool(acc.refresh_token),
        "profile": acc.profile_raw or {},
    }


class LoginManager:
    """一批进行中的官方登录会话。

    `requester` / `probe_fn` 可注入，测试里用假上游，不碰真实网络。
    """

    def __init__(self, requester: Callable | None = None,
                 probe_fn: Callable | None = None):
        self._request = requester or _request_json
        self._probe_one = probe_fn or probe.probe_one
        self._sessions: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ---- 会话管理 -------------------------------------------------
    def _put(self, state: str, info: dict) -> None:
        with self._lock:
            self._sessions[state] = info

    def _get(self, state: str) -> dict | None:
        with self._lock:
            return dict(self._sessions[state]) if state in self._sessions else None

    def _drop(self, state: str) -> None:
        with self._lock:
            self._sessions.pop(state, None)

    def cancel(self, state: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(state or ""), None) is not None

    def pending(self) -> int:
        """当前还在等的会话数，界面/测试用。"""
        with self._lock:
            return len(self._sessions)

    def _gc(self) -> None:
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._sessions.items()
                        if now - v.get("created", 0) > LOGIN_TTL_SECONDS]:
                self._sessions.pop(key, None)

    # ---- 三段流程 -------------------------------------------------
    def start(self, realm: str = "intl", platform: str = DEFAULT_PLATFORM,
              proxy: str = "", timeout: float = DEFAULT_TIMEOUT,
              fetch_credits: bool = True, target: dict | None = None) -> dict:
        """向官方申请一个登录 state，返回给浏览器打开的 authUrl。

        `target` 是已有账户那一行的摘要（id / uid / realm / name）：带上它，
        poll 成功后按行 id 覆盖凭据，而不是另插一行。
        """
        cfg = realm_config(realm)
        proxy = normalize_proxy(proxy)
        platform = str(platform or DEFAULT_PLATFORM).strip()[:40] or DEFAULT_PLATFORM
        url = "%s%s?platform=%s" % (cfg["base"], AUTH_STATE_PATH,
                                    urllib.parse.quote(platform))
        status, payload = self._request(url, "POST", _headers(cfg), {},
                                        timeout, proxy)
        if status >= 400:
            raise WebLoginError("申请授权失败：HTTP %s %s" % (status, _upstream_text(payload)))
        code = payload.get("code")
        if code not in (0, None, "0"):
            raise WebLoginError("申请授权失败：code=%s %s" % (code, _upstream_text(payload)))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        state = str((data or {}).get("state") or "").strip()
        auth_url = str((data or {}).get("authUrl") or (data or {}).get("auth_url") or "").strip()
        if not state or not auth_url:
            raise WebLoginError("上游没返回 state / authUrl：%s" % _upstream_text(payload))
        # 只允许官方登录页，避免上游被劫持时把浏览器送到别处
        if not auth_url.startswith(("https://", "http://")):
            raise WebLoginError("登录链接不是合法 URL")

        target_info = None
        if isinstance(target, dict) and target.get("id"):
            target_info = {k: target.get(k) for k in
                           ("id", "uid", "realm", "name", "exit_port")}
        self._gc()
        self._put(state, {"created": time.time(), "realm": str(realm).strip().lower(),
                          "platform": platform, "proxy": proxy, "state": state,
                          "fetch_credits": bool(fetch_credits), "target": target_info})
        return {
            "ok": True,
            "status": "pending",
            "state": state,
            "authUrl": auth_url,
            "realm": str(realm).lower(),
            "realm_label": cfg["name"],
            "platform": platform,
            "proxy": proxy,
            "expires_in": LOGIN_TTL_SECONDS,
            "target": target_info,
        }

    def poll(self, state: str, fetch_credits: bool | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
        """轮询一次。拿到 token 就拉资料、写库、清会话。

        只有 `status=ok` 才落库；pending / error / expired 都不改本地数据。
        """
        state = str(state or "").strip()
        info = self._get(state)
        if not info:
            return {"ok": True, "status": "unknown",
                    "message": "登录会话不存在或已完成，请重新获取登录链接"}
        if time.time() - info.get("created", 0) > LOGIN_TTL_SECONDS:
            self._drop(state)
            return {"ok": True, "status": "expired",
                    "message": "登录窗口已过期（10 分钟），请重新获取链接"}

        realm = str(info.get("realm") or "intl").lower()
        cfg = realm_config(realm)
        proxy = normalize_proxy(info.get("proxy"))
        # 开关落在会话里：轮询是 GET，不该让前端每次重复带一个写库选项
        if fetch_credits is None:
            fetch_credits = bool(info.get("fetch_credits", True))
        headers = _headers(cfg)
        token_url = "%s%s?state=%s" % (cfg["base"], AUTH_TOKEN_PATH,
                                       urllib.parse.quote(state))
        try:
            status, payload = self._request(token_url, "GET", headers, None,
                                            timeout, proxy)
        except Exception as exc:  # noqa: BLE001 — 轮询期间网络抖动不算失败
            return {"ok": True, "status": "pending",
                    "message": "轮询出错：%s: %s" % (type(exc).__name__, str(exc)[:160])}
        if status >= 400:
            return {"ok": True, "status": "error",
                    "message": "取 token 失败：HTTP %s %s" % (status, _upstream_text(payload))}

        code = payload.get("code")
        if code in (LOGIN_PENDING, str(LOGIN_PENDING)):
            return {"ok": True, "status": "pending",
                    "message": str(payload.get("msg") or "等待浏览器完成登录授权")}
        if code not in (0, None, "0"):
            return {"ok": True, "status": "error",
                    "message": "取 token 失败：code=%s %s" % (code, _upstream_text(payload))}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not (data or {}).get("accessToken") and not (data or {}).get("access_token"):
            return {"ok": True, "status": "pending", "message": "等待上游下发 token"}

        # 资料接口失败不影响入库：hub 里也是 best-effort，昵称退回 uid 前 8 位。
        profile: dict = {}
        acct_url = "%s%s?state=%s" % (cfg["base"], LOGIN_ACCOUNT_PATH,
                                      urllib.parse.quote(state))
        at = str(data.get("accessToken") or data.get("access_token"))
        acct_headers = dict(headers)
        acct_headers["Authorization"] = "Bearer " + at
        try:
            _, got = self._request(acct_url, "GET", acct_headers, None,
                                   min(timeout, 15.0), proxy)
            raw = got.get("data") if isinstance(got.get("data"), dict) else {}
            profile = dict(raw) if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001 — 同上，资料拿不到也要能入库
            profile = {}

        try:
            acc = build_account(info, data, profile)
        except WebLoginError as exc:
            return {"ok": True, "status": "error", "message": str(exc)}

        target = info.get("target") or None
        if target and not self._same_identity(acc.uid, str(target.get("uid") or "")):
            # 拿 A 的号去覆盖 B 那一行是最难查的一种串号，宁可不写。
            # 目标 uid 是 token 指纹（老导入的兜底值）时放行：那本来就不是
            # 上游给的真实 uid，重新登录拿到真 uid 才是对的。
            self._drop(state)
            return {"ok": True, "status": "mismatch",
                    "message": "这次登录的账号 uid 是 %s，和目标行 %s 不一致，本地库没有改动。"
                               "要把这个号新增进来，请点「改为新增」"
                               % (acc.uid[:24], str(target.get("name") or target.get("uid"))[:24]),
                    "account": account_view(acc), "target": target}

        stats: dict = {}
        row_id = 0
        try:
            if target:
                # 按行 id 覆盖：出口端口、批次、风控结论都留着，只换凭据和资料
                stats = store.rebind(int(target["id"]), acc,
                                     batch="oauth-" + clock.compact(), source=SOURCE)
                row_id = int(target["id"]) if stats.get("updated") else 0
            else:
                stats = store.upsert([acc], batch="oauth-" + clock.compact(),
                                     source=SOURCE)
                row_id = self._row_id(acc.uid, acc.realm)
        except Exception as exc:  # noqa: BLE001 — 唯一键冲突等要回给界面，不能 500
            return {"ok": True, "status": "error",
                    "message": "写入账户库失败：%s: %s" % (type(exc).__name__, str(exc)[:160]),
                    "target": target}
        if not row_id:
            return {"ok": True, "status": "error",
                    "message": "目标行已不存在，请刷新账户库后重试", "target": target}

        credits: dict = {}
        if fetch_credits and row_id:
            try:
                res = self._probe_one(acc, timeout=min(timeout, 20.0), proxy=proxy)
                store.update_credits([{
                    "id": row_id,
                    "state": res.get("state"),
                    "remain": res.get("remain"),
                    "used": res.get("used"),
                    "size": res.get("size"),
                    "checked_at": res.get("checked_at"),
                    "note": res.get("error") or "",
                    "restrict": bool(res.get("restrict")),
                }])
                credits = {"state": res.get("state") or "", "remain": res.get("remain"),
                           "used": res.get("used"), "size": res.get("size"),
                           "tier": res.get("tier") or "", "error": res.get("error") or ""}
            except Exception as exc:  # noqa: BLE001 — 积分是附加信息，失败不回滚入库
                credits = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}

        self._drop(state)
        return {
            "ok": True,
            "status": "ok",
            "message": "登录成功，已写入本地账户库",
            "account": account_view(acc, row_id),
            "store": stats,
            "credits": credits,
            "proxy": proxy,
            "target": target,
        }

    @staticmethod
    def _same_identity(logged_uid: str, target_uid: str) -> bool:
        return (not target_uid or logged_uid == target_uid
                or target_uid.startswith("sha256-"))

    @staticmethod
    def _row_id(uid: str, realm: str) -> int:
        """刚写进去那行的 id。按 uid 精确匹配，避免同名账号错位。"""
        try:
            rows = store.list_accounts(query=uid, limit=50)
        except Exception:  # noqa: BLE001 — 拿不到 id 就不查积分，不影响入库
            return 0
        for row in rows:
            if str(row.get("uid") or "") == uid and str(row.get("realm") or "") == realm:
                return int(row.get("id") or 0)
        return 0


# 全局一个管理器：state 是内存里的会话键，多个 Handler 实例必须共享同一份表
LOGINS = LoginManager()
