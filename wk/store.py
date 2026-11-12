"""本地账户库：把导入过的 token 按「UID + 版本」落盘留痕。

唯一键是 (uid, realm)：
- uid 是账号的真实身份，一个账号只有一个 uid，同一个 uid 重复导入就是同一个账号；
- realm 只有 cn / intl 两种取值。uid 理论上全局唯一，但仍按版本分开存——
  两套上游各发各的号，分开存不会因为上游侧 id 空间重叠而互相覆盖；
- name 只是给人看的标签（备注名 → 昵称 → 邮箱 → 手机号 → uid 依次回落），
  可以重名、可以随时改，不参与唯一性判断。

用名称做唯一键的后果是「两个不同账号起了同一个备注名就被合并成一条」，
这是静默丢账号，比报错难查得多。

库文件在 data/accounts.db（SQLite）。明文存 token，因为它的用途就是本地备份。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from . import token as tok
from . import clock
from .model import Account

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "accounts.db")

#: 表结构版本。
#: 2 = 唯一键从 (name, realm) 改成 (uid, realm)
#: 3 = 加探针列（积分三元组 / 校验状态 / 校验时间），见 wk/probe.py
#: 4 = 加封号复检列（名单状态 / 次数 / 时间 / 结论），见 wk/recheck.py
#: 5 = 加导入批次列（最近批次 / 首次批次 / 导入时间）+ 风控来源标记
#: 7 = 加每日活跃历史表 activity，见 wk/activity.py
#: 8 = 加账户级精确出口 exit_proxy（官网登录时选的代理 URL）
SCHEMA_VERSION = 8

#: 探针状态的中文名。与 wk/probe.py 的 STATE_* 常量一一对应，
#: 界面上不再自己拼字符串。
PROBE_LABELS = {
    "valid": "有效",
    "invalid": "已失效",
    "unknown": "未确认",
    "skipped": "不可校验",
}

#: 复检名单状态的中文名。与 wk/recheck.py 的 TRACK_* 常量一一对应。
RECHECK_LABELS = {
    "pending": "待复检",
    "banned": "封号中",
    "revived": "已解封",
    "auth": "凭据失效",
    "unknown": "未确认",
}

#: 账号是怎么进风控区的。界面在批次列旁边标一个来源，方便判断该不该去敲门：
#: 导入时就标了风控的号，复检大概率还是被封，白敲一次。
RISK_LABELS = {
    "import": "导入标记",
    "verify": "验证发现",
    "manual": "手工加入",
}

#: 导入时自动进复检名单的文本特征。
#:
#: 对齐 banned-retry.mjs 的「扫 /status 自动识别」那条路径：来源系统（hub /
#: manager / panel）把禁用原因写成 "banned by upstream" 的，导进来就该进名单。
#:
#: 只收风控措辞，绝不能把 "manual disable" 也扫进来——那是用户自己手动停用的
#: 健康号，拖去敲门既浪费一次真实请求，也可能给它招新的风控。banned-retry
#: 特意维护独立名单就是为了避开这个坑，这里用特征词达成同样效果。
BANNED_HINTS = (
    "banned by upstream",
    "request illegal",
    "11140",
    "账号存在异常行为",
    "请求被拦截",
    "account has been restricted",
    "unusual activity",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL,
    realm        TEXT NOT NULL DEFAULT 'intl',
    name         TEXT,
    nickname     TEXT,
    email        TEXT,
    phone        TEXT,
    access_token TEXT,
    refresh_token TEXT,
    device_token TEXT,
    token_state  TEXT,
    expires_at   INTEGER DEFAULT 0,
    domain       TEXT,
    enterprise_id TEXT,
    enabled      INTEGER DEFAULT 1,
    source       TEXT,
    payload      TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    -- 探针回填：上游实测值，与 payload 里导入时带的 credits 是两回事。
    -- 导入的 credits 是来源系统上次拉取的快照，可能是几周前的；这三列只由
    -- wk/probe.py 写，NULL 表示没查过（不是 0 余额）。
    credit_remain REAL,
    credit_used   REAL,
    credit_size   REAL,
    credit_state  TEXT,
    checked_at    INTEGER DEFAULT 0,
    checked_note  TEXT,
    -- 封号复检（见 wk/recheck.py）。recheck_state 为空串 = 不在名单里。
    -- 判据与探针分开：一个号可以积分正常（credit_state=valid）但对话被
    -- 风控拦掉，那种号只有这里能标出来。
    recheck_state TEXT DEFAULT '',
    recheck_count INTEGER DEFAULT 0,
    recheck_at    INTEGER DEFAULT 0,
    recheck_note  TEXT,
    -- 导入批次。batch/imported_at 是「最近一次」导入，first_* 是「第一次」，
    -- 重复导入同一份文件不会把 first_* 冲掉 —— 想找「最早那批导进来的」
    -- 只能靠它。时间都是北京时间字符串（见 wk/clock.py）。
    batch             TEXT,
    imported_at       TEXT,
    first_batch       TEXT,
    first_imported_at TEXT,
    -- 进风控区的来源：'import' 导入时明确标了风控，'verify' 后续验证发现，
    -- '' 表示不在风控区。和 recheck_state 分开存，是因为「怎么进来的」
    -- 决定它该不该被自动复检——导入时就标了风控的号，复检多半也白搭。
    risk_src TEXT DEFAULT '',
    -- 复检出口：这是 mihomo 的 listener 端口（0 = 直连，不下发代理）。
    -- 存端口而不是代理 URL，是因为池子重排时端口是稳定坐标，节点名会变；
    -- 而且用户是按端口绑账号的（一个端口 = 一个固定出口）。
    exit_port INTEGER DEFAULT 0,
    -- 官网登录时选的精确代理 URL。端口表里的项由前端按 mihomo.host 拼好，
    -- 手填的可能是 http://127.0.0.1:7891 / 带认证的独立代理，拼不回端口；
    -- 这一个字段存原串，出站和导出都优先用它。空串 = 没绑，按 exit_port 走。
    exit_proxy TEXT DEFAULT '',
    UNIQUE (uid, realm)
);
CREATE INDEX IF NOT EXISTS idx_accounts_realm ON accounts(realm);
CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name);
CREATE INDEX IF NOT EXISTS idx_accounts_updated ON accounts(updated_at DESC);

CREATE TABLE IF NOT EXISTS import_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch      TEXT NOT NULL,
    source     TEXT,
    total      INTEGER DEFAULT 0,
    inserted   INTEGER DEFAULT 0,
    updated    INTEGER DEFAULT 0,
    skipped    INTEGER DEFAULT 0,
    detail     TEXT,
    created_at TEXT
);

-- 每日活跃的尝试历史（见 wk/activity.py）。一次尝试一行，不覆盖：
-- 「今天跑没跑成」要能回看过去几天，单列 last_daily 只会留下最后一次，
-- 失败原因第二天就没了。
CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id       INTEGER DEFAULT 0,
    uid          TEXT,
    name         TEXT,
    realm        TEXT,
    at           INTEGER DEFAULT 0,
    ok           INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    mode         TEXT,
    phase        TEXT,
    status       INTEGER DEFAULT 0,
    model        TEXT,
    proxy        TEXT,
    elapsed_ms   INTEGER DEFAULT 0,
    conversation TEXT,
    detail       TEXT,
    trigger      TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_row ON activity(row_id, at DESC);
"""


def _now() -> str:
    """库里所有时间列的默认值：北京时间字符串。"""
    return clock.stamp()


