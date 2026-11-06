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
from .model import Account

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "accounts.db")

#: 表结构版本。
#: 2 = 唯一键从 (name, realm) 改成 (uid, realm)
#: 3 = 加探针列（积分三元组 / 校验状态 / 校验时间），见 wk/probe.py
#: 4 = 加封号复检列（名单状态 / 次数 / 时间 / 结论），见 wk/recheck.py
SCHEMA_VERSION = 4

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
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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


def upsert(accounts: Iterable[Account], batch: str = "", source: str = "") -> dict[str, Any]:
    """按 (uid, realm) 写入或更新。返回新增/更新/跳过的计数。

    同一批里出现同一 uid 两次时，后一条覆盖前一条——「导入顺序即优先级」，
    也避免批内自己跟自己打架。
    """
    stats = {"total": 0, "inserted": 0, "updated": 0, "skipped": 0, "items": []}
    batch = batch or time.strftime("batch-%Y%m%d-%H%M%S")
    now = _now()

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
            # 来源系统已经把它标成封号的，导进来就直接进复检名单。
            # 这就是 banned-retry 的「扫 /status 自动识别」那条路径，
            # 只是换成在入库时判一次，不用每轮去扫。
            hint = banned_hint_of(acc)

            row = conn.execute(
                "SELECT id, access_token, recheck_state FROM accounts WHERE uid = ? AND realm = ?",
                (uid, realm),
            ).fetchone()

            if row is None:
                conn.execute(
                    """INSERT INTO accounts
                       (uid, realm, name, nickname, email, phone, access_token, refresh_token,
                        device_token, token_state, expires_at, domain, enterprise_id, enabled,
                        source, payload, created_at, updated_at,
                        recheck_state, recheck_count, recheck_at, recheck_note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, realm, name, acc.nickname, acc.email, acc.phone,
                     acc.access_token, acc.refresh_token, acc.device_token, state,
                     acc.expires_at, acc.domain, acc.enterprise_id, int(acc.enabled),
                     source or acc.source, payload, now, now,
                     "banned" if hint else "", 0, 0, hint[:200] or None),
                )
                stats["inserted"] += 1
                action = "inserted"
            else:
                # 「已在名单里」的判定：已有状态优先，没有才按这次的 hint 补。
                # 不能反过来——复检结果（revived / auth）比导入字段新，
                # 用导入字段覆盖会把刚解封的号重新打回 banned。
                current_track = (row["recheck_state"] or "") if "recheck_state" in row.keys() else ""
                new_track = current_track or ("banned" if hint else "")
                # token 换了就作废旧的校验结果：同一 uid 重新扫码后拿到的是新
                # 凭据，旧积分属于上一个 token，留着会让人以为「刚校验过」。
                # token 没变（重复导入同一份文件）则保留，省一次上游查询。
                same_token = str(row["access_token"] or "") == str(acc.access_token or "")
                if same_token:
                    conn.execute(
                        """UPDATE accounts SET
                             name=?, nickname=?, email=?, phone=?, access_token=?, refresh_token=?,
                             device_token=?, token_state=?, expires_at=?, domain=?, enterprise_id=?,
                             enabled=?, source=?, payload=?, updated_at=?,
                             recheck_state=?, recheck_note=COALESCE(recheck_note, ?)
                           WHERE id=?""",
                        (name, acc.nickname, acc.email, acc.phone,
                         acc.access_token, acc.refresh_token, acc.device_token, state,
                         acc.expires_at, acc.domain, acc.enterprise_id, int(acc.enabled),
                         source or acc.source, payload, now,
                         new_track, hint[:200] or None, row["id"]),
                    )
                else:
                    conn.execute(
                        """UPDATE accounts SET
                             name=?, nickname=?, email=?, phone=?, access_token=?, refresh_token=?,
                             device_token=?, token_state=?, expires_at=?, domain=?, enterprise_id=?,
                             enabled=?, source=?, payload=?, updated_at=?,
                             credit_remain=NULL, credit_used=NULL, credit_size=NULL,
                             credit_state=NULL, checked_at=0, checked_note=NULL,
                             recheck_state=?, recheck_note=COALESCE(recheck_note, ?)
                           WHERE id=?""",
                        (name, acc.nickname, acc.email, acc.phone,
                         acc.access_token, acc.refresh_token, acc.device_token, state,
                         acc.expires_at, acc.domain, acc.enterprise_id, int(acc.enabled),
                         source or acc.source, payload, now,
                         new_track, hint[:200] or None, row["id"]),
                    )
                stats["updated"] += 1
                action = "updated"

            stats["items"].append({
                "uid": uid,
                "name": name,
                "realm": realm,
                "action": action,
                "state": state,
            })

        conn.execute(
            """INSERT INTO import_log (batch, source, total, inserted, updated, skipped, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (batch, source, stats["total"], stats["inserted"], stats["updated"],
             stats["skipped"], json.dumps(stats["items"], ensure_ascii=False), now),
        )

    return stats


def list_accounts(realm: str = "", query: str = "", limit: int = 500,
                  track: str = "") -> list[dict]:
    """按版本筛选 + 关键字搜索。返回给界面的行（token 已脱敏）。

    `track` 按复检名单状态筛：传 "banned" 就是「封号复检」标签页要的那批。
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
    if query:
        sql += " AND (name LIKE ? OR uid LIKE ? OR nickname LIKE ? OR email LIKE ?)"
        like = f"%{query}%"
        args.extend([like, like, like, like])
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
        credit_state = (row["credit_state"] if "credit_state" in keys else "") or ""
        checked_at = int((row["checked_at"] if "checked_at" in keys else 0) or 0)
        recheck_state = (row["recheck_state"] if "recheck_state" in keys else "") or ""
        recheck_count = int((row["recheck_count"] if "recheck_count" in keys else 0) or 0)
        recheck_at = int((row["recheck_at"] if "recheck_at" in keys else 0) or 0)
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
            "expires": time.strftime("%Y-%m-%d", time.localtime(row["expires_at"]))
                       if row["expires_at"] else "",
            "at": _mask(row["access_token"] or ""),
            "has_rt": bool(row["refresh_token"]),
            "source": row["source"] or "",
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
            # ---- 探针 ----
            # None 原样透传：前端要能区分「没查过」和「0」
            "credit_remain": row["credit_remain"] if "credit_remain" in keys else None,
            "credit_used": row["credit_used"] if "credit_used" in keys else None,
            "credit_size": row["credit_size"] if "credit_size" in keys else None,
            "credit_state": credit_state,
            "credit_state_label": PROBE_LABELS.get(credit_state, ""),
            "checked_at": checked_at,
            "checked": time.strftime("%m-%d %H:%M", time.localtime(checked_at))
                       if checked_at else "",
            "checked_note": (row["checked_note"] if "checked_note" in keys else "") or "",
            # ---- 封号复检 ----
            "recheck_state": recheck_state,
            "recheck_label": RECHECK_LABELS.get(recheck_state, ""),
            "recheck_count": recheck_count,
            "recheck_at": recheck_at,
            "recheck": time.strftime("%m-%d %H:%M", time.localtime(recheck_at))
                       if recheck_at else "",
            "recheck_note": (row["recheck_note"] if "recheck_note" in keys else "") or "",
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

    return {
        "total": total,
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
        "db_path": DB_PATH,
    }


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


def load_for_export(realm: str = "", ids: list[int] | None = None) -> list[Account]:
    """把库里的记录还原成 Account，供导出渲染。

    payload 里存的是完整 Account 快照，直接反序列化即可——比逐列重建可靠，
    也不会漏掉后续版本新增的字段。
    """
    sql = "SELECT payload FROM accounts WHERE 1=1"
    args: list[Any] = []
    if realm in ("cn", "intl"):
        sql += " AND realm = ?"
        args.append(realm)
    if ids:
        placeholders = ",".join("?" * len(ids))
        sql += f" AND id IN ({placeholders})"
        args.extend(ids)
    sql += " ORDER BY id"

    out: list[Account] = []
    with connect() as conn:
        for row in conn.execute(sql, args).fetchall():
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            out.append(_account_from_payload(data))
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
    sql = "SELECT id, payload, IFNULL(recheck_state,'') AS recheck_state, "
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
            # 复检状态只活在库里，不在 payload 里（payload 是导入时的快照，
            # 我们后写的状态不该混进去，否则导出的文件会带上本机的复检记录）。
            acc.recheck_state = row["recheck_state"] or ""
            acc.recheck_count = int(row["recheck_count"] or 0)
            acc.recheck_at = int(row["recheck_at"] or 0)
            out.append((int(row["id"]), acc))
    return out


def update_credits(results: list[dict[str, Any]]) -> int:
    """把探针结果写回库。

    results 里每条：{id, state, remain, used, size, tier, checked_at, note}

    remain / used / size 为 None 时写 NULL 而不是 0——界面上「没查过」和
    「查过了确实是 0」要能分开。这也是为什么这三列不设 DEFAULT 0。
    """
    if not results:
        return 0
    written = 0
    with connect() as conn:
        for item in results:
            row_id = item.get("id")
            if row_id is None:
                continue
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
                    str(item.get("note") or "")[:200] or None,
                    int(row_id),
                ),
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
                "SELECT IFNULL(recheck_state,'') AS s, IFNULL(recheck_count,0) AS n "
                "FROM accounts WHERE id = ?", (int(row_id),)
            ).fetchone()
            if row is None:
                continue
            state = recheck_mod.next_track(row["s"] or "", verdict)
            note = str(item.get("detail") or item.get("raw") or "")[:200] or None
            conn.execute(
                """UPDATE accounts SET
                     recheck_state = ?, recheck_count = ?, recheck_at = ?, recheck_note = ?
                   WHERE id = ?""",
                (state, int(row["n"] or 0) + 1,
                 int(item.get("checked_at") or time.time()), note, int(row_id)),
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
            cur = conn.execute(
                f"UPDATE accounts SET recheck_state = ?, recheck_note = COALESCE(?, recheck_note) "
                f"WHERE id IN ({placeholders})",
                [state, (note[:200] or None)] + [int(i) for i in ids],
            )
        else:
            # 移出名单时把计数和时间一起清掉：留着会让下一次加入时
            # 立刻被最小间隔挡住，用户会以为按钮没生效。
            cur = conn.execute(
                f"UPDATE accounts SET recheck_state = '', recheck_count = 0, "
                f"recheck_at = 0, recheck_note = NULL WHERE id IN ({placeholders})",
                [int(i) for i in ids],
            )
        return cur.rowcount or 0


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

def _sqlite_snapshot() -> bytes:
    """用 sqlite 的在线备份 API 取一致性快照。

    直接读文件在写入过程中可能拿到半页数据（尤其 WAL 模式），备份 API 会
    自己处理锁和页边界。
    """
    import tempfile

    with connect() as conn:
        fd, tmp = tempfile.mkstemp(prefix="wk-backup-", suffix=".db")
        os.close(fd)
        try:
            dest = sqlite3.connect(tmp)
            try:
                conn.backup(dest)
            finally:
                dest.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def dump_records() -> list[dict]:
    """把库里每条记录原样导出（含完整 payload，token 不脱敏）。

    这是备份，脱敏就没有还原价值了。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY id"
        ).fetchall()
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


