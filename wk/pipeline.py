"""转换管线：校验 → 补全 → 去重 → 过滤 → 统计。

每一步都产出可解释的结果，而不是悄悄丢数据。界面上的「跳过 3 条」必须能
点开看到是哪 3 条、为什么跳过——跨项目迁移里静默丢账号是最难排查的事故。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import token as tok
from .model import Account


@dataclass
class Issue:
    """单条账号的问题记录。level: error 会被跳过，warn 只提示。"""

    index: int
    uid: str
    level: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "uid": self.uid,
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }


@dataclass
class ConvertResult:
    accounts: list[Account] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    input_count: int = 0
    dropped: int = 0
    merged: int = 0
    filled_uid: int = 0

    @property
    def kept(self) -> int:
        return len(self.accounts)

    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    def warn_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "warn")


def derive_uid(acc: Account) -> str:
    """从 token 派生 uid。

    静态 Key 的形态是 ck_<id>.<secret>，取 <id> 段派生 apikey-<id>——
    与 Go 版面板、hub 同一口径，两边导出的文件可以互认。
    """
    token = str(acc.access_token or "").strip()
    if token.startswith(("ck_", "pt_")):
        head = token.split(".", 1)[0]
        ident = head.split("_", 1)[1] if "_" in head else head
        if ident:
            return f"apikey-{ident}"
    from_jwt = tok.jwt_uid(token)
    if from_jwt:
        return from_jwt
    return ""


def fill_missing(accounts: list[Account], result: ConvertResult) -> list[Account]:
    """补 uid / realm / domain / expiresAt。"""
    for acc in accounts:
        if not acc.uid:
            derived = derive_uid(acc)
            if derived:
                acc.uid = derived
                result.filled_uid += 1
        if not acc.realm:
            acc.realm = tok.detect_realm(acc.access_token, acc.domain, acc.api_key)
        if not acc.domain:
            acc.domain = tok.domain_for_realm(acc.realm)
        if not acc.expires_at and acc.access_token:
            acc.expires_at = tok.jwt_exp(acc.access_token)
    return accounts


def validate(accounts: list[Account], opts: dict) -> ConvertResult:
    """逐条校验。返回只含通过项的 ConvertResult。"""
    result = ConvertResult(input_count=len(accounts))
    require_token = bool(opts.get("require_token", True))
    allow_envelope = bool(opts.get("allow_envelope", True))
    allow_expired = bool(opts.get("allow_expired", True))

    kept: list[Account] = []
    for index, acc in enumerate(accounts, 1):
        if not acc.uid:
            result.issues.append(Issue(index, "", "error", "missing_uid",
                                       "缺少 uid，且无法从 token 推导"))
            result.dropped += 1
            continue

        if not acc.has_token:
            if require_token:
                result.issues.append(Issue(index, acc.uid, "error", "missing_token",
                                           "缺少 access_token，目标项目会拒绝导入"))
                result.dropped += 1
                continue
            result.issues.append(Issue(index, acc.uid, "warn", "missing_token",
                                       "无 access_token，按当前设置仍保留"))

        if acc.is_envelope:
            if not allow_envelope:
                result.issues.append(Issue(index, acc.uid, "error", "envelope",
                                           "token 是加密信封，当前设置不允许导出"))
                result.dropped += 1
                continue
            result.issues.append(Issue(index, acc.uid, "warn", "envelope",
                                       "token 为加密信封：可导入可切换，签到/积分不可用"))

        state = tok.token_state(acc.access_token, acc.expires_at, acc.refresh_token)
        if state in ("expired",) and not allow_expired:
            result.issues.append(Issue(index, acc.uid, "error", "expired",
                                       "token 已过期且无 refresh_token，当前设置会跳过"))
            result.dropped += 1
            continue
        if state == "expired":
            result.issues.append(Issue(index, acc.uid, "warn", "expired",
                                       "token 已过期且无 refresh_token，导入后需重新登录"))
        elif state == "refreshable":
            result.issues.append(Issue(index, acc.uid, "warn", "refreshable",
                                       "token 已过期但带 refresh_token，可由目标项目续期"))
        elif state == "expiring":
            result.issues.append(Issue(index, acc.uid, "warn", "expiring",
                                       "token 24 小时内到期"))

        if acc.access_token and not acc.is_envelope and not acc.is_api_key:
            # 非 JWT 的明文 token 多半是复制残缺（少了段）
            if acc.access_token.count(".") != 2:
                result.issues.append(Issue(index, acc.uid, "warn", "not_jwt",
                                           "access_token 不是三段式 JWT，也不是静态 Key"))

        kept.append(acc)

    result.accounts = kept
    return result


def dedupe(accounts: list[Account], key: str, policy: str) -> tuple[list[Account], int]:
    """去重。

    key: uid | token | uid_token
    policy: keep_first | keep_last | keep_newest | keep_both

    keep_both 会把重复项改成 uid-2、uid-3 追加——这是唯一「不丢行」的策略，
    用在「同一账号两个环境各有一份凭据、都要保留」的场景。
    """
    if key == "none" or not accounts:
        return accounts, 0

    def key_of(acc: Account) -> str:
        if key == "token":
            return acc.access_token or f"__noid__{id(acc)}"
        if key == "uid_token":
            return acc.uid or acc.access_token or f"__noid__{id(acc)}"
        return acc.uid or f"__noid__{id(acc)}"

    buckets: dict[str, list[Account]] = {}
    order: list[str] = []
    for acc in accounts:
        k = key_of(acc)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(acc)

    merged = 0
    out: list[Account] = []
    for k in order:
        group = buckets[k]
        if len(group) == 1:
            out.append(group[0])
            continue
        merged += len(group) - 1
        if policy == "keep_both":
            base_uid = group[0].uid
            out.append(group[0])
            for n, acc in enumerate(group[1:], 2):
                acc.uid = f"{base_uid}-{n}"
                out.append(acc)
            continue
        if policy == "keep_last":
            out.append(group[-1])
        elif policy == "keep_newest":
            # 到期时间大的更可能是最新一次登录换出来的凭据
            out.append(max(group, key=lambda a: (a.expires_at, a.created_at)))
        else:  # keep_first
            out.append(group[0])
    return out, merged


def convert(accounts: list[Account], opts: dict | None = None) -> ConvertResult:
    """完整管线。opts 见 server.py 的 /api/convert 参数说明。"""
    opts = opts or {}
    result = validate(accounts, opts)
    kept = fill_missing(result.accounts, result)

    # 补 uid 后可能出现新的重复（两条都缺 uid、都从同一 token 推导），再查一遍空 uid
    still_missing = [a for a in kept if not a.uid]
    if still_missing:
        for acc in still_missing:
            result.issues.append(Issue(0, "", "error", "missing_uid",
                                       "补全后仍缺 uid，已跳过"))
            result.dropped += 1
        kept = [a for a in kept if a.uid]

    key = str(opts.get("dedupe_key") or "uid")
    policy = str(opts.get("dedupe_policy") or "keep_first")
    kept, merged = dedupe(kept, key, policy)
    result.merged = merged
    result.accounts = kept

    if opts.get("only_enabled"):
        before = len(kept)
        kept = [a for a in kept if a.enabled]
        result.dropped += before - len(kept)
        result.accounts = kept

    if opts.get("fill_source"):
        for acc in kept:
            if not acc.source:
                acc.source = str(opts["fill_source"])

    return result


def summarize(accounts: list[Account]) -> dict[str, Any]:
    """给界面和导出报告用的统计。"""
    total = len(accounts)
    states: dict[str, int] = {}
    realms: dict[str, int] = {}
    domains: dict[str, int] = {}
    fields = {
        "access_token": 0,
        "refresh_token": 0,
        "device_token": 0,
        "email": 0,
        "phone": 0,
        "nickname": 0,
        "enterprise_id": 0,
        "proxy": 0,
        "expires_at": 0,
    }

    for acc in accounts:
        state = tok.token_state(acc.access_token, acc.expires_at, acc.refresh_token)
        states[state] = states.get(state, 0) + 1
        realm = acc.realm or "unknown"
        realms[realm] = realms.get(realm, 0) + 1
        if acc.domain:
            domains[acc.domain] = domains.get(acc.domain, 0) + 1
        for key in fields:
            if getattr(acc, key, None):
                fields[key] += 1

    return {
        "total": total,
        "states": [
            {"key": k, "label": tok.STATE_LABEL.get(k, k), "count": v}
            for k, v in sorted(states.items(), key=lambda kv: -kv[1])
        ],
        "realms": [
            {"key": k, "label": tok.REALM_LABEL.get(k, "未知"), "count": v}
            for k, v in sorted(realms.items(), key=lambda kv: -kv[1])
        ],
        "domains": [
            {"key": k, "count": v} for k, v in sorted(domains.items(), key=lambda kv: -kv[1])[:8]
        ],
        "fields": [
            {"key": k, "count": v, "ratio": round(v / total * 100, 1) if total else 0}
            for k, v in fields.items()
        ],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