def _migrate(conn: sqlite3.Connection) -> None:
    """把老库迁到当前结构。

    v1 → v2：唯一键从 (name, realm) 换成 (uid, realm)。
    v2 → v3：补探针列。SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已有表加列，
            所以必须显式 ALTER；漏了这一步，老库升级后每次写入都会
            "no column named credit_remain"。

    v1 按名称合并过，信息已经丢了——只能按现有 uid 重新去重。这里保留最新
    一条，其余丢弃，并在 import_log 里留一条迁移记录，避免「怎么少了几条」
    变成无头案。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if version >= SCHEMA_VERSION and cols:
        return

    if cols and "uid" not in cols:
        conn.execute("DROP TABLE IF EXISTS accounts")
        cols = set()

    conn.executescript(SCHEMA)

    # ---- v2 → v3：补列 ----
    # 逐列判断而不是按版本号，是因为有人可能跑过中间版本、也可能手工改过库，
    # 按实际缺什么补什么最稳。
    extra_cols = {
        "credit_remain": "REAL",
        "credit_used": "REAL",
        "credit_size": "REAL",
        "credit_state": "TEXT",
        "checked_at": "INTEGER DEFAULT 0",
        "checked_note": "TEXT",
        # ---- v3 → v4：封号复检 ----
        # recheck_state 的默认值给空串而不是 NULL：查询里到处是
        # `WHERE recheck_state = 'banned'`，NULL 参与比较永远是假，会让
        # 老库里那些没复检过的号彻底查不出来。
        "recheck_state": "TEXT DEFAULT ''",
        "recheck_count": "INTEGER DEFAULT 0",
        "recheck_at": "INTEGER DEFAULT 0",
        "recheck_note": "TEXT",
        # ---- v4 → v5：导入批次 + 风控来源 ----
        # 老库这两列是空的：不知道历史上那批是什么时候导的，也不该瞎猜。
        # 界面遇空值显示「—」，不拿 updated_at 冒充导入时间。
        "batch": "TEXT",
        "imported_at": "TEXT",
        "first_batch": "TEXT",
        "first_imported_at": "TEXT",
        "risk_src": "TEXT DEFAULT ''",
        # ---- v5 → v6：复检出口端口 ----
        # 老库全 0，等于「没分配」，复检按直连走 —— 这正是升级前没配过
        # 出口的人本来在做的事。
        "exit_port": "INTEGER DEFAULT 0",
        # ---- v7 → v8：官网登录时绑定的精确代理 URL ----
        # 老库全空，行为与升级前完全一致（按 exit_port 走）。
        "exit_proxy": "TEXT DEFAULT ''",
    }
    current = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    for name, decl in extra_cols.items():
        if name not in current:
            try:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")
            except sqlite3.Error:
                # 并发打开时可能被别人先加了；真加不上也别让整个打开失败，
                # 后面的 upsert 会显式带列，缺列会立刻报出来而不是静默丢数据。
                pass

    if version < 2 and cols:
        # 老表存在且列齐：按 (uid, realm) 去重，留 updated_at 最新的一条
        try:
            conn.execute("""
                DELETE FROM accounts WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY uid, realm ORDER BY updated_at DESC, id DESC
                        ) AS rn FROM accounts
                    ) WHERE rn = 1
                )
            """)
        except sqlite3.Error:
            pass

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def account_name(acc: Account) -> str:
    """展示名。优先级 note > nickname > email > phone > uid。

    note 是用户自己写的备注（最可能是他认得的名字），uid 是一串 UUID
    （最不认得）。名称只用于显示和搜索，不参与唯一性判断。
    """
    for value in (acc.note, acc.nickname, acc.email, acc.phone, acc.uid):
        text = str(value or "").strip()
        if text:
            return text
    return "未命名"


def banned_hint_of(acc: Account) -> str:
    """从导入字段里找「来源系统已经把它标成封号」的证据。

    字段名各家不同（reason / disabled_reason / note / 整个 payload），按名字
    猜一定会漏，所以直接把这几处拼起来扫特征词，命中就把关键词附近挖出来
    给界面显示——不把整份 payload 塞进提示里。
    """
    chunks: list[str] = []
    for value in (acc.note, acc.extra.get("reason") if isinstance(acc.extra, dict) else "",
                  acc.extra.get("disabled_reason") if isinstance(acc.extra, dict) else ""):
        if value:
            chunks.append(str(value))
    if isinstance(acc.extra, dict):
        raw = acc.extra.get("_raw")
        if isinstance(raw, str):
            chunks.append(raw)
    text = " ".join(chunks)
    low = text.lower()
    for hint in BANNED_HINTS:
        pos = low.find(hint.lower())
        if pos >= 0:
            start = max(0, pos - 20)
            return text[start:pos + len(hint) + 30].strip()
    return ""


def identity_of(acc: Account) -> str:
    """唯一身份 = uid。uid 缺失时用 token 指纹兜底，保证仍然能落地。

    JWT 能取到 uid 的场合 pipeline 已经补过；到这里还空着，说明是静态 Key
    或非 JWT token，用 access_token 的 sha256 前 16 位当稳定标识——
    同一个 token 重复导入仍然命中同一条，不会每次新增。
    """
    uid = str(acc.uid or "").strip()
    if uid:
        return uid
    token = str(acc.access_token or "").strip()
    if token:
        return "sha256-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return "unnamed-" + hashlib.sha256(
        f"{acc.nickname}|{acc.email}|{acc.phone}".encode("utf-8")
    ).hexdigest()[:12]


def _realm_of(acc: Account) -> str:
    """版本只有两种取值。缺了就按 token 推断，推断不出按 intl。"""
    realm = str(acc.realm or "").strip().lower()
    if realm in ("cn", "intl"):
        return realm
    return tok.detect_realm(acc.access_token, acc.domain, acc.api_key)


def upsert(accounts: Iterable[Account], batch: str = "", source: str = "",
           risk: bool = False, risk_map: dict[str, bool] | None = None) -> dict[str, Any]:
    """按 (uid, realm) 写入或更新。返回新增/更新/跳过的计数。

    同一批里出现同一 uid 两次时，后一条覆盖前一条——「导入顺序即优先级」，
    也避免批内自己跟自己打架。

    `batch` 是这一批的批次号，写进每条记录（`batch` 存最近一次，`first_batch`
    存第一次）：账户库要能按批次筛、按批次导，靠的就是它。

    `risk=True` 表示这批账号导入前就已知处于风控状态，直接进风控区。
    不传就是正常账号。

    逐行覆盖用 `risk_map = {uid: True/False}`：True 把这行标成风控，False
    把这行**从风控区拿出来**（导入页里单行点「正常」用的就是它）。没在表里
    的行走整批的 `risk`。两种粒度并存是因为现实里两种都会出现：整批从风控
    系统导出的，和一批里混了两三个已知封号的。

    **导入字段里的风控措辞不再决定名单状态。** 以前 `banned_hint_of` 一命中
    就把整条塞进 banned：来源系统备注里带一句「请求被拦截」，整批号都会被
    拖进风控区，正常号跟着一起被当风控处理。现在措辞只作为备注证据留档，
    进不进风控区由调用方显式传 `risk` / `risk_map`，或者由后续验证
    （复检 / 探针）写回。
    """
    stats = {"total": 0, "inserted": 0, "updated": 0, "skipped": 0,
             "risk": 0, "items": []}
    batch = batch or "batch-" + clock.compact()
    now = _now()
    marks = risk_map or {}

    with connect() as conn:
        for acc in accounts:
            stats["total"] += 1
            if not acc.has_token and not acc.is_envelope:
                stats["skipped"] += 1
                continue

            uid = identity_of(acc)
            realm = _realm_of(acc)
            name = account_name(acc)
            state = tok.token_state(acc.access_token, acc.expires_at, acc.refresh_token)
            payload = json.dumps(acc.to_dict(), ensure_ascii=False)
            # 只当证据用，不再据此改状态
            hint = banned_hint_of(acc)
            # 逐行标记优先于整批开关；False 是「显式正常」，不是「没意见」
            explicit = uid in marks
            row_risk = bool(marks[uid]) if explicit else bool(risk)
            mark = "banned" if row_risk else ""
            src = "import" if row_risk else ""
            if row_risk:
                stats["risk"] += 1

            row = conn.execute(
                "SELECT id, access_token, recheck_state, IFNULL(risk_src,'') AS risk_src "
                "FROM accounts WHERE uid = ? AND realm = ?",
                (uid, realm),
            ).fetchone()

            if row is None:
                conn.execute(
                    """INSERT INTO accounts
                       (uid, realm, name, nickname, email, phone, access_token, refresh_token,
                        device_token, token_state, expires_at, domain, enterprise_id, enabled,
                        source, payload, created_at, updated_at,
                        recheck_state, recheck_count, recheck_at, recheck_note,
                        batch, imported_at, first_batch, first_imported_at, risk_src)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, realm, name, acc.nickname, acc.email, acc.phone,
                     acc.access_token, acc.refresh_token, acc.device_token, state,
                     acc.expires_at, acc.domain, acc.enterprise_id, int(acc.enabled),
                     source or acc.source, payload, now, now,
                     mark, 0, 0, (hint[:200] or None) if row_risk else None,
                     batch, now, batch, now, src),
                )
                stats["inserted"] += 1
                action = "inserted"
            else:
                # 已有状态优先，除非这一批（或这一行）明确给了风控判断。不能
                # 无条件用新值覆盖：复检结果（revived / auth）比导入动作新，
                # 覆盖会把刚解封的号重新打回 banned。
                current_track = (row["recheck_state"] or "") if "recheck_state" in row.keys() else ""
                current_src = (row["risk_src"] or "") if "risk_src" in row.keys() else ""
                if explicit:
                    # 用户在这一行上按了「风控」/「正常」，就是要把状态改过来，
                    # 包括把 banned 清掉——这是唯一能手动移出风控区的入口。
                    new_track = "banned" if row_risk else ""
                    new_src = "import" if row_risk else ""
                elif risk:
                    new_track, new_src = "banned", "import"
                else:
                    new_track, new_src = current_track, current_src
                # token 换了就作废旧的校验结果：同一 uid 重新扫码后拿到的是新
                # 凭据，旧积分属于上一个 token，留着会让人以为「刚校验过」。
                # token 没变（重复导入同一份文件）则保留，省一次上游查询。
                same_token = str(row["access_token"] or "") == str(acc.access_token or "")
                common = (name, acc.nickname, acc.email, acc.phone,
                          acc.access_token, acc.refresh_token, acc.device_token, state,
                          acc.expires_at, acc.domain, acc.enterprise_id, int(acc.enabled),
                          source or acc.source, payload, now,
                          new_track, (hint[:200] or None) if row_risk else None,
                          batch, now, batch, now, new_src)
                if same_token:
                    conn.execute(
                        """UPDATE accounts SET
                             name=?, nickname=?, email=?, phone=?, access_token=?, refresh_token=?,
                             device_token=?, token_state=?, expires_at=?, domain=?, enterprise_id=?,
                             enabled=?, source=?, payload=?, updated_at=?,
                             recheck_state=?, recheck_note=COALESCE(?, recheck_note),
                             batch=?, imported_at=?,
                             first_batch=COALESCE(first_batch, ?),
                             first_imported_at=COALESCE(first_imported_at, ?),
                             risk_src=?
                           WHERE id=?""",
                        common + (row["id"],),
                    )
                else:
                    conn.execute(
                        """UPDATE accounts SET
                             name=?, nickname=?, email=?, phone=?, access_token=?, refresh_token=?,
                             device_token=?, token_state=?, expires_at=?, domain=?, enterprise_id=?,
                             enabled=?, source=?, payload=?, updated_at=?,
                             recheck_state=?, recheck_note=COALESCE(?, recheck_note),
                             credit_remain=NULL, credit_used=NULL, credit_size=NULL,
                             credit_state=NULL, checked_at=0, checked_note=NULL,
                             batch=?, imported_at=?,
                             first_batch=COALESCE(first_batch, ?),
                             first_imported_at=COALESCE(first_imported_at, ?),
                             risk_src=?
                           WHERE id=?""",
                        common + (row["id"],),
                    )
                stats["updated"] += 1
                action = "updated"

            stats["items"].append({
                "uid": uid,
                "name": name,
                "realm": realm,
                "action": action,
                "state": state,
                "risk": row_risk,
            })

        conn.execute(
            """INSERT INTO import_log (batch, source, total, inserted, updated, skipped, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (batch, source, stats["total"], stats["inserted"], stats["updated"],
             stats["skipped"], json.dumps(stats["items"], ensure_ascii=False), now),
        )

    return stats


def batches(limit: int = 50) -> list[dict]:
    """批次列表，新的在前。账户库的批次筛选下拉用它。

    计数从 accounts 表算，不从 import_log 算：同一条记录重复导入只算一次，
    用户点「这批」想看的是「这批导进来、现在还在库里的有几个」。
    """
    limit = max(1, min(int(limit or 50), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT IFNULL(batch,'') AS b, COUNT(*) AS n, "
            "       MAX(IFNULL(imported_at,'')) AS at "
            "FROM accounts WHERE IFNULL(batch,'') <> '' "
            "GROUP BY b ORDER BY at DESC, b DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"batch": r["b"], "count": r["n"], "at": r["at"] or ""} for r in rows]


def row_by_id(row_id: int) -> dict[str, Any] | None:
    """按行 id 取一条最小摘要。「已有账户重新登录」用它认目标行。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, uid, realm, name, nickname, email, "
            "IFNULL(exit_port,0) AS exit_port, "
            "IFNULL(exit_proxy,'') AS exit_proxy FROM accounts WHERE id = ?",
            (int(row_id),),
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row["id"]), "uid": row["uid"] or "",
            "realm": row["realm"] or "", "name": row["name"] or "",
            "nickname": row["nickname"] or "", "email": row["email"] or "",
            "exit_port": int(row["exit_port"] or 0),
            "exit_proxy": str(row["exit_proxy"] or "")}


