"""本地账户库的单测：唯一性按 (uid, realm)，名称不参与判定。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import store  # noqa: E402
from wk.model import Account  # noqa: E402


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 每个用例用独立库文件，互不干扰
        self._old_dir = store.DATA_DIR
        self._old_path = store.DB_PATH
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "accounts.db")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        store.DATA_DIR = self._old_dir
        store.DB_PATH = self._old_path

    def acc(self, uid: str, name: str, realm: str = "cn", token: str = "at-1") -> Account:
        a = Account()
        a.uid = uid
        a.nickname = name
        a.realm = realm
        a.access_token = token
        a.expires_at = 4102444800
        return a


class UniquenessTests(StoreTestBase):
    def test_same_uid_updates(self) -> None:
        store.upsert([self.acc("u-1", "阿岩", token="at-old")])
        stats = store.upsert([self.acc("u-1", "阿岩", token="at-new")])
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["updated"], 1)

        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        # 更新后 token 是新的
        exported = store.load_for_export()
        self.assertEqual(exported[0].access_token, "at-new")

    def test_same_name_different_uid_stays_separate(self) -> None:
        """两个不同账号起了同一个名字，不能被合并——这正是名称不能当主键的原因。"""
        store.upsert([self.acc("u-1", "同名")])
        store.upsert([self.acc("u-2", "同名")])
        rows = store.list_accounts()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["uid"] for r in rows}, {"u-1", "u-2"})

    def test_same_uid_different_realm_stays_separate(self) -> None:
        store.upsert([self.acc("u-1", "阿岩", realm="cn")])
        store.upsert([self.acc("u-1", "阿岩", realm="intl")])
        rows = store.list_accounts()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["realm"] for r in rows}, {"cn", "intl"})

    def test_uid_missing_falls_back_to_token_hash(self) -> None:
        a = self.acc("", "无 uid")
        store.upsert([a])
        store.upsert([self.acc("", "无 uid")])  # 同一 token
        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["uid"].startswith("sha256-"))

    def test_rename_keeps_uniqueness(self) -> None:
        store.upsert([self.acc("u-1", "阿岩"), self.acc("u-2", "阿岩")])
        rows = store.list_accounts()
        target = [r for r in rows if r["uid"] == "u-1"][0]
        store.rename(target["id"], "改名后的阿岩")
        rows = store.list_accounts()
        self.assertEqual(len(rows), 2)
        names = {r["uid"]: r["name"] for r in rows}
        self.assertEqual(names["u-1"], "改名后的阿岩")
        self.assertEqual(names["u-2"], "阿岩")

    def test_rename_to_existing_name_allowed(self) -> None:
        store.upsert([self.acc("u-1", "A"), self.acc("u-2", "B")])
        rows = store.list_accounts()
        target = [r for r in rows if r["uid"] == "u-1"][0]
        store.rename(target["id"], "B")
        rows = store.list_accounts()
        self.assertEqual(len(rows), 2)  # 重名不删行

    def test_rename_empty_rejected(self) -> None:
        store.upsert([self.acc("u-1", "A")])
        with self.assertRaises(ValueError):
            store.rename(store.list_accounts()[0]["id"], "   ")

    def test_tokenless_skipped(self) -> None:
        a = self.acc("u-1", "空号")
        a.access_token = ""
        stats = store.upsert([a])
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(store.list_accounts(), [])


class QueryTests(StoreTestBase):
    def setUp(self) -> None:
        super().setUp()
        store.upsert([
            self.acc("u-cn-1", "国内一号", realm="cn"),
            self.acc("u-cn-2", "国内二号", realm="cn"),
            self.acc("u-intl-1", "国际一号", realm="intl"),
        ])

    def test_filter_by_realm(self) -> None:
        self.assertEqual(len(store.list_accounts(realm="cn")), 2)
        self.assertEqual(len(store.list_accounts(realm="intl")), 1)
        self.assertEqual(len(store.list_accounts()), 3)

    def test_search(self) -> None:
        self.assertEqual(len(store.list_accounts(query="国内")), 2)
        self.assertEqual(len(store.list_accounts(query="intl")), 1)

    def test_stats(self) -> None:
        s = store.stats()
        self.assertEqual(s["total"], 3)
        realms = {r["key"]: r["count"] for r in s["realms"]}
        self.assertEqual(realms, {"cn": 2, "intl": 1})
        self.assertEqual(len(s["history"]), 1)
        self.assertTrue(s["db_path"].endswith("accounts.db"))

    def test_export_by_realm(self) -> None:
        cn = store.load_for_export(realm="cn")
        self.assertEqual(len(cn), 2)
        self.assertTrue(all(a.realm == "cn" for a in cn))

    def test_delete_and_clear(self) -> None:
        ids = [r["id"] for r in store.list_accounts(realm="intl")]
        self.assertEqual(store.delete(ids), 1)
        self.assertEqual(len(store.list_accounts()), 2)
        self.assertEqual(store.clear(), 2)
        self.assertEqual(store.list_accounts(), [])


class PayloadRoundTripTests(StoreTestBase):
    def test_full_account_survives(self) -> None:
        a = self.acc("u-1", "完整号")
        a.refresh_token = "rt-1"
        a.device_token = "dt-1"
        a.email = "a@example.com"
        a.enterprise_id = "ent-9"
        a.note = "备注"
        a.weight = 3
        a.disabled_models = ["m1", "m2"]
        store.upsert([a])

        back = store.load_for_export()[0]
        self.assertEqual(back.uid, "u-1")
        self.assertEqual(back.refresh_token, "rt-1")
        self.assertEqual(back.device_token, "dt-1")
        self.assertEqual(back.email, "a@example.com")
        self.assertEqual(back.enterprise_id, "ent-9")
        self.assertEqual(back.disabled_models, ["m1", "m2"])
        self.assertEqual(back.weight, 3)


class BackupRestoreTests(StoreTestBase):
    """备份包要能完整来回：条数、token、版本、名称全对上。"""

    def _seed(self) -> None:
        a = self.acc("u-cn-1", "国内一号", realm="cn", token="at-cn")
        a.refresh_token = "rt-cn"
        a.email = "cn@example.com"
        b = self.acc("u-intl-1", "国际一号", realm="intl", token="at-intl")
        store.upsert([a, b])

    def test_backup_zip_contains_both_payloads(self) -> None:
        import io
        import zipfile

        self._seed()
        blob, name = store.backup_bytes()
        self.assertTrue(name.endswith(".zip"))
        self.assertTrue(blob.startswith(b"PK"))

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            self.assertEqual(names, {"manifest.json", "accounts.json", "accounts.db"})
            manifest = __import__("json").loads(zf.read("manifest.json"))
            self.assertEqual(manifest["accounts"], 2)
            self.assertEqual(manifest["format"], "wk-switch-backup")

    def test_restore_into_empty_db(self) -> None:
        self._seed()
        blob, name = store.backup_bytes()

        store.clear()
        self.assertEqual(store.list_accounts(), [])

        result = store.restore_bytes(blob, filename=name, mode="merge")
        self.assertEqual(result["restored"], 2)
        self.assertEqual(result["inserted"], 2)

        rows = {r["uid"]: r for r in store.list_accounts()}
        self.assertEqual(set(rows), {"u-cn-1", "u-intl-1"})
        self.assertEqual(rows["u-cn-1"]["realm"], "cn")
        self.assertEqual(rows["u-intl-1"]["realm"], "intl")

        tokens = {a.uid: a.access_token for a in store.load_for_export()}
        self.assertEqual(tokens["u-cn-1"], "at-cn")
        self.assertEqual(tokens["u-intl-1"], "at-intl")

    def test_restore_merge_updates_existing(self) -> None:
        self._seed()
        blob, name = store.backup_bytes()

        # 备份之后把 token 改坏，还原应当把它盖回去
        broken = self.acc("u-cn-1", "国内一号", realm="cn", token="at-broken")
        store.upsert([broken])

        result = store.restore_bytes(blob, filename=name, mode="merge")
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["updated"], 2)
        tokens = {a.uid: a.access_token for a in store.load_for_export()}
        self.assertEqual(tokens["u-cn-1"], "at-cn")

    def test_restore_replace_drops_extra_rows(self) -> None:
        self._seed()
        blob, name = store.backup_bytes()

        store.upsert([self.acc("u-late", "备份之后加的", realm="cn")])
        self.assertEqual(len(store.list_accounts()), 3)

        result = store.restore_bytes(blob, filename=name, mode="replace")
        self.assertEqual(result["restored"], 2)
        uids = {r["uid"] for r in store.list_accounts()}
        self.assertEqual(uids, {"u-cn-1", "u-intl-1"})

    def test_restore_from_plain_json(self) -> None:
        import json

        self._seed()
        dump = {
            "format": "wk-switch-backup",
            "version": 1,
            "accounts": [
                {"uid": "u-json", "realm": "intl",
                 "account": {"uid": "u-json", "realm": "intl",
                             "nickname": "JSON 还原", "access_token": "at-json"}},
            ],
        }
        store.clear()
        result = store.restore_bytes(
            json.dumps(dump).encode("utf-8"), filename="backup.json", mode="merge"
        )
        self.assertEqual(result["inserted"], 1)
        rows = store.list_accounts()
        self.assertEqual(rows[0]["uid"], "u-json")
        self.assertEqual(rows[0]["realm"], "intl")

    def test_restore_rejects_foreign_json(self) -> None:
        with self.assertRaises(ValueError):
            store.restore_bytes(b'{"hello": "world"}', filename="x.json", mode="merge")

    def test_restore_rejects_bad_mode(self) -> None:
        with self.assertRaises(ValueError):
            store.restore_bytes(b"{}", filename="x.json", mode="wipe")


class ProbeColumnTests(StoreTestBase):
    """探针列（v3）：迁移、回写、与 token 变更的关系。"""

    def test_upgrade_from_v2_adds_columns(self) -> None:
        """v2 老库打开时自动补探针列。

        漏了这一步，升级后每次写入都会报 "no column named credit_remain"，
        而且是在用户点保存时才炸，比启动就炸难查得多。
        """
        import sqlite3

        # 手搓一个 v2 结构的库（没有探针列）
        conn = sqlite3.connect(store.DB_PATH)
        conn.executescript("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL, realm TEXT NOT NULL DEFAULT 'intl',
                name TEXT, nickname TEXT, email TEXT, phone TEXT,
                access_token TEXT, refresh_token TEXT, device_token TEXT,
                token_state TEXT, expires_at INTEGER DEFAULT 0,
                domain TEXT, enterprise_id TEXT, enabled INTEGER DEFAULT 1,
                source TEXT, payload TEXT, created_at TEXT, updated_at TEXT,
                UNIQUE (uid, realm)
            );
            PRAGMA user_version = 2;
        """)
        conn.execute(
            "INSERT INTO accounts (uid, realm, name, access_token, payload, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("u-old", "cn", "老账号", "at-old", "{}", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()

        # 打开即迁移
        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["credit_remain"])
        self.assertEqual(rows[0]["credit_state"], "")

        # 迁移后能正常回写
        written = store.update_credits([{
            "id": rows[0]["id"], "state": "valid", "remain": 12.5,
            "used": 7.5, "size": 20.0, "checked_at": 1790000000, "note": "",
        }])
        self.assertEqual(written, 1)
        row = store.list_accounts()[0]
        self.assertAlmostEqual(row["credit_remain"], 12.5)
        self.assertEqual(row["credit_state"], "valid")
        self.assertEqual(row["credit_state_label"], "有效")
        self.assertTrue(row["checked"])

    def test_update_credits_keeps_none_distinct_from_zero(self) -> None:
        """没查到写 NULL，查到 0 写 0。两者在界面上意思相反。"""
        store.upsert([self.acc("u-a", "甲"), self.acc("u-b", "乙", token="at-2")])
        rows = {r["uid"]: r for r in store.list_accounts()}
        store.update_credits([
            {"id": rows["u-a"]["id"], "state": "valid", "remain": 0,
             "used": 100, "size": 100, "checked_at": 1790000000},
            {"id": rows["u-b"]["id"], "state": "unknown", "remain": None,
             "used": None, "size": None, "checked_at": 1790000000,
             "note": "连接超时"},
        ])
        after = {r["uid"]: r for r in store.list_accounts()}
        self.assertEqual(after["u-a"]["credit_remain"], 0)
        self.assertIsNone(after["u-b"]["credit_remain"])
        self.assertEqual(after["u-b"]["credit_state"], "unknown")
        self.assertEqual(after["u-b"]["checked_note"], "连接超时")

    def test_token_change_clears_stale_credit(self) -> None:
        """同一 uid 换了 token，旧积分必须作废。

        留着会显示成「刚校验过」，但那个余额属于已经被替换掉的凭据。
        """
        store.upsert([self.acc("u-x", "某号", token="at-old")])
        row = store.list_accounts()[0]
        store.update_credits([{
            "id": row["id"], "state": "valid", "remain": 88.0,
            "used": 12.0, "size": 100.0, "checked_at": 1790000000,
        }])
        self.assertAlmostEqual(store.list_accounts()[0]["credit_remain"], 88.0)

        # 换 token 重新导入
        store.upsert([self.acc("u-x", "某号", token="at-new")])
        after = store.list_accounts()[0]
        self.assertIsNone(after["credit_remain"], "换 token 后旧积分应清空")
        self.assertEqual(after["credit_state"], "")
        self.assertEqual(after["checked_at"], 0)

    def test_same_token_keeps_credit(self) -> None:
        """重复导入同一份文件时保留已查到的积分，别白花一次上游查询。"""
        store.upsert([self.acc("u-y", "某号", token="at-same")])
        row = store.list_accounts()[0]
        store.update_credits([{
            "id": row["id"], "state": "valid", "remain": 42.0,
            "used": 8.0, "size": 50.0, "checked_at": 1790000000,
        }])
        store.upsert([self.acc("u-y", "某号", token="at-same")])
        self.assertAlmostEqual(store.list_accounts()[0]["credit_remain"], 42.0)

    def test_load_for_probe_returns_tokens(self) -> None:
        """探测要真 token，不能拿脱敏后的列表去发请求。"""
        store.upsert([self.acc("u-p", "甲", token="at-full-token"),
                      self.acc("u-q", "乙", realm="intl", token="at-intl")])
        pairs = store.load_for_probe()
        self.assertEqual(len(pairs), 2)
        tokens = {acc.access_token for _, acc in pairs}
        self.assertEqual(tokens, {"at-full-token", "at-intl"})

        # 按 id 取与按 realm 筛都要能用
        only = store.load_for_probe(realm="intl")
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0][1].realm, "intl")

        by_id = store.load_for_probe(ids=[pairs[0][0]])
        self.assertEqual(len(by_id), 1)
        self.assertEqual(by_id[0][0], pairs[0][0])


