"""国际版等级任务：把成长中心那五个任务跑完并领奖。

## 为什么单独一个模块

每日活跃（`wk/activity.py`）要的是「今天有一次跑完的网页会话」，一天一次就够；
等级任务是一次性的清单，每个任务有自己的判据：

    first_chat       完成一次对话
    skill_installed  装一个技能并对话
    expert_summoned  召唤一次专家并对话
    template_used    用一次模板并对话
    wechat_linked    绑定微信 —— 只能在官方客户端做，本模块只检测

前四个能做得干净的共同点是：**先做真实动作，再回读服务端状态**。上游给的是
`/activity/growth/tasks` 里的 `status` / `progress`，只要它没变成 `completed`
或 `claimed`，就一个奖励都不领 —— HTTP 200 不等于任务完成，这是这套接口最
容易踩的坑（领奖接口对未完成的任务也返回 200）。

## 判据事件

任务完成与否由成长中心的事件流决定，不是「有没有发过请求」。网页对话只负责
让服务端生成会话与 messageId，真正的判据是随对话一起上报的
`agent_task_created / chat_request_send / chat_message_response ...` 事件组，
模板 / 专家 / 技能各有自己的事件组。这些事件用 `POST /v2/report` 发，body 是
数组，每条都要带 `userId`（缺了会被静默丢弃，且照样回 200）。

`wechat_linked` 永远不上报：它要的是微信侧的绑定结果，伪造事件等于冒领奖励，
而且绑没绑服务端一查就知道。这里只按服务端状态提示去官方客户端操作。

## 出口

和每日活跃共用同一套出口选择（`activity.proxy_for`）：固定代理 →
官网登录绑的精确 URL → 账户自己的 exit_port → 导入自带 proxy → 兜底 → 直连。
不在这里另开一份配置，否则同一个号在两个页面从不同 IP 出去。

## 落库

`level_task` 存每个账号每个任务的当前快照（每次整体覆盖），`level_run` 存每次
运行的记录（只追加）。界面读快照画格子，读记录看历史。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Iterable

from . import activity, clock, probe, store
from .model import Account

# ------------------------------------------------------------------ 端点与常量

BASE = "https://www.workbuddy.ai"

#: 任务列表的三个口径。它们不是彼此的超集：等级族没有 /v2，桌面族没有
#: `X-Client-Platform`，小程序族带 `miniprogram`。少跑一个就会漏任务。
TASK_SOURCES: tuple[tuple[str, str, str | None], ...] = (
    ("level", "/activity/growth/tasks", None),
    ("desktop", "/v2/activity/growth/tasks", ""),
    ("mp", "/v2/activity/growth/tasks", "miniprogram"),
)
ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
CLAIM_PATH = "/activity/growth/tasks/%s/claim"
REPORT_PATH = "/v2/report"

WEB_CONVERSATIONS_PATH = "/console/webchat/conversations"
WEB_CHAT_PATH = "/console/chat/completions"
SCENES_PATH = "/v2/as/support/scenes?locale=en"
EXPERT_LIST_PATH = "/v2/operation-platform/market/expert/list"
SKILL_LIST_PATH = "/v2/operation-platform/market/skill/list"
PLUGINS_INSTALLED_PATH = "/console/as/user/plugins/installed"
PLUGINS_INSTALL_PATH = "/console/as/user/plugins/install"
MARKET_SOURCES_PATH = "/console/as/marketplace/sources"

#: 五个任务，顺序就是界面上的顺序（和成长中心一致）。
CODES: tuple[str, ...] = (
    "first_chat",
    "skill_installed",
    "expert_summoned",
    "template_used",
    "wechat_linked",
)

LABELS: dict[str, str] = {
    "first_chat": "完成一次对话",
    "skill_installed": "完成技能安装",
    "expert_summoned": "召唤一次专家",
    "template_used": "使用一次模板",
    "wechat_linked": "链接微信",
}

#: 界面上那一列格子用的短名。四个字以内，窄屏也不换行。
SHORT: dict[str, str] = {
    "first_chat": "对话",
    "skill_installed": "技能",
    "expert_summoned": "专家",
    "template_used": "模板",
    "wechat_linked": "微信",
}

#: 只能在官方客户端完成的。脚本不伪造、不冒领。
CLIENT_ONLY = {"wechat_linked"}

#: 需要自动跑完的任务数（不含客户端项）。「这个号做完了没」按它算。
AUTO_CODES = tuple(c for c in CODES if c not in CLIENT_ONLY)

#: 网页对话的请求头。渠道校验（11-128）按这组 UA 识别客户端，换成浏览器 UA
#: 建出来的会话不会被成长中心当成一次任务对话。
CHAT_UA = "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
WEB_UA = activity.WEB_UA

#: 判据事件里带的客户端指纹版本，与上面 UA 对齐。
IDE_VERSION = "5.8.4"
EVENT_MODEL = "fast-model"

DEFAULT_MODELS = ("deepseek-v4.1-flash", "hy3", "hy4-preview", "hy4-preview-f")

CONFIG_PATH = os.path.join(store.ROOT, "config", "leveltask.json")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "daily_at": "09:10",
    "interval_minutes": 180,
    "concurrency": 1,
    "timeout": 120,
    "models": "",          # 空 = 内置四个免费模型
    "install_skill": True,  # 关掉只检测技能市场，不真的装插件
    "prompt_chat": "Hello, please reply OK.",
}

_BOOL_KEYS = ("enabled", "install_skill")
_INT_KEYS = {"concurrency": (1, 6), "timeout": (30, 600),
             "interval_minutes": (15, 1440)}
_TEXT_KEYS = ("daily_at", "models", "prompt_chat")


# ------------------------------------------------------------------ 配置读写

def load_config() -> dict[str, Any]:
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


# ------------------------------------------------------------------ HTTP 底座

def _headers(acc: Account, kind: str = "level", platform: str | None = None,
             report: bool = False) -> dict[str, str]:
    """按调用面拼请求头。

    `kind`：
      level  —— `/activity/growth/tasks` 等级族，**不能**带 X-Client-Platform，
                带了上游会改下发口径（同一批任务变成空列表）。
      task   —— `/v2/...` 桌面/小程序族，`platform` 有值才带平台头。
      web    —— `/console/...` 网页会话与插件接口，走浏览器 UA。
    `report=True` 再加一个 X-Request-ID，事件上报接口用它去重。
    """
    bearer = "Bearer " + str(acc.access_token or "")
    if kind == "web":
        head = {
            "Authorization": bearer,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": BASE,
            "Referer": BASE + "/",
            "User-Agent": WEB_UA,
            "X-Domain": "www.workbuddy.ai",
            "X-Product": "SaaS",
        }
    else:
        head = {
            "Authorization": bearer,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": CHAT_UA,
            "X-Domain": "www.workbuddy.ai",
            "X-Product": "SaaS",
            "Origin": BASE,
            "Referer": BASE + "/profile/growth-center",
        }
        if platform:
            head["X-Client-Platform"] = platform
    if report:
        head["X-Request-ID"] = uuid.uuid4().hex
    uid = str(acc.uid or "").strip()
    if uid:
        head["X-User-Id"] = uid
    return head


def _read_body(resp: Any) -> str:
    try:
        raw = resp.read()
    except Exception:  # noqa: BLE001 — 已经读过或连接已断
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _parse(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        return {"_raw": text[:400]}
    return payload if isinstance(payload, dict) else {"_list": payload}


def _http(acc: Account, proxy: str, method: str, path: str, body: Any = None,
          timeout: float = 25.0, headers: dict[str, str] | None = None
          ) -> dict[str, Any]:
    """一次 JSON 请求。网络错误也返回值，不往上抛 —— 单号失败不该拖垮整批。

    返回 `{status, payload, text}`；`status=0` 表示没连上（payload 里带
    `_error`），与「连上了但业务失败」分开，界面才分得清是网络还是账号问题。
    """
    url = path if path.startswith("http") else BASE + path
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers if headers is not None
                                 else _headers(acc))
    try:
        with probe._opener(proxy).open(req, timeout=timeout) as resp:  # noqa: SLF001
            text = _read_body(resp)
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        text = _read_body(exc)
        status = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — 超时、DNS、代理断开
        return {"status": 0, "payload": {"_error": f"{type(exc).__name__}: {exc}"},
                "text": ""}
    return {"status": status, "payload": _parse(text), "text": text}


def _snippet(text: str, limit: int = 140) -> str:
    raw = str(text or "")
    if "<html" in raw.lower():
        raw = "上游返回 HTML 错误页"
    return " ".join(raw.split())[:limit]


def is_restricted(status: int, payload: dict[str, Any]) -> bool:
    """账号级风控：11140 / request illegal。

    和「技能没装成」「专家市场空」这类业务失败分开：命中这个就别再发请求了，
    继续敲只会把号弄得更脏。
    """
    code = payload.get("code") if isinstance(payload, dict) else None
    blob = json.dumps(payload, ensure_ascii=False).lower() if payload else ""
    if code in (11140, "11140"):
        return True
    marks = ("11140", "request illegal", "account restricted",
             "账号受限", "账号被限制")
    return int(status or 0) in (401, 403) and any(m in blob for m in marks)


def _failure(status: int, payload: dict[str, Any]) -> str:
    """业务失败的一句话。空串 = 这次调用成功。"""
    if status == 0:
        return str((payload or {}).get("_error") or "连接失败")[:160]
    if is_restricted(status, payload):
        return "账号受限（%s）" % (payload.get("code") or status)
    code = (payload or {}).get("code")
    if status != 200:
        return ("HTTP %s %s" % (status, _snippet((payload or {}).get("msg")
                                                or (payload or {}).get("_raw")
                                                or ""))).strip()
    if code not in (0, 200, None, "0", "200"):
        msg = (payload or {}).get("msg") or (payload or {}).get("message") or ""
        return ("code=%s %s" % (code, _snippet(msg, 100))).strip()
    return ""


# ------------------------------------------------------------------ 任务行解析

def task_code(row: dict[str, Any]) -> str:
    return str(row.get("task_code") or row.get("code") or "").strip()


def task_status(row: dict[str, Any]) -> str:
    return str(row.get("accept_status") or row.get("status") or "").strip().lower()


def task_progress(row: dict[str, Any]) -> tuple[float | None, float | None]:
    progress = row.get("progress") if isinstance(row, dict) else None
    if not isinstance(progress, dict):
        return None, None
    cur, tgt = progress.get("current"), progress.get("target")
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        cur = None
    if isinstance(tgt, bool) or not isinstance(tgt, (int, float)):
        tgt = None
    return cur, tgt


def is_claimed(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    claimed = row.get("claimed")
    return (task_status(row) == "claimed" or claimed is True
            or str(claimed).strip().lower() == "true")


def is_locked(row: dict[str, Any]) -> bool:
    return bool(isinstance(row, dict) and row.get("locked"))


def is_ready(row: dict[str, Any]) -> bool:
    """能领奖了：completed，或者 accepted 且进度满。"""
    if not isinstance(row, dict) or is_claimed(row) or is_locked(row):
        return False
    status = task_status(row)
    if status == "completed":
        return True
    if status != "accepted":
        return False
    cur, tgt = task_progress(row)
    return (isinstance(cur, (int, float)) and isinstance(tgt, (int, float))
            and tgt > 0 and cur >= tgt)


def _rank(row: dict[str, Any]) -> tuple[int, int]:
    """三源合并时的优先级。已经被领过的行不能被较弱的行覆盖。"""
    if is_claimed(row):
        rank = 50
    else:
        status = task_status(row)
        rank = {"completed": 40, "accepted": 30, "in_progress": 30,
                "already_accepted": 30, "not_accepted": 20,
                "available": 10}.get(status, 0)
        if is_locked(row):
            rank = 5
    return rank, {"level": 2, "desktop": 1, "mp": 0}.get(str(row.get("_source") or ""), 0)


def progress_text(row: dict[str, Any]) -> str:
    status = task_status(row) or "unknown"
    cur, tgt = task_progress(row)
    if cur is None or tgt is None:
        return status
    return "%s %s/%s" % (status, _num(cur), _num(tgt))


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def fetch_tasks(acc: Account, proxy: str, timeout: float = 25.0
                ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读三个口径的任务列表，按任务码合并。返回 (rows, 错误)。"""
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for source, path, platform in TASK_SOURCES:
        head = (_headers(acc, "level") if source == "level"
                else _headers(acc, "task", platform=platform or None))
        res = _http(acc, proxy, "GET", path, timeout=timeout, headers=head)
        if is_restricted(res["status"], res["payload"]):
            return merged, errors + ["账号受限（%s）" % res["payload"].get("code")]
        fail = _failure(res["status"], res["payload"])
        if fail:
            errors.append("%s %s" % (path, fail))
            continue
        rows = (res["payload"].get("data") or {}).get("tasks")
        if not isinstance(rows, list):
            errors.append("%s 未返回 tasks 列表" % path)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = task_code(row)
            if code not in CODES:
                continue
            item = dict(row)
            item["_source"] = source
            item["_path"] = path
            item["_platform"] = platform
            old = merged.get(code)
            if old is None or _rank(item) > _rank(old):
                merged[code] = item
    return merged, errors


