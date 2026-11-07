"""只读探针：校验凭据是否仍然有效，顺带取回积分余额。

## 为什么单独一个模块

导入的 token 是从别的系统搬过来的。搬过来时它有 `expiresAt`，但那只是签名里
写的时间戳——服务端有没有把这个号封掉、token 有没有被别人换掉、权限有没有
被收回，本地一个字段都看不出来。要看，只能拿这个 token 去问一次上游。

## 只读的边界

本模块只发两类请求，都是查询：

    POST {base}/billing/meter/get-user-resource-summary      套餐概览
    POST {base}/billing/meter/get-user-resource              积分明细

**不包含任何刷新逻辑。** 这条是硬约束，不是偷懒：

上游用 refresh token rotation——每次 refresh 签发新 RT，旧 RT 立即作废。
本工具持有的是从别处导入的副本，一旦拿 refresh_token 去换 access_token，
真正管辖这个账号的那套系统（hub / manager / panel）手里的 RT 就废了，
它下次刷新直接 401，正在服务的账号当场掉线。为了校验一次就打断别人的
服务，代价不对等。

所以这里遇到 401 就记「无效」，不去续命。要续命请到原系统里做。

同理也不碰签到、不做任何写操作——`daily-checkin` 那种接口签一次就少一次，
探针没资格替用户决定。

## 上游口径来源

接口路径、请求头、响应字段名照抄两个已经在生产跑的项目，没有自己发明：

- `ardeyouxipianyi/workbuddy2api-hub`  `wb_accounts.py:388-398`、`3081-3236`
- `L0NE-6/WorkBuddy-Daily`  `credits_report.py:56-70`、`207-216`

响应字段有多套变体（`CycleCapacityRemain` / `CycleCapacityRemainPrecise` /
`CapacityRemain`），两个区域答的还不完全一样，所以取值走“挨个试”而不是
写死一个键。
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from . import clock
from .model import Account

# ------------------------------------------------------------------ 区域端点
#: 国内版两个 base 都在生产里出现过：hub 用 codebuddy.cn（网关域名），
#: daily 用 copilot.tencent.com（客户端域名）。同一个账号在两边都能查，
#: 所以按顺序试，谁先回 code=0 就用谁——这样单边出故障时探针不会整体哑掉。
CN_BASES = ("https://copilot.tencent.com", "https://www.codebuddy.cn")
INTL_BASES = ("https://www.workbuddy.ai",)

#: 计费端点。国内版有的路径带 /v2 前缀有的不带，两个项目各有各的口径，
#: 所以带前缀和不带前缀各留一份，按 404 自动切换。
RESOURCE_SUMMARY_PATHS = (
    "/billing/meter/get-user-resource-summary",
    "/v2/billing/meter/get-user-resource-summary",
)
RESOURCE_PATHS = (
    "/billing/meter/get-user-resource",
    "/v2/billing/meter/get-user-resource",
)
ENTERPRISE_USAGE_PATHS = (
    "/v2/billing/meter/get-enterprise-user-usage",
    "/billing/meter/get-enterprise-user-usage",
)

#: 桌面端 UA。取 daily 项目实测能过的那个三段式形态：产品 / 产品 AI / 内置 CLI。
#: 换成 `WorkBuddy/5.8.4` 这种两段式在部分边缘节点会被判成非客户端流量。
USER_AGENT = "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/2.137.1"
IDE_VERSION = "5.5.2"

#: 分页查询体里的产品码。积分资源包的固定产品编码，抄自 daily `CREDITS_PRODUCT_CODE`。
PRODUCT_CODE = "p_tcaca"

#: 状态 0 = 生效中，3 = 已用完但未过期。两种都要，否则刚跑完额度的账号
#: 会被当成「查不到积分」，看起来像查错了。
PACKAGE_STATUS = [0, 3]

#: 到期窗口。取 101 年而不是 1 年：免费包和体验包的 CycleEndTime 能差出
#: 好几年，窗口开小了这些包直接查不出来，余额显示成 0。
EXPIRY_WINDOW_DAYS = 365 * 101

#: 状态取值。给数据库和界面共用，别的地方不要重新发明字符串。
STATE_VALID = "valid"        # 上游认这个 token
STATE_INVALID = "invalid"    # 上游明确拒绝（401/403 或 code 提示登录过期）
STATE_UNKNOWN = "unknown"    # 网络故障、超时、上游 5xx——不下结论
STATE_SKIPPED = "skipped"    # 本地就知道查不了（加密信封 / 无 token）

#: 这些字出现在上游回执里，基本就是「这个 token 不能用了」。
#: 实测码：401 直接给 HTML，业务层则回 code=10001 + 这些文案之一。
_AUTH_FAIL_HINTS = (
    "登录已过期", "登录过期", "请重新登录", "未登录", "token 无效",
    "token invalid", "unauthorized", "invalid token", "not login",
    "认证失败", "凭证无效",
)


# ------------------------------------------------------------------ 小工具
def _now() -> int:
    return int(time.time())


def _num(value: Any, default: float = 0.0) -> float:
    """上游把数字写成字符串、整数、浮点都有，统一转 float，失败给默认值。"""
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    # inf / nan 会污染 sum，当 0 处理
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _pick(mapping: dict, *keys: str) -> Any:
    """按顺序取第一个非空值。字段名各版本不一致，靠这个兜。"""
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None and value != "":
                return value
    return None


def _first_layer(payload: Any, *keys: str) -> dict:
    """从嵌套响应里挖出业务数据。

    两种形状都见过：
      {"code":0,"data":{"Response":{"Data":{"Accounts":[...]}}}}
      {"code":0,"data":{"Accounts":[...]}}
    所以不写死路径，顺着键名往下找。
    """
    node = payload
    for key in ("data",) + keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key) or {}
    return node if isinstance(node, dict) else {}


def _accounts_of(payload: Any) -> list:
    """从响应里取出 Accounts 数组，兼容 data.Response.Data 与 data 两层。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    response = data.get("Response")
    if isinstance(response, dict):
        inner = response.get("Data")
        if isinstance(inner, dict) and isinstance(inner.get("Accounts"), list):
            return inner["Accounts"]
    if isinstance(data.get("Accounts"), list):
        return data["Accounts"]
    return []