class BatchTests(StoreTestBase):
    """导入批次：同一批导进来的号要能一次捞出来，也要能看出是什么时候导的。"""

    def test_batch_and_imported_at_recorded(self) -> None:
        store.upsert([self.acc("u-b1", "甲")], batch="import-20261106-101010")
        row = store.list_accounts()[0]
        self.assertEqual(row["batch"], "import-20261106-101010")
        self.assertEqual(row["first_batch"], "import-20261106-101010")
        # 时间是北京时间字符串，格式固定，不可能是空的
        self.assertRegex(row["imported_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_first_batch_survives_reimport(self) -> None:
        """重复导入只改「最近批次」，首次批次得留着——不然找不出最早那批。"""
        store.upsert([self.acc("u-b2", "甲")], batch="import-20261101-090000")
        store.upsert([self.acc("u-b2", "甲")], batch="import-20261106-101010")
        row = store.list_accounts()[0]
        self.assertEqual(row["batch"], "import-20261106-101010")
        self.assertEqual(row["first_batch"], "import-20261101-090000")

    def test_filter_by_batch(self) -> None:
        store.upsert([self.acc("u-b3", "甲"), self.acc("u-b4", "乙")],
                     batch="import-20261106-101010")
        store.upsert([self.acc("u-b5", "丙")], batch="import-20261106-111111")
        rows = store.list_accounts(batch="import-20261106-111111")
        self.assertEqual([r["nickname"] for r in rows], ["丙"])

    def test_batches_counts(self) -> None:
        store.upsert([self.acc("u-b6", "甲"), self.acc("u-b7", "乙")],
                     batch="import-20261106-101010")
        store.upsert([self.acc("u-b8", "丙")], batch="import-20261106-111111")
        got = {b["batch"]: b["count"] for b in store.batches()}
        self.assertEqual(got["import-20261106-101010"], 2)
        self.assertEqual(got["import-20261106-111111"], 1)
        # 新的批次排在前面
        self.assertEqual(store.batches()[0]["batch"], "import-20261106-111111")

    def test_search_matches_batch(self) -> None:
        store.upsert([self.acc("u-b9", "甲")], batch="import-20261106-101010")
        self.assertEqual(len(store.list_accounts(query="20261106-101010")), 1)


class RiskImportTests(StoreTestBase):
    """风控配置：不配置就是正常号，配置了直接进风控区。

    这条链以前是「导入字段里出现风控措辞就自动进名单」，结果是来源系统
    备注里带一句「请求被拦截」，整批号都被拖进风控区，正常号跟着一起
    被当风控处理。现在状态只由显式配置或后续验证写。
    """

    def _with_note(self, uid: str, note: str) -> Account:
        a = self.acc(uid, "某号", token="at-" + uid)
        a.note = note
        return a

    def test_default_is_normal(self) -> None:
        store.upsert([self.acc("u-r1", "甲")], batch="b-normal")
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "")
        self.assertEqual(row["risk_src"], "")
        # 正常号不该出现在风控区
        self.assertEqual(store.list_accounts(track="banned"), [])

    def test_risk_import_goes_straight_to_risk_area(self) -> None:
        stats = store.upsert([self.acc("u-r2", "甲")], batch="b-risk", risk=True)
        self.assertEqual(stats["risk"], 1)
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "banned")
        self.assertEqual(row["risk_src"], "import")
        self.assertEqual(row["risk_label"], "导入标记")
        self.assertEqual(len(store.list_accounts(track="banned")), 1)

    def test_banned_wording_in_note_does_not_auto_flag(self) -> None:
        """来源备注里带风控措辞，没勾风控就不该被塞进风控区。"""
        store.upsert([self._with_note("u-r3", "banned by upstream")], batch="b1")
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "")
        self.assertEqual(store.list_accounts(track="banned"), [])

    def test_verify_finding_moves_to_risk_area(self) -> None:
        """后续验证发现风控：探针回写 restrict 时自动进风控区。"""
        store.upsert([self.acc("u-r4", "甲")], batch="b1")
        row = store.list_accounts()[0]
        store.update_credits([{
            "id": row["id"], "state": "invalid", "checked_at": 1790000000,
            "note": "被风控拦截（账号级限制）", "restrict": True,
        }])
        got = store.list_accounts()[0]
        self.assertEqual(got["recheck_state"], "banned")
        self.assertEqual(got["risk_src"], "verify")

    def test_risk_import_overrides_normal_state(self) -> None:
        store.upsert([self.acc("u-r5", "甲")], batch="b1")
        store.upsert([self.acc("u-r5", "甲")], batch="b2", risk=True)
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "banned")
        self.assertEqual(row["risk_src"], "import")

    def test_manual_add_marks_manual_source(self) -> None:
        store.upsert([self.acc("u-r6", "甲")], batch="b1")
        row = store.list_accounts()[0]
        store.set_track([row["id"]], "banned")
        got = store.list_accounts()[0]
        self.assertEqual(got["risk_src"], "manual")
        store.set_track([row["id"]], "")
        self.assertEqual(store.list_accounts()[0]["risk_src"], "")

    def test_backup_restore_keeps_risk_state(self) -> None:
        """还原不能把风控区放空——那是备份最该保住的东西。"""
        store.upsert([self.acc("u-r7", "甲")], batch="b1", risk=True)
        blob, _ = store.backup_bytes()
        store.clear()
        self.assertEqual(store.list_accounts(), [])
        store.restore_bytes(blob, "backup.zip", mode="replace")
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "banned")
        self.assertEqual(row["risk_src"], "import")


