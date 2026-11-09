"""国际版每日活跃：网页端会话（真正计入官方活跃）+ 桌面对话（兜底），结果落 SQLite。

## 为什么不是「一个 POST 就完事」

官方口径要的是一次**跑完的网页端 agent 会话**，不是一条 chat completion。
`POST /console/as/conversations/` 只是排队，agent 要等客户端接上沙箱、把这一轮
请求进去才会跑；只建会话不接沙箱，会话永远停在 CREATING，不算一次有效对话。

所以网页腿的步骤是固定的：

    建会话 → GET .../{id}/session 拿沙箱 link/token
          → SSE 连上沙箱拿 Acp-Connection-Id
          → initialize / session/load / session/set_model / session/prompt
          → 轮询 GET .../{id} 直到 status=completed
          → POST .../{id}/stop（失败轮次再 delete）

外加一条桌面腿（`POST /v2/chat/completions`，一条 "Hi"）：它是老做法，网页腿
不通时至少证明对话链路是活的；两条腿的结果分开记，不合并成「成功」。

## 出口

逐账号走自己的 `exit_port`（mihomo listener），与账户库、导出写 proxy 用的是
同一个坐标。`config/activity.json` 的 `proxy` 非空时压过它（调试用），
`fallback_proxy` 只在没配出口时兜底。直连永远排在最后——同一台机器上跑几十个
号，直连等于把它们的请求特征绑在一个 IP 上。

## 落库

一次尝试一行，写 `activity` 表：时间 / 行 id / uid / 名称 / 结果 / 阶段 / 状态码 /
模型 / 出口 / 耗时 / 会话 id / 说明 / 触发来源。不写 token、不写 prompt 内容之外的
东西；`detail` 是给人看的一句话，超长截断。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from . import clock, probe, store
from .model import Account

# ------------------------------------------------------------------ 端点与常量

WEB_ORIGIN = "https://www.workbuddy.ai"
CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"

#: 网页站的 UA。与桌面端 CLI 的 UA 不是一回事：网页接口认浏览器指纹，
#: 拿 CLI 的 UA 去建会话会被当成异常客户端。
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: 单账号同时存在的活跃会话上限。上游对 status=working 的会话有硬上限，
#: 攒满了建会话直接 429 / 10105 —— 每轮不 stop 的实现在第十天就会哑掉。
ACTIVE_LIMIT = 10
PRUNE_KEEP = 8
ACTIVE_STATUSES = ("working", "creating", "pending")

PROTOCOL_VERSION = 1
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False},
                       "terminal": False}

CONFIG_PATH = os.path.join(store.ROOT, "config", "activity.json")

#: 配置默认值。`enabled` 默认开：这是本地服务，没有它就不会自己跑；
#: 关掉只是「不自动跑」，手动按钮照常可用。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "web": True,            # 网页腿（计入官方活跃）
    "desktop": True,        # 桌面腿（先发一条最小的）
    "prompt": "Hi",
    "chat_model": probe.CHAT_MODEL,
    "models": "",           # 候选模型，逗号分隔；空 = 只用 chat_model + 内置兜底
    "proxy": "",            # 固定代理，压过 exit_port
    "use_exit": True,       # 按账户自己的 exit_port 出站
    "fallback_proxy": "",   # 没配出口时的兜底；空 = 直连
    "timeout": 120,         # 网页腿单轮等待上限（秒）
    "rounds": 2,            # 网页腿最多试几轮
    "concurrency": 2,       # 同时跑几个号
    "daily_at": "08:30",    # 自动运行的起始时刻（北京时间）
    "interval_minutes": 30,  # 过了起始时刻后，每隔多久补一次没成功的号
    "skip_done": True,      # 当日已成功的直接跳过
}

#: 写入配置时只认这些键，避免前端传进来一堆没处放的字段被静默吞掉。
_BOOL_KEYS = ("enabled", "web", "desktop", "use_exit", "skip_done")
_INT_KEYS = {"timeout": (20, 1800), "rounds": (1, 6), "concurrency": (1, 8),
             "interval_minutes": (5, 1440)}
_TEXT_KEYS = ("prompt", "chat_model", "models", "proxy", "fallback_proxy",
              "daily_at")


# ------------------------------------------------------------------ 配置读写

def load_config() -> dict[str, Any]:
    """读配置，缺的键按默认值补齐。坏文件当作空配置，不拦住运行。"""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key in DEFAULTS:
        if key in raw:
            cfg[key] = raw[key]
    return _clean(cfg)


def _clean(cfg: dict[str, Any]) -> dict[str, Any]:
    """按类型收口。前端与手改的文件都从这里过一遍，下游不用各判各的。"""
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key not in cfg:
            continue
        val = cfg[key]
        if key in _BOOL_KEYS:
            out[key] = bool(val)
        elif key in _INT_KEYS:
            low, high = _INT_KEYS[key]
            try:
                out[key] = max(low, min(high, int(val)))
            except (TypeError, ValueError):
                out[key] = DEFAULTS[key]
        else:
            out[key] = str(val if val is not None else "").strip()
    return out


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """合并保存。只认已知键；写临时文件再替换，避免半截 JSON。"""
    cfg = load_config()
    for key, val in (patch or {}).items():
        if key in DEFAULTS:
            cfg[key] = val
    cfg = _clean(cfg)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return cfg


# ------------------------------------------------------------------ 出口选择

def is_risk(row: dict[str, Any]) -> bool:
    """这一行是不是风控账号。

    判据只用 `recheck_state == 'banned'`（封号中）：导入时标过风控的号也写这个值
    （`store.upsert` 里 `mark = "banned" if row_risk else ""`），所以一个字段覆盖
    两种来源。`risk_src` 只是来源标记，解封后仍留着历史，不能拿它当判据 ——
    拿它判会把已解封的号永久排除在活跃之外。
    """
    return str(row.get("recheck_state") or "") == "banned"


def proxy_for(acc: Account, cfg: dict[str, Any], exit_port: int = 0) -> str:
    """这一号从哪出去。

    顺序：固定代理 → 账号自己的 exit_port → 兜底代理 → 直连。
    端口转 URL 用 mihomo.host（和复检、导出同一份配置），不在这里再猜地址。
    """
    fixed = str(cfg.get("proxy") or "").strip()
    if fixed:
        return fixed
    port = int(exit_port or 0)
    if cfg.get("use_exit", True) and port:
        rc = {}
        try:
            from . import recheck
            rc = recheck.load_config()
        except Exception:  # noqa: BLE001 — 读不到就用空 host，落到兜底
            rc = {}
        host = str((rc.get("mihomo") or {}).get("host") or "").strip()
        if host:
            return f"http://{host}:{port}"
    return str(cfg.get("fallback_proxy") or "").strip()


# ------------------------------------------------------------------ 桌面腿

def _desktop_body(model: str, prompt: str, variant: str) -> dict[str, Any]:
    """桌面对话请求体。

    hub 的原样是「只有一条 user Hi」；但 2026-11 的上游加了渠道校验，首条不是
    system 会 400 / 11128，与账号状态无关。所以先发 hub 形态，400 再补 system。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 10,
    }
    if variant == "official":
        body["messages"] = [
            {"role": "system", "content": "You are WorkBuddy, a helpful AI assistant."},
            {"role": "user", "content": prompt},
        ]
    else:
        body["reasoning_effort"] = "none"
    return body


