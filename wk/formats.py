"""七种账户文件格式的识别、解析与渲染。

格式清单（均已对照各项目源码核对）：

| id            | 项目                                  | 容器 | 形态                                  |
|---------------|---------------------------------------|------|---------------------------------------|
| hub_document  | ardeyouxipianyi/workbuddy2api-hub      | json | {format,version,count,accounts:[...]} |
| hub_dir_zip   | ardeyouxipianyi/workbuddy2api-hub      | zip  | accounts/<uid>.json，每账户一文件     |
| manager_zip   | ithtelab/workbuddy-manager             | zip  | workbuddy-<uid>.json，嵌套形          |
| panel_zip     | misakano7545/workbuddy2api-panel       | zip  | workbuddy-<uid>.json，嵌套形          |
| switch_json   | changexbc/workbuddy-switch             | json | 数组，扁平 snake_case                 |
| qoder_json    | NextAgentX/qoder-trae-workbuddy-switch | json | 数组，扁平 snake_case + id/auth_raw   |
| daily_json    | L0NE-6/WorkBuddy-Daily                 | json | 数组，{phone,access_token,env_line}   |
| daily_env     | L0NE-6/WorkBuddy-Daily                 | text | 每行 手机号:AT:RT                     |

hub 的 accounts 目录是每账户一个扁平 camelCase JSON；manager / panel 的 auth_dir
是每账户一个嵌套 JSON。两者都用 ZIP 传，但文件名前缀和内部形态不同——
导错地方的表现是「导入 0 个账号」，不是报错，所以必须分开。
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable

from .model import Account, from_row
from . import token as tok

# ZIP 解包上限：账户包不该有这么大，超了说明拿错文件或压缩炸弹。
MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 5000


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    project: str
    url: str
    container: str  # json | zip | text
    desc: str
    filename_hint: str = ""


PROFILES: dict[str, Profile] = {
    p.id: p
    for p in [
        Profile("hub_document", "Hub 导出文档", "workbuddy2api-hub",
                "https://github.com/ardeyouxipianyi/workbuddy2api-hub", "json",
                "单文件，accounts 数组，扁平 camelCase，带 format/version 头",
                "workbuddy-accounts.json"),
        Profile("hub_dir_zip", "Hub 账户目录", "workbuddy2api-hub",
                "https://github.com/ardeyouxipianyi/workbuddy2api-hub", "zip",
                "accounts/<uid>.json，每账户一个扁平 camelCase 文件", "hub-accounts.zip"),
        Profile("manager_zip", "Manager 账号包", "workbuddy-manager",
                "https://github.com/ithtelab/workbuddy-manager", "zip",
                "workbuddy-<uid>.json，嵌套形 account/auth/device_token", "accounts.zip"),
        Profile("panel_zip", "Panel 凭据目录", "workbuddy2api-panel",
                "https://github.com/misakano7545/workbuddy2api-panel", "zip",
                "workbuddy-<uid>.json，嵌套形（与 manager 同构，可直接换目录）", "panel-auths.zip"),
        Profile("switch_json", "Switch 账号库", "workbuddy-switch",
                "https://github.com/changexbc/workbuddy-switch", "json",
                "JSON 数组，扁平 snake_case，含 token_type/auth_raw/profile_raw", "accounts.json"),
        Profile("qoder_json", "Qoder/Trae Switch 账号库", "qoder-trae-workbuddy-switch",
                "https://github.com/NextAgentX/qoder-trae-workbuddy-switch", "json",
                "JSON 数组，扁平 snake_case，含 id/createdAt", "accounts.json"),
        Profile("daily_json", "Daily 登录结果", "WorkBuddy-Daily",
                "https://github.com/L0NE-6/WorkBuddy-Daily", "json",
                "JSON 数组，{phone,access_token,refresh_token,env_line,login_at}", "wb_login_result.json"),
        Profile("daily_env", "Daily 环境变量行", "WorkBuddy-Daily",
                "https://github.com/L0NE-6/WorkBuddy-Daily", "text",
                "每行 手机号:AT:RT，或 AT:RT；可直接粘进 WORKBUDDY_REFRESH_TOKEN", "wb_env.txt"),
    ]
}

# 导入侧识别出的来源标签（不是输出目标，只用于界面显示）
SOURCE_LABEL = {
    "hub_document": "Hub 导出文档",
    "hub_flat": "Hub 单账户文件",
    "nested": "嵌套形凭据（Manager / Panel）",
    "switch_array": "Switch 账号库",
    "daily_json": "Daily 登录结果",
    "daily_env": "环境变量行",
    "unknown_array": "未识别的账号数组",
    "unknown_object": "未识别的账号对象",
}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

@dataclass
class ParsedSource:
    rows: list[dict]
    source: str
    notes: list[str]
    files: list[str]

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)


def decode_text(data: bytes) -> str:
    """按 BOM → utf-8 → gbk 顺序解码。

    国内项目导出的 CSV/JSON 常见 GBK；直接 utf-8 会炸在第一个中文昵称上。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _looks_like_env_lines(text: str) -> bool:
    """判断是不是 Daily 项目那种 `手机号:AT:RT` 纯文本。

    判据：至少一行含两个英文冒号，且没有 JSON 结构字符。
    """
    stripped = text.strip()
    if not stripped or stripped[0] in "[{":
        return False
    lines = [ln.strip() for ln in stripped.replace("@", "\n").splitlines() if ln.strip()]
    if not lines:
        return False
    hit = 0
    for line in lines[:10]:
        if line.count(":") >= 2:
            hit += 1
    return hit > 0