class RiskMapTests(StoreTestBase):
    """逐行标风控：`risk_map={uid: True/False}` 覆盖整批开关。

    一批里混两个已知封号的号是常态，整批一个开关表达不了。同时 False 必须
    是「显式正常」而不是「没意见」——用户点「标为正常」就是要把它从风控区
    拿出来，这是唯一的手动出口。
    """

    def test_map_marks_single_row(self) -> None:
        store.upsert([self.acc("u-m1", "甲"), self.acc("u-m2", "乙")],
                     batch="b1", risk_map={"u-m1": True})
        rows = {r["uid"]: r for r in store.list_accounts()}
        self.assertEqual(rows["u-m1"]["recheck_state"], "banned")
        self.assertEqual(rows["u-m1"]["risk_src"], "import")
        self.assertEqual(rows["u-m2"]["recheck_state"], "")

    def test_map_beats_batch_flag(self) -> None:
        """整批标了风控，其中一行显式标正常 → 那一行不进风控区。"""
        store.upsert([self.acc("u-m3", "甲"), self.acc("u-m4", "乙")],
                     batch="b1", risk=True, risk_map={"u-m4": False})
        rows = {r["uid"]: r for r in store.list_accounts()}
        self.assertEqual(rows["u-m3"]["recheck_state"], "banned")
        self.assertEqual(rows["u-m4"]["recheck_state"], "")

    def test_explicit_normal_pulls_out_of_risk_area(self) -> None:
        store.upsert([self.acc("u-m5", "甲")], batch="b1", risk=True)
        self.assertEqual(store.list_accounts(track="banned")[0]["uid"], "u-m5")
        store.upsert([self.acc("u-m5", "甲")], batch="b2", risk_map={"u-m5": False})
        self.assertEqual(store.list_accounts(track="banned"), [])
        self.assertEqual(store.list_accounts()[0]["risk_src"], "")

    def test_no_map_keeps_existing_state(self) -> None:
        """什么都没标时不能把已有的 banned 冲掉——重复导入是常事。"""
        store.upsert([self.acc("u-m6", "甲")], batch="b1", risk=True)
        store.upsert([self.acc("u-m6", "甲")], batch="b2")
        self.assertEqual(store.list_accounts()[0]["recheck_state"], "banned")