def desktop_daily(acc: Account, cfg: dict[str, Any], proxy: str,
                  model: str) -> dict[str, Any]:
    """发一条最小对话，返回 {ok, status, model, variant, elapsed_ms, detail}。

    只用于「对话链路还活着」这个事实；真正的活跃由网页腿计入。
    """
    started = time.time()
    prompt = str(cfg.get("prompt") or "Hi")
    headers = probe._chat_headers(acc)          # noqa: SLF001 — 同包内部复用
    headers["Accept"] = "application/json"
    attempts: list[dict[str, Any]] = []
    last: dict[str, Any] = {"ok": False, "status": 0, "detail": "没有可用地址"}

    for base in probe._chat_bases(acc):         # noqa: SLF001
        for path in probe.CHAT_PATHS:
            url = base + path
            for variant in ("hub", "official"):
                body = _desktop_body(model, prompt, variant)
                try:
                    status, payload = probe._post_json(  # noqa: SLF001
                        url, body, headers, float(cfg.get("timeout") or 60), proxy)
                except (urllib.error.URLError, OSError) as exc:
                    last = {"ok": False, "status": 0,
                            "detail": f"{type(exc).__name__}: {exc}"}
                    attempts.append({"url": url, "variant": variant, "status": 0,
                                     "detail": last["detail"][:160]})
                    break                            # 这个 base 不通，换下一个
                verdict = probe._read_chat_reply(status, payload, url, started)  # noqa: SLF001
                attempts.append({"url": url, "variant": variant, "status": status,
                                 "verdict": verdict.get("verdict"),
                                 "detail": str(verdict.get("detail") or "")[:160]})
                if status == 404:
                    break                            # 路径不存在
                if status == 400 and variant == "hub":
                    continue                         # 换 official 形态再试一次
                last = {
                    "ok": verdict.get("verdict") == probe.RECHECK_REVIVED,
                    "status": status,
                    "model": model,
                    "variant": variant,
                    "detail": str(verdict.get("detail") or ""),
                }
                last["attempts"] = attempts
                last["elapsed_ms"] = int((time.time() - started) * 1000)
                return last
            if last.get("status") not in (404,):
                # 400 已经试过两种形态，换路径没有意义；网络错误则换 base
                if last.get("status") == 400:
                    break
    last["attempts"] = attempts
    last["elapsed_ms"] = int((time.time() - started) * 1000)
    return last