def parse_env_lines(text: str) -> list[dict]:
    """解析 `手机号:AT:RT` / `AT:RT` / 单 token 三种行形态。

    与 WorkBuddy-Daily 的 load_accounts 同一口径：`@` 也是行分隔符。
    冒号必须是英文冒号——全角冒号是青龙变量最常见的粘贴事故。
    """
    rows = []
    for line in text.replace("@", "\n").splitlines():
        item = line.strip().strip(",").strip()
        if not item or item.startswith("#"):
            continue
        parts = item.split(":")
        if len(parts) >= 3:
            rows.append({
                "phone": parts[0].strip(),
                "access_token": parts[1].strip(),
                "refresh_token": ":".join(parts[2:]).strip(),
            })
        elif len(parts) == 2:
            rows.append({
                "access_token": parts[0].strip(),
                "refresh_token": parts[1].strip(),
            })
        else:
            rows.append({"access_token": item})
    return rows


def classify_json(blob: Any) -> tuple[str, list[dict]]:
    """把解析出来的 JSON 结构归到某个来源标签，并展开成行列表。"""
    if isinstance(blob, list):
        rows = [r for r in blob if isinstance(r, dict)]
        if rows and any("env_line" in r or "phone" in r for r in rows):
            return "daily_json", rows
        if rows and any("access_token" in r for r in rows):
            return "switch_array", rows
        return "unknown_array", rows

    if isinstance(blob, dict):
        if isinstance(blob.get("accounts"), list):
            rows = [r for r in blob["accounts"] if isinstance(r, dict)]
            return "hub_document", rows
        # 嵌套形：auth / account 任一存在即为凭据文件
        if isinstance(blob.get("auth"), dict) or isinstance(blob.get("account"), dict):
            return "nested", [blob]
        if any(k in blob for k in ("accessToken", "access_token")):
            if "env_line" in blob:
                return "daily_json", [blob]
            return "hub_flat", [blob]
        return "unknown_object", [blob]

    return "unknown_object", []


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """过滤 zip 成员：跳过目录、软链、越界路径与超大条目。"""
    out = []
    total = 0
    for info in zf.infolist()[:MAX_ENTRIES]:
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # 拒绝绝对路径与 .. 穿越：解包方（目标项目）可能直接按名字落盘
        if name.startswith("/") or ".." in name.split("/"):
            continue
        if info.file_size > MAX_ENTRY_BYTES:
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            break
        out.append(info)
    return out


