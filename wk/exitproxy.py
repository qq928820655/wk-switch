"""按账户出口（exit_port）改写导出账户的 proxy 字段。

本工具给每个号钉的出口是 DB 里的 ``accounts.exit_port``（0 = 直连），
hub 的每日活跃 / chat 走的是账户对象自己的 ``self.proxy``：
``wb_accounts.py:2889`` 桌面腿 ``urlopen(req, proxy=self.proxy)``、
``wb_accounts.py:2619`` 网页腿 ``wb_webagent.run_turn(..., proxy=self.proxy)``。

hub 加载账户文件时（``wb_accounts.py:987-990``）把 ``proxy`` 存成
``proxy_legacy``，再由 ``apply_proxy_slots()``（``wb_accounts.py:4614``）
拿 ``proxySlot`` 去 settings.json 的 ``proxy_slots`` 里查出 URL 覆盖它。
所以只写 ``proxy`` 不够：**必须同时清掉 ``proxySlot``**，否则这个号
仍会跟着 hub 的代理槽走，本工具配的端口被无声丢掉。

这个模块只做「DB 端口 → Account.proxy / proxy_slot」这一步翻译，
不认识 HTTP、不碰数据库、不写文件（除自己的 config）。导出链路在
拿到 Account 列表之后、交给 formats.render() 之前调用 ``apply()``。
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Iterable

from . import recheck

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "exitproxy.json")

_lock = threading.Lock()

#: 只放行主机名 / IPv4 形态的字符。IPv6 字面量不支持：mihomo 的
#: listener 端口本来就是单独一列，host 里再带冒号只会拼出畸形 URL。
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")

DEFAULTS: dict[str, Any] = {
    #: 导出时是否按账户出口写 proxy。关掉就完全不碰账户文件的 proxy 字段。
    "enabled": True,
    #: override = 一律按 exit_port 写（含 0 = 清成直连）
    #: fill     = 只写当前没有 proxy / proxySlot 的号，保留 hub 原有槽绑定
    "mode": "override",
    #: mihomo 所在主机名，不含端口（端口由每个账户的 exit_port 提供）。
    #: 留空则回落到 config/recheck.json 的 mihomo.host。
    "host": "",
    #: hub 的 urlopen(ProxyHandler) 只认 http/https。
    "scheme": "http",
    #: all = 所有版本都写；intl = 只写国际版（国内版没有每日活跃任务）。
    "scope": "all",
}


def _coerce(cfg: dict) -> dict:
    out = dict(cfg)
    out["enabled"] = bool(out.get("enabled", True))
    mode = str(out.get("mode") or "").strip().lower()
    out["mode"] = mode if mode in ("override", "fill") else DEFAULTS["mode"]
    scope = str(out.get("scope") or "").strip().lower()
    out["scope"] = scope if scope in ("all", "intl") else DEFAULTS["scope"]
    scheme = str(out.get("scheme") or "").strip().lower()
    out["scheme"] = scheme if scheme in ("http", "https") else DEFAULTS["scheme"]
    out["host"] = str(out.get("host") or "").strip()
    return out


def load_config() -> dict:
    """读 config/exitproxy.json。坏文件/缺文件都回落默认值。"""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return _coerce(cfg)


def save_config(patch: dict) -> dict:
    """合并写盘。原子替换 + .bak，和 recheck.save_config() 同口径。"""
    with _lock:
        merged = load_config()
        merged.update({k: v for k, v in (patch or {}).items() if k in DEFAULTS})
        merged = _coerce(merged)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            if os.path.exists(CONFIG_PATH):
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
        return merged


def resolve_host(cfg: dict | None = None) -> str:
    """拿到干净的代理主机名。

    允许界面上填 ``cli-proxy-mihomo`` / ``1.2.3.4`` / ``http://host:9090``，
    后两种的 scheme 和端口都剥掉：端口永远来自账户自己的 exit_port，
    external-controller 的 9090 不是代理监听口，拼进去会变成两段端口。
    留空回落到 recheck 的节点配置（含 WB_PROXY_DISCOVER_HOST 环境变量）。
    """
    cfg = cfg or load_config()
    text = str(cfg.get("host") or "").strip()
    if not text:
        mihomo = recheck.load_config().get("mihomo") or {}
        text = str(mihomo.get("host") or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0].strip()
    return text if _HOST_RE.match(text) else ""


def proxy_url(port: int, cfg: dict | None = None) -> str:
    """``exit_port`` → hub 认识的代理 URL。0 / 坏 host 返回空串（直连）。"""
    port = int(port or 0)
    if port <= 0 or port > 65535:
        return ""
    cfg = cfg or load_config()
    host = resolve_host(cfg)
    if not host:
        return ""
    return f"{cfg['scheme']}://{host}:{port}"


def apply(rows: Iterable[tuple[int, Any, int]], cfg: dict | None = None) -> dict:
    """把出口写进 Account，返回统计。

    ``rows`` 是 ``(row_id, Account, exit_port)`` 三元组，由
    ``store.load_for_export(with_ids=True)`` 一次 SQL 给全，避免再查一遍库
    或者靠 uid 去重对齐（uid 可能重复，row_id 才是唯一身份）。

    原地改 Account 的 ``proxy`` / ``proxy_slot``；所有 profile 只要渲染了
    proxy 字段就自然带上，formats 不用改。
    """
    cfg = _coerce(cfg or load_config())
    rows = list(rows)
    host = resolve_host(cfg)
    stat: dict[str, Any] = {
        "enabled": cfg["enabled"],
        "mode": cfg["mode"],
        "scope": cfg["scope"],
        "host": host,
        "applied": 0,     # 写了具体代理 URL
        "cleared": 0,     # exit_port=0，清成直连
        "skipped": 0,     # 未按出口改写，保留账户文件原值
    }
    if not cfg["enabled"]:
        stat["skipped"] = len(rows)
        return stat

    for _row_id, acc, port in rows:
        realm = str(getattr(acc, "realm", "") or "").lower()
        if cfg["scope"] == "intl" and realm != "intl":
            stat["skipped"] += 1
            continue
        bound = bool(getattr(acc, "proxy_slot", "") or getattr(acc, "proxy", ""))
        if cfg["mode"] == "fill" and bound:
            stat["skipped"] += 1
            continue
        url = proxy_url(port, cfg)
        # 槽的存在会让 hub 在加载时用 settings.json 覆盖 proxy，必须清掉。
        acc.proxy = url
        acc.proxy_slot = ""
        if url:
            stat["applied"] += 1
        else:
            stat["cleared"] += 1
    return stat