def _looks_like_auth_failure(text: str) -> bool:
    low = str(text or "").lower()
    return any(hint.lower() in low for hint in _AUTH_FAIL_HINTS)


def _opener(proxy: str = ""):
    """按账号自己的代理出站。

    没配代理就显式装空 ProxyHandler——不吃 HTTPS_PROXY 环境变量。理由是
    校验是逐个账号出站的，一旦某个账号该走代理、某个不该走，环境变量会把
    两者混在一起；升级模块那边就踩过代理挂掉连带把整个请求拖死的坑。
    """
    proxy = str(proxy or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url: str, body: dict, headers: dict, timeout: float,
               proxy: str = "") -> tuple[int, Any]:
    """POST 一个 JSON，返回 (状态码, 解析后的响应体)。

    非 2xx 也把 body 读出来：上游 401 常常带一段 HTML 错误页，或者业务层
    用 200 + code 表示失败。只看状态码会把这两种情况混成一种。
    """
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    opener = _opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                return resp.status, {"_raw": raw[:400].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            parsed = {"_raw": raw[:400].decode("utf-8", "replace")}
        return exc.code, parsed


def _bases(acc: Account) -> tuple[str, ...]:
    return CN_BASES if (acc.realm or "").lower() == "cn" else INTL_BASES


def _headers(acc: Account) -> dict:
    """计费端点的请求头。

    只发必需的：鉴权、内容类型、客户端身份。hub 那份还带 X-Machine-ID /
    X-Session-ID / X-Request-ID，但两个项目里 daily 用更小的集合就通了，
    说明网关不强制。少发几个头就少几条能被拿去做账号关联的特征。
    """
    head = {
        "Authorization": "Bearer " + acc.access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": IDE_VERSION,
        "X-Product": "WorkBuddy",
    }
    # 静态 API Key 两个鉴权头都发：计费端点任一个都认，但两套路径口径不同，
    # 统一形态省得后面再分叉。
    if acc.is_api_key:
        head["X-API-Key"] = acc.access_token
    uid = str(acc.uid or "").strip()
    if uid:
        head["X-User-Id"] = uid
    domain = str(acc.domain or "").strip()
    if domain:
        head["X-Domain"] = domain
    if acc.enterprise_id:
        head["X-Enterprise-Id"] = acc.enterprise_id
        head["X-Tenant-Id"] = acc.enterprise_id
    else:
        head["X-No-Enterprise-Id"] = "1"
    return head


# ------------------------------------------------------------------ 解析积分
def _package_credit(entry: dict) -> tuple[float, float, float]:
    """从单个资源包里取 (总量, 剩余, 已用)。

    字段家族有三套，且摘要与明细的命名顺序刚好相反（摘要 `CycleRemainCapacity`，
    明细 `CycleCapacityRemain`）。全部收进来，一次取值不区分来源，后面就不用
    维护两套解析。

    优先 precise 版本：整包容量常有小数，取整版本会把它抹平。
    """
    size = _num(_pick(entry, "CycleCapacitySizePrecise", "CycleCapacitySize",
                      "CycleTotalCapacity", "CapacitySizePrecise", "CapacitySize",
                      "TotalCapacity"))
    remain = _num(_pick(entry, "CycleCapacityRemainPrecise", "CycleCapacityRemain",
                        "CycleRemainCapacity", "CapacityRemainPrecise",
                        "CapacityRemain", "RemainCapacity"))
    used = _num(_pick(entry, "CycleCapacityUsedPrecise", "CycleCapacityUsed",
                      "CycleUsedCapacity", "CapacityUsedPrecise",
                      "CapacityUsed", "UsedCapacity"))

    # 三个数只给了两个时补第三个。上游不同版本漏字段的情况很常见，
    # 缺一个就把整包算成 0 会让人以为积分被清了。
    if size > 0 and used <= 0 and 0 <= remain <= size:
        used = max(0.0, size - remain)
    elif size > 0 and remain <= 0 and 0 <= used <= size:
        remain = max(0.0, size - used)
    elif size <= 0 and (remain > 0 or used > 0):
        size = remain + used
    return size, remain, used


def _credit_from_accounts(accounts: Iterable[dict]) -> tuple[float, float, float, int]:
    """汇总一批资源包 → (总量, 剩余, 已用, 包个数)。"""
    size = remain = used = 0.0
    count = 0
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        s, r, u = _package_credit(entry)
        size += s
        remain += r
        used += u
        count += 1
    return round(size, 2), round(remain, 2), round(used, 2), count


def _credit_from_summary(data: dict) -> tuple[float, float, float, int]:
    """摘要接口的 Packages 汇总。

    解析直接复用 _package_credit——摘要和明细的字段名已经被它一起收了，
    再写一套只会两处不同步。
    """
    packages = data.get("Packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        return 0.0, 0.0, 0.0, 0
    size = remain = used = 0.0
    count = 0
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        s, r, u = _package_credit(entry)
        size += s
        remain += r
        used += u
        count += 1
    return round(size, 2), round(remain, 2), round(used, 2), count


def _tier_from_summary(data: dict) -> str:
    """套餐名。免费号这里是空，付费号通常有「专业版」这类名字。

    没有 IsPaidUser 就返回空串——「没问到」不能写成「免费」。
    """
    if not isinstance(data, dict) or "IsPaidUser" not in data:
        return ""
    name = str(data.get("SubscriptionPackageName") or "").strip()
    if name:
        return name
    return "付费版" if data.get("IsPaidUser") else "免费版"


# ------------------------------------------------------------------ 单账号探测
def probe_one(acc: Account, timeout: float = 12.0, proxy: str = "") -> dict:
    """探一个账号，返回结果字典。

    返回字段：
      state        valid / invalid / unknown / skipped
      error        失败原因（成功时为空）
      remain/used/size  积分三元组，未知时为 None 不是 0
      tier         套餐名
      restrict     True 表示这次的拒绝来自账号级风控（不是 token 失效），
                   调用方据此把号移进风控区
      checked_at   本次探测时间戳
      latency_ms   耗时
      endpoint     实际打通的那个地址

    关键：remain 用 None 表示「不知道」，不用 0。0 是「查过了，真的是 0」，
    两者在界面上意思完全不同——一个是没余额，一个是没查到。
    """
    started = time.time()
    out: dict[str, Any] = {
        "state": STATE_UNKNOWN,
        "error": "",
        "remain": None,
        "used": None,
        "size": None,
        "tier": "",
        "packages": 0,
        "checked_at": _now(),
        "latency_ms": 0,
        "endpoint": "",
        "restrict": False,
    }

    # ---- 本地就能判定的，不发请求 ----
    token = str(acc.access_token or "").strip()
    if not token:
        out.update(state=STATE_SKIPPED, error="没有 access token")
        return out
    if acc.is_envelope:
        # 加密信封是客户端本地密钥加密的，服务端不认这串，硬发只会拿 401。
        out.update(state=STATE_SKIPPED, error="token 为加密信封，无法校验")
        return out
    if acc.is_api_key:
        # 静态 Key 走企业口径，另一条链路，见 _probe_api_key
        return _probe_api_key(acc, timeout=timeout, proxy=proxy, out=out, started=started)

    headers = _headers(acc)
    proxy = proxy or acc.proxy

    last_error = ""
    for base in _bases(acc):
        summary_url, summary_body = "", {}
        status, body = 0, {}
        answered = False  # 有没有拿到「非 404」的响应
        # ---- 摘要接口：一次请求既能判有效性，也带套餐名 ----
        for path in RESOURCE_SUMMARY_PATHS:
            url = base + path
            try:
                status, body = _post_json(url, {}, headers, timeout, proxy)
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                status, body = 0, {}
                break
            if status == 404:
                # 这版网关不认这个前缀。两个前缀都 404 说明 base 选错了，
                # 不能当成「通过」——那会把路径错误显示成有效账号。
                continue
            summary_url, summary_body = url, body if isinstance(body, dict) else {}
            answered = True
            break

        if status == 0:
            continue  # 这个 base 连不通，试下一个
        if not answered:
            last_error = f"{base} 上所有计费接口都 404（网关版本不匹配）"
            continue  # 换下一个 base

        # ---- 明确拒绝 ----
        if status in (401, 403):
            # 403 里夹着风控措辞的，是账号级限制而不是 token 失效：
            # 两者要分开，风控的号该进风控区等解封，不是让人去重扫码。
            restricted = looks_restricted(_body_text(body))
            out.update(state=STATE_INVALID,
                       error="被风控拦截（账号级限制）" if restricted
                       else "上游拒绝：登录已过期或 token 无效")
            if restricted:
                out["restrict"] = True
            out["latency_ms"] = int((time.time() - started) * 1000)
            out["endpoint"] = base
            return out

        code = summary_body.get("code")
        msg = str(_pick(summary_body, "msg", "message", "error") or "")
        if code not in (0, "0", None):
            if looks_restricted(msg) or looks_restricted(_body_text(summary_body)):
                out.update(state=STATE_INVALID, error=msg or "被风控拦截")
                out["restrict"] = True
            elif _looks_like_auth_failure(msg) or status == 401:
                out.update(state=STATE_INVALID, error=msg or "上游拒绝")
            else:
                out.update(state=STATE_UNKNOWN, error=f"code {code}: {msg}"[:200])
            out["latency_ms"] = int((time.time() - started) * 1000)
            out["endpoint"] = base
            return out

        # ---- 通过：算积分 ----
        data = summary_body.get("data")
        data = data if isinstance(data, dict) else {}
        out["state"] = STATE_VALID
        out["tier"] = _tier_from_summary(data)
        out["endpoint"] = base

        size, remain, used, count = _credit_from_summary(data)

        # ---- 明细接口：摘要只有容量三元组，逐包明细才给准数 ----
        detail = _fetch_detail(acc, base, headers, timeout, proxy)
        if detail is not None:
            d_size, d_remain, d_used, d_count = _credit_from_accounts(detail)
            # 明细优先。摘要那套 Cycle*Capacity 在某些账号上不填，全是 0，
            # 拿它覆盖明细会把有余额的账号显示成 0。
            if d_count and (d_size > 0 or d_remain > 0 or d_used > 0):
                size, remain, used, count = d_size, d_remain, d_used, d_count

        if count:
            out.update(remain=remain, used=used, size=size, packages=count)
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out

    # 所有 base 都没连上：这是网络问题，不是账号问题，不能判成无效
    out.update(state=STATE_UNKNOWN, error=last_error or "所有上游地址都不可达")
    out["latency_ms"] = int((time.time() - started) * 1000)
    return out


def _fetch_detail(acc: Account, base: str, headers: dict, timeout: float,
                  proxy: str) -> list | None:
    """拉逐包明细。拿不到就返回 None，让调用方保留摘要的结果。"""
    # 窗口边界按北京时间给：上游按北京时间算套餐到期日，用 UTC 边界会在
    # 每天早上 8 点之前漏掉「今天到期」的那一包。
    begin = clock.stamp()
    end = clock.stamp(time.time() + EXPIRY_WINDOW_DAYS * 86400)
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": PRODUCT_CODE,
        "Status": list(PACKAGE_STATUS),
        "PackageEndTimeRangeBegin": begin,
        "PackageEndTimeRangeEnd": end,
    }
    for path in RESOURCE_PATHS:
        try:
            status, payload = _post_json(base + path, body, headers, timeout, proxy)
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError):
            return None
        if status == 404:
            continue
        if status != 200:
            return None
        if isinstance(payload, dict) and payload.get("code") not in (0, "0", None):
            return None
        return _accounts_of(payload)
    return None