# ------------------------------------------------------------------ 网页腿：HTTP

def _web_headers(acc: Account) -> dict[str, str]:
    """网页站的请求头。只有 bearer 与 X-User-Id，不带桌面端那套网关头。"""
    head = {
        "Authorization": "Bearer " + str(acc.access_token or ""),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/app",
        "User-Agent": WEB_UA,
    }
    uid = str(acc.uid or "").strip()
    if uid:
        head["X-User-Id"] = uid
    return head


def _web_call(acc: Account, path: str, proxy: str, timeout: float = 30.0,
              method: str = "GET", body: Any = None) -> dict[str, Any]:
    """调网页站的一次接口，返回解析后的 JSON（非 dict 一律包成 {}）。"""
    url = CONVERSATIONS_URL + path.lstrip("/")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_web_headers(acc),
                                 method=method)
    opener = probe._opener(proxy)          # noqa: SLF001 — 同包内部复用
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {"_raw": raw[:400]}
    return payload if isinstance(payload, dict) else {}


def conversation_create(acc: Account, proxy: str, prompt: str, model: str,
                        timeout: float = 30.0) -> tuple[str, dict[str, Any] | None]:
    """建一条网页会话，返回 (会话 id, 失败信息)。失败信息带 retryable / limit。"""
    body = {
        "prompt": prompt,
        "model": model,
        "conversationOrigin": "workbuddy-app",
        # 网页端建会话固定带这项（抓包所得），少它上游按旧版客户端处理。
        "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
    }
    try:
        payload = _web_call(acc, "", proxy, timeout, "POST", body)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return "", {"message": _clean_text(f"HTTP {exc.code}: {detail}", exc.code, 140),
                    "retryable": exc.code >= 500 or exc.code in (408, 429),
                    "limit": exc.code == 429 or "10105" in detail}
    except Exception as exc:  # noqa: BLE001
        return "", {"message": f"{type(exc).__name__}: {exc}", "retryable": True,
                    "limit": False}
    data = payload.get("data") or {}
    cid = str(data.get("id") or "")
    if payload.get("code") not in (0, None) or not cid:
        return "", {"message": f"code={payload.get('code')} msg={payload.get('msg')}",
                    "retryable": False, "limit": False}
    return cid, None


def conversation_get(acc: Account, cid: str, suffix: str, proxy: str,
                     timeout: float = 30.0) -> dict[str, Any]:
    return _web_call(acc, urllib.parse.quote(cid) + suffix, proxy, timeout)


def conversation_status(acc: Account, cid: str, proxy: str) -> str:
    try:
        payload = conversation_get(acc, cid, "", proxy)
    except Exception:  # noqa: BLE001 — 查状态失败不该打断这一轮
        return ""
    return str(((payload.get("data") or {}).get("status")) or "")


def conversation_release(acc: Account, cid: str, proxy: str,
                         delete: bool = False) -> bool:
    """结束一条会话，释放活跃名额。收尾失败不改判已跑完的结果。"""
    if not cid:
        return False
    stopped = False
    for action in (("stop", "delete") if delete else ("stop",)):
        try:
            payload = _web_call(acc, urllib.parse.quote(cid) + "/" + action,
                                proxy, 20.0, "POST", {})
            stopped = stopped or payload.get("code") in (0, None)
        except Exception:  # noqa: BLE001
            continue
    return stopped


def conversation_prune(acc: Account, proxy: str, keep: int = PRUNE_KEEP) -> int:
    """停掉最旧的一批活跃会话，腾出名额。返回停掉几条。"""
    try:
        payload = _web_call(acc, "?page=1&pageSize=50", proxy, 20.0)
    except Exception:  # noqa: BLE001
        return 0
    rows = ((payload.get("data") or {}).get("conversations")) or []
    if not isinstance(rows, list):
        return 0
    active = [r for r in rows
              if str((r or {}).get("status") or "").lower() in ACTIVE_STATUSES]
    freed = 0
    for row in active[keep:]:
        if conversation_release(acc, str(row.get("id") or ""), proxy, delete=True):
            freed += 1
    return freed


# ------------------------------------------------------------------ 网页腿：ACP