def rebind(row_id: int, acc: Account, batch: str = "",
           source: str = "") -> dict[str, Any]:
    """按行 id 用新凭据覆盖这一行（「已有账户重新登录」）。

    `upsert` 按 (uid, realm) 找人；重新登录后上游可能给出不同的 uid / 区域，
    按唯一键会插出新行，用户原来设的出口端口、批次、勾选记录、风控结论全丢。
    这里直接认行 id：身份和凭据字段随新 token 更新，其余元数据原样保留。

    token 换了就作废旧积分快照（与 `upsert` 同口径）：旧余额属于上一个
    token。风控状态不由这里改——重新登录不自动判定解封，仍由复检/校验决定。
    """
    with connect() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE id = ?",
                           (int(row_id),)).fetchone()
        if row is None:
            return {"updated": 0, "inserted": 0, "id": 0, "total": 0}
        now = _now()
        batch = batch or "oauth-" + clock.compact()
        payload = json.dumps(acc.to_dict(), ensure_ascii=False)
        state = tok.token_state(acc.access_token, acc.expires_at, acc.refresh_token)
        conn.execute(
            """UPDATE accounts SET
                 uid=?, realm=?, name=?, nickname=?, email=?, phone=?,
                 access_token=?, refresh_token=?, device_token=?, token_state=?,
                 expires_at=?, domain=?, enterprise_id=?, enabled=?, source=?,
                 payload=?, updated_at=?, batch=?, imported_at=?,
                 credit_remain=NULL, credit_used=NULL, credit_size=NULL,
                 credit_state=NULL, checked_at=0, checked_note=NULL
               WHERE id=?""",
            (identity_of(acc), _realm_of(acc), account_name(acc), acc.nickname,
             acc.email, acc.phone, acc.access_token, acc.refresh_token,
             acc.device_token, state, acc.expires_at, acc.domain,
             acc.enterprise_id, int(acc.enabled), source or acc.source,
             payload, now, batch, now, int(row_id)),
        )
        return {"updated": 1, "inserted": 0, "id": int(row_id), "total": 1}


def _payload_bool(payload: str, key: str, default: bool = True) -> bool:
    """从账户快照里读一个布尔开关。

    payload 是完整 Account.to_dict() 的 JSON，字段名是 snake_case。旧库、
    手工导入、别的写入方都可能缺这个键或写成字符串，一律按缺省值兜住，
    不让一条脏数据把整个列表接口搞挂。
    """
    try:
        data = json.loads(payload or "{}")
    except (ValueError, TypeError):
        return default
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


#: 搜索时用来把 `recheck_state` 映射成「人话」的 SQL 片段。
#:
#: 用户搜「风控」时不需要记住四个档位分别叫什么，搜「封号」「解封」「待复检」
#: 也能直接落到对应档位；不在名单里的号则统一挂「正常」。放在 SQL 里而不是
#: Python 里过滤，是因为分页/排序都在 SQL 侧，捞出来再筛会把 LIMIT 用歪。
_ZONE_WORDS = """(CASE IFNULL(recheck_state,'')
    WHEN 'banned'  THEN '风控 风控账户 封号 封号中 banned'
    WHEN 'pending' THEN '风控 风控账户 待复检 待确认 pending'
    WHEN 'revived' THEN '风控 风控账户 解封 已解封 revived'
    WHEN 'auth'    THEN '风控 风控账户 凭据失效 auth'
    WHEN 'unknown' THEN '风控 风控账户 未确认 unknown'
    ELSE '正常 正常账户 normal' END)"""