# ------------------------------------------------------------------ 事件上报

def _identity(uid: str) -> dict[str, Any]:
    """事件里那套客户端身份。每个号固定一份（按 uid 派生），换号就换机器。"""
    def digest(tag: str) -> str:
        return hashlib.sha256(("%s:%s" % (tag, uid)).encode("utf-8")).hexdigest()[:36]

    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": str(uid),
        "username": "",
        "userNickname": "",
        "product": "SaaS",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": IDE_VERSION,
        "machineId": digest("machine"),
        "sessionId": digest("session"),
        "qimei36": digest("qimei"),
        "extName": "workbuddy-desktop",
        "extVersion": IDE_VERSION,
        "os": "win32",
        "osVersion": "10.0.26220",
        "arch": "x64",
        "cpuModel": "11th Gen Intel(R) Core(TM) i5-11320H @ 3.20GHz",
        "cpuCores": 20,
        "memorySize": 24,
    }


def report_events(acc: Account, proxy: str, events: list[dict[str, Any]],
                  timeout: float = 25.0) -> tuple[bool, str]:
    """把判据事件发给成长中心。body 是数组，每条都要带 userId。"""
    if not events:
        return False, "没有可上报事件"
    uid = str(acc.uid or "").strip()
    if not uid:
        return False, "缺少 uid（事件会被静默丢弃）"
    now_ms = int(time.time() * 1000)
    body = []
    for event in events:
        item = dict(_identity(uid))
        item.update(event)
        item.setdefault("timestamp", now_ms)
        item["userId"] = uid
        body.append(item)
    res = _http(acc, proxy, "POST", REPORT_PATH, body=body, timeout=timeout,
                headers=_headers(acc, "level", report=True))
    fail = _failure(res["status"], res["payload"])
    if fail:
        return False, "事件未确认：%s" % fail
    return True, "事件已上报（%d 条）" % len(body)