def parse_bytes(filename: str, data: bytes) -> ParsedSource:
    """解析一个文件（JSON / ZIP / 纯文本），返回原始行列表与来源标签。"""
    notes: list[str] = []
    rows: list[dict] = []
    files: list[str] = []

    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"ZIP 包损坏：{exc}") from exc
        with zf:
            members = _safe_members(zf)
            if not members:
                raise ValueError("ZIP 包里没有可读文件")
            kinds: set[str] = set()
            for info in members:
                name = info.filename.replace("\\", "/")
                base = name.rsplit("/", 1)[-1]
                if not base.lower().endswith((".json", ".txt", ".info")):
                    continue
                try:
                    raw = zf.read(info)
                except (OSError, zipfile.BadZipFile) as exc:
                    notes.append(f"{name} 读取失败：{exc}")
                    continue
                if not raw.strip():
                    continue
                try:
                    blob = json.loads(decode_text(raw))
                except ValueError:
                    if base.lower().endswith(".txt") and _looks_like_env_lines(decode_text(raw)):
                        rows.extend(parse_env_lines(decode_text(raw)))
                        kinds.add("daily_env")
                        files.append(name)
                    else:
                        notes.append(f"{name} 不是合法 JSON，已跳过")
                    continue
                kind, sub = classify_json(blob)
                rows.extend(sub)
                kinds.add(kind)
                files.append(name)
        source = _pick_source(kinds)
        if len(files) > 1:
            notes.append(f"包内共 {len(files)} 个文件，合并为 {len(rows)} 条记录")
        return ParsedSource(rows, source, notes, files)

    text = decode_text(data)
    if not text.strip():
        raise ValueError("文件内容为空")

    if _looks_like_env_lines(text):
        rows = parse_env_lines(text)
        return ParsedSource(rows, "daily_env", notes, [filename])

    try:
        blob = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"不是合法 JSON：{exc}") from exc

    kind, rows = classify_json(blob)
    if isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        meta = {k: v for k, v in blob.items() if k != "accounts"}
        if meta.get("format"):
            notes.append(f"文档格式标记：{meta['format']} v{meta.get('version', '?')}")
    return ParsedSource(rows, kind, notes, [filename])


def _pick_source(kinds: set[str]) -> str:
    """一个包里混了多种形态时，取最能说明问题的那个标签。"""
    for key in ("hub_document", "nested", "hub_flat", "switch_array", "daily_json", "daily_env"):
        if key in kinds:
            return key
    return next(iter(kinds), "unknown_array")


def to_accounts(rows: Iterable[dict], source: str = "") -> tuple[list[Account], list[str]]:
    """原始行 → Account 列表，同时收集逐条错误（坏行不阻塞整批）。"""
    accounts: list[Account] = []
    errors: list[str] = []
    for index, row in enumerate(rows, 1):
        try:
            acc = from_row(row, source)
        except ValueError as exc:
            errors.append(f"第 {index} 条：{exc}")
            continue
        # uid 缺失时从 JWT 补；补不出就留空，交给 pipeline 决定是否跳过
        if not acc.uid and acc.access_token:
            acc.uid = tok.jwt_uid(acc.access_token)
        if not acc.realm:
            acc.realm = tok.detect_realm(acc.access_token, acc.domain, acc.api_key)
        if not acc.domain:
            acc.domain = tok.domain_for_realm(acc.realm)
        if not acc.expires_at and acc.access_token:
            acc.expires_at = tok.jwt_exp(acc.access_token)
        accounts.append(acc)
    return accounts, errors


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _merge_extra(payload: dict, acc: Account) -> dict:
    """把原行里的未知字段补进输出。

    跨版本迁移最容易出的事故是「导进去少了一半字段」，而不是报错。
    已知键优先，未知键不覆盖。
    """
    for key, value in (acc.extra or {}).items():
        if key not in payload and value is not None:
            payload[key] = value
    return payload