def list_accounts(realm: str = "", query: str = "", limit: int = 500,
                  track: str = "", batch: str = "", zone: str = "") -> list[dict]:
    """按版本 / 风控分区 / 批次筛选 + 关键字搜索。返回给界面的行（token 已脱敏）。

    `track` 按复检名单状态筛：传 "banned" 就是「封号复检」标签页要的那批。
    `zone` 按两分区筛：`normal` = 不在复检名单里（正常账户），`risk` = 名单里
    的任意档位（风控账户）。两者同时给时以 AND 叠加，界面不会这么传。
    `batch` 按导入批次筛：同一批导进来的号一次性全捞出来（对账、整批重打、
    整批删除都靠它）。
    用参数而不是另写一个查询，是为了让排序、脱敏、字段组装只有一份实现——
    两份实现迟早会漂移，界面上就会出现某个字段在这里有、那里没有。
    """
    sql = "SELECT * FROM accounts WHERE 1=1"
    args: list[Any] = []
    if realm in ("cn", "intl"):
        sql += " AND realm = ?"
        args.append(realm)
    if track:
        # 老库刚打开、ALTER 还没跑到时没有这列，用 IFNULL 也救不了；
        # 那种情况由 _migrate 负责，这里只管正常路径。
        sql += " AND IFNULL(recheck_state,'') = ?"
        args.append(track)
    if zone == "normal":
        # 空串 = 不在名单里。NULL 也按正常账户算：老库 ALTER 前可能是 NULL。
        sql += " AND IFNULL(recheck_state,'') = ''"
    elif zone == "risk":
        sql += " AND IFNULL(recheck_state,'') != ''"
    if batch:
        sql += " AND IFNULL(batch,'') = ?"
        args.append(batch)
    if query:
        # 状态词和名称/uid/邮箱/批次一起进 LIKE：搜「风控」= 只看风控区，
        # 搜「正常」= 只看正常账户；分区筛选之上再叠一层也不会错位。
        sql += (" AND (name LIKE ? OR uid LIKE ? OR nickname LIKE ? OR email LIKE ?"
                " OR IFNULL(batch,'') LIKE ?"
                " OR " + _ZONE_WORDS + " LIKE ?)")
        like = f"%{query}%"
        args.extend([like, like, like, like, like, like])
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()

    out = []
    for row in rows:
        state = row["token_state"] or tok.token_state(
            row["access_token"] or "", row["expires_at"] or 0, row["refresh_token"] or ""
        )
        # 探针列可能是 NULL（没查过）——sqlite3.Row 缺列时也不会报，这里用
        # keys() 兜一下，兼容升级前的旧库刚打开、ALTER 还没跑到的瞬间。
        keys = row.keys()
        # hub 的账户级开关叫 tasksEnabled，本项目存成 Account.tasks_enabled。
        # 只有国际版用它决定「每日活跃」；国内版也给个值，界面自己判断不显示。
        daily = _payload_bool(row["payload"] if "payload" in keys else "",
                              "tasks_enabled", True)
        credit_state = (row["credit_state"] if "credit_state" in keys else "") or ""
        checked_at = int((row["checked_at"] if "checked_at" in keys else 0) or 0)
        recheck_state = (row["recheck_state"] if "recheck_state" in keys else "") or ""
        recheck_count = int((row["recheck_count"] if "recheck_count" in keys else 0) or 0)
        recheck_at = int((row["recheck_at"] if "recheck_at" in keys else 0) or 0)
        row_batch = (row["batch"] if "batch" in keys else "") or ""
        first_batch = (row["first_batch"] if "first_batch" in keys else "") or ""
        out.append({
            "id": row["id"],
            "name": row["name"],
            "realm": row["realm"],
            "realm_label": tok.REALM_LABEL.get(row["realm"], "未知"),
            "uid": row["uid"] or "",
            "nickname": row["nickname"] or "",
            "email": row["email"] or row["phone"] or "",
            "state": state,
            "state_label": tok.STATE_LABEL.get(state, state),
            "expires_at": row["expires_at"] or 0,
            # 到期日是上游给的时间戳，也按北京时间算，否则「今天到期」会在
            # 早上 8 点之前判错一天
            "expires": clock.stamp(row["expires_at"], "%Y-%m-%d")
                       if row["expires_at"] else "",
            "at": _mask(row["access_token"] or ""),
            "has_rt": bool(row["refresh_token"]),
            "source": row["source"] or "",
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
            # ---- 导入批次 ----
            "batch": row_batch,
            "imported_at": (row["imported_at"] if "imported_at" in keys else "") or "",
            "first_batch": first_batch,
            "first_imported_at": (row["first_imported_at"] if "first_imported_at" in keys else "") or "",
            "risk_src": (row["risk_src"] if "risk_src" in keys else "") or "",
            "risk_label": RISK_LABELS.get((row["risk_src"] if "risk_src" in keys else "") or "", ""),
            # ---- 探针 ----
            # None 原样透传：前端要能区分「没查过」和「0」
            "credit_remain": row["credit_remain"] if "credit_remain" in keys else None,
            "credit_used": row["credit_used"] if "credit_used" in keys else None,
            "credit_size": row["credit_size"] if "credit_size" in keys else None,
            "credit_state": credit_state,
            "credit_state_label": PROBE_LABELS.get(credit_state, ""),
            "checked_at": checked_at,
            "checked": clock.stamp(checked_at, "%m-%d %H:%M") if checked_at else "",
            "checked_note": (row["checked_note"] if "checked_note" in keys else "") or "",
            # ---- 封号复检 ----
            # zone 是两分区的单一事实来源：前端「正常 / 风控」筛选、图标、
            # 下拉分组都读它，不各自按 recheck_state 再推一遍。
            "zone": "risk" if recheck_state else "normal",
            "zone_label": "风控账户" if recheck_state else "正常账户",
            "recheck_state": recheck_state,
            "recheck_label": RECHECK_LABELS.get(recheck_state, ""),
            "recheck_count": recheck_count,
            "recheck_at": recheck_at,
            "recheck": clock.stamp(recheck_at, "%m-%d %H:%M") if recheck_at else "",
            "recheck_note": (row["recheck_note"] if "recheck_note" in keys else "") or "",
            # 出口端口：0 = 直连。节点名由前端拿端口去 nodes 表里查，
            # store 不认识 mihomo，别在这里硬塞一份名单。
            "exit_port": int((row["exit_port"] if "exit_port" in keys else 0) or 0),
            # 官网登录时绑的精确代理 URL。界面要能显示「这行不是直连」，
            # 否则 exit_port=0 的手填代理号看起来跟真的直连一模一样。
            "exit_proxy": str((row["exit_proxy"] if "exit_proxy" in keys else "") or ""),
            # 每日活跃（hub: tasksEnabled）。默认开，与 hub 的账户缺省一致。
            "daily_active": daily,
        })
    return out


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:8] + "…" + value[-4:] if len(value) > 14 else "*" * len(value)


def stats() -> dict[str, Any]:
    """面板顶部的汇总。"""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        by_realm = conn.execute(
            "SELECT realm, COUNT(*) AS n FROM accounts GROUP BY realm"
        ).fetchall()
        by_state = conn.execute(
            "SELECT token_state AS s, COUNT(*) AS n FROM accounts GROUP BY token_state"
        ).fetchall()
        last = conn.execute(
            "SELECT batch, source, total, inserted, updated, skipped, created_at "
            "FROM import_log ORDER BY id DESC LIMIT 5"
        ).fetchall()
        zones = conn.execute(
            "SELECT CASE WHEN IFNULL(recheck_state,'') = '' THEN 'normal' "
            "ELSE 'risk' END AS z, COUNT(*) AS n FROM accounts GROUP BY z"
        ).fetchall()

    zone_counts = {"normal": 0, "risk": 0}
    for row in zones:
        if row["z"] in zone_counts:
            zone_counts[row["z"]] = row["n"]

    return {
        "total": total,
        # 分区计数是整库的，不随当前 realm/batch/搜索变：分段按钮显示的是
        # 「库里有多少正常号 / 多少风控号」，跟着筛选变会让人以为号丢了。
        "zones": zone_counts,
        "realms": [
            {"key": r["realm"], "label": tok.REALM_LABEL.get(r["realm"], "未知"), "count": r["n"]}
            for r in by_realm
        ],
        "states": [
            {"key": r["s"] or "unknown",
             "label": tok.STATE_LABEL.get(r["s"] or "unknown", r["s"] or "未知"),
             "count": r["n"]}
            for r in by_state
        ],
        "history": [
            {"batch": r["batch"], "source": r["source"], "total": r["total"],
             "inserted": r["inserted"], "updated": r["updated"],
             "skipped": r["skipped"], "at": r["created_at"]}
            for r in last
        ],
        # 批次下拉的候选。limit 50 够用：一批几百条的人不会攒出 50 批还不清理，
        # 真攒到了也该按时间翻，不该把下拉塞成几百项。
        "batches": batches(50),
        "db_path": DB_PATH,
    }


def picker_rows(track: str = "banned", query: str = "",
                limit: int = 2000, zone: str = "") -> list[dict]:
    """「把账号加进名单」下拉的数据源。

    只排除**已经在目标档位**的账号：在别的档位的、以及压根不在名单里的都要
    能选。以前按「recheck_state 非空」一刀切，用户把号从正常区挪进风控区
    之后，名单里看不到、下拉里也选不到，看起来像被删了。

    `zone` 让用户把搜索范围限定在正常账户 / 风控账户里；每行都带 `zone`，
    前端按它分两组显示，混在一串 option 里靠后缀分辨太费眼。

    账户库默认只给 500 行，库一大就有一半号永远进不了这个下拉；这里单独
    放宽到 2000 并支持按名称 / uid / 邮箱 / 状态词搜。复杂度 O(n)，
    n = 库里行数，瓶颈在 list_accounts 的整表脱敏，够用到几千条；
    再大就该走分页了。
    """
    rows = list_accounts(query=query, limit=max(1, min(int(limit or 2000), 5000)),
                         zone=zone)
    out = []
    for r in rows:
        state = r["recheck_state"] or ""
        if state == track:
            continue
        zone_key = "risk" if state else "normal"
        out.append({
            "id": r["id"],
            "name": r["name"],
            "uid": r["uid"],
            "email": r["email"],
            "realm_label": r["realm_label"],
            "state": state,
            "state_label": RECHECK_LABELS.get(state, "正常账号"),
            "zone": zone_key,
            "zone_label": "风控账户" if zone_key == "risk" else "正常账户",
        })
    return out