def _probe_api_key(acc: Account, timeout: float, proxy: str, out: dict,
                   started: float) -> dict:
    """静态 API Key：不能刷新、没有个人身份，额度走企业口径端点。

    hub 那边实测 `/billing/meter/get-user-resource-summary` 对 Key 直接 401，
    个人 get-user-resource 返回空 Accounts，所以这里只问企业用量。
    """
    headers = _headers(acc)
    proxy = proxy or acc.proxy
    for base in _bases(acc):
        for path in ENTERPRISE_USAGE_PATHS:
            try:
                status, payload = _post_json(base + path, {}, headers, timeout, proxy)
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
                out.update(state=STATE_UNKNOWN, error=f"{type(exc).__name__}: {exc}"[:200])
                continue
            if status == 404:
                continue  # 换下一种前缀
            if status in (401, 403):
                out.update(state=STATE_INVALID, error="上游拒绝：API Key 无效或已吊销")
                out["latency_ms"] = int((time.time() - started) * 1000)
                out["endpoint"] = base
                return out
            if status != 200:
                out.update(state=STATE_UNKNOWN, error=f"HTTP {status}")
                out["latency_ms"] = int((time.time() - started) * 1000)
                return out

            data = payload.get("data") if isinstance(payload, dict) else None
            data = data if isinstance(data, dict) else {}
            limit = _num(_pick(data, "limit_num", "LimitNum", "limitNum", "total"))
            used = _num(_pick(data, "used_num", "UsedNum", "usedNum", "used"))
            if limit <= 0 and used <= 0:
                # 企业端点回 200 但没数——Key 有效，只是没有额度记录
                out.update(state=STATE_VALID, error="")
                out["latency_ms"] = int((time.time() - started) * 1000)
                out["endpoint"] = base
                return out
            out.update(state=STATE_VALID, remain=round(max(0.0, limit - used), 2),
                       used=round(used, 2), size=round(limit, 2), packages=1,
                       endpoint=base)
            out["latency_ms"] = int((time.time() - started) * 1000)
            return out

    out.update(state=STATE_UNKNOWN, error="企业用量端点不可达")
    out["latency_ms"] = int((time.time() - started) * 1000)
    return out


