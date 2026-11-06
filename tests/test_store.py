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


if __name__ == "__main__":
    unittest.main(verbosity=2)