def recheck_stats() -> dict[str, Any]:
    """复检名单的计数。标签页徽章与设置卡片用。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT IFNULL(recheck_state,'') AS s, COUNT(*) AS n FROM accounts GROUP BY s"
        ).fetchall()
    counts = {key: 0 for key in ("pending", "banned", "revived", "auth")}
    for row in rows:
        key = row["s"] or ""
        if key in counts:
            counts[key] = row["n"]
    return {**counts, "tracked": sum(counts.values())}


def load_for_export(realm: str = "", ids: list[int] | None = None,
                    with_ids: bool = False):
    """把库里的记录还原成 Account，供导出渲染。

    payload 里存的是完整 Account 快照，直接反序列化即可——比逐列重建可靠，
    也不会漏掉后续版本新增的字段。

    ``with_ids=True`` 额外返回 ``(row_id, Account, exit_port)`` 三元组：
    exit_port 是 DB 独立列、不进 payload，但导出时要按它给每个账户写各自的
    proxy（见 wk/exitproxy.py），所以这里一次 SQL 带出来，别让调用方再查
    一遍库靠 uid 对齐——uid 可能重复，row_id 才是唯一身份。
    """
    sql = "SELECT id, payload, IFNULL(exit_port,0) AS exit_port, " \
          "IFNULL(exit_proxy,'') AS exit_proxy FROM accounts WHERE 1=1"
    args: list[Any] = []
    if realm in ("cn", "intl"):
        sql += " AND realm = ?"
        args.append(realm)
    if ids:
        placeholders = ",".join("?" * len(ids))
        sql += f" AND id IN ({placeholders})"
        args.extend(ids)
    sql += " ORDER BY id"

    out: list[Any] = []
    with connect() as conn:
        for row in conn.execute(sql, args).fetchall():
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            acc = _account_from_payload(data)
            # 行级出口绑定不进 payload：出站和导出都从 DB 列读，挂到对象上
            # 供调用方使用（Account.to_dict 会剥掉它，不会泄漏到账户文件）。
            acc.exit_proxy = str(row["exit_proxy"] or "")
            if with_ids:
                out.append((int(row["id"]), acc, int(row["exit_port"] or 0)))
            else:
                out.append(acc)
    return out


def _account_from_payload(data: dict) -> Account:
    """payload 反序列化。

    只回填 dataclass 真正声明的字段——to_dict() 会附带 has_token / is_envelope
    这类派生属性，直接 setattr 会撞到只读 property。
    """
    from dataclasses import fields

    acc = Account()
    valid = {f.name for f in fields(Account)}
    for key, value in (data or {}).items():
        if key in valid:
            setattr(acc, key, value)
    return acc


def rename(row_id: int, new_name: str) -> dict[str, Any]:
    """改展示名。

    名称不参与唯一性，所以允许重名、也不做合并——改名字只是换个标签，
    绝不该导致另一条账号被删掉。
    """
    new_name = str(new_name or "").strip()
    if not new_name:
        raise ValueError("名称不能为空")

    with connect() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE id = ?", (row_id,)).fetchone()
        if not row:
            raise ValueError("记录不存在")
        conn.execute(
            "UPDATE accounts SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, _now(), row_id),
        )
    return {"ok": True, "id": row_id, "name": new_name}


def load_for_probe(ids: list[int] | None = None, realm: str = "") -> list[tuple[int, Account]]:
    """取出待校验的账号，带行 id。

    和 load_for_export 的区别是它要回填结果，所以必须知道每一条对应哪一行。
    ids 为空表示「库里全部」（可按 realm 再筛一层），界面上「校验全部」走这条。
    """
    sql = "SELECT id, payload, IFNULL(exit_proxy,'') AS exit_proxy, "
    sql += "IFNULL(recheck_state,'') AS recheck_state, "
    sql += "IFNULL(recheck_count,0) AS recheck_count, IFNULL(recheck_at,0) AS recheck_at "
    sql += "FROM accounts WHERE 1=1"
    args: list[Any] = []
    if ids:
        sql += " AND id IN (%s)" % ",".join("?" * len(ids))
        args.extend(int(i) for i in ids)
    if realm in ("cn", "intl"):
        sql += " AND realm = ?"
        args.append(realm)
    sql += " ORDER BY id"
    out: list[tuple[int, Account]] = []
    with connect() as conn:
        for row in conn.execute(sql, args).fetchall():
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            acc = _account_from_payload(data)
            # 官网登录绑的精确代理优先于 exit_port，复检要按它出站
            acc.exit_proxy = str(row["exit_proxy"] or "")
            # 复检状态只活在库里，不在 payload 里（payload 是导入时的快照，
            # 我们后写的状态不该混进去，否则导出的文件会带上本机的复检记录）。
            acc.recheck_state = row["recheck_state"] or ""
            acc.recheck_count = int(row["recheck_count"] or 0)
            acc.recheck_at = int(row["recheck_at"] or 0)
            out.append((int(row["id"]), acc))
    return out


def update_credits(results: list[dict[str, Any]]) -> int:
    """把探针结果写回库。

    results 里每条：{id, state, remain, used, size, tier, checked_at, note, restrict}

    remain / used / size 为 None 时写 NULL 而不是 0——界面上「没查过」和
    「查过了确实是 0」要能分开。这也是为什么这三列不设 DEFAULT 0。

    `restrict=True` 表示这次失败是**账号级风控**（403/11140 那类），不是
    token 过期。这种号同时写进风控区：校验的用处一半就在这里，发现了却
    让它留在正常区，下一轮又得从头再查一遍。
    """
    if not results:
        return 0
    written = 0
    with connect() as conn:
        for item in results:
            row_id = item.get("id")
            if row_id is None:
                continue
            note = str(item.get("note") or "")[:200] or None
            conn.execute(
                """UPDATE accounts SET
                     credit_remain = ?, credit_used = ?, credit_size = ?,
                     credit_state = ?, checked_at = ?, checked_note = ?
                   WHERE id = ?""",
                (
                    item.get("remain"),
                    item.get("used"),
                    item.get("size"),
                    str(item.get("state") or ""),
                    int(item.get("checked_at") or 0),
                    note,
                    int(row_id),
                ),
            )
            if item.get("restrict"):
                # 只从「不在名单」挪到 banned，不回退已有结论：已经解封
                # （revived）的号如果再次被判风控，这里覆盖回 banned 是对的，
                # 但 auth（凭据失效）不该被一条 403 改写成风控。
                row = conn.execute(
                    "SELECT IFNULL(recheck_state,'') AS s, IFNULL(risk_src,'') AS src "
                    "FROM accounts WHERE id = ?", (int(row_id),)
                ).fetchone()
                if row is not None and (row["s"] or "") in ("", "unknown", "revived"):
                    conn.execute(
                        "UPDATE accounts SET recheck_state = 'banned', risk_src = ?,"
                        " recheck_note = COALESCE(recheck_note, ?) WHERE id = ?",
                        (row["src"] or "verify", note, int(row_id)),
                    )
            written += 1
    return written


def update_recheck(results: list[dict[str, Any]]) -> int:
    """把复检结论写回库。

    results 里每条：{id, verdict, detail, raw, checked_at, skipped}

    `skipped`（被最小间隔挡下）不写：它没有产生新的结论，写进去会把
    recheck_count 刷高，下一次判断间隔时就失真了。
    """
    if not results:
        return 0
    from . import recheck as recheck_mod

    written = 0
    with connect() as conn:
        for item in results:
            row_id = item.get("id")
            if row_id is None or item.get("skipped"):
                continue
            verdict = str(item.get("verdict") or "")
            row = conn.execute(
                "SELECT IFNULL(recheck_state,'') AS s, IFNULL(recheck_count,0) AS n, "
                "IFNULL(risk_src,'') AS risk_src "
                "FROM accounts WHERE id = ?", (int(row_id),)
            ).fetchone()
            if row is None:
                continue
            state = recheck_mod.next_track(row["s"] or "", verdict)
            # 400 的结论里带着上游原话和「试过哪几种组合」，200 字会把
            # 定位信息截没；界面结论列本来就把全文挂在 title 上。
            note = str(item.get("detail") or item.get("raw") or "")[:800] or None
            # 「验证发现是风控」要记来源：复检判定仍被封的号写 verify，
            # 导入时就标了 risk 的保留 import（那是更强的证据，不该被覆盖成
            # 一个更弱的来源）。解封后清空——它已经不在风控区了。
            risk_src = row["risk_src"] or ""
            if state == "banned":
                risk_src = risk_src or "verify"
            elif state in ("revived", "auth", ""):
                risk_src = ""
            conn.execute(
                """UPDATE accounts SET
                     recheck_state = ?, recheck_count = ?, recheck_at = ?, recheck_note = ?,
                     risk_src = ?
                   WHERE id = ?""",
                (state, int(row["n"] or 0) + 1,
                 int(item.get("checked_at") or time.time()), note, risk_src,
                 int(row_id)),
            )
            written += 1
    return written


def set_track(ids: list[int], state: str, note: str = "") -> int:
    """手工把账号加进 / 移出复检名单。

    `state=""` 表示移出名单（健康号归位）。这里不校验账号是否真的被封——
    用户手工加进来的号就是要敲门，判断留给复检本身去做。
    """
    if not ids:
        return 0
    if state and state not in RECHECK_LABELS:
        raise ValueError(f"未知的名单状态：{state}")
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        if state:
            # 风控来源记 manual：这批不是导入时标的风控，也不是复检判出来的，
            # 是用户自己划进来的。三种来源分开记，页面上才看得出该不该去敲门。
            # 从「已解封 / 凭据失效」重新划回名单时，旧的敲门次数和时间要
            # 归零：留着会被最小间隔直接跳过，用户刚加进来的号一次都不测。
            # SQLite 的 UPDATE 表达式读的都是旧值，和 SET 的书写顺序无关。
            cur = conn.execute(
                f"UPDATE accounts SET "
                f"recheck_count = CASE WHEN IFNULL(recheck_state,'') IN ('revived','auth') "
                f"                     THEN 0 ELSE recheck_count END, "
                f"recheck_at = CASE WHEN IFNULL(recheck_state,'') IN ('revived','auth') "
                f"                  THEN 0 ELSE recheck_at END, "
                f"recheck_state = ?, "
                f"recheck_note = COALESCE(?, recheck_note), "
                f"risk_src = CASE WHEN IFNULL(risk_src,'') = '' THEN 'manual' "
                f"                 ELSE risk_src END "
                f"WHERE id IN ({placeholders})",
                [state, (note[:200] or None)] + [int(i) for i in ids],
            )
        else:
            # 移出名单时把计数、时间和来源一起清掉：留着会让下一次加入时
            # 立刻被最小间隔挡住，用户会以为按钮没生效。
            cur = conn.execute(
                f"UPDATE accounts SET recheck_state = '', recheck_count = 0, "
                f"recheck_at = 0, recheck_note = NULL, risk_src = '' "
                f"WHERE id IN ({placeholders})",
                [int(i) for i in ids],
            )
        return cur.rowcount or 0


def recheck_ids() -> list[int]:
    """名单内所有行 id。批量分配出口时当默认范围。

    只取在名单里的号：不在名单里的号根本不会被复检，给它分配端口只会
    让界面显示一个永远用不上的数字。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE IFNULL(recheck_state,'') != '' ORDER BY id"
        ).fetchall()
    return [int(r[0]) for r in rows]