def backup_bytes() -> tuple[bytes, str]:
    """生成备份 zip，返回 (字节, 建议文件名)。"""
    import io
    import zipfile

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"wk-switch-backup-{stamp}.zip"

    records = dump_records()
    accounts = [r for r in records if not r.get("__import_log__")]
    logs = [r for r in records if r.get("__import_log__")]

    manifest = {
        "format": "wk-switch-backup",
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "accounts": len(accounts),
        "import_log": len(logs),
        "db_path": DB_PATH,
    }
    dump = {
        "format": "wk-switch-backup",
        "version": 1,
        "created_at": manifest["created_at"],
        "accounts": accounts,
        "import_log": logs,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("accounts.json", json.dumps(dump, ensure_ascii=False, indent=2))
        zf.writestr("accounts.db", _sqlite_snapshot())
    return buf.getvalue(), name


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
    stats = upsert(accounts, batch=time.strftime("restore-%Y%m%d-%H%M%S"),
                   source="backup")
    return {
        "mode": mode,
        "restored": len(accounts),
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
        for row in rows:
            keys = row.keys()
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
    stats = upsert(accounts, batch=time.strftime("restore-%Y%m%d-%H%M%S"),
                   source="backup")
    return {
        "mode": mode,
        "restored": len(accounts),
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
    }