def _chat_chain(conversation_id: str, request_id: str, message_id: str,
                model: str) -> list[dict[str, Any]]:
    """一次网页对话的六连事件，字段对齐成长中心的判据。"""
    msg = str(message_id or (conversation_id + "-m"))
    common = {
        "traceId": request_id,
        "rootRequestId": request_id,
        "parentConversationId": conversation_id,
        "agentName": "cli",
        "agentType": "main",
        "codebuddy.session_id": conversation_id,
        "codebuddy.conversation_request_id": request_id,
    }
    return [
        {"eventCode": "agent_task_created", "source": "LOCAL", "name": "working",
         "task_target": "local", "mode": "craft", "requestModelId": model,
         "requestModelName": model, "has_repo": False, "repo_type": "none",
         "workspace_type": "empty", "has_connector": False, "connector_types": [],
         "has_mention": False, "mention_types": [], "has_template": False,
         "action": "", "template_name": "", "has_expert": False, "expert_id": "",
         "expert_name": "", "expert_industry_id": "", "has_skill": False,
         "skill_names": [], "conversationId": conversation_id, "messageId": msg,
         "buddyId": "", "buddyName": ""},
        {"eventCode": "chat_message_send", "messageId": msg + "-assistant",
         "historyCount": 0, "isContextTruncated": False, "currentStepCount": 1,
         **common},
        {"eventCode": "chat_request_send", "inputLength": 24, "isPlan": False,
         "isAutoExecuteTerminal": False, "isAutoModify": False,
         "codebaseEnable": False, "maxToken": 0, "maxSteps": 500,
         "temperature": 0, "maxRetries": 0, "mentionContexts": [],
         "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
         "mentionContextCount": 0, "command": "", "recommendId": "",
         "skillId": "", "skillCount": 0, "totalCount": 0, **common},
        {"eventCode": "chat_message_response", "messageId": msg + "-assistant",
         "responseModelId": model, "inputToken": 120, "outputToken": 80,
         "totalToken": 200, "cachedTokens": 0, "cachedWriteTokens": 0,
         "cachedMissTokens": 0, "isSuccessful": True, "messageErrorCode": "",
         "finishReason": "stop", "firstTokenAt": int(time.time() * 1000),
         "conversationId": conversation_id, **common},
        {"eventCode": "chat_message_status", "messageId": msg + "-assistant",
         "messageErrorCode": "0", **common},
        {"eventCode": "chat_request_response", "mode": "craft",
         "toolCallCount": 0, "inputToken": 120, "outputToken": 80,
         "totalToken": 200, "cachedTokens": 0, "cachedWriteTokens": 0,
         "cachedMissTokens": 0, "isSuccessful": True, "messageErrorCode": "",
         "finishReason": "stop", **common},
    ]


def _template_events(template: dict[str, str], conversation_id: str,
                     request_id: str) -> list[dict[str, Any]]:
    tid, tname = template["id"], template["name"]
    return [
        {"eventCode": "agent_task_created", "source": "CLOUD", "name": "",
         "mode": "craft", "requestModelId": "default", "action": tid,
         "has_template": True, "template_id": tid, "template_name": tname,
         "requestId": request_id, "conversationId": conversation_id},
        {"eventCode": "agent_task_created_with_template", "templateId": tid,
         "templateName": tname, "template_id": tid, "mode": "working",
         "isCustomModel": True, "id": tid, "name": tname,
         "requestId": request_id, "conversationId": conversation_id},
        {"eventCode": "template_used", "template_id": tid, "templateId": tid,
         "templateName": tname, "task_mode": "working", "id": tid, "name": tname,
         "source": "growth-center", "conversationId": conversation_id},
        {"eventCode": "playbook_prompt_send", "ext1": str(uuid.uuid4()),
         "requestId": request_id, "id": tid, "name": tname, "type": "other",
         "promptLength": 30, "isOfficial": 1, "source": "growth-center",
         "conversationId": conversation_id},
    ]