# ------------------------------------------------------------------ 批量探测
class Prober:
    """带并发闸的批量探测。

    为什么要闸：一批几十个账号同时打同一个上游，是标准的风控触发姿势。
    这里两件事同时做：

    1. 同时在飞的请求数封顶（默认 4）。上游每个账号的额度查询都要走一次
       计费链路，并发拉满除了快几百毫秒没有任何好处。
    2. 每个请求发出前随机停 200~500ms。等间隔的请求节奏太整齐，按间隔
       特征一眼就能筛出来是脚本。

    另外同一账号的探测加了进程内互斥：界面上连点两次「校验」，第二次会
    等第一次的结果，而不是再花一次上游调用。
    """

    def __init__(self, concurrency: int = 4, timeout: float = 12.0,
                 min_gap: float = 0.20, max_gap: float = 0.50):
        self.concurrency = max(1, int(concurrency or 1))
        self.timeout = max(3.0, float(timeout or 12.0))
        self.min_gap = max(0.0, float(min_gap or 0.0))
        self.max_gap = max(self.min_gap, float(max_gap or self.min_gap))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def run(self, accounts: Iterable[Account],
            on_result: Callable[[Account, dict], None] | None = None,
            should_stop: Callable[[], bool] | None = None) -> list[dict]:
        """跑完一批，按输入顺序返回结果。

        用线程而不是 asyncio：整站是纯标准库 + ThreadingHTTPServer，为了
        一次批量查询引入事件循环不值得。并发上限只有 4，线程开销可以忽略。
        """
        items = list(accounts)
        if not items:
            return []

        results: list[dict | None] = [None] * len(items)
        # 局部导入：只有批量路径需要线程池，单发路径不该为它付导入开销
        from concurrent.futures import ThreadPoolExecutor

        def work(index: int, acc: Account) -> None:
            if should_stop and should_stop():
                results[index] = {
                    "state": STATE_UNKNOWN, "error": "已取消", "remain": None,
                    "used": None, "size": None, "tier": "", "packages": 0,
                    "checked_at": _now(), "latency_ms": 0, "endpoint": "",
                }
                return
            # 每个请求前错开，别让四个线程在同一毫秒出手
            time.sleep(random.uniform(self.min_gap, self.max_gap))
            key = f"{acc.uid}|{acc.realm}"
            lock = self._lock_for(key)
            with lock:
                result = probe_one(acc, timeout=self.timeout, proxy=acc.proxy)
            results[index] = result
            if on_result:
                try:
                    on_result(acc, result)
                except Exception:
                    # 回调是给人看的进度，它出错不该把整批拖垮
                    pass

        with ThreadPoolExecutor(max_workers=self.concurrency,
                                thread_name_prefix="wk-probe") as pool:
            futures = [pool.submit(work, i, acc) for i, acc in enumerate(items)]
            for fut in futures:
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - 单个账号失败不影响整批
                    index = futures.index(fut)
                    results[index] = {
                        "state": STATE_UNKNOWN, "error": f"{type(exc).__name__}: {exc}"[:200],
                        "remain": None, "used": None, "size": None, "tier": "",
                        "packages": 0, "checked_at": _now(), "latency_ms": 0,
                        "endpoint": "",
                    }

        return [r for r in results if r is not None]


