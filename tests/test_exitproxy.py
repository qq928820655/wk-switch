"""按账户出口写 proxy 的单测：端口 → hub 账户文件字段映射。

跑法：python -m unittest discover -s tests -v

hub 侧依据（源码在 ardeyouxipianyi/workbuddy2api-hub）：
  wb_accounts.py:987-990   加载时 proxy 存成 proxy_legacy
  wb_accounts.py:4614      apply_proxy_slots() 用 proxySlot 覆盖 proxy
  wb_accounts.py:2889      每日活跃桌面腿 urlopen(proxy=self.proxy)
  wb_accounts.py:2619      每日活跃网页腿 run_turn(proxy=self.proxy)
所以本工具必须同时写 proxy、清 proxySlot，否则槽绑定会把出口盖掉。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import exitproxy, formats, recheck, store  # noqa: E402
from wk.model import Account  # noqa: E402


class ExitProxyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

        self._old_db = (store.DATA_DIR, store.DB_PATH)
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "accounts.db")

        self._old_ep = exitproxy.CONFIG_PATH
        self._old_rc = recheck.CONFIG_PATH
        cfgdir = os.path.join(self.tmp.name, "config")
        os.makedirs(cfgdir, exist_ok=True)
        exitproxy.CONFIG_PATH = os.path.join(cfgdir, "exitproxy.json")
        recheck.CONFIG_PATH = os.path.join(cfgdir, "recheck.json")
        with open(recheck.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"mihomo": {"host": "cli-proxy-mihomo"}}, fh)
        # 环境变量会盖掉配置里的 host，测试里钉死
        self._env = mock.patch.dict(os.environ, {
            "WB_PROXY_DISCOVER_HOST": "", "WB_PROXY_DISCOVER_PORTS": "",
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def _restore(self) -> None:
        store.DATA_DIR, store.DB_PATH = self._old_db
        exitproxy.CONFIG_PATH = self._old_ep
        recheck.CONFIG_PATH = self._old_rc

    def acc(self, uid: str, realm: str = "intl", proxy: str = "",
            slot: str = "") -> Account:
        a = Account()
        a.uid = uid
        a.nickname = uid
        a.realm = realm
        a.access_token = "at-" + uid
        a.expires_at = 4102444800
        a.proxy = proxy
        a.proxy_slot = slot
        return a

    def seed(self, ports: list[int]) -> list[int]:
        """按给定端口插号，返回行 id。"""
        store.upsert([self.acc(f"u-{i}") for i in range(len(ports))])
        # list_accounts() 按更新时间排序，和导出的 ORDER BY id 不一致；
        # 用同一条 SQL 的 id 顺序，端口才不会错配到别的号上。
        ids = [r[0] for r in store.load_for_export(with_ids=True)]
        for row_id, port in zip(ids, ports):
            store.set_exit_port([row_id], port)
        return ids

    def rows(self):
        return store.load_for_export(with_ids=True)


class ConfigTests(ExitProxyTestBase):
    def test_default_host_falls_back_to_node_config(self):
        cfg = exitproxy.load_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "override")
        self.assertEqual(exitproxy.resolve_host(cfg), "cli-proxy-mihomo")
        self.assertEqual(exitproxy.proxy_url(17978, cfg),
                         "http://cli-proxy-mihomo:17978")

    def test_explicit_host_wins_and_scheme_port_are_stripped(self):
        for raw, expected in (
            ("10.0.0.9", "10.0.0.9"),
            ("http://10.0.0.9:9090/", "10.0.0.9"),
            ("https://proxy.local:9090", "proxy.local"),
        ):
            cfg = {**exitproxy.load_config(), "host": raw}
            self.assertEqual(exitproxy.resolve_host(cfg), expected, raw)
        # 非法字符不能拼进 URL
        self.assertEqual(exitproxy.resolve_host({"host": "bad host/../x"}), "")

    def test_proxy_url_rejects_zero_and_out_of_range(self):
        self.assertEqual(exitproxy.proxy_url(0), "")
        self.assertEqual(exitproxy.proxy_url(-1), "")
        self.assertEqual(exitproxy.proxy_url(70000), "")
        self.assertEqual(exitproxy.proxy_url(17901, {"host": "", "scheme": "https"}),
                         "https://cli-proxy-mihomo:17901")

    def test_save_config_is_atomic_and_coerces_bad_values(self):
        merged = exitproxy.save_config({"enabled": False, "mode": "nope",
                                        "scope": "intl", "host": "my-mihomo"})
        self.assertFalse(merged["enabled"])
        self.assertEqual(merged["mode"], "override")
        self.assertEqual(merged["scope"], "intl")
        self.assertEqual(exitproxy.load_config()["host"], "my-mihomo")
        self.assertTrue(os.path.exists(exitproxy.CONFIG_PATH + ".bak") is False)
        exitproxy.save_config({"enabled": True})
        self.assertTrue(os.path.exists(exitproxy.CONFIG_PATH + ".bak"))
        self.assertTrue(exitproxy.load_config()["enabled"])

    def test_broken_config_file_falls_back_to_defaults(self):
        with open(exitproxy.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(exitproxy.load_config()["mode"], "override")


class ApplyTests(ExitProxyTestBase):
    def test_each_account_gets_its_own_port_and_slot_is_cleared(self):
        self.seed([17901, 17902, 17903])
        rows = self.rows()
        # 先给一个号塞上 hub 的槽绑定，override 应当把它清掉
        rows[0][1].proxy_slot = "slot-7"
        rows[0][1].proxy = "http://old-slot:8080"
        stat = exitproxy.apply(rows, exitproxy.load_config())

        self.assertEqual(stat["applied"], 3)
        self.assertEqual(stat["skipped"], 0)
        urls = [formats.to_hub_flat(a)["proxy"] for _, a, _ in rows]
        self.assertEqual(urls, [
            "http://cli-proxy-mihomo:17901",
            "http://cli-proxy-mihomo:17902",
            "http://cli-proxy-mihomo:17903",
        ])
        # 槽不清，hub 的 apply_proxy_slots() 会用 settings.json 盖掉这个 URL
        self.assertTrue(all(formats.to_hub_flat(a)["proxySlot"] == ""
                            for _, a, _ in rows))

    def test_login_bound_proxy_wins_over_zero_port_and_fill_mode(self):
        """官网登录绑的精确代理不能被 exit_port=0 清成直连，也不能被「仅补空」跳过。"""
        url = "http://127.0.0.1:7891"
        self.seed([0])
        rows = self.rows()
        rows[0][1].exit_proxy = url
        rows[0][1].proxy = "http://old:8080"
        rows[0][1].proxy_slot = "slot-9"
        cfg = {**exitproxy.load_config(), "mode": "fill"}
        stat = exitproxy.apply(rows, cfg)
        self.assertEqual(stat["bound"], 1)
        self.assertEqual(stat["cleared"], 0)
        flat = formats.to_hub_flat(rows[0][1])
        self.assertEqual(flat["proxy"], url)
        self.assertEqual(flat["proxySlot"], "")
        self.assertNotIn("exitProxy", flat)

    def test_port_for_proxy_only_matches_configured_mihomo_host(self):
        self.assertEqual(exitproxy.port_for_proxy("http://cli-proxy-mihomo:17955"), 17955)
        self.assertEqual(exitproxy.port_for_proxy("cli-proxy-mihomo:17955"), 17955)
        self.assertEqual(exitproxy.port_for_proxy("http://user:pw@cli-proxy-mihomo:17955"),
                         17955)
        # 手填的独立代理主机名对不上，硬取端口会拼到另一台机器
        self.assertEqual(exitproxy.port_for_proxy("http://127.0.0.1:7891"), 0)
        self.assertEqual(exitproxy.port_for_proxy(""), 0)
        self.assertEqual(exitproxy.port_for_proxy("http://cli-proxy-mihomo:99999"), 0)

    def test_zero_port_clears_proxy_to_direct(self):
        self.seed([17901, 0])
        rows = self.rows()
        rows[1][1].proxy = "http://old:8080"
        rows[1][1].proxy_slot = "slot-2"
        stat = exitproxy.apply(rows, exitproxy.load_config())
        self.assertEqual(stat["applied"], 1)
        self.assertEqual(stat["cleared"], 1)
        flat = formats.to_hub_flat(rows[1][1])
        self.assertEqual(flat["proxy"], "")
        self.assertEqual(flat["proxySlot"], "")

    def test_fill_mode_preserves_existing_hub_binding(self):
        self.seed([17901, 17902])
        rows = self.rows()
        rows[0][1].proxy_slot = "slot-7"
        rows[0][1].proxy = "http://old-slot:8080"
        cfg = {**exitproxy.load_config(), "mode": "fill"}
        stat = exitproxy.apply(rows, cfg)
        self.assertEqual(stat["applied"], 1)
        self.assertEqual(stat["skipped"], 1)
        self.assertEqual(formats.to_hub_flat(rows[0][1])["proxySlot"], "slot-7")
        self.assertEqual(formats.to_hub_flat(rows[1][1])["proxy"],
                         "http://cli-proxy-mihomo:17902")

    def test_scope_intl_leaves_cn_untouched(self):
        store.upsert([self.acc("u-intl", "intl", proxy="http://keep:1", slot="s1"),
                      self.acc("u-cn", "cn", proxy="http://keep:2", slot="s2")])
        first = store.load_for_export(with_ids=True)
        intl_id = next(i for i, a, _p in first if a.uid == "u-intl")
        store.set_exit_port([intl_id], 17901)
        rows = self.rows()
        cfg = {**exitproxy.load_config(), "scope": "intl"}
        stat = exitproxy.apply(rows, cfg)
        by_uid = {a.uid: a for _, a, _ in rows}
        self.assertEqual(stat["applied"], 1)
        self.assertEqual(stat["skipped"], 1)
        self.assertEqual(formats.to_hub_flat(by_uid["u-cn"])["proxySlot"], "s2")
        self.assertEqual(formats.to_hub_flat(by_uid["u-intl"])["proxySlot"], "")

    def test_disabled_leaves_every_field_untouched(self):
        self.seed([17901, 17902])
        rows = self.rows()
        rows[0][1].proxy_slot = "slot-7"
        stat = exitproxy.apply(rows, {**exitproxy.load_config(), "enabled": False})
        self.assertEqual(stat["skipped"], 2)
        self.assertEqual(formats.to_hub_flat(rows[0][1])["proxySlot"], "slot-7")
        self.assertEqual(formats.to_hub_flat(rows[1][1])["proxy"], "")

    def test_with_ids_keeps_row_order_and_ports(self):
        self.seed([17901, 0, 17903])
        rows = self.rows()
        self.assertEqual([p for _, _, p in rows], [17901, 0, 17903])
        self.assertEqual([i for i, _, _ in rows], sorted(i for i, _, _ in rows))
        # 不带 with_ids 时仍是原来的 Account 列表，旧调用方不受影响
        self.assertTrue(all(isinstance(a, Account) for a in store.load_for_export()))


class ExportRouteTests(ExitProxyTestBase):
    def _fake(self):
        import server

        class Fake(server.Handler):
            def __init__(self):
                self.out = None

            def _json(self, status, data):
                self.out = (status, data)

            def _respond_files(self, outputs, profile_id, opts, realm_mode, total):
                self.out = (200, {"ok": True, "opts": opts,
                                  "outputs": outputs, "total": total})

        return Fake()

    def test_hub_document_carries_per_account_proxy(self):
        self.seed([17901, 17902, 0])
        fake = self._fake()
        fake._store_export({"profile": "hub_document", "scope": "all",
                            "exit_proxy": {"enabled": True, "host": "",
                                           "mode": "override", "scope": "all"}})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["opts"]["exit_proxy"]["applied"], 2)
        self.assertEqual(data["opts"]["exit_proxy"]["cleared"], 1)
        doc = json.loads(data["outputs"][0].data.decode("utf-8"))
        urls = [a["proxy"] for a in doc["accounts"]]
        self.assertEqual(urls[:2], ["http://cli-proxy-mihomo:17901",
                                    "http://cli-proxy-mihomo:17902"])
        self.assertEqual(urls[2], "")
        self.assertTrue(all(a["proxySlot"] == "" for a in doc["accounts"]))
        # tasksEnabled 等原有字段不能被这一步冲掉
        self.assertTrue(all(a["tasksEnabled"] for a in doc["accounts"]))

    def test_hub_dir_zip_writes_one_proxy_per_file(self):
        self.seed([17901, 17905])
        fake = self._fake()
        fake._store_export({"profile": "hub_dir_zip", "scope": "all"})
        status, data = fake.out
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data["outputs"][0].data)) as zf:
            docs = [json.loads(zf.read(n)) for n in zf.namelist()
                    if n.endswith(".json")]
        self.assertEqual(sorted(d["proxy"] for d in docs),
                         ["http://cli-proxy-mihomo:17901",
                          "http://cli-proxy-mihomo:17905"])

    def test_explicit_false_skips_without_writing_config(self):
        self.seed([17901])
        fake = self._fake()
        fake._store_export({"profile": "hub_document", "scope": "all",
                            "exit_proxy": False})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertFalse(data["opts"]["exit_proxy"]["enabled"])
        self.assertEqual(data["opts"]["exit_proxy"]["applied"], 0)
        doc = json.loads(data["outputs"][0].data.decode("utf-8"))
        self.assertEqual(doc["accounts"][0]["proxy"], "")
        self.assertFalse(os.path.exists(exitproxy.CONFIG_PATH))


if __name__ == "__main__":
    unittest.main(verbosity=2)