def to_hub_flat(acc: Account) -> dict:
    """hub 的扁平 camelCase 形态（对应 wb_accounts.Account.to_dict）。"""
    payload = {
        "uid": acc.uid,
        "nickname": acc.nickname,
        "domain": acc.domain,
        "realm": acc.realm,
        "platform": acc.platform or "desktop",
        "product": acc.product or "workbuddy",
        "enterpriseId": acc.enterprise_id,
        "accessToken": acc.access_token,
        "refreshToken": acc.refresh_token,
        "expiresAt": acc.expires_at,
        "addedAt": acc.created_at or int(time.time()),
        "source": acc.source or "wk-switch",
        "apiKey": acc.is_api_key,
        "proxySlot": acc.proxy_slot,
        "proxy": acc.proxy,
        "enabled": acc.enabled,
        "tasksEnabled": acc.tasks_enabled,
        "weight": acc.weight,
        "note": acc.note,
        "disabledModels": list(acc.disabled_models),
        "credits": acc.credits,
        "tier": acc.tier,
    }
    if acc.email:
        payload["email"] = acc.email
    if acc.phone:
        payload["phone"] = acc.phone
    return _merge_extra(payload, acc)


def to_nested(acc: Account) -> dict:
    """manager / panel 的嵌套形态（account + auth + device_token）。"""
    auth = {
        "accessToken": acc.access_token,
        "refreshToken": acc.refresh_token,
        "expiresAt": acc.expires_at,
        "domain": acc.domain,
        "realm": acc.realm,
    }
    account = {
        "uid": acc.uid,
        "enterpriseId": acc.enterprise_id,
        "nickname": acc.nickname,
    }
    if acc.email:
        account["email"] = acc.email
    doc: dict[str, Any] = {"account": account, "auth": auth}
    # 空 device_token 不写：保持与旧文件同形，避免引入无意义空键
    if acc.device_token:
        doc["device_token"] = acc.device_token
    return doc


def to_switch_flat(acc: Account) -> dict:
    """changexbc/workbuddy-switch 的 AccountRecord 形态。"""
    payload = {
        "id": acc.uid,
        "uid": acc.uid,
        "nickname": acc.nickname,
        "email": acc.email,
        "access_token": acc.access_token,
        "refresh_token": acc.refresh_token,
        "token_type": acc.token_type or "Bearer",
        "domain": acc.domain,
        "expiresAt": acc.expires_at,
        "refreshExpiresAt": acc.refresh_expires_at,
        "createdAt": acc.created_at or int(time.time()),
    }
    if acc.auth_raw is not None:
        payload["auth_raw"] = acc.auth_raw
    if acc.profile_raw is not None:
        payload["profile_raw"] = acc.profile_raw
    return _merge_extra(payload, acc)


def to_daily(acc: Account) -> dict:
    """WorkBuddy-Daily 的 wb_login_result.json 形态。"""
    phone = acc.phone or acc.note or acc.uid
    env_line = f"{phone}:{acc.access_token}:{acc.refresh_token}" if phone else f"{acc.access_token}:{acc.refresh_token}"
    return {
        "phone": phone,
        "access_token": acc.access_token,
        "refresh_token": acc.refresh_token,
        "env_line": env_line,
        "login_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(acc.created_at or time.time())),
    }