def usable(row: dict) -> bool:
    """这个账号现在能不能直接拿去用。

    和 `state == valid` 不是一回事，hub 也是这么分的：

    - **valid**：上游认这个 token，能查到数据。这是「凭据有效」。
    - **usable**：在 valid 之上还有额度。remain 查到了且 <= 0 的号，
      拿去跑会立刻 402/余额不足，对使用者来说等于不可用。

    remain 为 None（上游没回明细）时不算不可用：没有证据表明它没额度，
    判成不可用会把好号挡在外面。这是 hub 的 `wb_accounts.py` 里
    `available = code == 0 and (remain > 0 or remain is None)` 同一个判断。
    """
    if str(row.get("state") or "") != STATE_VALID:
        return False
    remain = row.get("remain")
    if isinstance(remain, (int, float)):
        return float(remain) > 0
    return True


def summarize(results: Iterable[dict]) -> dict:
    """把一批结果折成计数，给界面顶部显示。

    多给一个 `usable`：有效但没额度的号，用户关心的是「能用几个」，
    不是「token 没过期几个」。
    """
    counts = {STATE_VALID: 0, STATE_INVALID: 0, STATE_UNKNOWN: 0, STATE_SKIPPED: 0}
    remain = 0.0
    known_remain = 0
    ok = 0
    exhausted = 0
    for row in results:
        state = str(row.get("state") or STATE_UNKNOWN)
        if state in counts:
            counts[state] += 1
        if usable(row):
            ok += 1
        elif state == STATE_VALID:
            # 有效但余额为 0：单独计数，界面上一句话就能说清「几个能用、
            # 几个是空号」，不用人去逐个看积分列
            exhausted += 1
        value = row.get("remain")
        if isinstance(value, (int, float)):
            remain += float(value)
            known_remain += 1
    return {
        **counts,
        "total": sum(counts.values()),
        "usable": ok,
        "exhausted": exhausted,
        "remain_total": round(remain, 2),
        "with_credit": known_remain,
    }