def _expert_events(expert: dict[str, str], conversation_id: str, request_id: str,
                   message_id: str, prompt: str, model: str
                   ) -> list[dict[str, Any]]:
    eid = expert["id"]
    name = expert["name"]
    title = expert.get("profession") or name
    etype = expert.get("expertType") or "agent"
    version = expert.get("version") or "1.0.0"
    events = [
        {"eventCode": "expert_summon_click", "id": eid, "name": name,
         "expertTitle": title, "type": "agent", "expertType": etype,
         "source": "builtin", "mode": "LOCAL"},
        {"eventCode": "expert_summoned", "id": eid, "name": name,
         "expertTitle": title, "type": "agent", "expertType": etype,
         "source": "builtin", "version": version, "mode": "LOCAL"},
    ]
    chain = _chat_chain("", conversation_id, message_id, model)
    for event in chain:
        if event.get("eventCode") == "agent_task_created":
            event.update({"has_expert": True, "expert_id": eid,
                          "expert_name": name, "expert_industry_id": ""})
    chain.append({
        "eventCode": "expert_actual_use", "id": eid, "name": name,
        "expertTitle": title, "type": "", "expertType": etype,
        "source": "builtin", "version": version, "cost": 0,
        "characterCount": len(prompt), "mode": "LOCAL",
        "conversationId": conversation_id, "requestId": request_id,
        "messageId": message_id, "requestModelId": model,
        "requestModelName": model,
    })
    return events + chain


def _skill_events(skill: dict[str, str], conversation_id: str, request_id: str,
                  message_id: str, model: str) -> list[dict[str, Any]]:
    events = _chat_chain("", conversation_id, message_id, model)
    for event in events:
        if event.get("eventCode") == "chat_message_response":
            event["finishReason"] = "tool_calls"
    events.append({
        "eventCode": "skill_info", "id": skill.get("slug") or skill["name"],
        "skillId": skill["id"], "skillVersion": "1.0.0",
        "toolStatus": "success", "fileCount": 0,
        "source": "workbuddy-desktop", "conversationId": conversation_id,
        "requestId": request_id, "messageId": message_id,
        "requestModelId": model, "requestModelName": model, "traceId": request_id,
    })
    return events


def _growth_meta(events: list[dict[str, Any]], request_id: str,
                 expert: dict[str, str] | None = None, prompt: str = "",
                 uid: str = "", model: str = EVENT_MODEL) -> dict[str, Any]:
    """网页对话请求体里的 `_meta`。专家任务要额外带专家上下文。"""
    meta: dict[str, Any] = {
        "growthEvent": json.dumps(events, ensure_ascii=False),
        "promptRequestId": request_id,
        "clientSendTime": int(time.time() * 1000),
        "userId": str(uid),
        "mode": "LOCAL",
        "model": model,
    }
    if expert:
        meta.update({
            "expertId": expert.get("id") or "",
            "expert": {"id": expert.get("id") or "", "name": expert.get("name") or "",
                       "prompt": prompt[:50]},
            "tags": ["expert:" + str(expert.get("id") or "")],
        })
    return meta


# ------------------------------------------------------------------ 网页对话

def _models(cfg: dict[str, Any]) -> list[str]:
    raw = str(cfg.get("models") or "")
    names = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    return names or list(DEFAULT_MODELS)


def _create_conversation(acc: Account, proxy: str, timeout: float
                         ) -> tuple[str, str]:
    """建一条网页会话，返回 (会话 id, 失败说明)。"""
    if not str(acc.uid or "").strip():
        return "", "缺少 uid，无法建网页会话"
    head = _headers(acc, "web")
    head["Accept"] = "application/json"
    body = {"name": "WorkBuddy task-%s" % uuid.uuid4().hex[:8]}
    res = _http(acc, proxy, "POST", WEB_CONVERSATIONS_PATH, body=body,
                timeout=timeout, headers=head)
    fail = _failure(res["status"], res["payload"])
    data = (res["payload"].get("data") or {}) if isinstance(res["payload"], dict) else {}
    cid = str(data.get("conversationId") or data.get("id") or "")
    if fail or not cid:
        return "", fail or "建会话没有返回会话 id"
    return cid, ""


def _stream_chat(acc: Account, proxy: str, body: dict[str, Any],
                 timeout: float = 90.0) -> dict[str, Any]:
    """发一次流式对话，读完 SSE。返回 {status, finished, text, message_id, error}。

    单独抽出来是为了能被打桩：单测不联网也能验证「没读到 messageId 就不算
    完成」这条判定。
    """
    head = _headers(acc, "web")
    head["Accept"] = "text/event-stream"
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(BASE + WEB_CHAT_PATH, data=data, method="POST",
                                 headers=head)
    try:
        resp = probe._opener(proxy).open(req, timeout=timeout)  # noqa: SLF001
    except urllib.error.HTTPError as exc:
        text = _read_body(exc)
        return {"status": int(exc.code or 0), "finished": False, "text": "",
                "message_id": "", "error": "HTTP %s %s" % (exc.code, _snippet(text))}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "finished": False, "text": "", "message_id": "",
                "error": "%s: %s" % (type(exc).__name__, exc)}

    parts: list[str] = []
    message_id = ""
    finished = False
    try:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("[DONE]", "[完成]", "[✅完成]"):
                finished = True
                break
            try:
                item = json.loads(chunk)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if not message_id:
                message_id = str(item.get("id") or item.get("messageId")
                                 or item.get("message_id") or "")
            for choice in item.get("choices") or []:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                content = delta.get("content") if isinstance(delta, dict) else ""
                if content:
                    parts.append(str(content))
    except Exception as exc:  # noqa: BLE001 — 流被上游掐断
        return {"status": 0, "finished": False, "text": "".join(parts),
                "message_id": message_id,
                "error": "流读取失败: %s" % str(exc)[:80]}
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    text = "".join(parts)
    ok = finished or bool(message_id and text)
    return {"status": 200, "finished": bool(ok), "text": text,
            "message_id": message_id,
            "error": "" if ok else "对话未完成或缺少服务端 messageId"}


def web_conversation(acc: Account, proxy: str, prompt: str, cfg: dict[str, Any],
                     meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """建会话 + 跑一轮对话，返回 {ok,note,conversation,message_id,model}。"""
    timeout = float(cfg.get("timeout") or 120)
    cid, fail = _create_conversation(acc, proxy, 25.0)
    if fail:
        return {"ok": False, "note": fail, "conversation": "", "message_id": "",
                "model": ""}
    last = ""
    for model in _models(cfg):
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "model": model,
            "stream": True,
            "conversationId": cid,
        }
        if meta:
            body["_meta"] = meta
        res = _stream_chat(acc, proxy, body, timeout)
        if res.get("finished"):
            return {"ok": True, "note": "网页对话已完成", "conversation": cid,
                    "message_id": str(res.get("message_id") or ""),
                    "model": model, "text": res.get("text") or ""}
        last = str(res.get("error") or "")
        if is_restricted(int(res.get("status") or 0), {"msg": last}):
            return {"ok": False, "note": "账号受限（11140）", "conversation": cid,
                    "message_id": "", "model": model}
    return {"ok": False, "note": last or "模型链全部未完成", "conversation": cid,
            "message_id": "", "model": ""}


