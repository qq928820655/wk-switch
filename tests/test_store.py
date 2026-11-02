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


if __name__ == "__main__":
    unittest.main(verbosity=2)
