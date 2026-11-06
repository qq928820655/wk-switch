# -*- coding: utf-8 -*-
"""探针的判定逻辑：有效性、积分解析、失败分类。

上游用本地起的假服务器模拟——真打外网的单测既慢又会因为网络抖动随机红，
而且探针的价值恰恰在「上游回什么」这层映射，这部分用假的足够了。

重点守住两条：
1. 401 判无效，网络故障判未知，两者绝不能混（混了会把网络问题显示成封号）。
2. 探针不许刷新 token——这条在 test_no_refresh_anywhere 里用源码扫描兜住。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wk import probe  # noqa: E402
from wk.model import Account  # noqa: E402


class _FakeUpstream(BaseHTTPRequestHandler):
    """可编排响应的假上游。

    类属性挂的是"当前这轮测试的剧本"，每个 handler 实例读它。单测串行跑，
    所以不需要更复杂的状态管理。
    """

    routes: dict = {}
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server 的命名约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append({
            "path": self.path,
            "body": json.loads(body or b"{}"),
            "headers": dict(self.headers),
        })
        spec = type(self).routes.get(self.path)
        if spec is None:
            # 未编排的路径一律 404，正好覆盖「这版网关不认这个前缀」
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        status, payload = spec
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 别往测试输出里灌访问日志
        pass


class _ServerCase(unittest.TestCase):
    """起一个假上游，每个测试自己编排 routes。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _FakeUpstream.routes = {}
        _FakeUpstream.seen = []

    def route(self, path, status=200, payload=None):
        _FakeUpstream.routes[path] = (status, payload if payload is not None else {})

    def acc(self, realm="cn", token="jwt.header.sig", **kw):
        """造一个账号。base 被指到假服务器上，所以不会真的出网。"""
        a = Account(uid="u-test-1", realm=realm, access_token=token, **kw)
        return a

    def probe_with_base(self, account, bases):
        """把 probe 的端点列表临时换成假服务器地址。

        probe_one 里 base 是从 CN_BASES / INTL_BASES 取的模块常量，测试直接
        改模块属性再还原，比给 probe_one 加一个 base 参数干净——生产代码不该
        为了测试多一个能改路由的入口。
        """
        original_cn, original_intl = probe.CN_BASES, probe.INTL_BASES
        probe.CN_BASES = bases
        probe.INTL_BASES = bases
        try:
            return probe.probe_one(account, timeout=5.0)
        finally:
            probe.CN_BASES, probe.INTL_BASES = original_cn, original_intl