# ------------------------------------------------------------------ 动作前置

def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            for key in ("zh", "en"):
                if isinstance(value.get(key), str) and value[key].strip():
                    return value[key].strip()
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def fetch_scene(acc: Account, proxy: str, timeout: float = 20.0
                ) -> tuple[dict[str, str] | None, str]:
    """取一个模板场景。这个接口不需要登录，但出口要跟着账号走。"""
    res = _http(acc, proxy, "GET", SCENES_PATH, timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": WEB_UA})
    fail = _failure(res["status"], res["payload"])
    if fail:
        return None, "场景表 %s" % fail
    rows = (res["payload"].get("data") or {}).get("scenes")
    for row in rows or []:
        if isinstance(row, dict) and row.get("id") is not None:
            return {"id": str(row.get("id")),
                    "name": _first_text(row.get("name"), row.get("title"), row.get("id"))}, ""
    return None, "场景表没有可用模板"


def fetch_expert(acc: Account, proxy: str, expected_id: str = "",
                 timeout: float = 25.0) -> tuple[dict[str, str] | None, str]:
    """专家市场里挑一个。任务行里带 expertId 时按它找，找不到就报错不凑数。"""
    first: dict[str, str] | None = None
    for page in range(1, 4):
        res = _http(acc, proxy, "POST", EXPERT_LIST_PATH,
                    body={"page": page, "page_size": 50, "expert_type": "agent"},
                    timeout=timeout, headers=_headers(acc, "task"))
        fail = _failure(res["status"], res["payload"])
        if fail:
            return None, "专家市场 %s" % fail
        rows = (res["payload"].get("data") or {}).get("experts") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            eid = str(row.get("expert_id") or row.get("source_id") or row.get("id") or "")
            if not eid:
                continue
            item = {
                "id": eid,
                "name": _first_text(row.get("display_name_zh"), row.get("displayName"),
                                    row.get("name"), eid),
                "profession": _first_text(row.get("profession_zh"), row.get("profession"),
                                          row.get("display_name_zh"), eid),
                "version": str(row.get("version") or "1.0.0"),
                "expertType": str(row.get("expert_type") or "agent"),
            }
            if expected_id and eid == expected_id:
                return item, ""
            if first is None:
                first = item
        if len(rows) < 50:
            break
    if expected_id:
        return None, "专家市场未找到任务绑定的专家 %s" % expected_id
    if first:
        return first, ""
    return None, "专家市场没有可用专家"


def fetch_skill(acc: Account, proxy: str, timeout: float = 25.0
                ) -> tuple[dict[str, str] | None, str]:
    res = _http(acc, proxy, "POST", SKILL_LIST_PATH,
                body={"page": 1, "page_size": 8}, timeout=timeout,
                headers=_headers(acc, "task"))
    fail = _failure(res["status"], res["payload"])
    if fail:
        return None, "技能市场 %s" % fail
    rows = (res["payload"].get("data") or {}).get("skills") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("skill_id") or row.get("skillId") or row.get("id") or "")
        if not sid:
            continue
        name = _first_text(row.get("name"), row.get("display_name"),
                           row.get("displayName"), sid)
        slug = str(row.get("slug") or row.get("plugin_name")
                   or row.get("skill_slug") or name or sid)
        return {"id": sid, "slug": slug, "name": name}, ""
    return None, "技能市场没有可用技能"


def _skill_installed(acc: Account, proxy: str, skill: dict[str, str],
                     timeout: float = 20.0) -> bool:
    """已装插件列表里能不能找到这个技能。装完必须回读，不能只看 200。"""
    res = _http(acc, proxy, "GET", PLUGINS_INSTALLED_PATH, timeout=timeout,
                headers=_headers(acc, "task"))
    if _failure(res["status"], res["payload"]):
        return False
    plugins = (res["payload"].get("data") or {}).get("plugins") or []
    for row in plugins:
        if not isinstance(row, dict):
            continue
        name = str(row.get("plugin_name") or row.get("name") or "")
        pid = str(row.get("id") or row.get("plugin_id") or "")
        if name in (skill.get("slug"), skill.get("name")) or (skill.get("id") and pid == skill["id"]):
            return True
    return False


def install_skill(acc: Account, proxy: str, cfg: dict[str, Any],
                  skill: dict[str, str]) -> tuple[bool, str]:
    """装一个技能，并用已装列表回读确认。"""
    if _skill_installed(acc, proxy, skill):
        return True, "技能已安装（回读确认）"
    if not cfg.get("install_skill", True):
        return False, "配置关闭了技能安装"
    res = _http(acc, proxy, "GET", MARKET_SOURCES_PATH, timeout=20.0,
                headers=_headers(acc, "task"))
    fail = _failure(res["status"], res["payload"])
    if fail:
        return False, "技能来源 %s" % fail
    sources = (res["payload"].get("data") or {}).get("sources") or []
    market_id = next((str(r.get("id")) for r in sources
                      if isinstance(r, dict) and r.get("id")), "")
    if not market_id:
        return False, "没取到 marketplace_id"
    res = _http(acc, proxy, "POST", PLUGINS_INSTALL_PATH,
                body={"plugin_name": skill.get("slug") or skill["name"],
                      "marketplace_id": market_id, "version": "latest"},
                timeout=30.0, headers=_headers(acc, "task"))
    fail = _failure(res["status"], res["payload"])
    if fail:
        return False, "技能安装 %s" % fail
    for _ in range(3):
        if _skill_installed(acc, proxy, skill):
            return True, "技能安装成功（回读确认）"
        time.sleep(1.0)
    return False, "安装接口已接受，但回读没发现插件"


def _expert_id_from_row(row: dict[str, Any]) -> str:
    text = str((row or {}).get("jump_url") or "")
    import re
    match = re.search(r"(?:expertId|expert_id|id)=(ex_[A-Za-z0-9]+)", text)
    return match.group(1) if match else ""


# ------------------------------------------------------------------ 五个任务的动作

def _act_first_chat(acc: Account, proxy: str, cfg: dict[str, Any],
                    row: dict[str, Any]) -> tuple[bool, str]:
    prompt = str(cfg.get("prompt_chat") or DEFAULTS["prompt_chat"])
    chat = web_conversation(acc, proxy, prompt, cfg)
    if not chat["ok"]:
        return False, chat["note"]
    ok, note = report_events(acc, proxy, _chat_chain(
        str(chat["conversation"]), uuid.uuid4().hex,
        str(chat["message_id"]), str(chat["model"])))
    return ok, "真实对话已完成；%s" % note