def _proxy_parts(proxy_url: str) -> tuple[str, int | None, dict[str, str]]:
    """代理 URL → (host, port, CONNECT 头)。

    裸 http.client + set_tunnel 不会自动加 Proxy-Authorization；认证代理缺这个
    头一律回 407，表现就是「桌面腿正常、网页腿 407」。
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        return "", None, {}
    parsed = urllib.parse.urlparse(raw)
    host, port = parsed.hostname, parsed.port
    user, password = parsed.username, parsed.password
    if not host:
        authority = raw.split("://")[-1].split("/")[0]
        userinfo, _, hostport = authority.rpartition("@")
        host, _, port_text = hostport.rpartition(":")
        if not host:
            host, port_text = hostport, ""
        try:
            port = int(port_text) if port_text else None
        except ValueError:
            port = None
        if userinfo:
            user, _, password = userinfo.partition(":")
    headers: dict[str, str] = {}
    if user and password:
        creds = "%s:%s" % (urllib.parse.unquote(user), urllib.parse.unquote(password))
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
            creds.encode("utf-8")).decode("ascii")
    return host or "", port, headers


class AcpError(Exception):
    """ACP 驱动失败。调用方把 message 直接写进结果里。"""


class AcpChannel:
    """ACP streamable-HTTP：一条 SSE 收事件，POST 发 JSON-RPC 请求。"""

    def __init__(self, link: str, token: str, user_agent: str,
                 proxy: str = "", timeout: float = 30.0):
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self.proxy = proxy
        self.timeout = timeout
        self.connection_id = ""
        self.events: queue.Queue = queue.Queue()
        self.updates = 0
        self.chunks = 0
        self._conn = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    def _target(self):
        parsed = urllib.parse.urlparse(self.link)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AcpError("沙箱地址不可用: %r" % self.link[:80])
        return parsed, parsed.port or (443 if parsed.scheme == "https" else 80)

    def _connect(self):
        parsed, port = self._target()
        cls = (http.client.HTTPSConnection if parsed.scheme == "https"
               else http.client.HTTPConnection)
        if self.proxy:
            host, proxy_port, tunnel = _proxy_parts(self.proxy)
            conn = cls(host, proxy_port or 8080, timeout=self.timeout)
            conn.set_tunnel(parsed.hostname, port, headers=tunnel)
            return conn
        return cls(parsed.hostname, port, timeout=self.timeout)

    def open(self) -> str:
        parsed, _ = self._target()
        conn = self._connect()
        conn.putrequest("GET", parsed.path or "/")
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Authorization", "Bearer " + self.token)
        conn.putheader("User-Agent", self.user_agent)
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise AcpError("SSE 通道返回 HTTP %s" % resp.status)
        cid = resp.getheader("Acp-Connection-Id") or ""
        if not cid:
            conn.close()
            raise AcpError("SSE 通道没有返回 Acp-Connection-Id")
        self._conn = conn
        self.connection_id = cid
        self._reader = threading.Thread(target=self._read_events, args=(resp,),
                                        daemon=True)
        self._reader.start()
        return cid

    def _read_events(self, resp) -> None:
        try:
            while not self._stop.is_set():
                line = resp.fp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":
                    update = (message.get("params") or {}).get("update") or {}
                    self.updates += 1
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        self.chunks += 1
                self.events.put(message)
        except Exception as exc:  # noqa: BLE001 — 连接被关、超时
            self.events.put({"__stream_error__": f"{type(exc).__name__}: {exc}"})
        finally:
            self.events.put(None)

    def request(self, method: str, params: dict, request_id: int) -> int:
        parsed, _ = self._target()
        body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                           "method": method, "params": params}).encode("utf-8")
        conn = self._connect()
        try:
            conn.putrequest("POST", parsed.path or "/")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", "application/json, text/event-stream")
            conn.putheader("Acp-Connection-Id", self.connection_id)
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("User-Agent", self.user_agent)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            conn.send(body)
            resp = conn.getresponse()
            resp.read()
            status = resp.status
        finally:
            conn.close()
        if status not in (200, 202):
            raise AcpError("%s 返回 HTTP %s" % (method, status))
        return status

    def close(self) -> None:
        self._stop.set()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _set_model(channel: AcpChannel, session_id: str, model: str) -> bool:
    """把这一轮用的模型钉到会话上。

    建会话请求体里的 model 只写进 manifest，真正决定这一轮模型的是这条扩展
    请求。少了它，会话按账号默认模型跑——网页端默认 Auto 是付费模型，每天的
    活跃就在扣积分。失败不致命，只记一句。
    """
    try:
        channel.request("session/set_model",
                        {"sessionId": session_id, "modelId": model}, 3)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_turn(link: str, token: str, session_id: str, cwd: str, prompt: str,
             poll_status: Callable[[], str] | None = None,
             wait_seconds: float = 120, poll_interval: float = 3.0,
             proxy: str = "", model: str = "",
             log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """接上沙箱把这一轮跑完，返回 {ok,status,events,chunks,elapsed_ms,error}。"""
    log = log or (lambda _m: None)
    started = time.time()
    channel = AcpChannel(link, token, WEB_UA, proxy=proxy)
    status = ""
    error = ""
    finished = False
    try:
        channel.open()
        channel.request("initialize",
                        {"protocolVersion": PROTOCOL_VERSION,
                         "clientCapabilities": CLIENT_CAPABILITIES}, 1)
        channel.request("session/load",
                        {"sessionId": session_id, "cwd": cwd or "/workspace",
                         "mcpServers": []}, 2)
        if model:
            _set_model(channel, session_id, model)
        channel.request("session/prompt",
                        {"sessionId": session_id,
                         "prompt": [{"type": "text", "text": prompt}]}, 4)
        deadline = time.time() + max(1.0, float(wait_seconds))
        while time.time() < deadline:
            if poll_status is not None:
                try:
                    status = str(poll_status() or "")
                except Exception as exc:  # noqa: BLE001
                    log(f"状态查询失败: {exc}")
                if status == "completed":
                    finished = True
                    break
                if status in ("failed", "error"):
                    error = f"会话状态={status}"
                    break
            if poll_interval:
                time.sleep(poll_interval)
    except AcpError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        channel.close()
    elapsed = int((time.time() - started) * 1000)
    if not status and poll_status is not None:
        try:
            status = str(poll_status() or "")
        except Exception:  # noqa: BLE001
            pass
    if not finished and not error:
        error = f"会话在 {int(wait_seconds)}s 内没有跑完（状态={status or '未知'}）"
    return {"ok": finished and not error, "status": status,
            "events": channel.updates, "chunks": channel.chunks,
            "elapsed_ms": elapsed, "error": _readable(error)}


def _clean_text(text: Any, status: int = 0, limit: int = 200) -> str:
    """把上游的报错压成一句人话。

    HTML 错误页（401/403 常见）整段塞进表格里没法看：换行、标签、<center>，
    一行撑到几百像素高。这里取 <title>，压掉空白，再按状态码补一句能操作的。
    """
    raw = str(text or "")
    if "<html" in raw.lower():
        match = re.search(r"<title>([^<]*)</title>", raw, re.I)
        raw = (match.group(1) if match else "") or "上游返回 HTML 错误页"
    raw = re.sub(r"\s+", " ", raw).strip()
    if status == 401:
        return "凭据失效（401），需要重新登录"
    if status == 403:
        return "上游 403：" + (raw[:limit] or "无正文")
    if status == 429:
        return "上游限流（429），稍后再试"
    return raw[:limit]


def _readable(error: str) -> str:
    """把 Windows 的网络错误翻成人话，原始码保留在括号里。"""
    text = str(error or "")
    if not text:
        return ""
    if "10053" in text or "ConnectionAborted" in text:
        return ("沙箱 TLS 握手被本机网络中断（WinError 10053）："
                "容器/防火墙/代理规则挡住了 *.sandbox.cloudstudio.club")
    if "10054" in text or "ConnectionReset" in text:
        return "沙箱连接被重置（WinError 10054）"
    if "timed out" in text.lower() or "Timeout" in text:
        return "连接超时"
    return text[:200]


# ------------------------------------------------------------------ 网页腿

def web_daily(acc: Account, cfg: dict[str, Any], proxy: str, model: str) -> dict[str, Any]:
    """跑一次网页端活跃会话。返回 {ok,phase,conversation,status,chunks,elapsed_ms,error}。"""
    started = time.time()
    prompt = str(cfg.get("prompt") or "Hi")
    wait = float(cfg.get("timeout") or 120)
    rounds = int(cfg.get("rounds") or 2)
    tried: list[dict[str, Any]] = []
    result: dict[str, Any] = {"ok": False, "phase": "create", "conversation": "",
                              "status": "", "chunks": 0, "elapsed_ms": 0,
                              "error": "未开始", "rounds": 0, "model": model}

    for index in range(max(1, rounds)):
        cid, fail = conversation_create(acc, proxy, prompt, model)
        if fail and fail.get("limit"):
            freed = conversation_prune(acc, proxy)
            cid, fail = conversation_create(acc, proxy, prompt, model)
            if fail:
                result.update(phase="create",
                              error="活跃会话已达上限（%d）%s" % (
                                  ACTIVE_LIMIT,
                                  f"，已自动结束 {freed} 条旧会话" if freed else ""),
                              rounds=index + 1)
                result["elapsed_ms"] = int((time.time() - started) * 1000)
                return result
        if fail:
            tried.append({"round": index + 1, "phase": "create",
                          "error": str(fail.get("message") or "")[:160]})
            result.update(phase="create", error=str(fail.get("message") or "建会话失败"),
                          rounds=index + 1)
            if not fail.get("retryable"):
                break
            time.sleep(1.5)
            continue

        keep = False
        try:
            try:
                sess = (conversation_get(acc, cid, "/session", proxy) or {}).get("data") or {}
            except Exception as exc:  # noqa: BLE001
                tried.append({"round": index + 1, "phase": "session",
                              "error": str(exc)[:160]})
                result.update(phase="session", conversation=cid,
                              error=f"session 查询失败: {exc}", rounds=index + 1)
                time.sleep(1.5)
                continue
            link = sess.get("link") or sess.get("endpoint") or ""
            token = sess.get("token") or ""
            sid = sess.get("sessionId") or sess.get("session_id") or cid
            cwd = sess.get("cwd") or "/workspace"
            if not link or not token:
                tried.append({"round": index + 1, "phase": "session",
                              "error": "沙箱未就绪（没有 link/token）"})
                result.update(phase="session", conversation=cid,
                              error="沙箱未就绪（没有 link/token）", rounds=index + 1)
                time.sleep(2.0)
                continue

            turn = run_turn(link, token, sid, cwd, prompt,
                            poll_status=lambda: conversation_status(acc, cid, proxy),
                            wait_seconds=wait, proxy=proxy, model=model)
            result.update(phase="turn", conversation=cid,
                          status=turn.get("status") or "",
                          chunks=turn.get("chunks") or 0,
                          ok=bool(turn.get("ok")),
                          error=turn.get("error") or "",
                          rounds=index + 1)
            keep = bool(turn.get("ok"))
            tried.append({"round": index + 1, "phase": "turn",
                          "ok": keep, "status": turn.get("status"),
                          "chunks": turn.get("chunks"),
                          "error": str(turn.get("error") or "")[:160]})
            if keep:
                break
        finally:
            # 不 stop 的话这条会话一直占活跃名额，攒满十条之后建不出新会话。
            # 成功的一轮 stop 完保留在网页端历史里，失败的直接删。
            conversation_release(acc, cid, proxy, delete=not keep)

    result["attempts"] = tried
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


# ------------------------------------------------------------------ 单账号

def run_one(acc: Account, cfg: dict[str, Any] | None = None,
            exit_port: int = 0, trigger: str = "manual") -> dict[str, Any]:
    """跑一个号：桌面腿（可选）→ 网页腿（可选）。

    `ok` 以网页腿为准：网页腿开着的时候，桌面腿成功只说明对话链路活着，
    不计入官方活跃；网页腿失败就整次算失败，不拿桌面腿的结果糊过去。
    """
    cfg = cfg or load_config()
    started = time.time()
    proxy = proxy_for(acc, cfg, exit_port)
    realm = (acc.realm or "").lower()
    out: dict[str, Any] = {
        "ok": False, "skipped": False, "phase": "", "status": 0, "mode": "",
        "model": "", "proxy": proxy, "elapsed_ms": 0, "conversation": "",
        "detail": "", "desktop": None, "web": None,
    }
    if realm != "intl":
        out.update(skipped=True, detail="国内版没有每日活跃")
        return out
    if not str(acc.access_token or "").strip():
        out.update(skipped=True, detail="没有 access token")
        return out
    if acc.is_envelope:
        out.update(skipped=True, detail="token 为加密信封，无法直接使用")
        return out
    if acc.is_api_key:
        out.update(skipped=True, detail="静态 API Key 不走会话链路")
        return out

    chain = probe.model_chain(str(cfg.get("chat_model") or ""),
                              cfg.get("models") or "", realm)
    model = chain[0] if chain else probe.CHAT_MODEL
    out["model"] = model
    desktop_on = bool(cfg.get("desktop"))
    web_on = bool(cfg.get("web"))

    if desktop_on:
        desk = desktop_daily(acc, cfg, proxy, model)
        out["desktop"] = desk
        out["status"] = desk.get("status") or 0
        out["phase"] = "desktop"
        if desk.get("ok"):
            out["mode"] = "desktop"
            out["detail"] = "桌面对话通过"
        else:
            out["detail"] = str(desk.get("detail") or "桌面对话失败")

    if web_on:
        web = web_daily(acc, cfg, proxy, model)
        out["web"] = web
        out["conversation"] = web.get("conversation") or ""
        out["phase"] = web.get("phase") or out["phase"]
        out["mode"] = "web+desktop" if desktop_on else "web"
        out["ok"] = bool(web.get("ok"))
        if web.get("ok"):
            out["detail"] = web.get("error") or (
                f"网页会话跑完：{web.get('chunks') or 0} 段输出")
        else:
            out["detail"] = str(web.get("error") or "网页会话失败")
            if desktop_on and out.get("desktop") and out["desktop"].get("ok"):
                out["detail"] = "桌面腿通过，网页腿失败：" + out["detail"]
    elif desktop_on:
        out["ok"] = bool((out.get("desktop") or {}).get("ok"))
    else:
        out.update(skipped=True, detail="两条腿都关着")

    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["detail"] = _clean_text(out["detail"], int(out.get("status") or 0), 400)
    return out


def record(row_id: int, acc: Account, res: dict[str, Any],
           trigger: str = "manual") -> dict[str, Any]:
    """把一次尝试写进 activity 表，返回落库后的行。"""
    return store.activity_add({
        "row_id": int(row_id),
        "uid": str(acc.uid or ""),
        "name": store.account_name(acc),
        "realm": str(acc.realm or ""),
        "at": int(time.time()),
        "ok": 1 if res.get("ok") else 0,
        "skipped": 1 if res.get("skipped") else 0,
        "mode": str(res.get("mode") or ""),
        "phase": str(res.get("phase") or ""),
        "status": int(res.get("status") or 0),
        "model": str(res.get("model") or ""),
        "proxy": str(res.get("proxy") or ""),
        "elapsed_ms": int(res.get("elapsed_ms") or 0),
        "conversation": str(res.get("conversation") or ""),
        "detail": str(res.get("detail") or ""),
        "trigger": str(trigger or "manual"),
    })


# ------------------------------------------------------------------ 批量运行器

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
    "skipped": 0, "started_at": 0, "finished_at": 0, "last": "",
    "trigger": "", "errors": [],
}


def state() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def _set(**patch: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(patch)


def _bump(key: str, delta: int = 1) -> None:
    with _STATE_LOCK:
        _STATE[key] = int(_STATE.get(key) or 0) + delta


def busy() -> bool:
    with _STATE_LOCK:
        return bool(_STATE["running"])


def run_batch(pairs: Iterable[tuple[int, Account]], cfg: dict[str, Any] | None = None,
              trigger: str = "manual", skip_done: bool | None = None,
              background: bool = True) -> dict[str, Any]:
    """跑一批号。pairs 是 [(行 id, Account, exit_port)] 或 [(行 id, Account)]。

    `background=True` 时立刻返回，进度走 `state()`；手动点一次按钮不该让 HTTP
    连接挂两分钟。返回 {"started": n} 或同步结果。
    """
    cfg = cfg or load_config()
    if skip_done is None:
        skip_done = bool(cfg.get("skip_done", True))
    # 风控账号在这里再挡一次：pending / all 由 _pairs 过滤，但「勾选跑」是前端
    # 直接把行 id 传进来的（selected 走 load_for_export），绕过了 _pairs。
    # 放在 run_batch 里过滤，三个入口就都守住了，不用每个入口各写一遍。
    risk_ids = {int(r["id"]) for r in store.list_accounts(realm="intl", limit=2000)
                if is_risk(r)}
    todo: list[tuple[int, Account, int]] = []
    for item in pairs:
        if len(item) == 3:
            row_id, acc, port = item  # type: ignore[misc]
        else:
            row_id, acc = item        # type: ignore[misc]
            port = 0
        if int(row_id) in risk_ids:
            continue
        if skip_done and store.activity_done_today(int(row_id)):
            continue
        todo.append((int(row_id), acc, int(port or 0)))

    with _STATE_LOCK:
        if _STATE["running"]:
            return {"started": 0, "busy": True}
        _STATE.update({"running": True, "total": len(todo), "done": 0, "ok": 0,
                       "failed": 0, "skipped": 0, "started_at": int(time.time()),
                       "finished_at": 0, "last": "", "trigger": trigger,
                       "errors": []})

    def worker() -> None:
        results: list[dict[str, Any]] = []
        try:
            width = max(1, int(cfg.get("concurrency") or 2))
            if width == 1 or len(todo) <= 1:
                for row_id, acc, port in todo:
                    results.append(_run_and_record(row_id, acc, cfg, port, trigger))
            else:
                batches = [todo[i:i + width] for i in range(0, len(todo), width)]
                for chunk in batches:
                    threads = []
                    box: list[dict[str, Any]] = []
                    for row_id, acc, port in chunk:
                        th = threading.Thread(
                            target=lambda r=row_id, a=acc, p=port:
                            box.append(_run_and_record(r, a, cfg, p, trigger)),
                            daemon=True)
                        th.start()
                        threads.append(th)
                    for th in threads:
                        th.join()
                    results.extend(box)
        finally:
            ok = sum(1 for r in results if r.get("ok"))
            skipped = sum(1 for r in results if r.get("skipped"))
            failed = len(results) - ok - skipped
            _set(running=False, finished_at=int(time.time()), ok=ok,
                 failed=failed, skipped=skipped, last="完成")

    if background:
        threading.Thread(target=worker, daemon=True).start()
        return {"started": len(todo)}
    worker()
    return {"started": len(todo), "sync": True, **_result_snapshot()}


def _result_snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return {"ok": _STATE["ok"], "failed": _STATE["failed"],
                "skipped": _STATE["skipped"], "total": _STATE["total"]}


def _run_and_record(row_id: int, acc: Account, cfg: dict[str, Any], port: int,
                    trigger: str) -> dict[str, Any]:
    try:
        res = run_one(acc, cfg, exit_port=port, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 — 单号异常不能拖垮整批
        res = {"ok": False, "phase": "internal", "detail": f"{type(exc).__name__}: {exc}",
               "elapsed_ms": 0, "mode": "", "model": "", "proxy": ""}
    try:
        record(row_id, acc, res, trigger)
    except Exception:  # noqa: BLE001
        pass
    _bump("done")
    if res.get("skipped"):
        _bump("skipped")
    elif res.get("ok"):
        _bump("ok")
    else:
        _bump("failed")
        with _STATE_LOCK:
            _STATE["errors"] = (_STATE.get("errors") or [])[-4:] + [
                f"{store.account_name(acc)}: {str(res.get('detail') or '')[:120]}"]
    _set(last=f"{store.account_name(acc)} · {'成功' if res.get('ok') else ('跳过' if res.get('skipped') else '失败')}")
    return res


def _pairs(realm: str = "intl", only_pending: bool = False
           ) -> list[tuple[int, Account, int]]:
    """账号 → [(行 id, Account, exit_port)]。

    两个排除项，都是账户级的：

    - 每日活跃关掉的号（`daily_active is False`）。开关的语义是「不参与活跃」，
      不是「只是给 hub 看的」，本程序自己也不该跑它。
    - 风控账号（`recheck_state == 'banned'`）。封着的号跑也是白跑：桌面腿会被
      403 / 11140 顶回来，网页腿建会话就失败，一天跑几十次只会把出口 IP 和号
      一起弄得更脏。解封后（复检判 revived 或手动移出风控）下一轮自动恢复参与，
      不需要回来重新开一次。
    """
    rows = store.list_accounts(realm=realm, limit=2000)
    skip = {int(r["id"]) for r in rows
            if r.get("daily_active") is False or is_risk(r)}
    out: list[tuple[int, Account, int]] = []
    for row_id, acc, port in store.load_for_export(realm=realm, with_ids=True):
        row_id = int(row_id)
        if row_id in skip:
            continue
        if only_pending and store.activity_done_today(row_id):
            continue
        out.append((row_id, acc, int(port or 0)))
    return out


def pending_pairs(realm: str = "intl") -> list[tuple[int, Account, int]]:
    """今天还没成功的号，带上各自的 exit_port。"""
    return _pairs(realm, only_pending=True)


def all_pairs(realm: str = "intl") -> list[tuple[int, Account, int]]:
    """全部参与活跃的号（含今天已成功的，用于「重跑」）。"""
    return _pairs(realm, only_pending=False)


# ------------------------------------------------------------------ 定时调度

_SCHED = {"started": False, "last": 0.0, "lock": threading.Lock()}


def start_scheduler() -> bool:
    """起后台调度线程。重复调用无副作用（服务重启只跑一份）。"""
    with _SCHED["lock"]:
        if _SCHED["started"]:
            return False
        _SCHED["started"] = True
    threading.Thread(target=_loop, daemon=True, name="wk-activity").start()
    return True


def _loop() -> None:
    # 30 秒一次：够细到能接住 daily_at 那一分钟，又不至于把库读穿。
    while True:
        time.sleep(30)
        try:
            sweep_once()
        except Exception:  # noqa: BLE001 — 调度线程不能因为一次异常就死
            continue


def sweep_once(now: float | None = None) -> dict[str, Any]:
    """到点补跑。返回这次做了什么，方便自测与日志。

    三条件同时满足才跑：开关开着 / 过了每日起始时刻 / 距上次扫描超过补跑间隔。
    「当日已成功」由 pending_pairs 过滤，所以一天里同一号只会成功一次，失败
    的号在间隔到点后再试。
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ran": False, "why": "disabled"}
    stamp = clock.dt(now)
    raw = str(cfg.get("daily_at") or "08:30")
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    hour, minute = (int(match.group(1)), int(match.group(2))) if match else (8, 30)
    if stamp.hour * 60 + stamp.minute < hour * 60 + minute:
        return {"ran": False, "why": "before_window"}
    interval = int(cfg.get("interval_minutes") or 30) * 60
    if _SCHED["last"] and (now or time.time()) - _SCHED["last"] < interval:
        return {"ran": False, "why": "interval"}
    if busy():
        return {"ran": False, "why": "busy"}
    pairs = pending_pairs("intl")
    if not pairs:
        _SCHED["last"] = now or time.time()
        return {"ran": False, "why": "nothing_pending"}
    _SCHED["last"] = now or time.time()
    run_batch(pairs, cfg, trigger="auto", skip_done=True, background=True)
    return {"ran": True, "started": len(pairs)}
