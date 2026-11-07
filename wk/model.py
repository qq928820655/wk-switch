"""统一账户模型。

七个项目的账户文件字段名各不相同（camelCase / snake_case / 嵌套 / 扁平），
但语义是同一组：uid、昵称、AT、RT、到期、域、realm、企业、设备 token。
这里定义唯一的中间表示，所有格式的读入先落到 Account，写出时再按目标格式渲染。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

# 字段别名表：从左到右依次尝试，第一个非空值生效。
# 顺序有意义——嵌套形里 auth 层和 account 层可能同时出现同名字段，调用方
# 传进来的层顺序决定谁优先。
ALIASES: dict[str, tuple[str, ...]] = {
    "uid": ("uid", "user_id", "userId", "id", "account_id"),
    "nickname": ("nickname", "name", "display_name", "displayName", "user_name", "username"),
    "email": ("email", "mail", "user_email"),
    "phone": ("phone", "mobile", "phone_number", "phoneNumber"),
    "access_token": ("accessToken", "access_token", "token", "at"),
    "refresh_token": ("refreshToken", "refresh_token", "rt"),
    "token_type": ("tokenType", "token_type"),
    "expires_at": ("expiresAt", "expires_at", "expiry", "expired_at"),
    "refresh_expires_at": ("refreshExpiresAt", "refresh_expires_at"),
    "domain": ("domain", "host", "site"),
    "realm": ("realm", "region", "zone"),
    "enterprise_id": ("enterpriseId", "enterprise_id", "corp_id", "tenant_id"),
    "device_token": ("device_token", "deviceToken"),
    "proxy": ("proxy", "proxy_url", "proxyUrl"),
    "proxy_slot": ("proxySlot", "proxy_slot"),
    "enabled": ("enabled", "active", "is_enabled"),
    "weight": ("weight", "priority"),
    "note": ("note", "remark", "comment", "label"),
    "source": ("source", "from", "origin"),
    "platform": ("platform", "os", "client"),
    "product": ("product", "app", "product_name"),
    "credits": ("credits", "balance", "quota", "credit"),
    "tier": ("tier", "plan", "level"),
    "created_at": ("createdAt", "created_at", "addedAt", "added_at", "login_at"),
    "api_key": ("apiKey", "api_key", "isApiKey"),
    "tasks_enabled": ("tasksEnabled", "tasks_enabled"),
    "disabled_models": ("disabledModels", "disabled_models"),
    "auth_raw": ("auth_raw", "authRaw"),
    "profile_raw": ("profile_raw", "profileRaw"),
    "last_checkin": ("lastCheckin", "last_checkin"),
    "last_daily_chat": ("lastDailyChat", "last_daily_chat"),
    "last_error": ("lastError", "last_error"),
    "cooldown_until": ("cooldownUntil", "cooldown_until"),
    "restricted_until": ("restrictedUntil", "restricted_until"),
    "env_line": ("env_line",),
}

# 归一化后仍要原样透传、不参与语义判断的杂项字段。
# 它们不丢弃：目标项目可能新增了自己的字段，跨版本迁移时静默丢字段比报错更糟。
PASSTHROUGH_PREFIXES = ("last", "restricted", "cooldown")


def _first(layers: list[dict], key: str) -> Any:
    """按层顺序取第一个非空值。层顺序由调用方决定（越靠前优先级越高）。"""
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for alias in ALIASES.get(key, (key,)):
            if alias in layer:
                value = layer[alias]
                if value is None or value == "":
                    continue
                return value
    return None


def _as_int(value: Any) -> int:
    """时间戳/计数统一转 int；解析不了就 0（不编造值）。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "enabled", "y"):
        return True
    if text in ("0", "false", "no", "off", "disabled", "n"):
        return False
    return default


# 秒 / 毫秒分界：1e11 秒已是公元 5138 年，真实凭证不可能；
# 1e11 毫秒 = 1973 年，毫秒制当前值（~1.8e12）远在其上。两侧都不会误判。
_MILLIS_THRESHOLD = 100_000_000_000


def normalize_epoch(value: Any) -> int:
    """把秒/毫秒混用的时间戳统一到 Unix 秒。"""
    ts = _as_int(value)
    if ts > _MILLIS_THRESHOLD:
        return ts // 1000
    return ts