def normal_ids() -> list[int]:
    """不在复检名单里的行（正常区）。账户库「一键分配出口」的默认范围。

    风控区那批号由复检页自己分配，两边各管各的：一个端口被两边同时用上，
    就分不清这一条出口上跑的是复检敲门还是积分查询了。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE IFNULL(recheck_state,'') = '' ORDER BY id"
        ).fetchall()
    return [int(r[0]) for r in rows]


def exit_ports_in_use(group: str = "") -> set[int]:
    """某个区正在用的出口端口集合。

    `group="risk"` 只看复检名单里的号，`"normal"` 只看正常号，空串看全库。
    一键分配时拿**对方**的集合去扣可用端口，两边才不会撞到同一个端口上。
    只看 > 0 的值：0 是直连，不是一个槽位。
    """
    sql = ("SELECT DISTINCT IFNULL(exit_port,0) AS p FROM accounts "
           "WHERE IFNULL(exit_port,0) > 0")
    if group == "risk":
        sql += " AND IFNULL(recheck_state,'') != ''"
    elif group == "normal":
        sql += " AND IFNULL(recheck_state,'') = ''"
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return {int(r["p"]) for r in rows}


def set_exit_port(ids: list[int], port: int) -> int:
    """把一批账号的出口钉到同一个端口。`port=0` 改回直连。

    这是用户在账户库里**显式**改出口的入口（逐行下拉、一键分配、全部直连）。
    既然他又选了一次，官网登录时绑的精确代理就作废：不清的话 `exit_proxy`
    会一直压过端口，用户点「直连」也还是从旧代理出去。
    """
    if not ids:
        return 0
    port = max(0, int(port or 0))
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE accounts SET exit_port = ?, exit_proxy = '' "
            f"WHERE id IN ({placeholders})",
            [port] + [int(i) for i in ids],
        )
        return cur.rowcount or 0


def set_login_exit(row_id: int, proxy: str, port: int = 0) -> int:
    """官网登录成功后把这一趟的出口钉到该行。

    `proxy` 存原串（可能是 http://127.0.0.1:7891、带认证的独立代理），
    `port` 只在它是 mihomo listener URL 时给端口（由 exitproxy.port_for_proxy
    算），否则 0。两列一起写，界面「出口」列才不会把手填代理显示成直连。

    `proxy=""` 是用户在弹窗里显式选了「直连」：两列一起清，重登才会真的
    改回直连。前端读取默认值失败、没传 proxy 时不调这个函数，保留原绑定。
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE accounts SET exit_port = ?, exit_proxy = ? WHERE id = ?",
            (max(0, int(port or 0)), str(proxy or "").strip(), int(row_id)),
        )
        return cur.rowcount or 0


def set_daily_active(ids: list[int], enabled: bool) -> dict[str, int]:
    """设置国际版账户的每日活跃开关（hub 里的 `tasksEnabled`）。

    值存在 payload 的 Account 快照里，而不是新开一列：导出 hub 账户文件时
    `to_hub_flat()` 把它渲染成 `tasksEnabled`，导入时 `from_row()` 又读回来，
    来回只有一份状态，不会和别的列漂移。国内版没有每日活跃，直接排除。

    复杂度 O(n)，n = 选中行数；每行一次 JSON 解析 + 一次 UPDATE，批量只在
    界面勾选时触发，不是热路径。
    """
    wanted = [int(i) for i in (ids or [])]
    if not wanted:
        return {"changed": 0, "skipped": 0}
    placeholders = ",".join("?" * len(wanted))
    now = _now()
    with connect() as conn:
        found = conn.execute(
            f"SELECT id, realm, payload FROM accounts WHERE id IN ({placeholders})",
            wanted,
        ).fetchall()
        updates = []
        for row in found:
            if row["realm"] != "intl":
                continue
            try:
                data = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["tasks_enabled"] = bool(enabled)
            updates.append((json.dumps(data, ensure_ascii=False), now, int(row["id"])))
        if updates:
            conn.executemany(
                "UPDATE accounts SET payload = ?, updated_at = ? WHERE id = ?",
                updates,
            )
    return {"changed": len(updates), "skipped": len(found) - len(updates)}


# ------------------------------------------------------------------ 每日活跃历史

#: 历史行的查询白名单。前端传 `range=today|1d|7d|30d|all`，其余一律按 7d。
ACTIVITY_RANGES = {"today": 0, "1d": 1, "7d": 7, "30d": 30, "all": None}


def activity_add(rec: dict[str, Any]) -> dict[str, Any]:
    """写一行活跃尝试，返回落库后的行（含 id 与可读时间）。"""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO activity
               (row_id, uid, name, realm, at, ok, skipped, mode, phase, status,
                model, proxy, elapsed_ms, conversation, detail, trigger)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(rec.get("row_id") or 0), str(rec.get("uid") or ""),
             str(rec.get("name") or ""), str(rec.get("realm") or ""),
             int(rec.get("at") or time.time()), int(rec.get("ok") or 0),
             int(rec.get("skipped") or 0), str(rec.get("mode") or ""),
             str(rec.get("phase") or ""), int(rec.get("status") or 0),
             str(rec.get("model") or ""), str(rec.get("proxy") or ""),
             int(rec.get("elapsed_ms") or 0), str(rec.get("conversation") or ""),
             str(rec.get("detail") or ""), str(rec.get("trigger") or "manual")))
        row_id = cur.lastrowid
    return {"id": row_id, **rec}


def activity_done_today(row_id: int) -> bool:
    """这一行今天有没有成功过。按北京时间零点算，不是滚动 24 小时。"""
    start = clock.day_start()
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM activity WHERE row_id = ? AND ok = 1 AND at >= ? LIMIT 1",
            (int(row_id), int(start))).fetchone()
    return row is not None