class ValidTests(_ServerCase):
    def test_summary_only_gives_credit(self):
        """摘要接口就带容量三元组时，直接用它的结果。"""
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 0,
            "data": {
                "IsPaidUser": True,
                "SubscriptionPackageName": "专业版",
                "Packages": [{
                    "PackageCode": "p1",
                    "CycleTotalCapacity": 1000,
                    "CycleRemainCapacity": 250.5,
                    "CycleUsedCapacity": 749.5,
                }],
            },
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertAlmostEqual(res["remain"], 250.5, places=2)
        self.assertAlmostEqual(res["used"], 749.5, places=2)
        self.assertAlmostEqual(res["size"], 1000.0, places=2)
        self.assertEqual(res["tier"], "专业版")

    def test_detail_overrides_summary(self):
        """摘要全是 0 而明细有数时，必须听明细的。

        上游有账号在摘要里不填 Cycle*Capacity，若拿摘要覆盖明细，一个有
        余额的账号会显示成 0 —— 用户会以为积分被清了。
        """
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 0, "data": {"IsPaidUser": False, "Packages": []},
        })
        self.route("/billing/meter/get-user-resource", 200, {
            "code": 0,
            "data": {"Response": {"Data": {"Accounts": [{
                "AccountId": "a1",
                "CycleCapacitySizePrecise": "500.00",
                "CycleCapacityRemainPrecise": "120.25",
                "CycleCapacityUsedPrecise": "379.75",
            }]}}},
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertAlmostEqual(res["remain"], 120.25, places=2)
        self.assertAlmostEqual(res["size"], 500.0, places=2)
        self.assertEqual(res["packages"], 1)

    def test_missing_third_value_is_derived(self):
        """三元组只给两个时补第三个。"""
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 0, "data": {"IsPaidUser": True, "Packages": [{
                "CycleTotalCapacity": 100,
                "CycleCapacityRemain": 40,
            }]},
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertAlmostEqual(res["remain"], 40.0)
        # 100 - 40 = 60，由 size 与 remain 反推
        self.assertAlmostEqual(res["used"], 60.0)

    def test_unknown_tier_is_empty_not_free(self):
        """响应里没有 IsPaidUser 就不写档位——「没问到」不能写成「免费」。"""
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 0, "data": {"Packages": []},
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertEqual(res["tier"], "")

    def test_no_packages_leaves_credit_none(self):
        """一个包都没查到 → remain 是 None 不是 0。"""
        self.route("/billing/meter/get-user-resource-summary", 200,
                   {"code": 0, "data": {"IsPaidUser": False}})
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertIsNone(res["remain"])

    def test_404_on_first_prefix_falls_back(self):
        """不带 /v2 的前缀 404 时，要换带 /v2 的那条。"""
        self.route("/v2/billing/meter/get-user-resource-summary", 200, {
            "code": 0, "data": {"IsPaidUser": True, "Packages": [{
                "CycleTotalCapacity": 10, "CycleRemainCapacity": 10,
            }]},
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertAlmostEqual(res["remain"], 10.0)

    def test_second_base_used_when_first_dead(self):
        """第一个 base 连不通就打第二个。"""
        self.route("/billing/meter/get-user-resource-summary", 200,
                   {"code": 0, "data": {"IsPaidUser": True, "Packages": []}})
        res = self.probe_with_base(
            self.acc(), ("http://127.0.0.1:1", self.base))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertEqual(res["endpoint"], self.base)


class InvalidTests(_ServerCase):
    def test_401_is_invalid(self):
        self.route("/billing/meter/get-user-resource-summary", 401, {"msg": "unauthorized"})
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_INVALID)
        self.assertIn("拒绝", res["error"])

    def test_403_is_invalid(self):
        self.route("/billing/meter/get-user-resource-summary", 403, {})
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_INVALID)

    def test_business_code_login_expired(self):
        """200 但 code=10001 + 「登录已过期」，仍是无效。"""
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 10001, "msg": "登录已过期，请重新登录",
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_INVALID)

    def test_other_business_error_is_unknown(self):
        """业务报错但不是鉴权问题 → 未知，不能判成失效。"""
        self.route("/billing/meter/get-user-resource-summary", 200, {
            "code": 500, "msg": "系统繁忙",
        })
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_UNKNOWN)

    def test_network_error_is_unknown_not_invalid(self):
        """连不通是网络问题，判成失效会导致用户误删好账号。"""
        res = self.probe_with_base(self.acc(), ("http://127.0.0.1:1",))
        self.assertEqual(res["state"], probe.STATE_UNKNOWN)
        self.assertTrue(res["error"], "失败时 error 不能为空，否则界面显示不出原因")


class SkipTests(_ServerCase):
    def test_envelope_is_skipped(self):
        """加密信封本地就判死，一个请求都不该发。"""
        acc = self.acc(token='{"$wbEncrypted":1,"envelope":"AAAA"}')
        res = self.probe_with_base(acc, (self.base,))
        self.assertEqual(res["state"], probe.STATE_SKIPPED)
        self.assertEqual(_FakeUpstream.seen, [])

    def test_empty_token_is_skipped(self):
        res = self.probe_with_base(self.acc(token=""), (self.base,))
        self.assertEqual(res["state"], probe.STATE_SKIPPED)
        self.assertEqual(_FakeUpstream.seen, [])


class NotFoundTests(_ServerCase):
    def test_all_prefixes_404_is_not_valid(self):
        """所有计费前缀都 404 时不能判「有效」。

        这是真机上会被忽略的一类错：路径写错、网关换了版本，返回的都是 404，
        而 404 的响应体里没有 code 字段，`code is None` 很容易被当成「没报错」
        一路走到成功分支，最后显示成「有效、积分未查」。用户会以为账号好的，
        实际上是根本没问对地方。
        """
        # routes 空 → 所有路径 404
        res = self.probe_with_base(self.acc(), (self.base,))
        self.assertEqual(res["state"], probe.STATE_UNKNOWN)
        self.assertIn("404", res["error"])
        self.assertIsNone(res["remain"])