class ExitPortTests(StoreTestBase):
    """每行一个出口端口（0 = 直连）。复检实际走哪条线全靠它。"""

    def test_default_is_direct(self) -> None:
        store.upsert([self.acc("u-e1", "甲")])
        self.assertEqual(store.list_accounts()[0]["exit_port"], 0)

    def test_set_same_port_for_many(self) -> None:
        store.upsert([self.acc("u-e2", "甲"), self.acc("u-e3", "乙")])
        ids = [r["id"] for r in store.list_accounts()]
        self.assertEqual(store.set_exit_port(ids, 17950), 2)
        self.assertEqual({r["exit_port"] for r in store.list_accounts()}, {17950})

    def test_set_map_per_row(self) -> None:
        """随机分配必须一行一个值，一条 UPDATE 写不了。"""
        store.upsert([self.acc("u-e4", "甲"), self.acc("u-e5", "乙")])
        rows = store.list_accounts()
        pairs = [(rows[0]["id"], 17950), (rows[1]["id"], 17951)]
        self.assertEqual(store.set_exit_map(pairs), 2)
        got = {r["id"]: r["exit_port"] for r in store.list_accounts()}
        self.assertEqual(got[rows[0]["id"]], 17950)
        self.assertEqual(got[rows[1]["id"]], 17951)

    def test_clear_to_direct(self) -> None:
        store.upsert([self.acc("u-e6", "甲")])
        rid = store.list_accounts()[0]["id"]
        store.set_exit_port([rid], 17950)
        store.set_exit_port([rid], 0)
        self.assertEqual(store.list_accounts()[0]["exit_port"], 0)

    def test_exit_ports_of_batch(self) -> None:
        store.upsert([self.acc("u-e7", "甲"), self.acc("u-e8", "乙")])
        rows = store.list_accounts()
        store.set_exit_map([(rows[0]["id"], 17952)])
        got = store.exit_ports_of([r["id"] for r in rows])
        self.assertEqual(got[rows[0]["id"]], 17952)
        self.assertEqual(got[rows[1]["id"]], 0)

    def test_recheck_ids_only_tracked(self) -> None:
        """没在名单里的号不参与分配——分配一个永远用不上的端口没有意义。"""
        store.upsert([self.acc("u-e9", "甲"), self.acc("u-e10", "乙")])
        rows = store.list_accounts()
        store.set_track([rows[0]["id"]], "banned")
        self.assertEqual(store.recheck_ids(), [rows[0]["id"]])

    def test_backup_keeps_exit_port(self) -> None:
        store.upsert([self.acc("u-e11", "甲")])
        rid = store.list_accounts()[0]["id"]
        store.set_exit_port([rid], 17960)
        blob, _ = store.backup_bytes()
        store.clear()
        store.restore_bytes(blob, "b.zip", mode="replace")
        self.assertEqual(store.list_accounts()[0]["exit_port"], 17960)

    def test_untrack_keeps_exit_port(self) -> None:
        """移出名单不清出口：加回来时还想用原来那个端口。"""
        store.upsert([self.acc("u-e12", "甲")])
        rid = store.list_accounts()[0]["id"]
        store.set_track([rid], "banned")
        store.set_exit_port([rid], 17970)
        store.set_track([rid], "")
        self.assertEqual(store.list_accounts()[0]["exit_port"], 17970)