def activity_rows(range_key: str = "7d", result: str = "", uid: str = "",
                  row_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    """读历史，最新在前。筛选在 SQL 里做完，前端只渲染。"""
    key = str(range_key or "7d").strip().lower()
    if key not in ACTIVITY_RANGES:
        key = "7d"
    sql = "SELECT * FROM activity WHERE 1=1"
    args: list[Any] = []
    days = ACTIVITY_RANGES[key]
    if key == "today":
        sql += " AND at >= ?"
        args.append(int(clock.day_start()))
    elif days is not None:
        sql += " AND at >= ?"
        args.append(int(time.time()) - days * 86400)
    if result == "ok":
        sql += " AND ok = 1"
    elif result == "failed":
        sql += " AND ok = 0 AND skipped = 0"
    elif result == "skipped":
        sql += " AND skipped = 1"
    if uid:
        sql += " AND uid = ?"
        args.append(str(uid))
    if row_id:
        sql += " AND row_id = ?"
        args.append(int(row_id))
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 1000)))
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["at_text"] = clock.stamp(item.get("at") or 0)
        item["ok"] = bool(item.get("ok"))
        item["skipped"] = bool(item.get("skipped"))
        out.append(item)
    return out


def activity_summary() -> dict[str, int]:
    """今日 + 近 7 天的计数，给界面顶部那排数字用。"""
    day = int(clock.day_start())
    week = int(time.time()) - 7 * 86400
    with connect() as conn:
        def count(since: int, where: str) -> int:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM activity WHERE at >= ? AND {where}",
                (since,)).fetchone()
            return int(row["n"] or 0)
        return {
            "today_ok": count(day, "ok = 1"),
            "today_failed": count(day, "ok = 0 AND skipped = 0"),
            "today_skipped": count(day, "skipped = 1"),
            "week_ok": count(week, "ok = 1"),
            "week_failed": count(week, "ok = 0 AND skipped = 0"),
        }


def activity_last() -> dict[int, dict[str, Any]]:
    """每一行最近一次尝试：{row_id: {ok, at, detail, at_text}}。

    账号列表上要显示「今天跑过没、结果如何」，一次 GROUP BY 取完，
    不在循环里逐行查。
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.row_id, a.ok, a.skipped, a.at, a.detail, a.trigger
               FROM activity a
               JOIN (SELECT row_id, MAX(at) AS last_at FROM activity
                     GROUP BY row_id) m
                 ON a.row_id = m.row_id AND a.at = m.last_at""").fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        out[int(row["row_id"])] = {
            "ok": bool(row["ok"]),
            "skipped": bool(row["skipped"]),
            "at": int(row["at"] or 0),
            "at_text": clock.stamp(row["at"] or 0),
            "detail": row["detail"] or "",
            "trigger": row["trigger"] or "",
        }
    return out


def exit_ports_of(ids: Iterable[int]) -> dict[int, int]:
    """`{行 id: 出口端口}`。复检前查一次，别在循环里逐行查库。"""
    rows = [int(i) for i in ids]
    if not rows:
        return {}
    placeholders = ",".join("?" * len(rows))
    with connect() as conn:
        got = conn.execute(
            f"SELECT id, IFNULL(exit_port,0) AS p FROM accounts "
            f"WHERE id IN ({placeholders})", rows
        ).fetchall()
    return {int(r["id"]): int(r["p"] or 0) for r in got}


def set_exit_map(pairs: Iterable[tuple[int, int]]) -> int:
    """按 `(行 id, 端口)` 逐行写。随机分配走这条——同一批里每行端口不同，
    一条 UPDATE 写不了，但又必须在一个事务里落，否则中途失败会留下半个
    分配结果。"""
    rows = [(max(0, int(port or 0)), int(i)) for i, port in pairs]
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            "UPDATE accounts SET exit_port = ?, exit_proxy = '' WHERE id = ?", rows)
        return len(rows)


