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

## 出口 IP：三种模式

同一个出口 IP 是当初触发风控的原因之一（README 5.1：同出口 IP 紧接连开，
靠后的全挂），复检时继续用同一个 IP 敲门，等于告诉上游「还是我」。但反过来
也不是越散越好——排查看的是「封的是号还是 IP」，那就得控住变量。所以出口
策略做成三档，由 `exit_mode` 选：

| 值 | 行为 | 什么时候用 |
|----|------|-----------|
| `random` | 每个号从可用 mihomo 节点里随机挑一个 | 默认。尽量不撞 IP |
| `same` | 所有号固定同一个出口，取 `exit_pin` | 验证「这个 IP 干净」，只测号本身 |
| `custom` | 走 `exit_custom` 里手填的地址，按号顺序轮换 | 手上有一套别家的代理，或要指定地区 |

`same` 且 `exit_pin` 为空时不下发代理，交给账号自带的 `proxy`，没有就直连。
`custom` 的地址只认 http/https：urllib 的 ProxyHandler 不支持 socks5，收
socks 进来只会在运行时抛错，所以在解析阶段就剔掉。
"""

from __future__ import annotations

import errno
import json
import os
import random
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

#: 出口策略。顺序就是界面上的排列顺序。
EXIT_SAME = "same"
EXIT_RANDOM = "random"
EXIT_CUSTOM = "custom"
EXIT_MODES = (EXIT_SAME, EXIT_RANDOM, EXIT_CUSTOM)

EXIT_LABELS = {
    EXIT_SAME: "同一出口",
    EXIT_RANDOM: "随机出口",
    EXIT_CUSTOM: "自定义出口",
}

DEFAULTS: dict[str, Any] = {
    #: 总开关。关掉后复检接口直接返回「未开启」，不做任何网络动作。
    "enabled": False,
    #: 同一个号两次复检的最小间隔（秒）。默认 6 小时。
    "min_interval": 6 * 3600,
    #: 号与号之间的固定间隔（秒）。不许配 0——那是并发敲门的另一种写法。
    "gap_seconds": 3.0,
    #: 单次对话请求超时。上游冷启动偶尔要 20 秒以上。
    "timeout": 25.0,
    #: 出口策略：random / same / custom。见模块顶部那张表。
    "exit_mode": "random",
    #: same 模式固定走这个出口。空 = 不下发代理（账号自带优先，其次直连）。
    "exit_pin": "",
    #: custom 模式的地址表，一行一个，支持 http://host:port 或裸 host:port。
    "exit_custom": "",
    #: 旧字段，只为读老配置文件保留。真正的行为由 exit_mode 决定，
    #: 保存时会跟着 exit_mode 一起写回，方便还在看这个字段的外部脚本。
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
        #: mihomo 的 external-controller。留空按 host 推 `http://<host>:9090`。
        #: 只有它能给出「这个端口走的是哪个节点」——出口 IP 分不出线路
        #: （同一订阅里两个节点出口 IP 常常一模一样）。
        "api": "",
        #: external-controller 的 secret，没设就留空。
        "secret": "",
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
    mode = str(cfg.get("exit_mode") or "").strip().lower()
    if mode not in EXIT_MODES:
        # 老配置文件只有 rotate_proxy：按它的值推一档，别让升级把设置清成默认
        mode = EXIT_RANDOM if cfg.get("rotate_proxy", True) else EXIT_SAME
    cfg["exit_mode"] = mode
    # rotate_proxy 是派生字段，写回时跟 exit_mode 保持一致
    cfg["rotate_proxy"] = mode == EXIT_RANDOM
    cfg["exit_pin"] = normalize_url(cfg.get("exit_pin"))
    custom = cfg.get("exit_custom")
    if isinstance(custom, (list, tuple)):
        # 允许前端直接传数组，存成一行一个
        custom = "\n".join(str(x) for x in custom)
    cfg["exit_custom"] = str(custom or "").strip()
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
        "api": normalize_url(str(mihomo.get("api") or "").strip()),
        "secret": str(mihomo.get("secret") or "").strip(),
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


def api_base(mihomo: dict) -> str:
    """mihomo 的 external-controller 地址。

    没配就按代理主机推 `http://<host>:9090`——hub 那套部署里 mihomo 跑在
    同一个 docker 网络，`cli-proxy-mihomo:9090` 是默认解。推不出来就返回空，
    调用方据此跳过，不硬编一个大概率连不上的地址去等超时。
    """
    api = normalize_url(str(mihomo.get("api") or "").strip())
    if api:
        return api.rstrip("/")
    host = str(mihomo.get("host") or "").strip()
    if not host:
        return ""
    if "://" in host:
        return normalize_url(host.rstrip("/"))
    if ":" in host:                      # host:port 形式，端口已经给全
        return normalize_url("http://" + host)
    return normalize_url(f"http://{host}:9090")


def _api_get(base: str, path: str, headers: dict,
             timeout: float) -> tuple[Any, int, str]:
    """GET 一个控制器端点，返回 (JSON, HTTP 状态码, 人话错误)。

    状态码 0 表示没走到 HTTP 层（连不上 / 超时），此时 error 里已经是可以
    照着改的说法。分开给状态码是因为调用方要靠它判断「这个端点不存在，
    换一条路」和「地址根本不通，别浪费时间试别的」。
    """
    # 强制直连：环境里 HTTPS_PROXY 指向本地代理时，urllib 会把 127.0.0.1
    # 也塞给代理，问一次要绕一圈还可能直接失败。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return None, int(exc.code), _explain_status(base, path, int(exc.code))
    except urllib.error.URLError as exc:
        return None, 0, f"{base}{path} {_why_unreachable(exc.reason, timeout)}"
    except TimeoutError:
        return None, 0, f"{base}{path} 超时 {timeout:g}s，地址不通"
    except OSError as exc:
        return None, 0, f"{base}{path} {_why_unreachable(exc, timeout)}"
    try:
        return json.loads(raw), code, ""
    except ValueError:
        return None, code, f"{base}{path} 返回的不是 JSON"


def _explain_status(base: str, path: str, code: int) -> str:
    if code in (401, 403):
        return f"{base} 返回 {code}，需要填 secret（或 secret 不对）"
    if code == 404:
        return f"{base}{path} 返回 404"
    return f"{base}{path} 返回 HTTP {code}"


def _listener_map(items: Any) -> dict[int, dict]:
    """把 `[{name, port, proxy}]` 折成 `{端口: {name, proxy}}`。"""
    out: dict[int, dict] = {}
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not (0 < port < 65536):
            continue
        out[port] = {
            "name": str(item.get("name") or "").strip(),
            "proxy": str(item.get("proxy") or "").strip(),
        }
    return out


def _fetch_listeners(cfg: dict, ports: Iterable[int] | None = None,
                     timeout: float = 2.5,
                     deep: bool = True) -> tuple[dict[int, dict], str]:
    """问 mihomo 「这些端口各走哪个节点」，返回 ({端口: {name, proxy}}, 错误说明)。

    为什么要这一步：出口 IP 分不出线路。同一条订阅里两个节点落在同一个
    AS 的相邻段，甚至直接共用出口，卡片上两格都是 `103.41.4.26`，看起来
    像同一个槽位检测了两遍。节点名才是用户在找的东西。

    **两条路**（顺序不能反）：

    1. `GET /listeners` —— 直接给出端口、监听名、钉死的出站节点。可惜
       官方 mihomo 的 API 里**没有这个端点**（`hub/route/server.go` 的
       路由表里没有 listener 相关项），只有部分面板 / fork 加了。所以它
       命中是意外，404 才是常态。
    2. `GET /connections` 反查 —— 每条连接都带 `inboundPort`（从哪个监听
       进来）和 `chains`（出站链路，`chains[0]` 是真正用的节点）。对着每个
       端口开一条 CONNECT 隧道并**先别关**，再读一次 `/connections`，就能
       把端口和节点名对上。贵一点（N 条长连接），所以只在 `deep=True`
       （用户点了「自动检测」）时才走。

    拿不到时**必须把原因带出去**：界面上只写「控制器未通」，用户不知道该
    改地址、补 secret，还是去给 mihomo 开 external-controller——这几种情况
    的处置完全不同。
    """
    base = api_base(cfg["mihomo"])
    if not base:
        return {}, "没填控制器地址，也没能从代理主机推出来（主机为空）"
    headers = {"Accept": "application/json"}
    secret = str(cfg["mihomo"].get("secret") or "").strip()
    if secret:
        headers["Authorization"] = "Bearer " + secret

    data, code, err = _api_get(base, "/listeners", headers, timeout)
    if code == 200:
        out = _listener_map(data.get("listeners") if isinstance(data, dict) else None)
        if out:
            return out, ""
        # 端点存在但一个 listener 都没有：端口可能是用 mixed-port / port
        # 之类开的，那就没有「每条线一个端口」这回事，也没有名字可给。
        return {}, (f"{base}/listeners 是空的：端口不是用 listeners 段开的，"
                    "一个端口对应一个节点这件事在这里不成立")
    if code in (401, 403):
        # 鉴权失败时别的端点也一样过不去，不用再试
        return {}, err
    if code == 0:
        # 连不上 / 超时：地址本身有问题，反查也没有意义
        return {}, err

    if not deep:
        return {}, f"{base} 没有 /listeners（官方 mihomo 不带这个端点），点「自动检测」再按连接反查"

    got, why = _map_by_connections(cfg, list(ports or []), base, headers, timeout)
    if got:
        return got, ""
    return {}, f"{base} 没有 /listeners（官方 mihomo 不带这个端点）；{why}"


def _probe_host(probe_url: str) -> str:
    """从探测地址取个能 CONNECT 出去的主机名。取不到就用 api.ipify.org。"""
    text = str(probe_url or "").strip()
    if not text:
        return "api.ipify.org"
    if "://" not in text:
        text = "http://" + text
    host = urllib.parse.urlsplit(text).hostname or ""
    return host or "api.ipify.org"


def _host_only(host: str) -> str:
    """把 `http://a:1` / `a:1234` 这类写法剥成能 connect 的主机名。"""
    text = str(host or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlsplit(text).hostname or ""
    elif text.count(":") == 1 and not text.startswith("["):
        text = text.split(":", 1)[0]
    return text


def _http_tunnel(host: str, port: int, target: str,
                 timeout: float) -> socket.socket | None:
    """开一条 HTTP CONNECT 隧道，成功返回仍然连着的 socket。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        sock.sendall((
            f"CONNECT {target}:443 HTTP/1.1\r\n"
            f"Host: {target}:443\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = sock.recv(512)
            if not chunk:
                break
            head += chunk
        if b"200" not in head.split(b"\r\n", 1)[0]:
            sock.close()
            return None
        return sock
    except OSError:
        sock.close()
        return None


def _socks_tunnel(host: str, port: int, target: str,
                  timeout: float) -> socket.socket | None:
    """开一条 SOCKS5 隧道。listener 的 type 是 socks 时只有这条能通。

    先试 HTTP CONNECT 再试这个：mixed listener 两种都收，但把 SOCKS5 的
    二进制问候发给一个纯 HTTP 端口，对端会直接把连接关掉，所以顺序不能反。
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")          # VER=5, NMETHODS=1, NO AUTH
        if sock.recv(2) != b"\x05\x00":
            sock.close()
            return None
        name = target.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name
                     + (443).to_bytes(2, "big"))
        reply = sock.recv(4)
        if len(reply) < 4 or reply[0] != 5 or reply[1] != 0:
            sock.close()
            return None
        return sock
    except OSError:
        sock.close()
        return None


def _open_tunnels(host: str, ports: list[int], target: str,
                  timeout: float) -> dict[int, socket.socket]:
    """对着每个端口开一条隧道，返回 {端口: 仍然开着的 socket}。

    隧道留着不关是全部意义所在：连接一断，mihomo 的 `/connections` 里就
    没这条记录了，也就读不到它走的哪个节点。调用方读完必须自己关。

    HTTP CONNECT 和 SOCKS5 都试一遍：listener 的 type 可能是 mixed 也可能
    是 socks，从外面看不出来。并发开是因为 32 个端口串行等超时能拖到一分多钟。
    """
    held: dict[int, socket.socket] = {}
    lock = threading.Lock()

    def one(port: int) -> None:
        sock = _http_tunnel(host, port, target, timeout)
        if sock is None:
            sock = _socks_tunnel(host, port, target, timeout)
        if sock is None:
            return
        with lock:
            held[port] = sock            # 所有权交出去，调用方负责关

    threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    # 一条隧道最多等两轮超时（HTTP 失败后再试 SOCKS），别让最慢那条拖住整体
    for t in threads:
        t.join(timeout * 2 + 1.0)
    return held


def _map_by_connections(cfg: dict, ports: list[int], base: str, headers: dict,
                        timeout: float) -> tuple[dict[int, dict], str]:
    """从 `/connections` 反查「这个端口走的是哪个节点」。

    mihomo 每条连接带 `metadata.inboundPort` 和 `chains`，前者是收到的监听
    端口，后者是出站链路（`[0]` 是真正在用的节点）。所以先让每个端口各挂
    一条连接，再读一次连接表，端口和节点名就对上了。

    这种反查只对「一个端口钉死一个节点」的 listener 有意义——普通 mixed-port
    上所有连接共享一个端口，反查出来全是同一个名字。
    """
    mihomo = cfg["mihomo"]
    host = _host_only(str(mihomo.get("host") or ""))
    if not host:
        return {}, "代理主机为空，没法开隧道反查"
    if not ports:
        return {}, "没有端口可反查"

    target = _probe_host(str(mihomo.get("probe_url") or ""))
    held = _open_tunnels(host, ports, target, min(timeout, 3.0))
    if not held:
        return {}, (f"从 {host} 的端口一条隧道都没开起来"
                    "（HTTP CONNECT 和 SOCKS5 都试过了：节点全挂，或这几个端口不是代理口）")
    try:
        data, code, err = _api_get(base, "/connections", headers, timeout)
        if code != 200 or not isinstance(data, dict):
            return {}, f"{base}/connections 读不到：{err or code}"
        out: dict[int, dict] = {}
        for item in (data.get("connections") or []):
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            try:
                in_port = int(meta.get("inboundPort") or 0)
            except (TypeError, ValueError):
                continue
            chains = [str(x) for x in (item.get("chains") or []) if x]
            if in_port in held and chains and in_port not in out:
                out[in_port] = {
                    "name": str(meta.get("inboundName") or "").strip(),
                    "proxy": chains[0],
                }
        if out:
            return out, ""
        return {}, (f"{base}/connections 里有 {len(data.get('connections') or [])} 条连接，"
                    "但没有带 inboundPort 的（这个版本太老）")
    finally:
        for sock in held.values():
            try:
                sock.close()
            except OSError:
                pass


def _why_unreachable(reason: Any, timeout: float) -> str:
    """把 socket 层的异常翻成一句能照着改的话。

    `Connection refused` 和「域名解析不到」在用户眼里都是「连不上」，但
    一个要改端口、一个要改主机名（或者把容器端口映射出来）。

    不能只看英文文本：Windows 上 `str(ConnectionRefusedError(10061, ...))`
    是「由于目标计算机积极拒绝，无法连接。」，一个 refused 都没有。所以
    errno 先判，文本只做兜底。
    """
    code = getattr(reason, "errno", None)
    if code in (errno.ECONNREFUSED, 10061):
        return "连接被拒绝：控制器没开，或端口不对"
    text = str(reason or "").lower()
    if "refused" in text or "积极拒绝" in text or "拒绝" in text:
        return "连接被拒绝：控制器没开，或端口不对"
    if "name or service not known" in text or "nodename nor servname" in text \
            or "getaddrinfo" in text or "temporary failure in name resolution" in text \
            or "解析" in text or "11001" in text:
        return "主机名解析不到：跨容器要写服务名，宿主机上要写 127.0.0.1"
    if "timed out" in text or "timeout" in text or "超时" in text:
        # Windows 上连本地没开的端口也是报 timeout 而不是 refused（
        # 系统层不立刻回 RST），所以两种可能都摆出来，不断言是哪一种。
        return f"超时 {timeout:g}s：端口没开，或被防火墙吞了"
    if "network is unreachable" in text or "no route to host" in text \
            or "不可达" in text:
        return "网络不可达：检查控制器是否 bind 在 0.0.0.0"
    return f"连不上（{reason or '原因未知'}）"


def fetch_listeners(cfg: dict, timeout: float = 2.5) -> dict[int, dict]:
    """只需要名单时的薄封装。错误原因走 `_fetch_listeners`。"""
    return _fetch_listeners(cfg, timeout)[0]


def port_of(url: str) -> int:
    try:
        return int(urllib.parse.urlsplit(url).port or 0)
    except (TypeError, ValueError):
        return 0


def normalize_url(value: Any) -> str:
    """把用户填的一个地址收拾成 `http://host:port`。

    容忍三种常见写法：`http://1.2.3.4:8080`、`1.2.3.4:8080`、`1.2.3.4:8080/`。
    只放行 http/https——urllib 的 ProxyHandler 收到 socks5 会直接抛
    `unknown url type`，与其运行时炸不如在这里返回空串。
    其他协议、空值、含空白的串一律返回空串，让调用方走「没配」的分支。
    """
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    if not parts.hostname:
        return ""
    try:
        port = parts.port
    except ValueError:      # 端口不是数字
        return ""
    if not port or not (0 < port < 65536):
        return ""
    return f"{parts.scheme}://{parts.hostname}:{port}"


def parse_urls(spec: Any) -> list[str]:
    """自定义出口地址表 → 去重保序的 URL 列表。

    分隔符吃逗号、分号、空格、换行——界面上是 textarea，用户会怎么排就怎么吃。
    """
    if isinstance(spec, (list, tuple)):
        chunks = [str(x) for x in spec]
    else:
        text = str(spec or "").replace(",", "\n").replace(";", "\n").replace(" ", "\n")
        chunks = text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        url = normalize_url(chunk)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:64]


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


def probe_urls(urls: Iterable[str], probe_url: str, timeout: float = 10.0) -> list[dict]:
    """并发探一批出口地址。并发 8：十个地址串行、每个超时 10 秒会像卡死。"""
    targets = [u for u in urls if u]
    if not targets:
        return []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, len(targets)),
                            thread_name_prefix="wk-exit") as pool:
        return list(pool.map(lambda u: probe_exit(u, probe_url, timeout), targets))


def discover_nodes(cfg: dict | None = None, probe_now: bool = True) -> dict:
    """扫一遍配置里的 mihomo 端口，返回可用节点。

    对齐 hub 的 `discover_proxy_slots()`：端口即槽位，能出网就算可用。
    `probe_now=False` 只列地址不测——页面上「自动检测」按钮才需要真测，
    打开页面时不该替用户发十个请求。

    每个节点带上 `name`（mihomo 里的出站节点名）。纯 IP 列表看不出是哪条线：
    同一订阅里两个节点出口 IP 一样是常事，卡片上两格数字一模一样，用户
    根本分不清自己配了两个槽还是一个。
    """
    cfg = cfg or load_config()
    mihomo = cfg["mihomo"]
    ports = parse_ports(mihomo["ports"])
    urls = [_proxy_url(mihomo["host"], port) for port in ports]
    # 问一次名单很轻（本地一个 HTTP），但 api 不通时会卡到超时。列表页
    # 给 1 秒，点「自动检测」给 2.5 秒——那时候用户已经在等结果了。
    # 名单和失败原因一起拿：拿不到名字时界面要能说清是哪一步断的
    # （地址错 / 要 secret / 端点不存在），只说「未通」等于没给信息。
    names, api_error = _fetch_listeners(
        cfg, ports=ports, timeout=2.5 if probe_now else 1.0, deep=probe_now)

    if not probe_now:
        nodes = [{"url": u, "ok": False, "exit_ip": "", "latency_ms": 0,
                  "error": ""} for u in urls]
        return {
            "host": mihomo["host"],
            "ports": ports,
            "probed": False,
            "nodes": _label_nodes(nodes, names),
            "ok_count": 0,
            "api": api_base(mihomo),
            "api_error": api_error,
            "named": bool(names),
        }

    if not urls:
        return {"host": mihomo["host"], "ports": [], "probed": True,
                "nodes": [], "ok_count": 0,
                "api": api_base(mihomo), "api_error": api_error,
                "named": bool(names)}

    nodes = _label_nodes(probe_urls(urls, mihomo["probe_url"], mihomo["timeout"]), names)

    return {
        "host": mihomo["host"],
        "ports": ports,
        "probed": True,
        "nodes": nodes,
        "ok_count": sum(1 for n in nodes if n.get("ok")),
        "api": api_base(mihomo),
        "api_error": api_error,
        "named": bool(names),
    }


#: 节点类型关键词，从节点名反推。顺序即优先级：名字里同时出现
#: 「住宅」和「代理」的（比如「港-住宅代理-01」）算住宅——那才是它的用处。
#: 只按名字猜，不查 IP 归属库：没有离线库就不编一个出来，猜错比不标更糟。
KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("住宅", ("住宅", "家宽", "家庭宽带", "家庭", "residential", "resi")),
    ("机房", ("机房", "数据中心", "datacenter", "data center", "data-center",
              "idc", "hosting", "服务器", "专线", "bgp")),
    ("代理", ("代理", "proxy", "socks", "隧道", "tunnel", "vpn")),
)


def node_kind(name: str) -> str:
    """从节点名推断这一条是住宅 / 机房 / 还是代理。推断不出返回空串。

    用户看节点列表时真正关心的是「这条线像不像真人家宽」——同一个订阅里
    机房 IP 和住宅 IP 混着，只能靠名字里那两个字分辨。名字没写就不猜。
    """
    text = str(name or "").strip().lower()
    if not text:
        return ""
    for label, words in KIND_HINTS:
        if any(word in text for word in words):
            return label
    return ""


def _label_nodes(nodes: list[dict], names: dict[int, dict]) -> list[dict]:
    """把「端口 → 节点名」贴到探测结果上。查不到就留空，前端退回显示端口。"""
    for node in nodes:
        port = port_of(str(node.get("url") or ""))
        meta = names.get(port) or {}
        node["port"] = port
        node["listener"] = meta.get("name", "")
        # proxy 是出站节点名（用户订阅里看到的那串），比 listener 名有用；
        # 有的版本只填其中一个，两个都留着让前端挑。
        node["name"] = meta.get("proxy") or meta.get("name", "")
        node["kind"] = node_kind(node["name"])
    return nodes


def plan_exit(cfg: dict, nodes: list[dict], index: int,
              rng: Any = None, ips: dict | None = None) -> dict:
    """决定第 `index` 个号从哪个出口出去。

    返回 {mode, mode_label, proxy, exit_ip, label}：
      proxy     交给 urllib 的代理串，空串表示不下发（账号自带优先，其次直连）
      exit_ip   已知的出口 IP，不知道就空串
      label     界面上显示的一行说明

    三档的分支顺序刻意这样排：custom 表为空时退化成 same，same 没 pin 时退化成
    「节点里取第一个」，都没节点就直连。任何时候都不返回半截状态。
    """
    mode = str(cfg.get("exit_mode") or EXIT_RANDOM)
    if mode not in EXIT_MODES:
        mode = EXIT_RANDOM
    lookup = dict(ips or {})
    for node in nodes or []:
        if node.get("ok") and node.get("exit_ip"):
            lookup.setdefault(node["url"], node["exit_ip"])
    usable = [n["url"] for n in (nodes or []) if n.get("ok")]

    if mode == EXIT_CUSTOM:
        urls = parse_urls(cfg.get("exit_custom"))
        if urls:
            url = urls[index % len(urls)]
            return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": url,
                    "exit_ip": lookup.get(url, ""), "label": url}
        mode = EXIT_SAME    # 表是空的，按同一出口处理

    if mode == EXIT_SAME:
        pin = normalize_url(cfg.get("exit_pin"))
        if pin:
            return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": pin,
                    "exit_ip": lookup.get(pin, ""), "label": pin}
        if usable:
            url = usable[0]
            return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": url,
                    "exit_ip": lookup.get(url, ""), "label": f"{url}（节点第一项）"}
        return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": "",
                "exit_ip": "", "label": "直连 / 账号自带"}

    if not usable:
        return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": "",
                "exit_ip": "", "label": "直连（没有可用节点）"}
    picker = rng or random
    url = picker.choice(usable)
    return {"mode": mode, "mode_label": EXIT_LABELS[mode], "proxy": url,
            "exit_ip": lookup.get(url, ""), "label": url}


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

    def __init__(self, cfg: dict | None = None, rng: Any = None):
        self.cfg = cfg or load_config()
        self.nodes: list[dict] = []
        #: url → 出口 IP。mihomo 节点由 discover_nodes 带回，自定义地址现探。
        self._ips: dict[str, str] = {}
        self._rng = rng

    def _nodes(self) -> list[dict]:
        if not self.cfg["mihomo"]["enabled"]:
            return []
        if not self.nodes:
            self.nodes = discover_nodes(self.cfg, probe_now=True)["nodes"]
            for node in self.nodes:
                if node.get("ok") and node.get("exit_ip"):
                    self._ips[node["url"]] = node["exit_ip"]
        return self.nodes

    def _warm_exits(self) -> None:
        """给「还不知道出口 IP」的地址补探一次。

        为什么要探：复检结果里要写「这个号从哪个 IP 出去的」。用户切到同一
        出口，多半就是在验「IP 干净但号被封」；切到自定义出口，是在对照几个
        出口谁通。不给 IP，这两条结论都没法自己核对。

        random 模式的 IP 由 discover_nodes 带回，这里不重复探。
        """
        mode = self.cfg.get("exit_mode")
        if mode == EXIT_CUSTOM:
            candidates = parse_urls(self.cfg.get("exit_custom"))
        elif mode == EXIT_SAME:
            pin = normalize_url(self.cfg.get("exit_pin"))
            candidates = [pin] if pin else []
        else:
            candidates = []
        urls = [u for u in candidates if u not in self._ips]
        if not urls:
            return
        for node in probe_urls(urls, self.cfg["mihomo"]["probe_url"],
                               self.cfg["mihomo"]["timeout"]):
            # 探不通也记一笔空串，避免同一批地址在下一轮重复探测
            self._ips[node["url"]] = node.get("exit_ip") or ""

    def _plan(self, index: int) -> dict:
        mode = self.cfg.get("exit_mode")
        # 出口已经钉死（custom 有表 / same 有 pin）时不去扫 mihomo 端口：
        # 扫一遍是十来个网络请求，白花几十秒，结果还用不上。
        pinned = bool(normalize_url(self.cfg.get("exit_pin")))
        if mode == EXIT_CUSTOM or (mode == EXIT_SAME and pinned):
            nodes: list[dict] = []
        else:
            nodes = self._nodes()
        return plan_exit(self.cfg, nodes, index, rng=self._rng, ips=self._ips)

    def run(self, pairs: Iterable[tuple[int, Account]], force: bool = False,
            on_result: Callable[[int, Account, dict], None] | None = None) -> list[dict]:
        """跑一轮。pairs 是 [(行 id, Account)]。

        返回值每项：{id, verdict, detail, raw, status, endpoint, latency_ms,
                     proxy, proxy_ip, exit_mode, checked_at, skipped}

        `force=True` 跳过最小间隔检查（界面上「立即复检」按钮）。
        """
        items = list(pairs)[: self.cfg["batch_limit"]]
        self._warm_exits()
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
                        "proxy": "", "proxy_ip": "", "exit_mode": self.cfg["exit_mode"],
                        "checked_at": now,
                    })
                    if on_result:
                        on_result(row_id, acc, results[-1])
                    continue

            # ---- 号与号之间停一下 ----
            if index:
                time.sleep(self.cfg["gap_seconds"])

            plan = self._plan(index)
            res = probe.chat_probe(acc, timeout=self.cfg["timeout"], proxy=plan["proxy"])
            item = {
                "id": row_id,
                "verdict": res["verdict"],
                "skipped": False,
                "detail": res["detail"],
                "raw": res["raw"],
                "status": res["status"],
                "endpoint": res["endpoint"],
                "latency_ms": res["latency_ms"],
                "proxy": plan["proxy"],
                "proxy_ip": plan["exit_ip"],
                "exit_mode": self.cfg["exit_mode"],
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