class GroupPortsTests(StoreTestBase):
    """两区分组：一键分配的范围、以及「哪些端口对方正在用」。"""

    def _ids(self):
        return {r["uid"]: r["id"] for r in store.list_accounts()}

    def test_normal_ids_excludes_risk(self):
        store.upsert([self.acc("u-a", "甲"), self.acc("u-b", "乙")])
        ids = self._ids()
        store.set_track([ids["u-a"]], "banned")
        self.assertEqual(store.normal_ids(), [ids["u-b"]])

    def test_exit_ports_in_use_split_by_group(self):
        store.upsert([self.acc("u-a", "甲"), self.acc("u-b", "乙"),
                      self.acc("u-c", "丙")])
        ids = self._ids()
        store.set_track([ids["u-a"]], "banned")
        store.set_exit_port([ids["u-a"]], 17950)
        store.set_exit_port([ids["u-b"]], 17951)
        self.assertEqual(store.exit_ports_in_use("risk"), {17950})
        self.assertEqual(store.exit_ports_in_use("normal"), {17951})
        self.assertEqual(store.exit_ports_in_use(), {17950, 17951})

    def test_zero_port_is_not_a_slot(self):
        store.upsert([self.acc("u-a", "甲")])
        ids = self._ids()
        store.set_track([ids["u-a"]], "banned")
        store.set_exit_port([ids["u-a"]], 0)
        self.assertEqual(store.exit_ports_in_use(), set())

    def test_untrack_moves_port_to_normal_group(self):
        store.upsert([self.acc("u-a", "甲")])
        ids = self._ids()
        store.set_track([ids["u-a"]], "banned")
        store.set_exit_port([ids["u-a"]], 17960)
        self.assertEqual(store.exit_ports_in_use("risk"), {17960})
        store.set_track([ids["u-a"]], "")
        self.assertEqual(store.exit_ports_in_use("risk"), set())
        self.assertEqual(store.exit_ports_in_use("normal"), {17960})