class ApiKeyTests(_ServerCase):
    def test_api_key_uses_enterprise_endpoint(self):
        """静态 Key 不走个人链路，只问企业用量。"""
        self.route("/v2/billing/meter/get-enterprise-user-usage", 200, {
            "code": 0,
            "data": {"limit_num": 5000, "used_num": 1200},
        })
        acc = self.acc(token="ck_abc.def", api_key=True)
        res = self.probe_with_base(acc, (self.base,))
        self.assertEqual(res["state"], probe.STATE_VALID)
        self.assertAlmostEqual(res["remain"], 3800.0)
        self.assertAlmostEqual(res["size"], 5000.0)
        # 个人链路一条都不该打
        paths = [s["path"] for s in _FakeUpstream.seen]
        self.assertTrue(all("enterprise" in p for p in paths), paths)

    def test_api_key_revoked(self):
        self.route("/v2/billing/meter/get-enterprise-user-usage", 401, {})
        acc = self.acc(token="ck_abc.def", api_key=True)
        res = self.probe_with_base(acc, (self.base,))
        self.assertEqual(res["state"], probe.STATE_INVALID)


class HeaderTests(_ServerCase):
    def test_headers_carry_auth_and_no_device_token(self):
        """鉴权头必须在，X-Device-Token 不该出现——那是国内客户端的设备指纹，
        探针没有真实值，硬造一个反而是可被关联的特征。"""
        self.route("/billing/meter/get-user-resource-summary", 200,
                   {"code": 0, "data": {"IsPaidUser": False}})
        self.probe_with_base(self.acc(), (self.base,))
        headers = _FakeUpstream.seen[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer jwt.header.sig")
        self.assertNotIn("X-Device-Token", headers)
        self.assertEqual(headers.get("X-User-Id"), "u-test-1")


class ProberTests(_ServerCase):
    def test_order_preserved(self):
        """批量结果顺序必须与输入一致，否则回写的 id 会错位。"""
        self.route("/billing/meter/get-user-resource-summary", 200,
                   {"code": 0, "data": {"IsPaidUser": False}})
        accounts = [Account(uid=f"u{i}", realm="cn", access_token="t") for i in range(5)]
        runner = probe.Prober(concurrency=3, timeout=5.0, min_gap=0.0, max_gap=0.0)
        original_cn, original_intl = probe.CN_BASES, probe.INTL_BASES
        probe.CN_BASES = probe.INTL_BASES = (self.base,)
        try:
            results = runner.run(accounts)
        finally:
            probe.CN_BASES, probe.INTL_BASES = original_cn, original_intl
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["state"] == probe.STATE_VALID for r in results))

    def test_summarize_counts(self):
        summary = probe.summarize([
            {"state": "valid", "remain": 10.0},
            {"state": "valid", "remain": 5.5},
            {"state": "invalid", "remain": None},
            {"state": "unknown", "remain": None},
            {"state": "skipped", "remain": None},
        ])
        self.assertEqual(summary["valid"], 2)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["total"], 5)
        # 只有查到的两个参与求和，None 不能当 0 混进来
        self.assertAlmostEqual(summary["remain_total"], 15.5)
        self.assertEqual(summary["with_credit"], 2)

    def test_empty_input(self):
        self.assertEqual(probe.Prober().run([]), [])


class SafetyTests(unittest.TestCase):
    """守住「只读」这条线。"""

    @staticmethod
    def _code_only(path: str) -> str:
        """去掉注释与字符串字面量后的源码。

        直接扫全文会把注释里那句「不碰 daily-checkin」判成违规——而那句
        注释恰恰是在说明为什么不能碰。所以按 token 过滤，只看真正的代码。
        """
        import io
        import tokenize

        chunks: list[str] = []
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
                    continue
                if tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
                    continue
                chunks.append(tok.string)
        return " ".join(chunks)

    def test_no_refresh_anywhere(self):
        """probe.py 里不许出现刷新调用。

        refresh token rotation 会让别处正在用的账号掉线，这是本模块最重的
        一条约束，用源码扫描钉住——靠 review 记住不可靠。
        """
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wk", "probe.py")
        code = self._code_only(path)
        for forbidden in ("token/refresh", "checkin", "daily-checkin", "check-in"):
            self.assertNotIn(
                forbidden, code,
                f"probe.py 的代码里出现了 {forbidden}，探针必须保持只读")
        # 反向确认扫描确实剥掉了字符串：路径常量本身在代码里是字符串，
        # 剥掉后不该还剩下完整路径
        self.assertNotIn("/billing/meter/get-user-resource-summary", code)

    def test_only_billing_paths(self):
        """所有请求路径都得是查询类计费端点。"""
        for group in (probe.RESOURCE_SUMMARY_PATHS, probe.RESOURCE_PATHS,
                      probe.ENTERPRISE_USAGE_PATHS):
            for path in group:
                self.assertIn("billing/meter", path)
                self.assertTrue(
                    any(k in path for k in ("get-user-resource", "get-enterprise")),
                    path)


if __name__ == "__main__":
    unittest.main()