@dataclass
class Account:
    """跨项目统一的账户记录。

    `extra` 保留原始行里所有未识别的键，导出时按需回填——跨项目迁移最容易
    出的事故就是「导进去少了一半字段」，而不是报错。
    """

    uid: str = ""
    nickname: str = ""
    email: str = ""
    phone: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = ""
    expires_at: int = 0
    refresh_expires_at: int = 0
    domain: str = ""
    realm: str = ""
    enterprise_id: str = ""
    device_token: str = ""
    proxy: str = ""
    proxy_slot: str = ""
    enabled: bool = True
    weight: int = 1
    note: str = ""
    source: str = ""
    platform: str = ""
    product: str = ""
    credits: int = 0
    tier: str = ""
    # ---- 探针回填（见 wk/probe.py）------------------------------------
    #: 积分三元组。用 float 是因为上游给的是 12.5 这种小数。
    #: None 表示「没查到」，0 表示「查到了，就是 0」——两者不能混。
    credit_remain: float | None = None
    credit_used: float | None = None
    credit_size: float | None = None
    #: valid / invalid / unknown / skipped，由 probe 模块定义
    credit_state: str = ""
    #: 上次探测时间戳（秒）与失败原因，都只存探针写的，不跟导入字段混
    checked_at: int = 0
    checked_note: str = ""
    # ---- 封号复检（见 wk/recheck.py）----------------------------------
    #: 名单状态：空 / pending / banned / revived / auth。
    #: 只有 banned 会进「封号复检」标签页。
    recheck_state: str = ""
    #: 复检次数与上次复检时间戳。min_interval 靠它判断能不能再敲一次。
    recheck_count: int = 0
    recheck_at: int = 0
    #: 上次复检的结论原文（上游原话），排查用
    recheck_note: str = ""
    created_at: int = 0
    api_key: bool = False
    tasks_enabled: bool = True
    disabled_models: list[str] = field(default_factory=list)
    auth_raw: Any = None
    profile_raw: Any = None
    env_line: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    #: 会话内稳定引用。导入时按行序打上 `r1` `r2`…，之后勾选、删除、逐行
    #: 标记都认它。**不能拿 uid 当键**：`keep_both` 会给重复项改 uid（加
    #: `-2` 后缀），改一次键就漂一次，用户勾好的行会突然对不上。
    #: 这个字段不进 `to_dict()`，所以不会出现在任何导出文件里。
    ref: str = ""
    #: 进管线之前的原始 uid。`keep_both` 是原地改 uid 的，而导入页里每改一个
    #: 选项就会重跑一遍 convert——没有这个基座，第二次跑会把 `x-2` 当成新
    #: 的原始 uid 再加一层后缀，跑几次就叠成 `x-2-2-2`。
    #: 同样不进 `to_dict()`。
    base_uid: str = ""

    # ---- 派生属性 -------------------------------------------------
    @property
    def has_token(self) -> bool:
        return bool(self.access_token.strip())

    @property
    def is_envelope(self) -> bool:
        """新版客户端（5.6.2+ AtRestEncryption）把 token 存成加密信封。

        形如 {"$wbEncrypted":1,"envelope":"base64"}。这种账号能导入、能切换，
        但签到/积分这类要真 token 的功能用不了——所以必须单独标出来，
        不能当普通 token 处理。
        """
        return is_envelope_value(self.access_token)

    @property
    def is_api_key(self) -> bool:
        """静态 API Key（ck_/pt_ 前缀）不是 JWT，也不能刷新。"""
        return self.api_key or self.access_token.startswith(("ck_", "pt_"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # ref / base_uid 是给界面认行和管线幂等用的，不属于账号数据。漏在
        # 导出里会让目标项目多出两个谁也不认识的字段，也可能被别家当成 uid
        # 的替代品读走。
        data.pop("ref", None)
        data.pop("base_uid", None)
        data["has_token"] = self.has_token
        data["is_envelope"] = self.is_envelope
        data["is_api_key"] = self.is_api_key
        return data

    def masked(self) -> dict[str, Any]:
        """给界面用的脱敏视图：token 只留头尾，避免明文凭据进前端。"""
        data = self.to_dict()
        for key in ("access_token", "refresh_token", "device_token"):
            data[key] = mask_secret(data.get(key) or "")
        return data


def is_envelope_value(value: Any) -> bool:
    """判断一个值是不是 WorkBuddy 加密信封。

    信封可能以对象形式出现，也可能是 JSON 字符串（环境变量里常见）。
    """
    if isinstance(value, dict):
        return "$wbEncrypted" in value or "envelope" in value
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return False
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(parsed, dict) and ("$wbEncrypted" in parsed or "envelope" in parsed)
    return False


def mask_secret(value: str) -> str:
    """脱敏：保留前 6 后 4，中间打码。长度不足则整体打码。"""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


def _stringify(value: Any) -> str:
    """把可能是对象/数组的展示字段压成字符串。

    外部导入文件里 nickname 偶尔是对象（加密信封），直接 str() 会得到
    "{'$wbEncrypted': 1, ...}" 这种脏数据，所以对象一律返回空串，
    让上层按 uid 回落。
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def from_row(row: dict, source: str = "") -> Account:
    """把任意项目的单条账户记录转成 Account。

    层的优先级：auth 子对象 > account 子对象 > 顶层。
    manager / panel 的嵌套形把 token 放 auth、身份放 account，
    而扁平形把两者都放顶层——两种都得能读。
    """
    if not isinstance(row, dict):
        raise ValueError("账号条目必须是对象")

    auth = row.get("auth") if isinstance(row.get("auth"), dict) else {}
    profile = row.get("account") if isinstance(row.get("account"), dict) else {}
    # 嵌套形里 token 在 auth、身份在 account；扁平形两者都在顶层。
    # 顺序 auth → account → 顶层 保证嵌套形不会漏字段。
    layers = [auth, profile, row]

    acc = Account()
    acc.uid = _stringify(_first(layers, "uid"))
    acc.nickname = _stringify(_first(layers, "nickname"))
    acc.email = _stringify(_first(layers, "email"))
    acc.phone = _stringify(_first(layers, "phone"))

    raw_at = _first(layers, "access_token")
    # 信封是对象时保留原结构（渲染时按目标格式再决定要不要序列化），
    # 是字符串时原样留着——两种都能被目标项目识别。
    acc.access_token = raw_at if isinstance(raw_at, (dict, str)) else _stringify(raw_at)
    if isinstance(acc.access_token, dict):
        acc.access_token = json.dumps(acc.access_token, ensure_ascii=False)

    acc.refresh_token = _stringify(_first(layers, "refresh_token"))
    acc.token_type = _stringify(_first(layers, "token_type"))
    acc.expires_at = normalize_epoch(_first(layers, "expires_at"))
    acc.refresh_expires_at = normalize_epoch(_first(layers, "refresh_expires_at"))
    acc.domain = _stringify(_first(layers, "domain"))
    acc.realm = _stringify(_first(layers, "realm")).lower()
    acc.enterprise_id = _stringify(_first(layers, "enterprise_id"))
    acc.device_token = _stringify(row.get("device_token") or _first(layers, "device_token"))
    acc.proxy = _stringify(_first(layers, "proxy"))
    acc.proxy_slot = _stringify(_first(layers, "proxy_slot"))
    acc.enabled = _as_bool(_first(layers, "enabled"), True)
    acc.weight = _as_int(_first(layers, "weight")) or 1
    acc.note = _stringify(_first(layers, "note"))
    acc.source = _stringify(_first(layers, "source")) or source
    acc.platform = _stringify(_first(layers, "platform"))
    acc.product = _stringify(_first(layers, "product"))
    acc.credits = _as_int(_first(layers, "credits"))
    acc.tier = _stringify(_first(layers, "tier"))
    acc.created_at = normalize_epoch(_first(layers, "created_at"))
    acc.api_key = _as_bool(_first(layers, "api_key"), False)
    acc.tasks_enabled = _as_bool(_first(layers, "tasks_enabled"), True)
    acc.env_line = _stringify(_first(layers, "env_line"))

    models = _first(layers, "disabled_models")
    if isinstance(models, list):
        acc.disabled_models = [str(m) for m in models]
    elif isinstance(models, str) and models.strip():
        acc.disabled_models = [m.strip() for m in models.split(",") if m.strip()]

    acc.auth_raw = row.get("auth_raw") or _first(layers, "auth_raw")
    acc.profile_raw = row.get("profile_raw") or _first(layers, "profile_raw")

    # 未识别字段原样保留。嵌套形的 auth/account 子对象已拆开处理，不再重复收。
    known = {alias for aliases in ALIASES.values() for alias in aliases}
    for key, value in row.items():
        if key in ("auth", "account", "auth_raw", "profile_raw", "path"):
            continue
        if key not in known:
            acc.extra[key] = value
    return acc