def _act_template(acc: Account, proxy: str, cfg: dict[str, Any],
                  row: dict[str, Any]) -> tuple[bool, str]:
    scene, err = fetch_scene(acc, proxy)
    if not scene:
        return False, err
    chat = web_conversation(acc, proxy, "Use the %s template and reply OK." % scene["name"], cfg)
    if not chat["ok"]:
        return False, chat["note"]
    ok, note = report_events(acc, proxy, _template_events(
        scene, str(chat["conversation"]), str(chat["message_id"])))
    return ok, "模板=%s；%s" % (scene["name"], note)


def _act_expert(acc: Account, proxy: str, cfg: dict[str, Any],
                row: dict[str, Any]) -> tuple[bool, str]:
    expert, err = fetch_expert(acc, proxy, _expert_id_from_row(row))
    if not expert:
        return False, err
    prompt = "Hello, please introduce yourself and reply OK."
    request_id = uuid.uuid4().hex
    meta = _growth_meta(
        [{"eventCode": "ExpertActualUse", "id": expert["id"],
          "extra": {"name": expert["name"],
                    "expertTitle": expert.get("profession") or expert["name"],
                    "expertType": expert.get("expertType") or "agent",
                    "source": "builtin",
                    "version": expert.get("version") or "1.0.0",
                    "cost": 0, "characterCount": len(prompt)},
          "expertType": expert.get("expertType") or "agent"}],
        request_id, expert=expert, prompt=prompt, uid=str(acc.uid or ""))
    chat = web_conversation(acc, proxy, prompt, cfg, meta=meta)
    if not chat["ok"]:
        return False, chat["note"]
    ok, note = report_events(acc, proxy, _expert_events(
        expert, str(chat["conversation"]), uuid.uuid4().hex,
        str(chat["message_id"]), prompt, str(chat["model"])))
    return ok, "专家=%s；%s" % (expert["name"], note)


def _act_skill(acc: Account, proxy: str, cfg: dict[str, Any],
               row: dict[str, Any]) -> tuple[bool, str]:
    skill, err = fetch_skill(acc, proxy)
    if not skill:
        return False, err
    installed, install_note = install_skill(acc, proxy, cfg, skill)
    if not installed:
        return False, install_note
    chat = web_conversation(acc, proxy, "Please use the %s skill and reply OK." % skill["name"], cfg)
    if not chat["ok"]:
        return False, chat["note"]
    ok, note = report_events(acc, proxy, _skill_events(
        skill, str(chat["conversation"]), uuid.uuid4().hex,
        str(chat["message_id"]), str(chat["model"])))
    return ok, "%s；技能=%s；%s" % (install_note, skill["name"], note)


ACTIONS: dict[str, Callable[[Account, str, dict[str, Any], dict[str, Any]],
                            tuple[bool, str]]] = {
    "first_chat": _act_first_chat,
    "template_used": _act_template,
    "expert_summoned": _act_expert,
    "skill_installed": _act_skill,
}


# ------------------------------------------------------------------ 接取 / 领奖 / 回读

def accept_task(acc: Account, proxy: str, code: str) -> tuple[bool, str]:
    """接取任务（只有桌面族需要）。接完回读状态，别信 accept 的即时响应。"""
    settled = ("accepted", "already_accepted", "completed", "claimed")
    last = ""
    for attempt in range(2):
        res = _http(acc, proxy, "POST", ACCEPT_PATH, body={"task_codes": [code]},
                    timeout=20.0, headers=_headers(acc, "task"))
        fail = _failure(res["status"], res["payload"])
        results = (res["payload"].get("data") or {}).get("results") or []
        item = next((r for r in results if isinstance(r, dict)
                     and str(r.get("task_code") or r.get("code") or "") == code),
                    results[0] if results and isinstance(results[0], dict) else {})
        last = str(item.get("status") or (res["payload"] or {}).get("msg") or "")
        if fail:
            if attempt == 0:
                time.sleep(1.0)
                continue
            return False, "接取失败 %s" % fail
        rows, _ = fetch_tasks(acc, proxy)
        status = task_status(rows.get(code) or {})
        if status in settled:
            return True, "已接取（%s）" % status
        last = status or last
        if attempt == 0:
            time.sleep(1.0)
    return False, "接取后状态仍为 %s" % (last or "unknown")