# ======================================================================
# 对话探测：给「封号复检」用
# ======================================================================
# 为什么不能用上面的计费接口判定封号：
#   实测（GenLiya/workbuddy-registration-notes README 5.2）账号被风控后，
#   计费接口照样返回 code=0、积分照样查得到——封的是「对话」这条链路。
#   所以想验证「解封了没有」，唯一有效的动作是发一次真实的对话请求。
#
# 为什么不能刷新 token：
#   见模块顶部。复检只用现有 access_token，401 就认了。

#: 对话端点。国内版和国际版的 base 与计费不是一套：
#:   计费 cn 走 www.codebuddy.cn，对话 cn 走 copilot.tencent.com；
#:   www.workbuddy.cn 是同一个后端的另一个入口，海外出口连 copilot 会被
#:   复位 TLS（hub 的 wb_identity 里记着这条），所以留作备用。
CHAT_BASES_CN = ("https://copilot.tencent.com", "https://www.workbuddy.cn")
CHAT_BASES_INTL = ("https://www.workbuddy.ai",)

#: 上游对话路径。hub 的 `CHAT_PATH = "/v2/chat/completions"`（wb_proxy.py:303），
#: 不带前缀的版本在部分边缘节点也认，按 404 自动切换。
CHAT_PATHS = ("/v2/chat/completions", "/chat/completions")

#: 探测模型。面板侧写 `cn:hy3` / `global:hy3`，那个前缀是面板自己用来分池的，
#: 转发到上游时被剥掉——上游认得的是裸 `hy3`。送带前缀的名字会拿到
#: 11133 model_param_invalid，把「还封着」误报成「参数错误」。
CHAT_MODEL = "hy3"

#: 400 之后的降级请求体。上游的 400 绝大多数不是账号问题，而是这个 body
#: 里有它不认的字段：某个版本不认 `stream`、有的网关把 `max_tokens` 卡在
#: 最小 16。带一版最朴素的再敲一次，能区分「参数问题」和「这个号真被拒了」。
CHAT_BODY_FALLBACK = {
    "model": CHAT_MODEL,
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 16,
}