@dataclass
class OutputFile:
    name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def _safe_name(value: str, fallback: str) -> str:
    """文件名清洗：目标项目按文件名解析 uid，所以只留安全字符。"""
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "")).strip("._ ")
    return text or fallback


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _zip_bytes(items: list[tuple[str, bytes]]) -> bytes:
    """打包 ZIP。同名条目加序号后缀，绝不丢行。

    同一个 uid 出现两条（两个环境各一份凭据）时，按 uid 命名会撞名。
    ZIP 里重名会让解压方行为不确定，但直接跳过就等于静默丢账号——
    这里改成 <name>-2.json，目标项目按文件内容取 uid，不受文件名影响。
    """
    buf = io.BytesIO()
    seen: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in items:
            count = seen.get(name, 0) + 1
            seen[name] = count
            if count > 1:
                head, dot, ext = name.rpartition(".")
                name = f"{head}-{count}.{ext}" if dot else f"{name}-{count}"
            zf.writestr(name, data)
    return buf.getvalue()


def render(accounts: list[Account], profile_id: str, opts: dict | None = None) -> list[OutputFile]:
    """按目标格式渲染。返回一个或多个文件（ZIP 目标返回单个 .zip）。

    opts 支持 realm_filter：cn / intl 只渲染该版本的账号。国内版和国际版的
    凭据进的是不同上游，混在一个包里导进去，另一半账号会在错误的出口上全 401，
    所以分版本导出是一等需求，不是可选项。
    """
    opts = opts or {}
    profile = PROFILES.get(profile_id)
    if not profile:
        raise ValueError(f"未知目标格式：{profile_id}")

    realm_filter = str(opts.get("realm_filter") or "").strip().lower()
    if realm_filter in ("cn", "intl"):
        accounts = [a for a in accounts if (a.realm or "").lower() == realm_filter]
    if not accounts:
        raise ValueError("没有可导出的账号")

    if profile_id == "hub_document":
        doc = {
            "format": "workbuddy-accounts",
            "version": 1,
            "exportedAt": _now_text(),
            "count": len(accounts),
            "accounts": [to_hub_flat(a) for a in accounts],
        }
        return [OutputFile(profile.filename_hint, _json_bytes(doc))]

    if profile_id == "hub_dir_zip":
        items = []
        for index, acc in enumerate(accounts, 1):
            name = _safe_name(acc.uid, f"account-{index}") + ".json"
            items.append((name, _json_bytes(to_hub_flat(acc))))
        return [OutputFile(profile.filename_hint, _zip_bytes(items))]

    if profile_id in ("manager_zip", "panel_zip"):
        items = []
        for index, acc in enumerate(accounts, 1):
            # 目标项目按 workbuddy-<uid>.json 扫目录，前缀不能省
            name = "workbuddy-" + _safe_name(acc.uid, f"account-{index}") + ".json"
            items.append((name, _json_bytes(to_nested(acc))))
        return [OutputFile(profile.filename_hint, _zip_bytes(items))]

    if profile_id == "switch_json":
        return [OutputFile(profile.filename_hint, _json_bytes([to_switch_flat(a) for a in accounts]))]

    if profile_id == "qoder_json":
        payload = []
        for acc in accounts:
            row = to_switch_flat(acc)
            # 这个项目用 id 做主键，缺了会当成新记录追加
            row["id"] = row.get("id") or acc.uid
            payload.append(row)
        return [OutputFile(profile.filename_hint, _json_bytes(payload))]

    if profile_id == "daily_json":
        return [OutputFile(profile.filename_hint, _json_bytes([to_daily(a) for a in accounts]))]

    if profile_id == "daily_env":
        lines = []
        for acc in accounts:
            phone = acc.phone or acc.note or acc.uid
            if phone and acc.refresh_token:
                lines.append(f"{phone}:{acc.access_token}:{acc.refresh_token}")
            elif acc.refresh_token:
                lines.append(f"{acc.access_token}:{acc.refresh_token}")
            else:
                lines.append(acc.access_token)
        text = "\n".join(lines) + "\n"
        return [OutputFile(profile.filename_hint, text.encode("utf-8"))]

    raise ValueError(f"目标格式 {profile_id} 未实现渲染")


def profile_list() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "project": p.project,
            "url": p.url,
            "container": p.container,
            "desc": p.desc,
        }
        for p in PROFILES.values()
    ]