def claim_task(acc: Account, proxy: str, code: str) -> tuple[bool, str]:
    """领奖。必须回读到 claimed；只收到 HTTP 200 一律不算领到。"""
    res = _http(acc, proxy, "POST", CLAIM_PATH % code, body={}, timeout=20.0,
                headers=_headers(acc, "task"))
    payload = res["payload"] if isinstance(res["payload"], dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    message = str(payload.get("msg") or payload.get("message") or "")
    already = (bool(data.get("already_claimed"))
               or any(word in message.lower() for word in ("already", "已领取", "已领过")))
    has_reward = any(data.get(key) not in (None, "", 0, "0")
                     for key in ("credit", "energy", "reward", "reward_credit",
                                 "reward_energy"))
    fail = _failure(res["status"], payload)
    if fail or not (already or has_reward or data.get("claimed") is True):
        return False, "领奖未确认%s" % ("：%s" % fail if fail else "")

    row: dict[str, Any] = {}
    readback = ""
    for attempt in range(3):
        if attempt:
            time.sleep(1.0)
        try:
            rows, errors = fetch_tasks(acc, proxy)
            row = rows.get(code) or {}
            if is_claimed(row):
                return True, "已领取（回读确认）"
            readback = "；".join(errors)
        except Exception as exc:  # noqa: BLE001
            readback = str(exc)
    return False, "领奖已提交但回读仍为 %s" % (
        progress_text(row) if row else (readback or "未读到任务行"))


def _wait_task(acc: Account, proxy: str, code: str, attempts: int = 3,
               gap: float = 3.0) -> dict[str, Any]:
    """动作做完等上游结算。刚上报完立刻读，多半还是旧状态。"""
    row: dict[str, Any] = {}
    for index in range(max(1, attempts)):
        if index:
            time.sleep(gap)
        rows, _ = fetch_tasks(acc, proxy)
        row = rows.get(code) or {}
        if is_ready(row) or is_claimed(row):
            return row
    return row


# ------------------------------------------------------------------ 单账号

def _snapshot(code: str, row: dict[str, Any] | None, note: str = "") -> dict[str, Any]:
    row = row or {}
    cur, tgt = task_progress(row)
    return {
        "code": code,
        "label": LABELS.get(code, code),
        "status": task_status(row),
        "claimed": is_claimed(row),
        "locked": is_locked(row),
        "progress": progress_text(row) if row else "",
        "current": cur,
        "target": tgt,
        "source": str(row.get("_source") or ""),
        "note": note,
        "at": int(time.time()),
    }


def run_one(row_id: int, acc: Account, exit_port: int = 0,
            cfg: dict[str, Any] | None = None, trigger: str = "manual"
            ) -> dict[str, Any]:
    """跑一个号的全部等级任务，返回结果摘要（含每个任务的快照）。"""
    cfg = cfg or load_config()
    started = time.time()
    proxy = activity.proxy_for(acc, activity.load_config(), exit_port)
    out: dict[str, Any] = {
        "ok": False, "skipped": False, "proxy": proxy, "done": 0, "claimed": 0,
        "detail": "", "tasks": [], "elapsed_ms": 0,
    }
    if str(acc.realm or "").lower() != "intl":
        out.update(skipped=True, detail="国内版没有等级任务")
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

    rows, errors = fetch_tasks(acc, proxy)
    if not rows:
        out["detail"] = "任务列表为空：%s" % ("；".join(errors) if errors else "服务端未下发")
        out["tasks"] = [_snapshot(code, None, "未下发") for code in CODES]
        return out

    snapshots: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for code in CODES:
        label = LABELS.get(code, code)
        row = rows.get(code)
        if row is None:
            snapshots[code] = _snapshot(code, None, "未下发")
            continue
        if is_claimed(row):
            snapshots[code] = _snapshot(code, row, "已领取")
            out["done"] += 1
            continue
        if code in CLIENT_ONLY:
            note = ("服务端已达标，需官方客户端确认绑定" if task_status(row) == "completed"
                    else "需在官方客户端绑定微信（不伪造）")
            snapshots[code] = _snapshot(code, row, note)
            continue
        if is_locked(row):
            unlock = str(row.get("valid_start") or row.get("unlock_at") or "")[:10]
            snapshots[code] = _snapshot(code, row, "未解锁%s" % ("（%s）" % unlock if unlock else ""))
            continue
        if str(row.get("_source") or "") == "mp":
            snapshots[code] = _snapshot(code, row, "仅小程序口径下发，未执行")
            continue

        if is_ready(row):
            ok, note = claim_task(acc, proxy, code)
        else:
            if (str(row.get("_source") or "") == "desktop"
                    and task_status(row) in ("not_accepted", "pending", "")):
                ok_accept, note_accept = accept_task(acc, proxy, code)
                if not ok_accept:
                    snapshots[code] = _snapshot(code, row, note_accept)
                    failures.append("%s：%s" % (label, note_accept))
                    continue
            action = ACTIONS.get(code)
            if action is None:
                snapshots[code] = _snapshot(code, row, "没有自动动作")
                continue
            ok_act, note_act = action(acc, proxy, cfg, row)
            if not ok_act:
                if "账号受限" in note_act:
                    snapshots[code] = _snapshot(code, row, note_act)
                    failures.append("%s：%s" % (label, note_act))
                    # 账号级拦截：这个号剩下的任务一个请求都不发了
                    for rest in CODES:
                        if rest not in snapshots:
                            snapshots[rest] = _snapshot(rest, rows.get(rest), "账号受限，未执行")
                    out["detail"] = "；".join(failures)
                    out["tasks"] = [snapshots[c] for c in CODES if c in snapshots]
                    out["elapsed_ms"] = int((time.time() - started) * 1000)
                    return out
                snapshots[code] = _snapshot(code, row, note_act)
                failures.append("%s：%s" % (label, note_act))
                continue
            fresh = _wait_task(acc, proxy, code)
            if is_claimed(fresh):
                snapshots[code] = _snapshot(code, fresh, "已领取")
                out["done"] += 1
                out["claimed"] += 1
                continue
            if not is_ready(fresh):
                state = progress_text(fresh) if fresh else "未读到"
                note = "动作已执行，状态仍为 %s，不冒领" % state
                snapshots[code] = _snapshot(code, fresh or row, note)
                failures.append("%s：%s" % (label, note))
                continue
            ok, note = claim_task(acc, proxy, code)

        if ok:
            snapshots[code] = _snapshot(code, {"status": "claimed", "claimed": True,
                                               "_source": row.get("_source")}, note)
            out["done"] += 1
            out["claimed"] += 1
        else:
            snapshots[code] = _snapshot(code, row, note)
            failures.append("%s：%s" % (label, note))

    # 收尾重读一次再落快照：领奖后上游可能把其他任务一起结算了。
    fresh_rows, _ = fetch_tasks(acc, proxy)
    if fresh_rows:
        for code in CODES:
            if code in fresh_rows:
                snapshots[code] = _snapshot(code, fresh_rows[code],
                                            snapshots.get(code, {}).get("note") or "")
    out["tasks"] = [snapshots[c] for c in CODES if c in snapshots]
    out["ok"] = not failures
    if failures:
        out["detail"] = "；".join(failures)[:400]
    else:
        out["detail"] = "已领 %d 个%s" % (
            out["done"], "（微信需客户端）" if "wechat_linked" in rows else "")
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    if errors:
        out["detail"] = (out["detail"] + "｜列表告警：" + "；".join(errors))[:400]
    return out


def record(row_id: int, acc: Account, res: dict[str, Any],
           trigger: str = "manual") -> None:
    """落库：任务快照 + 运行记录。快照失败不该拖垮运行记录。"""
    try:
        if res.get("tasks"):
            store.level_tasks_save(row_id, res["tasks"])
    except Exception:  # noqa: BLE001
        pass
    try:
        store.level_run_add({
            "row_id": int(row_id), "uid": str(acc.uid or ""),
            "name": store.account_name(acc), "at": int(time.time()),
            "ok": bool(res.get("ok")), "skipped": bool(res.get("skipped")),
            "done": int(res.get("done") or 0),
            "claimed": int(res.get("claimed") or 0),
            "proxy": str(res.get("proxy") or ""),
            "elapsed_ms": int(res.get("elapsed_ms") or 0),
            "detail": str(res.get("detail") or ""), "trigger": trigger,
        })
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 批量

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
    "skipped": 0, "claimed": 0, "started_at": 0, "finished_at": 0,
    "last": "", "trigger": "", "errors": [],
}


