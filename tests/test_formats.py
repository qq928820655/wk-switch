"""格式识别、归一化、渲染往返、去重的单测。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import formats, pipeline, token as tok  # noqa: E402
from wk.model import from_row, normalize_epoch  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")


def load(name: str) -> bytes:
    with open(os.path.join(SAMPLES, name), "rb") as fh:
        return fh.read()


class ParseTests(unittest.TestCase):
    """七个项目的样例都必须能被读进来，且字段落到同一组语义上。"""

    def check(self, filename: str, expect_source: str, has_nickname: bool = True) -> None:
        parsed = formats.parse_bytes(filename, load(filename))
        self.assertEqual(parsed.source, expect_source, f"{filename} 来源识别错误")
        self.assertEqual(len(parsed.rows), 5, f"{filename} 条数不对")

        accounts, errors = formats.to_accounts(parsed.rows, parsed.source)
        self.assertEqual(errors, [], f"{filename} 归一化报错：{errors}")

        first = accounts[0]
        self.assertEqual(first.uid, "u-cn-1001")
        if has_nickname:
            self.assertEqual(first.nickname, "阿岩")
        self.assertTrue(first.access_token.startswith("eyJ"))
        self.assertEqual(first.refresh_token, "rt-cn-1001-xxxxxxxxxxxxxxxx")
        self.assertEqual(first.realm, "cn")
        self.assertGreater(first.expires_at, 1_700_000_000)

    def test_hub_document(self) -> None:
        self.check("hub-export.json", "hub_document")

    def test_hub_zip(self) -> None:
        self.check("hub-accounts.zip", "hub_flat")

    def test_manager_zip(self) -> None:
        self.check("manager-accounts.zip", "nested")

    def test_panel_zip(self) -> None:
        self.check("panel-auths.zip", "nested")

    def test_switch_json(self) -> None:
        self.check("switch-accounts.json", "switch_array")

    def test_qoder_json(self) -> None:
        self.check("qoder-accounts.json", "switch_array")

    def test_daily_json(self) -> None:
        self.check("daily-login.json", "daily_json", has_nickname=False)

    def test_daily_env(self) -> None:
        parsed = formats.parse_bytes("daily-env.txt", load("daily-env.txt"))
        self.assertEqual(parsed.source, "daily_env")
        self.assertEqual(len(parsed.rows), 3)  # 只有带 RT 的账号才写成环境变量行
        self.assertTrue(parsed.rows[0]["access_token"].startswith("eyJ"))
        self.assertTrue(parsed.rows[0]["phone"].startswith("1380000"))

    def test_envelope_account(self) -> None:
        parsed = formats.parse_bytes("envelope-account.json", load("envelope-account.json"))
        accounts, _ = formats.to_accounts(parsed.rows)
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0].is_envelope)
        self.assertEqual(accounts[0].nickname, "信封号")

    def test_empty_file_rejected(self) -> None:
        with self.assertRaises(ValueError):
            formats.parse_bytes("empty.json", b"")

    def test_broken_json_rejected(self) -> None:
        with self.assertRaises(ValueError):
            formats.parse_bytes("bad.json", b"{not json")


class NormalizeTests(unittest.TestCase):
    def test_millis_converted(self) -> None:
        self.assertEqual(normalize_epoch(1795254516410), 1795254516)
        self.assertEqual(normalize_epoch(1795254516), 1795254516)
        self.assertEqual(normalize_epoch(""), 0)

    def test_nested_layers(self) -> None:
        row = {
            "account": {"uid": "n-1", "nickname": "嵌套", "enterpriseId": "e-9"},
            "auth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 100, "realm": "cn"},
            "device_token": "dt",
        }
        acc = from_row(row)
        self.assertEqual(acc.uid, "n-1")
        self.assertEqual(acc.nickname, "嵌套")
        self.assertEqual(acc.enterprise_id, "e-9")
        self.assertEqual(acc.access_token, "at")
        self.assertEqual(acc.device_token, "dt")

    def test_nickname_object_does_not_leak(self) -> None:
        # 加密信封形态的 nickname 是对象，直接 str() 会写出脏数据
        acc = from_row({"uid": "x", "access_token": "at",
                        "nickname": {"$wbEncrypted": 1, "envelope": "zz"}})
        self.assertEqual(acc.nickname, "")

    def test_unknown_fields_kept(self) -> None:
        acc = from_row({"uid": "x", "access_token": "at", "customField": "keep-me"})
        self.assertEqual(acc.extra.get("customField"), "keep-me")


class RenderTests(unittest.TestCase):
    """每个目标格式渲染后必须能被自己的解析器读回来。"""

    def accounts(self):
        parsed = formats.parse_bytes("hub-export.json", load("hub-export.json"))
        accs, _ = formats.to_accounts(parsed.rows)
        return accs

    def test_roundtrip_each_profile(self) -> None:
        source = self.accounts()
        for profile_id in ("hub_document", "hub_dir_zip", "manager_zip",
                           "panel_zip", "switch_json", "qoder_json",
                           "daily_json", "daily_env"):
            with self.subTest(profile=profile_id):
                outputs = formats.render(source, profile_id)
                self.assertEqual(len(outputs), 1)
                back = formats.parse_bytes(outputs[0].name, outputs[0].data)
                self.assertEqual(len(back.rows), len(source),
                                 f"{profile_id} 往返后条数变了")
                accounts, errors = formats.to_accounts(back.rows)
                self.assertEqual(errors, [])
                self.assertEqual(accounts[0].uid, source[0].uid)
                self.assertEqual(accounts[0].access_token, source[0].access_token)

    def test_manager_zip_filenames(self) -> None:
        outputs = formats.render(self.accounts(), "manager_zip")
        zf = zipfile.ZipFile(io.BytesIO(outputs[0].data))
        names = zf.namelist()
        self.assertTrue(all(n.startswith("workbuddy-") for n in names), names)
        self.assertTrue(all(n.endswith(".json") for n in names), names)

    def test_nested_shape_for_manager(self) -> None:
        outputs = formats.render(self.accounts(), "manager_zip")
        zf = zipfile.ZipFile(io.BytesIO(outputs[0].data))
        doc = json.loads(zf.read(zf.namelist()[0]))
        # manager 的 _uploaded_account 只认这两层
        self.assertIn("account", doc)
        self.assertIn("auth", doc)
        self.assertIn("accessToken", doc["auth"])
        self.assertIn("expiresAt", doc["auth"])

    def test_hub_document_header(self) -> None:
        outputs = formats.render(self.accounts(), "hub_document")
        doc = json.loads(outputs[0].data)
        self.assertEqual(doc["format"], "workbuddy-accounts")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["count"], len(doc["accounts"]))

    def test_empty_export_rejected(self) -> None:
        with self.assertRaises(ValueError):
            formats.render([], "hub_document")

    def test_unknown_profile_rejected(self) -> None:
        with self.assertRaises(ValueError):
            formats.render(self.accounts(), "nope")


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        parsed = formats.parse_bytes("hub-export.json", load("hub-export.json"))
        self.accounts, _ = formats.to_accounts(parsed.rows)

    def test_dedupe_by_uid(self) -> None:
        result = pipeline.convert(self.accounts, {"dedupe_key": "uid"})
        self.assertEqual(result.input_count, 5)
        self.assertEqual(result.kept, 4)
        self.assertEqual(result.merged, 1)

    def test_dedupe_keep_newest(self) -> None:
        result = pipeline.convert(self.accounts, {
            "dedupe_key": "uid", "dedupe_policy": "keep_newest",
        })
        kept = [a for a in result.accounts if a.uid == "u-cn-1001"]
        self.assertEqual(len(kept), 1)
        # 保留到期更晚的那条（第一条 20 天，重复那条 2 天）
        self.assertEqual(kept[0].refresh_token, "rt-cn-1001-xxxxxxxxxxxxxxxx")

    def test_dedupe_keep_both_renames(self) -> None:
        result = pipeline.convert(self.accounts, {
            "dedupe_key": "uid", "dedupe_policy": "keep_both",
        })
        self.assertEqual(result.kept, 5)
        uids = [a.uid for a in result.accounts]
        self.assertIn("u-cn-1001-2", uids)

    def test_no_dedupe(self) -> None:
        result = pipeline.convert(self.accounts, {"dedupe_key": "none"})
        self.assertEqual(result.kept, 5)

    def test_keep_both_is_idempotent(self) -> None:
        """同一份数据跑两遍 convert，uid 不能越叠越长。

        导入页每改一个选项就重跑一次 convert，而 keep_both 是原地改 uid 的：
        拿上一轮的 `x-2` 当基座，再加后缀就成了 `x-2-2`，跑几次叠几层。
        """
        opts = {"dedupe_key": "uid", "dedupe_policy": "keep_both"}
        first = [a.uid for a in pipeline.convert(self.accounts, opts).accounts]
        second = [a.uid for a in pipeline.convert(self.accounts, opts).accounts]
        third = [a.uid for a in pipeline.convert(self.accounts, opts).accounts]
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertIn("u-cn-1001-2", third)
        self.assertNotIn("u-cn-1001-2-2", third)

    def test_ref_never_leaks_into_export_payload(self) -> None:
        """`ref` 是给界面认行的，不属于账号数据。"""
        self.accounts[0].ref = "r1"
        self.assertNotIn("ref", self.accounts[0].to_dict())
        self.assertNotIn("ref", self.accounts[0].masked())

    def test_require_token_drops_empty(self) -> None:
        parsed = formats.parse_bytes("hub-export.json", load("hub-export.json"))
        rows = [dict(r) for r in parsed.rows]
        rows.append({"uid": "no-token", "nickname": "空号"})
        accounts, _ = formats.to_accounts(rows)
        result = pipeline.convert(accounts, {"require_token": True, "dedupe_key": "none"})
        self.assertEqual(result.kept, 5)
        self.assertTrue(any(i.code == "missing_token" for i in result.issues))

    def test_envelope_flagged_but_kept(self) -> None:
        parsed = formats.parse_bytes("envelope-account.json", load("envelope-account.json"))
        accounts, _ = formats.to_accounts(parsed.rows)
        result = pipeline.convert(accounts, {"dedupe_key": "none"})
        self.assertEqual(result.kept, 1)
        self.assertTrue(any(i.code == "envelope" for i in result.issues))

    def test_envelope_rejected_when_disallowed(self) -> None:
        parsed = formats.parse_bytes("envelope-account.json", load("envelope-account.json"))
        accounts, _ = formats.to_accounts(parsed.rows)
        result = pipeline.convert(accounts, {"dedupe_key": "none", "allow_envelope": False})
        self.assertEqual(result.kept, 0)

    def test_uid_filled_from_jwt(self) -> None:
        parsed = formats.parse_bytes("hub-export.json", load("hub-export.json"))
        rows = [dict(r) for r in parsed.rows[:1]]
        rows[0].pop("uid", None)
        accounts, _ = formats.to_accounts(rows)
        self.assertEqual(accounts[0].uid, "u-cn-1001")

    def test_summary_counts(self) -> None:
        stats = pipeline.summarize(self.accounts)
        self.assertEqual(stats["total"], 5)
        self.assertEqual(sum(s["count"] for s in stats["states"]), 5)
        self.assertEqual(sum(s["count"] for s in stats["realms"]), 5)


class TokenTests(unittest.TestCase):
    def test_realm_from_issuer(self) -> None:
        parsed = formats.parse_bytes("hub-export.json", load("hub-export.json"))
        accounts, _ = formats.to_accounts(parsed.rows)
        self.assertEqual(tok.detect_realm(accounts[0].access_token), "cn")
        self.assertEqual(tok.detect_realm(accounts[1].access_token), "intl")

    def test_api_key_forces_cn(self) -> None:
        self.assertEqual(tok.detect_realm("ck_abc.secret"), "cn")
        self.assertEqual(tok.detect_realm("pt_abc.secret"), "cn")

    def test_expired_state(self) -> None:
        self.assertEqual(tok.token_state("eyJ.x.y", 1, ""), "expired")
        self.assertEqual(tok.token_state("eyJ.x.y", 1, "rt"), "refreshable")

    def test_state_labels_present(self) -> None:
        for state in ("empty", "envelope", "api_key", "expired",
                      "refreshable", "expiring", "valid", "unknown"):
            self.assertIn(state, tok.STATE_LABEL)

    def test_domain_aligned_to_realm(self) -> None:
        # 区域以 token 为准，域名属于另一边时要纠正
        self.assertEqual(tok.domain_for_realm("cn", "www.workbuddy.ai"),
                         "copilot.tencent.com")
        self.assertEqual(tok.domain_for_realm("intl", "copilot.tencent.com"),
                         "www.workbuddy.ai")


class ZipSafetyTests(unittest.TestCase):
    def test_zip_slip_filtered(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.json", json.dumps({"uid": "x", "access_token": "at"}))
            zf.writestr("/abs.json", json.dumps({"uid": "y", "access_token": "at"}))
            zf.writestr("ok.json", json.dumps({"uid": "z", "access_token": "at"}))
        parsed = formats.parse_bytes("evil.zip", buf.getvalue())
        # 越界与绝对路径都被丢掉，只剩 ok.json
        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(parsed.rows[0]["uid"], "z")

    def test_bad_zip_rejected(self) -> None:
        with self.assertRaises(ValueError):
            formats.parse_bytes("bad.zip", b"PK\x03\x04broken")


if __name__ == "__main__":
    unittest.main(verbosity=2)