class PickerRowsTests(StoreTestBase):
    """复检页「把账号加进名单」下拉：跨档可见、大库可搜、挪进去查得到。"""

    def _ids(self):
        return {r["uid"]: r["id"] for r in store.list_accounts()}

    def test_other_tracks_stay_selectable(self):
        """只在目标档里排除：待复检 / 正常账号都要能选进封号中。"""
        store.upsert([self.acc("u-p1", "甲"), self.acc("u-p2", "乙"),
                      self.acc("u-p3", "丙")])
        ids = self._ids()
        store.set_track([ids["u-p2"]], "pending")
        store.set_track([ids["u-p3"]], "banned")

        got = store.picker_rows("banned")
        self.assertEqual({r["id"] for r in got}, {ids["u-p1"], ids["u-p2"]})
        labels = {r["id"]: r["state_label"] for r in got}
        self.assertEqual(labels[ids["u-p1"]], "正常账号")
        self.assertEqual(labels[ids["u-p2"]], "待复检")
        # 切到待复检档时，封号中的号也要能挪过来
        self.assertIn(ids["u-p3"], {r["id"] for r in store.picker_rows("pending")})

    def test_move_from_normal_to_banned_is_visible(self):
        """用户报的现象：正常号标成风控后，封号中这一档必须查得到。"""
        store.upsert([self.acc("u-m1", "挪过来的号")])
        rid = store.list_accounts()[0]["id"]
        store.set_track([rid], "banned", "手工加入")
        self.assertEqual([r["id"] for r in store.list_accounts(track="banned")], [rid])
        self.assertEqual(store.recheck_ids(), [rid])
        self.assertEqual(store.normal_ids(), [])
        # 已经在目标档，下拉里不必重复列
        self.assertNotIn(rid, {r["id"] for r in store.picker_rows("banned")})

    def test_search_finds_accounts_past_default_limit(self):
        """账户库默认只给 500 行，下拉不能把老号弄丢。"""
        bulk = [self.acc(f"u-bulk-{i}", f"批量-{i:04d}") for i in range(505)]
        bulk.append(self.acc("u-old", "老号-尾巴"))
        store.upsert(bulk)
        self.assertGreater(len(store.list_accounts(limit=5000)), 500)
        got = store.picker_rows("banned", query="老号-尾巴")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "老号-尾巴")

    def test_retrack_from_revived_resets_counter(self):
        """从「已解封」划回名单要清零计数，否则刚加就被最小间隔跳过。"""
        store.upsert([self.acc("u-rt1", "甲")])
        rid = store.list_accounts()[0]["id"]
        store.set_track([rid], "banned")
        with store.connect() as conn:
            conn.execute(
                "UPDATE accounts SET recheck_state='revived', recheck_count=7, "
                "recheck_at=12345 WHERE id=?", (rid,))
        store.set_track([rid], "banned")
        row = store.list_accounts()[0]
        self.assertEqual(row["recheck_state"], "banned")
        self.assertEqual(row["recheck_count"], 0)
        self.assertEqual(row["recheck_at"], 0)


