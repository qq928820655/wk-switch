"""封号复检：把被上游风控拦掉的账号，定时单独「敲一次门」。

## 逻辑来源

照 `GenLiya/workbuddy-registration-notes` 的 `banned-retry.mjs` 搬过来，
保留它踩出来的四条结论，改掉一条不适用的：

1. **必须发真实对话请求**。它试过重新 OAuth 登录——登录能成功、拿回同一个
   uid，但一发请求照样 `11140 request illegal`。说明封在账号层，不在会话层，
   重新登录解不了。所以复检动作只能是「发一次 max_tokens=5 的对话」。
2. **计费接口判不出封号**。同一个 README 5.2 记着「注册成功、也刷出积分，
   但一使用就被拦」——计费 200 而对话 403。所以本模块不复用 `probe_one`。
3. **成功率判断只有 200 算数**。其余一律留在名单里等下一轮。
4. **最小间隔**。banned-retry 默认 3600 秒，防手抖连点。这里默认 6 小时，
   因为每敲一次都是一次真实请求，敲太密等于自己续上风控。

**改掉的那一条**：它必须「临时暂停同域其它启用号」。原因写在它源码里——
面板按池子自动选号，不暂停的话请求会落到健康号身上，测不到被测号。
本工具不带池子：每个号拿自己的 access_token 单发，请求必然落在它自己身上。
所以不需要暂停任何别的号，也就没有「恢复失败导致好号被永久停用」这个风险。

## 速度：串行，不并发

`banned-retry.mjs` 是逐号串行 + 每次 sleep 1500ms。这里保持串行，理由不是
「简单」，而是 hub 在 `wb_proxy.py:15630` 留的那条红线：

> 看到 11140 立即停手，解封前严禁换号继续打同一台设备——设备指纹会连坐。

离线复检虽然不经过池子，但一批被封号同时从同一台机器打出去，特征一样。
所以：号与号之间固定间隔（默认 3 秒），并发度固定为 1，不给开关。

## 出口 IP

`--proxy` 一路可选。配了 mihomo 就每号换一个节点——同一个出口 IP 是当初
触发风控的原因之一（README 5.1：同出口 IP 紧接连开，靠后的全挂），复检时
继续用同一个 IP 敲门，等于告诉上游「还是我」。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from . import probe, store
from .model import Account

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "recheck.json")

#: 名单状态。空字符串 = 不在名单里（健康号）。
#: 只有 banned 会进「封号复检」标签页。
TRACK_NONE = ""
TRACK_PENDING = "pending"    # 手工加入 / 待首次复检
TRACK_BANNED = "banned"      # 复检仍被拦
TRACK_REVIVED = "revived"    # 复检通过（下一轮导入/探针会把它清掉）
TRACK_AUTH = "auth"          # 凭据失效，不是封号——留档但不该继续敲门

TRACK_LABELS = {
    TRACK_PENDING: "待复检",
    TRACK_BANNED: "封号中",
    TRACK_REVIVED: "已解封",
    TRACK_AUTH: "凭据失效",
    probe.RECHECK_UNKNOWN: "未确认",
}

#: 导入时自动入名单的文本特征由 store 那边定义（入库时要用同一份判据），
#: 这里只是转出来给调用方用，避免两处各写一份然后慢慢漂移。
BANNED_HINTS = store.BANNED_HINTS

DEFAULTS: dict[str, Any] = {
    #: 总开关。关掉后复检接口直接返回「未开启」，不做任何网络动作。
    "enabled": False,
    #: 同一个号两次复检的最小间隔（秒）。默认 6 小时。
    "min_interval": 6 * 3600,
    #: 号与号之间的固定间隔（秒）。不许配 0——那是并发敲门的另一种写法。
    "gap_seconds": 3.0,
    #: 单次对话请求超时。上游冷启动偶尔要 20 秒以上。
    "timeout": 25.0,
    #: 是否按号轮换出口节点。
    "rotate_proxy": True,
    #: 一轮最多复检几个号。名单上百个时，一轮敲太久没有意义，
    #: 而且请求越多越容易再次触发风控。
    "batch_limit": 20,
    #: 每日自动复检。banned-retry 原文就是「每天一次」，靠计划任务跑；
    #: 这里内置一个守护线程，省得用户再去配 crontab。
    "auto": True,
    #: 自动复检的触发小时（本地时间 0-23）。默认凌晨 3 点——那个时段上游
    #: 风控窗口刚过一轮，且不影响用户白天用。
    "auto_hour": 3,
    "mihomo": {
        "enabled": True,
        #: 默认值抄 hub 的 `PROXY_DISCOVER_HOST`：docker 网络里的服务名。
        "host": "cli-proxy-mihomo",
        #: 端口区间。hub 的 `_proxy_port_range()` 默认 17901-17910，
        #: 一个端口 = 一个 listener = 一个固定出口。
        "ports": "17901-17910",
        #: 探出口 IP 用。hub 的 `probe_proxy_exit` 也是打这个地址。
        "probe_url": "https://api.ipify.org",
        "timeout": 10.0,
    },
}

_lock = threading.Lock()


# ------------------------------------------------------------------ 配置
def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in (patch or {}).items():
        if key == "mihomo" and isinstance(value, dict):
            out["mihomo"] = {**base.get("mihomo", {}), **value}
        elif key in base:
            out[key] = value
    return out


def _coerce(cfg: dict) -> dict:
    """把配置钳到安全范围。

    上下限不是洁癖：
    - min_interval 小于 10 分钟，等于把复检变成持续敲门，风控只会更重；
    - gap_seconds 为 0 就是并发，踩 hub 留的连坐红线；
    - batch_limit 太大，一轮里敲门次数过多，同样是在堆特征。
    """
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["rotate_proxy"] = bool(cfg.get("rotate_proxy"))
    try:
        cfg["min_interval"] = max(600, int(cfg.get("min_interval") or DEFAULTS["min_interval"]))
    except (TypeError, ValueError):
        cfg["min_interval"] = DEFAULTS["min_interval"]
    try:
        cfg["gap_seconds"] = min(60.0, max(1.0, float(cfg.get("gap_seconds"))))
    except (TypeError, ValueError):
        cfg["gap_seconds"] = DEFAULTS["gap_seconds"]
    try:
        cfg["timeout"] = min(120.0, max(5.0, float(cfg.get("timeout"))))
    except (TypeError, ValueError):
        cfg["timeout"] = DEFAULTS["timeout"]
    try:
        cfg["batch_limit"] = min(200, max(1, int(cfg.get("batch_limit"))))
    except (TypeError, ValueError):
        cfg["batch_limit"] = DEFAULTS["batch_limit"]
    cfg["auto"] = bool(cfg.get("auto"))
    try:
        cfg["auto_hour"] = min(23, max(0, int(cfg.get("auto_hour"))))
    except (TypeError, ValueError):
        cfg["auto_hour"] = DEFAULTS["auto_hour"]

    mihomo = cfg.get("mihomo")
    if not isinstance(mihomo, dict):
        mihomo = {}
    # 环境变量优先：用户机器上 hub 已经配过一套，不该再让他在页面上抄一遍。
    host = str(os.environ.get("WB_PROXY_DISCOVER_HOST") or mihomo.get("host") or "").strip()
    ports = str(os.environ.get("WB_PROXY_DISCOVER_PORTS") or mihomo.get("ports") or "").strip()
    cfg["mihomo"] = {
        "enabled": bool(mihomo.get("enabled", True)),
        "host": host or DEFAULTS["mihomo"]["host"],
        "ports": ports or DEFAULTS["mihomo"]["ports"],
        "probe_url": str(mihomo.get("probe_url") or DEFAULTS["mihomo"]["probe_url"]).strip(),
        "timeout": _float(mihomo.get("timeout"), DEFAULTS["mihomo"]["timeout"], 3.0, 60.0),
    }
    return cfg


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def load_config() -> dict:
    """读 config/recheck.json。坏文件不炸，回默认值。"""
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝，别让调用方改到默认值
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg = _merge(cfg, raw)
    except (OSError, ValueError):
        pass
    return _coerce(cfg)


def save_config(patch: dict) -> dict:
    """合并写入。只写 config/recheck.json，原子替换，写坏可回滚。

    先把旧文件复制成 .bak 再写：这个文件被界面反复保存，一次断电写到一半
    就再也读不出来了，而它里面只有开关和端口，没有不可重建的东西。
    """
    with _lock:
        current = load_config()
        merged = _coerce(_merge(current, patch or {}))
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


# ------------------------------------------------------------------ mihomo
def parse_ports(spec: str) -> list[int]:
    """`17901-17910` / `17901,17903` / `7890` → 端口列表。

    抄 hub `_proxy_port_range()` 的语义，只是多支持逗号写法：这里给人手填，
    写一串不连续的端口比写区间更常见。
    """
    text = str(spec or "").strip()
    if not text:
        return []
    out: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            if lo.isdigit() and hi.isdigit():
                a, b = int(lo), int(hi)
                if a > b:
                    a, b = b, a
                out.extend(range(a, b + 1))
        elif chunk.isdigit():
            out.append(int(chunk))
    # 去重保序，并砍掉明显不是 HTTP 代理口的范围（防止手滑写 1-65535）
    seen: set[int] = set()
    clean = []
    for port in out:
        if 0 < port < 65536 and port not in seen:
            seen.add(port)
            clean.append(port)
    return clean[:64]


def _proxy_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _opener(proxy_url: str):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))


def probe_exit(proxy_url: str, probe_url: str, timeout: float = 10.0) -> dict:
    """测一个节点能不能出网，以及它的出口 IP。

    只判「通不通、从哪出去」。不做 IP 归属地查询——那要调第三方接口，
    多一个依赖、多一份限速风险，而这里只需要知道「换了没有」。
    """
    started = time.time()
    req = urllib.request.Request(probe_url, method="GET",
                                 headers={"User-Agent": "curl/8.5.0"})
    try:
        with _opener(proxy_url).open(req, timeout=timeout) as resp:
            ip = resp.read(64).decode("utf-8", "replace").strip()
        return {
            "url": proxy_url,
            "ok": bool(ip),
            "exit_ip": ip,
            "latency_ms": int((time.time() - started) * 1000),
            "error": "" if ip else "空响应",
        }
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return {
            "url": proxy_url,
            "ok": False,
            "exit_ip": "",
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:160],
        }


def discover_nodes(cfg: dict | None = None, probe_now: bool = True) -> dict:
    """扫一遍配置里的 mihomo 端口，返回可用节点。

    对齐 hub 的 `discover_proxy_slots()`：端口即槽位，能出网就算可用。
    `probe_now=False` 只列地址不测——页面上「自动检测」按钮才需要真测，
    打开页面时不该替用户发十个请求。
    """
    cfg = cfg or load_config()
    mihomo = cfg["mihomo"]
    ports = parse_ports(mihomo["ports"])
    urls = [_proxy_url(mihomo["host"], port) for port in ports]

    if not probe_now:
        return {
            "host": mihomo["host"],
            "ports": ports,
            "probed": False,
            "nodes": [{"url": u, "ok": False, "exit_ip": "", "latency_ms": 0, "error": ""}
                      for u in urls],
            "ok_count": 0,
        }

    if not urls:
        return {"host": mihomo["host"], "ports": [], "probed": True,
                "nodes": [], "ok_count": 0}

    # 并发 8：十个端口串行、每个超时 10 秒，最坏要 100 秒，按钮点下去像卡死。
    # 并发度按端口数封顶，不会把本机连接表打满。
    from concurrent.futures import ThreadPoolExecutor

    def probe(url: str) -> dict:
        return probe_exit(url, mihomo["probe_url"], mihomo["timeout"])

    with ThreadPoolExecutor(max_workers=min(8, len(urls)),
                            thread_name_prefix="wk-node") as pool:
        nodes = list(pool.map(probe, urls))

    return {
        "host": mihomo["host"],
        "ports": ports,
        "probed": True,
        "nodes": nodes,
        "ok_count": sum(1 for n in nodes if n.get("ok")),
    }


def _pick_proxy(nodes: list[dict], index: int) -> str:
    """按号轮换节点。节点不够就循环，一个都没通就直连。"""
    usable = [n["url"] for n in nodes if n.get("ok")]
    if not usable:
        return ""
    return usable[index % len(usable)]


# ------------------------------------------------------------------ 名单判定
def looks_banned(text: str) -> bool:
    """这段文字里有没有「封号」的证据。"""
    low = str(text or "").lower()
    return any(hint.lower() in low for hint in BANNED_HINTS)


def account_banned_hint(acc: Account) -> str:
    """从导入字段里找「来源系统已把它标成封号」的证据，命中返回原文片段。"""
    return store.banned_hint_of(acc)

# ------------------------------------------------------------------ 复检
class Runner:
    """串行复检器。并发度固定 1，没有开关（理由见模块顶部）。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.nodes: list[dict] = []

    def _nodes(self) -> list[dict]:
        if not self.cfg["mihomo"]["enabled"] or not self.cfg["rotate_proxy"]:
            return []
        if not self.nodes:
            self.nodes = discover_nodes(self.cfg, probe_now=True)["nodes"]
        return self.nodes

    def run(self, pairs: Iterable[tuple[int, Account]], force: bool = False,
            on_result: Callable[[int, Account, dict], None] | None = None) -> list[dict]:
        """跑一轮。pairs 是 [(行 id, Account)]。

        返回值每项：{id, verdict, detail, raw, status, endpoint, latency_ms,
                     proxy, checked_at, skipped}

        `force=True` 跳过最小间隔检查（界面上「立即复检」按钮）。
        """
        items = list(pairs)[: self.cfg["batch_limit"]]
        nodes = self._nodes()
        now = int(time.time())
        results: list[dict] = []

        for index, (row_id, acc) in enumerate(items):
            # ---- 最小间隔 ----
            if not force:
                last = int(getattr(acc, "recheck_at", 0) or 0)
                if last and now - last < self.cfg["min_interval"]:
                    left = int((self.cfg["min_interval"] - (now - last)) // 60)
                    results.append({
                        "id": row_id, "verdict": "skipped", "skipped": True,
                        "detail": f"距上次复检不足 {self.cfg['min_interval'] // 60} 分钟（还差 {left} 分）",
                        "raw": "", "status": 0, "endpoint": "", "latency_ms": 0,
                        "proxy": "", "checked_at": now,
                    })
                    if on_result:
                        on_result(row_id, acc, results[-1])
                    continue

            # ---- 号与号之间停一下 ----
            if index:
                time.sleep(self.cfg["gap_seconds"])

            proxy = _pick_proxy(nodes, index)
            res = probe.chat_probe(acc, timeout=self.cfg["timeout"], proxy=proxy)
            item = {
                "id": row_id,
                "verdict": res["verdict"],
                "skipped": False,
                "detail": res["detail"],
                "raw": res["raw"],
                "status": res["status"],
                "endpoint": res["endpoint"],
                "latency_ms": res["latency_ms"],
                "proxy": proxy,
                "checked_at": int(time.time()),
            }
            results.append(item)
            if on_result:
                on_result(row_id, acc, item)

        return results


def next_track(prev: str, verdict: str) -> str:
    """复检结论 → 名单状态。

    `revived` 不直接清空而是留一个 revived 标记：界面要能显示「刚解封」，
    下一轮导入或手动校验时才真正移出名单。直接清空的话，用户点完复检
    一刷新就看不到这个号了，会以为是丢了。
    """
    if verdict == probe.RECHECK_REVIVED:
        return TRACK_REVIVED
    if verdict == probe.RECHECK_BANNED:
        return TRACK_BANNED
    if verdict == probe.RECHECK_AUTH:
        # 凭据失效不是封号，但也不该继续敲门——等用户重新导入
        return TRACK_AUTH
    if verdict == probe.RECHECK_SKIPPED:
        return prev or TRACK_PENDING
    return TRACK_BANNED if prev == TRACK_BANNED else (prev or TRACK_PENDING)


def summarize(results: Iterable[dict]) -> dict:
    counts = {
        probe.RECHECK_REVIVED: 0,
        probe.RECHECK_BANNED: 0,
        probe.RECHECK_AUTH: 0,
        probe.RECHECK_UNKNOWN: 0,
        probe.RECHECK_SKIPPED: 0,
        "skipped": 0,
    }
    for row in results:
        if row.get("skipped"):
            counts["skipped"] += 1
            continue
        verdict = str(row.get("verdict") or probe.RECHECK_UNKNOWN)
        counts[verdict] = counts.get(verdict, 0) + 1
    return {**counts, "total": len(list(results)) if not isinstance(results, list) else len(results)}