def delete(ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        cur = conn.execute(f"DELETE FROM accounts WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def clear() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM import_log")
        return cur.rowcount


# ---------------------------------------------------------------- 备份 / 还原
# 备份包是一个 zip，里面两份东西：
#   accounts.db     —— sqlite 在线备份出来的完整库，还原后连 import_log 都在；
#   accounts.json   —— 明文可读的导出，库文件坏了还能从这份重建。
# 只给 db 的话，跨 sqlite 版本/平台会有兼容风险；只给 json 的话，导入历史丢了。
# 两份一起给，成本几乎为零。

def _sqlite_snapshot(row_ids: list[int] | None = None) -> bytes:
    """用 sqlite 的在线备份 API 取一致性快照。

    直接读文件在写入过程中可能拿到半页数据（尤其 WAL 模式），备份 API 会
    自己处理锁和页边界。

    `row_ids` 不为 None 时只留这些账号：部分备份里如果 db 还是整库，
    换机器的人只拿 accounts.db 还原就会把没选的号一起带回来。
    """
    import tempfile

    with connect() as conn:
        fd, tmp = tempfile.mkstemp(prefix="wk-backup-", suffix=".db")
        os.close(fd)
        try:
            dest = sqlite3.connect(tmp)
            try:
                conn.backup(dest)
                if row_ids is not None:
                    ids = [int(i) for i in row_ids]
                    if ids:
                        marks = ",".join("?" for _ in ids)
                        dest.execute(f"DELETE FROM accounts WHERE id NOT IN ({marks})", ids)
                        try:
                            # 每日活跃历史按 row_id 归属账号，一起裁掉，
                            # 否则留下的是「别人的号跑过活跃」的记录。
                            dest.execute(
                                f"DELETE FROM activity WHERE row_id NOT IN ({marks})", ids)
                        except sqlite3.OperationalError:
                            pass        # 老库可能还没有 activity 表
                    else:
                        dest.execute("DELETE FROM accounts")
                        try:
                            dest.execute("DELETE FROM activity")
                        except sqlite3.OperationalError:
                            pass
                    dest.commit()
                    # DELETE 只把页标成可用，页里被删账号的 token 明文还在文件中。
                    # 不 VACUUM 就等于把筛选外的号物理塞进了备份包。
                    dest.execute("VACUUM")
            finally:
                dest.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def dump_records(row_ids: list[int] | None = None) -> list[dict]:
    """把库里选中的记录原样导出（含完整 payload，token 不脱敏）。

    这是备份，脱敏就没有还原价值了。import_log 是导入历史、不按账号归属，
    部分备份也整份带着（manifest 里写明），清空还原后还能对账。
    """
    where = ""
    args: tuple = ()
    if row_ids is not None:
        ids = [int(i) for i in row_ids]
        where = " WHERE id IN (" + ",".join("?" for _ in ids) + ")" if ids else " WHERE 0"
        args = tuple(ids)

    with connect() as conn:
        rows = conn.execute("SELECT * FROM accounts" + where + " ORDER BY id",
                            args).fetchall()
        logs = conn.execute("SELECT * FROM import_log ORDER BY id").fetchall()

    return [{
        "uid": r["uid"],
        "realm": r["realm"],
        "name": r["name"],
        "token_state": r["token_state"],
        "enabled": r["enabled"],
        "source": r["source"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        # 复检 / 风控 / 批次这些是「导入之后才产生的状态」，payload 里没有。
        # 不一起存的话，还原完风控区就空了——用户刚把要保留的名单备份下来，
        # 一还原全变回正常号，比不备份还糟。
        "batch": r["batch"],
        "imported_at": r["imported_at"],
        "first_batch": r["first_batch"],
        "first_imported_at": r["first_imported_at"],
        "risk_src": r["risk_src"],
        "recheck_state": r["recheck_state"],
        "recheck_count": r["recheck_count"],
        "recheck_at": r["recheck_at"],
        "recheck_note": r["recheck_note"],
        # 出口端口也是导入后配的，和复检状态一样必须跟着备份走
        "exit_port": (r["exit_port"] if "exit_port" in r.keys() else 0) or 0,
        # 官网登录绑的精确代理 URL 同理：漏了它，还原后这行会静默退回直连
        "exit_proxy": (r["exit_proxy"] if "exit_proxy" in r.keys() else "") or "",
        "account": json.loads(r["payload"] or "{}"),
    } for r in rows] + [{
        "__import_log__": True,
        "batch": r["batch"],
        "source": r["source"],
        "total": r["total"],
        "inserted": r["inserted"],
        "updated": r["updated"],
        "skipped": r["skipped"],
        "detail": r["detail"],
        "created_at": r["created_at"],
    } for r in logs]


def backup_scope_ids(scope: str = "all", ids: list[int] | None = None,
                     zone: str = "", realm: str = "", query: str = "",
                     batch: str = "") -> tuple[list[int] | None, dict]:
    """把界面上的「筛选 / 勾选」翻译成 accounts 表的行 id。

    返回 `(row_ids, info)`；`row_ids is None` 表示整库、不裁剪，这是老版本
    「下载备份包」和直接打 GET 接口的默认行为。

    范围规则（和「从库导出」一致，但多一层筛选）：

    - 勾了一部分 → 只备勾的，且必须仍落在当前筛选结果里（筛掉了就不能偷偷
      把看不见的号打进包）；
    - 一条不勾 / 全勾 → 备当前筛选后的可见集；
    - 没有任何筛选、也没勾 → 整库。

    筛选条件在后端按 `list_accounts` 同一套 SQL 重算一遍，不信任前端传来的
    id 列表——前端算错或旧响应残留时，结果仍然和表格上看到的一致。
    O(n)，n = 库内行数；瓶颈是 list_accounts 的整表脱敏，几千条以内够用。
    """
    if scope not in ("all", "filtered", "selected"):
        raise ValueError("scope 只能是 all / filtered / selected")
    filters = {
        "zone": zone if zone in ("normal", "risk") else "",
        "realm": realm if realm in ("cn", "intl") else "",
        "q": (query or "").strip(),
        "batch": (batch or "").strip(),
    }
    total = int(stats().get("total") or 0)
    clean = not any(filters.values())
    if scope == "all" and clean:
        return None, {"mode": "all", "count": total, "total": total,
                      "partial": False, "filters": filters}

    visible = [int(r["id"]) for r in list_accounts(
        realm=filters["realm"], query=filters["q"], batch=filters["batch"],
        zone=filters["zone"], limit=max(1000, total * 2 + 10))]
    if not visible:
        raise ValueError("当前筛选没有可备份的账号")

    if scope == "selected":
        picked = {int(i) for i in (ids or [])}
        if not picked:
            raise ValueError("没有勾选账号")
        chosen = [i for i in visible if i in picked]
        if not chosen:
            raise ValueError("勾选的账号不在当前筛选结果里")
        mode = "selected"
    else:
        chosen = visible
        mode = "filtered"

    return chosen, {"mode": mode, "count": len(chosen), "total": total,
                    "partial": True, "filters": filters}


def backup_bytes(row_ids: list[int] | None = None,
                 scope: dict | None = None) -> tuple[bytes, str]:
    """生成备份 zip，返回 (字节, 建议文件名)。保持二元组，老调用方不用改。"""
    import io
    import zipfile

    stamp = clock.compact()
    records = dump_records(row_ids)
    accounts = [r for r in records if not r.get("__import_log__")]
    logs = [r for r in records if r.get("__import_log__")]

    info = dict(scope or {})
    info.setdefault("mode", "all" if row_ids is None else "selected")
    info.setdefault("count", len(accounts))
    info.setdefault("total", stats().get("total") or 0)
    info.setdefault("partial", row_ids is not None)
    info.setdefault("filters", {"zone": "", "realm": "", "q": "", "batch": ""})
    # 部分备份文件名带范围和条数：下载目录里一眼看出哪份是风控区、哪份是整库。
    suffix = "" if row_ids is None else f"-{info['mode']}-{info['count']}"
    name = f"wk-switch-backup-{stamp}{suffix}.zip"

    manifest = {
        "format": "wk-switch-backup",
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "created_at": clock.stamp(),
        "accounts": len(accounts),
        "import_log": len(logs),
        "import_log_scope": "all",
        "db_path": DB_PATH,
        "scope": info,
    }
    dump = {
        "format": "wk-switch-backup",
        "version": 1,
        "created_at": manifest["created_at"],
        "accounts": accounts,
        "import_log": logs,
        "scope": info,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("accounts.json", json.dumps(dump, ensure_ascii=False, indent=2))
        zf.writestr("accounts.db", _sqlite_snapshot(row_ids))
    return buf.getvalue(), name


#: 备份里要跟着还原的「导入之后产生的状态」列。
#:
#: exit_port 必须在这张表里：它是用户按号配的出口，跟复检结论一样属于
#: 「导入之后才产生的东西」。漏了它，还原会静默把所有人的出口清成直连，
#: 而复检下一次跑就从本机 IP 出去了——正好是最不该发生的那种偏差。
_META_COLS = ("batch", "imported_at", "first_batch", "first_imported_at",
              "risk_src", "recheck_state", "recheck_count", "recheck_at",
              "recheck_note", "exit_port", "exit_proxy")


def _restore_meta(items: list[dict]) -> int:
    """把备份里的复检 / 风控 / 批次状态写回库。

    `upsert` 只认「这次导入」的字段，备份带回来的历史状态得单独盖一遍。
    不盖的话，还原等于把所有号从风控区放出来——备份的意义正好反了。
    """
    written = 0
    with connect() as conn:
        for item in items:
            if not isinstance(item, dict) or item.get("__import_log__"):
                continue
            uid = str(item.get("uid") or "")
            realm = str(item.get("realm") or "")
            if not uid:
                continue
            cols = [c for c in _META_COLS if c in item]
            if not cols:
                continue
            sets = ", ".join(f"{c} = ?" for c in cols)
            values = [item.get(c) for c in cols]
            cur = conn.execute(
                f"UPDATE accounts SET {sets} WHERE uid = ? AND realm = ?",
                values + [uid, realm],
            )
            written += cur.rowcount or 0
    return written


def _restore_from_dump(data: dict, mode: str) -> dict[str, Any]:
    """从 accounts.json 还原。

    mode=merge   按 (uid, realm) 合并，已有记录用备份里的覆盖（默认）
    mode=replace 先清空再写入，等价于「恢复到备份那一刻」
    """
    items = data.get("accounts") or []
    if not isinstance(items, list):
        raise ValueError("备份里的 accounts 不是数组")

    accounts: list[Account] = []
    for item in items:
        raw = item.get("account") if isinstance(item, dict) else None
        if isinstance(raw, dict):
            accounts.append(_account_from_payload(raw))

    if mode == "replace":
        clear()

    # 走 upsert 而不是直接 INSERT：备份里的记录也遵循 (uid, realm) 唯一键，
    # 直接写会撞 UNIQUE 约束。
    stats = upsert(accounts, batch="restore-" + clock.compact(),
                   source="backup")
    meta = _restore_meta(items)
    return {
        "mode": mode,
        "restored": len(accounts),
        "meta": meta,
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
    }


def restore_bytes(raw: bytes, filename: str = "", mode: str = "merge") -> dict[str, Any]:
    """从备份包还原。接受 zip（含 accounts.json 或 accounts.db）或裸 JSON。"""
    import io
    import zipfile

    if mode not in ("merge", "replace"):
        raise ValueError("mode 只能是 merge 或 replace")

    is_zip = raw[:2] == b"PK" or filename.lower().endswith(".zip")
    if not is_zip:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"备份文件不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict) or "accounts" not in data:
            raise ValueError("JSON 里没有 accounts 字段，不是本工具的备份")
        return _restore_from_dump(data, mode)

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"压缩包损坏：{exc}") from exc

    names = set(zf.namelist())
    # 优先用明文 JSON：它自带完整 payload，不依赖 sqlite 版本
    for candidate in ("accounts.json", "backup.json", "data.json"):
        if candidate in names:
            try:
                data = json.loads(zf.read(candidate).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"{candidate} 解析失败：{exc}") from exc
            if isinstance(data, dict) and "accounts" in data:
                return _restore_from_dump(data, mode)

    if "accounts.db" in names:
        return _restore_from_sqlite(zf.read("accounts.db"), mode)

    raise ValueError("压缩包里没有 accounts.json 或 accounts.db")


def _restore_from_sqlite(blob: bytes, mode: str) -> dict[str, Any]:
    """只有 db 时的还原路径：把库文件写进临时文件，读出记录再走 upsert。

    不直接覆盖 data/accounts.db —— 覆盖会连当前库的 WAL 一起打乱，
    而且 replace 语义之外还多了一次不可回滚的文件替换。
    """
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="wk-restore-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"备份库结构不对：{exc}") from exc
        finally:
            conn.close()

        accounts = []
        meta_items = []
        for row in rows:
            keys = row.keys()
            # 复检 / 风控 / 批次状态：db 路径下这些列本来就在，直接带出来
            meta = {c: row[c] for c in _META_COLS if c in keys}
            meta["uid"] = row["uid"] if "uid" in keys else ""
            meta["realm"] = row["realm"] if "realm" in keys else ""
            meta_items.append(meta)
            payload = row["payload"] if "payload" in keys else None
            if payload:
                try:
                    accounts.append(_account_from_payload(json.loads(payload)))
                    continue
                except ValueError:
                    pass
            acc = Account()
            acc.uid = row["uid"] if "uid" in keys else ""
            acc.realm = row["realm"] if "realm" in keys else "intl"
            acc.nickname = row["nickname"] if "nickname" in keys else ""
            acc.email = row["email"] if "email" in keys else ""
            acc.phone = row["phone"] if "phone" in keys else ""
            acc.access_token = row["access_token"] if "access_token" in keys else ""
            acc.refresh_token = row["refresh_token"] if "refresh_token" in keys else ""
            accounts.append(acc)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if mode == "replace":
        clear()
    stats = upsert(accounts, batch="restore-" + clock.compact(),
                   source="backup")
    meta = _restore_meta(meta_items)
    return {
        "mode": mode,
        "restored": len(accounts),
        "meta": meta,
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
    }