#: 账号级风控的特征。
#: 实测正文（两套措辞都来自生产）：
#:   {"code":11140,"msg":"request illegal",
#:    "displayMsg":{"zh":"账号存在异常行为，已被限制使用"}}
#:   {"msg":"request illegal","displayMsg":{"zh":"请求被拦截，请重新发送"}}
#: 只收实测出现的措辞，不猜同义词——多收一个词就会把内容审核的 403 判成封号。
RESTRICT_CODE = "11140"
RESTRICT_MARKERS = (
    "request illegal",
    "请求被拦截",
    "账号存在异常行为",
    "已被限制",
    "unusual activity",
    "account has been restricted",
    "account is restricted",
)

#: 复检结论。和 STATE_* 分开：这里问的是「账号还能不能用对话」，
#: 不是「token 还有没有效」。一个号可以 token 有效但对话被封。
RECHECK_REVIVED = "revived"    # 对话通了 —— 风控已解除
RECHECK_BANNED = "banned"      # 仍被风控拦（11140 一类）
RECHECK_AUTH = "auth"          # 凭据本身失效（401），与封号无关
RECHECK_UNKNOWN = "unknown"    # 网络故障 / 限流 / 无法判定
RECHECK_SKIPPED = "skipped"    # 本地就知道测不了（无 token / 加密信封 / 静态 Key）


def _chat_bases(acc: Account) -> tuple[str, ...]:
    return CHAT_BASES_CN if (acc.realm or "").lower() == "cn" else CHAT_BASES_INTL


def looks_restricted(text: str) -> bool:
    """正文里有没有账号级风控的特征。

    单独立成函数是为了让测试能直接打它：这条判据错了，整套复检的结论
    就全反了（把「封着」判成「解封」会让人把废号重新导回生产）。
    """
    low = str(text or "").lower()
    if RESTRICT_CODE in low:
        return True
    return any(marker.lower() in low for marker in RESTRICT_MARKERS)


def _body_text(body: Any) -> str:
    """把响应体压成一段可搜索的文本。

    上游把关键信息分散在 msg / displayMsg.{zh,en} / error.message 里，
    有的版本还会把整个 JSON 再套一层字符串。全部拼起来搜，比逐字段试稳。
    """
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    for key in ("msg", "message", "error", "code", "detail", "displayMsg"):
        value = body.get(key)
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
        elif value is not None:
            parts.append(str(value))
    raw = body.get("_raw")
    if isinstance(raw, str):
        parts.append(raw)
    return " ".join(parts)


def _error_code(body: Any) -> str:
    """取上游的错误码。只认结构化字段，不从正文里正则捞——捞错一个数字
    就会把 400 解释成完全无关的原因。"""
    if not isinstance(body, dict):
        return ""
    for key in ("code", "codeName", "errorCode", "errCode"):
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, str)) and str(value).strip():
            return str(value).strip()
    err = body.get("error")
    if isinstance(err, dict):
        for key in ("code", "type"):
            value = err.get(key)
            if isinstance(value, (int, str)) and str(value).strip():
                return str(value).strip()
    return ""


#: 400 的已知成因。只收实测见过的码，猜同义词会把「参数问题」说成「封号」。
BAD_REQUEST_HINTS = {
    "11133": "模型名或参数不被接受",
    "11134": "请求参数不合法",
    "11135": "请求体缺字段",
}