class RecheckApiTests(StoreTestBase):
    """直接调 Handler 的取数方法，验证 URL / payload 参数解析，不启 HTTP 服务。"""

    def _ids(self):
        return {r["uid"]: r["id"] for r in store.list_accounts()}

    def _call_get(self, method: str, path: str):
        import server

        class Fake:
            def __init__(self, p):
                self.path = p
                self.out = None

            def _json(self, status, payload):
                self.out = (status, payload)

        fake = Fake(path)
        getattr(server.Handler, method)(fake)
        return fake.out

    def _call_post(self, method: str, payload: dict):
        import server

        class Fake:
            def __init__(self):
                self.out = None

            def _json(self, status, data):
                self.out = (status, data)

        fake = Fake()
        getattr(server.Handler, method)(fake, payload)
        return fake.out

    def test_picker_route_uses_requested_track_and_query(self):
        store.upsert([self.acc("u-a1", "甲"), self.acc("u-a2", "乙")])
        ids = self._ids()
        store.set_track([ids["u-a1"]], "pending")

        status, data = self._call_get(
            "_recheck_picker", "/api/recheck/picker?track=pending")
        self.assertEqual(status, 200)
        self.assertEqual(data["track"], "pending")
        self.assertEqual({r["id"] for r in data["rows"]}, {ids["u-a2"]})

        status, data = self._call_get(
            "_recheck_picker", "/api/recheck/picker?track=banned&q=乙")
        self.assertEqual(status, 200)
        self.assertEqual({r["id"] for r in data["rows"]}, {ids["u-a2"]})

    def test_track_route_returns_rows_of_requested_track(self):
        """加进哪一档，响应就给哪一档的表，界面不用再猜。"""
        store.upsert([self.acc("u-a3", "甲"), self.acc("u-a4", "乙")])
        ids = self._ids()
        status, data = self._call_post("_recheck_track", {
            "ids": [ids["u-a3"]], "state": "pending", "track": "pending",
            "note": "手工加入",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["track"], "pending")
        self.assertEqual([r["id"] for r in data["rows"]], [ids["u-a3"]])
        self.assertEqual(store.list_accounts(track="banned"), [])

        status, data = self._call_post("_recheck_track", {
            "ids": [ids["u-a3"]], "state": "banned", "track": "banned",
        })
        self.assertEqual(data["track"], "banned")
        self.assertEqual([r["id"] for r in data["rows"]], [ids["u-a3"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