def state() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def busy() -> bool:
    with _STATE_LOCK:
        return bool(_STATE["running"])


def _set(**patch: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(patch)


def _bump(key: str, delta: int = 1) -> None:
    with _STATE_LOCK:
        _STATE[key] = int(_STATE.get(key) or 0) + delta


def is_risk(row: dict[str, Any]) -> bool:
    """风控号不跑等级任务：请求会被 11140 顶回来，白跑还弄脏出口。"""
    return str(row.get("recheck_state") or "") == "banned"


def _is_done(tasks: dict[str, dict[str, Any]]) -> bool:
    """这个号的自动任务是不是都领完了。

    只按**服务端这次真下发了的任务**算：「未下发」的任务不该让一个号永远
    被捞出来重跑（有些任务只在特定活动期下发，等它等于天天白跑一轮）。
    没下发的任务等它出现时，下一轮自然会重新进待跑列表。
    """
    if not tasks:
        return False
    present = [code for code in AUTO_CODES if (tasks.get(code) or {}).get("status")]
    if not present:
        return False
    return all(bool(tasks[code].get("claimed")) for code in present)


def pairs(only_pending: bool = True) -> list[tuple[int, Account, int]]:
    """国际版 + 开关开着 + 非风控（+ 还没做完）。"""
    rows = store.list_accounts(realm="intl", limit=2000)
    skip = {int(r["id"]) for r in rows
            if r.get("level_tasks") is False or is_risk(r)}
    snapshots = store.level_tasks_map() if only_pending else {}
    out: list[tuple[int, Account, int]] = []
    for row_id, acc, port in store.load_for_export(realm="intl", with_ids=True):
        row_id = int(row_id)
        if row_id in skip:
            continue
        if only_pending and _is_done(snapshots.get(row_id) or {}):
            continue
        out.append((row_id, acc, int(port or 0)))
    return out


def run_batch(todo: Iterable[tuple[int, Account, int]],
              cfg: dict[str, Any] | None = None, trigger: str = "manual",
              background: bool = True) -> dict[str, Any]:
    cfg = cfg or load_config()
    items = [(int(r), a, int(p or 0)) for r, a, p in todo]
    with _STATE_LOCK:
        if _STATE["running"]:
            return {"started": 0, "busy": True}
        _STATE.update({"running": True, "total": len(items), "done": 0, "ok": 0,
                       "failed": 0, "skipped": 0, "claimed": 0,
                       "started_at": int(time.time()), "finished_at": 0,
                       "last": "", "trigger": trigger, "errors": []})

    def worker() -> None:
        try:
            width = max(1, int(cfg.get("concurrency") or 1))
            chunks = [items[i:i + width] for i in range(0, len(items), width)] or [[]]
            for chunk in chunks:
                threads = []
                for row_id, acc, port in chunk:
                    th = threading.Thread(
                        target=lambda r=row_id, a=acc, p=port: _run_and_record(r, a, cfg, p, trigger),
                        daemon=True)
                    th.start()
                    threads.append(th)
                for th in threads:
                    th.join()
        finally:
            _set(running=False, finished_at=int(time.time()), last="完成")

    if background:
        threading.Thread(target=worker, daemon=True, name="wk-leveltask").start()
        return {"started": len(items)}
    worker()
    snap = state()
    return {"started": len(items), **{k: snap[k] for k in ("ok", "failed", "skipped")}}


def _run_and_record(row_id: int, acc: Account, cfg: dict[str, Any], port: int,
                    trigger: str) -> dict[str, Any]:
    try:
        res = run_one(row_id, acc, port, cfg, trigger)
    except Exception as exc:  # noqa: BLE001 — 单号异常不拖垮整批
        res = {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc),
               "skipped": False, "done": 0, "claimed": 0, "tasks": [],
               "proxy": "", "elapsed_ms": 0}
    record(row_id, acc, res, trigger)
    _bump("done")
    if res.get("skipped"):
        _bump("skipped")
    elif res.get("ok"):
        _bump("ok")
    else:
        _bump("failed")
        with _STATE_LOCK:
            _STATE["errors"] = (_STATE.get("errors") or [])[-4:] + [
                "%s: %s" % (store.account_name(acc), str(res.get("detail") or "")[:120])]
    _bump("claimed", int(res.get("claimed") or 0))
    tail = "成功" if res.get("ok") else ("跳过" if res.get("skipped") else "失败")
    _set(last="%s · %s（+%d）" % (store.account_name(acc), tail,
                                 int(res.get("claimed") or 0)))
    return res


# ------------------------------------------------------------------ 定时

_SCHED = {"started": False, "last": 0.0, "boot": 0.0, "lock": threading.Lock()}

#: 启动后先等这么久再自动跑。等级任务一上来就是「每个号五次网页对话」，
#: 服务刚起（含刚升级完）时人还没看到设置，先给 5 分钟窗口去关掉自动运行。
BOOT_GRACE = 300


def start_scheduler() -> bool:
    with _SCHED["lock"]:
        if _SCHED["started"]:
            return False
        _SCHED["started"] = True
        _SCHED["boot"] = time.time()
    threading.Thread(target=_loop, daemon=True, name="wk-leveltask-sched").start()
    return True


def _loop() -> None:
    while True:
        time.sleep(60)
        try:
            sweep_once()
        except Exception:  # noqa: BLE001
            continue


def sweep_once(now: float | None = None) -> dict[str, Any]:
    """到点补跑。等级任务是一次性的，做完的号不会再被捞出来。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ran": False, "why": "disabled"}
    moment = now or time.time()
    if _SCHED["boot"] and moment - _SCHED["boot"] < BOOT_GRACE:
        return {"ran": False, "why": "boot_grace"}
    stamp = clock.dt(now)
    raw = str(cfg.get("daily_at") or "09:10")
    try:
        hour, minute = (int(x) for x in raw.split(":")[:2])
    except (TypeError, ValueError):
        hour, minute = 9, 10
    if stamp.hour * 60 + stamp.minute < hour * 60 + minute:
        return {"ran": False, "why": "before_window"}
    interval = int(cfg.get("interval_minutes") or 180) * 60
    if _SCHED["last"] and (now or time.time()) - _SCHED["last"] < interval:
        return {"ran": False, "why": "interval"}
    if busy():
        return {"ran": False, "why": "busy"}
    todo = pairs(only_pending=True)
    _SCHED["last"] = now or time.time()
    if not todo:
        return {"ran": False, "why": "nothing_pending"}
    run_batch(todo, cfg, trigger="auto", background=True)
    return {"ran": True, "started": len(todo)}