def _clip(text: str, limit: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def chat_probe(acc: Account, timeout: float = 25.0, proxy: str = "") -> dict:
    """发一次最小对话请求，判断这个号的对话链路还能不能用。

    返回：
      verdict    revived / banned / auth / unknown / skipped
      detail     一句话结论（给界面显示）
      raw       上游原话（截断 200 字，排查用）
      status    HTTP 状态码
      endpoint  实际打通的地址
      latency_ms

    成本：max_tokens=5、单轮、不流式。这个请求会真扣一点点额度，所以复检
    频率必须由调用方控制——本函数不做节流，`wk/recheck.py` 负责。
    """
    started = time.time()
    out: dict[str, Any] = {
        "verdict": RECHECK_UNKNOWN,
        "detail": "",
        "raw": "",
        "status": 0,
        "endpoint": "",
        "latency_ms": 0,
    }

    token = str(acc.access_token or "").strip()
    if not token:
        out.update(verdict=RECHECK_SKIPPED, detail="没有 access token")
        return out
    if acc.is_envelope:
        out.update(verdict=RECHECK_SKIPPED, detail="token 为加密信封，无法探测")
        return out
    if acc.is_api_key:
        # 静态 Key 不走 OAuth 对话链路，复检对它没有意义
        out.update(verdict=RECHECK_SKIPPED, detail="静态 API Key 不适用封号复检")
        return out

    headers = _headers(acc)
    headers["Accept"] = "application/json"
    body = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "stream": False,
    }
    proxy = proxy or acc.proxy

    last_error = ""
    for base in _chat_bases(acc):
        for path in CHAT_PATHS:
            url = base + path
            try:
                status, payload = _post_json(url, body, headers, timeout, proxy)
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                status, payload = 0, {}
                break  # 这个 base 连不通，换下一个
            if status == 404:
                continue  # 路径不对，试另一个前缀
            if status == 400:
                # 先按最朴素的 body 再敲一次。能过就说明刚才那份带了它不认的
                # 字段；过不了就把第一次的原话带出去（那是"上游为什么不收"
                # 的唯一线索，换成我们的措辞只会更难查）。
                retry_status, retry_payload = _post_json(
                    url, dict(CHAT_BODY_FALLBACK), headers, timeout, proxy)
                if retry_status != 400:
                    return _read_chat_reply(retry_status, retry_payload, url, started)
            return _read_chat_reply(status, payload, url, started)

        if status == 0:
            continue

    # 一个都没打通时把试过的地址写出来：用户要判断的是「是不是地址配错了」，
    # 只说「不可达」等于让他自己去猜我们打的是哪。
    bases = "、".join(_chat_bases(acc))
    out.update(detail=f"对话端点都不可达：{bases}",
               raw=last_error[:200])
    out["latency_ms"] = int((time.time() - started) * 1000)
    return out


def _read_chat_reply(status: int, payload: Any, url: str, started: float) -> dict:
    """把一次对话响应翻成复检结论。

    判定顺序不能换：先看风控特征，再看 401/403，最后才认 200。
    理由是上游偶尔在「被限制」时也回 200 + 一段业务错误体（网关降级），
    先认 200 会把封着的号判成解封。
    """
    text = _body_text(payload)
    out = {
        "status": status,
        "endpoint": url,
        "raw": text[:200],
        "latency_ms": int((time.time() - started) * 1000),
    }

    if looks_restricted(text):
        out.update(verdict=RECHECK_BANNED,
                   detail="仍被风控拦截（账号级限制，等上游自动解除）")
        return out

    if status == 401:
        # 401 只可能是凭据本身不认了（网关鉴权层直接拒，正文常常是空白
        # HTML，没有业务提示词可搜）。判 auth 而不是 unknown：这个号确实
        # 不该再敲门，等用户重新导入。
        out.update(verdict=RECHECK_AUTH, detail="凭据失效（401），需要重新登录")
        return out

    if status == 403:
        # 风控分支在上面已经 return 了，走到这里说明 403 不是风控措辞——
        # 多半是内容审核或权限，不能算「仍被封」，也不能算解封。
        out.update(verdict=RECHECK_UNKNOWN,
                   detail="HTTP 403，但正文没有风控特征，无法判定")
        return out

    if status == 400:
        # 400 是「上游收到了、但不认这份请求」，和账号状态无关：地址是通的
        # （不通会是 0 / 超时），凭据也过鉴权了（不认会是 401）。真正的
        # 起因几乎都在 body 或模型名上，所以把上游原话和错误码一起给出，
        # 让人自己判断是模型换了还是网关加了必填字段。
        code = _error_code(payload)
        hint = BAD_REQUEST_HINTS.get(code, "请求体被上游拒绝")
        said = _clip(text, 60)
        # 「地址和凭据都通」这句不能省：看到 400 的人第一反应是地址填错了，
        # 而这恰恰是这个状态码唯一能排除的东西（地址错是连不上，凭据错是 401）。
        detail = (f"HTTP 400，{hint}" + (f"（{code}）" if code else "")
                  + "；地址和凭据都通，不是账号状态")
        if said:
            detail += f"；上游：{said}"
        out.update(verdict=RECHECK_UNKNOWN, detail=detail)
        return out

    if status == 429:
        out.update(verdict=RECHECK_UNKNOWN, detail="上游限流，复检结果不可信")
        return out

    if status == 200:
        out.update(verdict=RECHECK_REVIVED, detail="对话已通，风控解除")
        return out

    if status == 0:
        out.update(verdict=RECHECK_UNKNOWN, detail="连接失败")
        return out

    out.update(verdict=RECHECK_UNKNOWN, detail=f"HTTP {status}，未确认")
    return out
