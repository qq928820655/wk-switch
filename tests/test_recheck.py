"""封号复检：判定口径、名单流转、mihomo 端口解析。"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wk import probe, recheck, store  # noqa: E402
from wk.model import Account  # noqa: E402


def acc(**kw) -> Account:
    base = dict(uid="u-1", realm="intl", access_token="tok", nickname="测试号")
    base.update(kw)
    return Account(**base)


class ReplyTests(unittest.TestCase):
    """`_read_chat_reply` 的判定口径。

    这一层出错，整套复检的结论就全反——把「还封着」判成「已解封」，用户会
    把废号重新导回生产；反过来判会把好号永久留在名单里。所以每个分支都要钉。
    """

    def reply(self, status, body):
        return probe._read_chat_reply(status, body, "https://x/v2/chat/completions", 0.0)

    def test_200_is_revived(self):
        res = self.reply(200, {"choices": [{"message": {"content": "pong"}}]})
        self.assertEqual(res["verdict"], probe.RECHECK_REVIVED)

    def test_11140_is_banned(self):
        body = {"code": 11140, "msg": "request illegal",
                "displayMsg": {"zh": "账号存在异常行为，已被限制使用"}}
        self.assertEqual(self.reply(403, body)["verdict"], probe.RECHECK_BANNED)

    def test_blocked_wording_is_banned(self):
        """README 里第二种形态：没有 code，只有「请求被拦截」。"""
        body = {"msg": "request illegal",
                "displayMsg": {"zh": "请求被拦截，请重新发送"}}
        self.assertEqual(self.reply(403, body)["verdict"], probe.RECHECK_BANNED)

    def test_banned_even_on_200(self):
        """上游偶尔用 200 带业务错误体。先认 200 会把封号判成解封。"""
        body = {"code": 11140, "msg": "request illegal"}
        self.assertEqual(self.reply(200, body)["verdict"], probe.RECHECK_BANNED)

    def test_401_is_auth_not_unknown(self):
        res = self.reply(401, {"_raw": "<html><title>401 Authorization Required</title>"})
        self.assertEqual(res["verdict"], probe.RECHECK_AUTH)

    def test_403_without_restrict_marker_is_unknown(self):
        """内容审核的 403 不是封号——判成 banned 会把人往错的方向引。"""
        body = {"msg": "content policy violation"}
        self.assertEqual(self.reply(403, body)["verdict"], probe.RECHECK_UNKNOWN)

    def test_429_is_unknown(self):
        self.assertEqual(self.reply(429, {"msg": "rate limit"})["verdict"],
                         probe.RECHECK_UNKNOWN)

    def test_500_is_unknown(self):
        self.assertEqual(self.reply(500, {})["verdict"], probe.RECHECK_UNKNOWN)


class BadRequestTests(unittest.TestCase):
    """400 的说明要能回答「是不是我地址填错了」。

    400 是「地址通了、凭据也过了，但上游不认这份请求」，和账号状态无关。
    界面上只写「未确认」时，用户第一反应永远是去改地址——所以文案必须把
    「地址和凭据都通」写出来，并且带上上游原话。
    """

    def reply(self, status, body):
        return probe._read_chat_reply(status, body, "https://x/v2/chat/completions", 0.0)

    def test_400_says_address_is_fine(self):
        res = self.reply(400, {"code": 11133, "msg": "model_param_invalid"})
        self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)
        self.assertIn("地址和凭据都通", res["detail"])
        self.assertIn("不是账号状态", res["detail"])

    def test_400_quotes_upstream(self):
        res = self.reply(400, {"msg": "max_tokens must be >= 16"})
        self.assertIn("max_tokens must be >= 16", res["detail"])

    def test_400_known_code_hint(self):
        res = self.reply(400, {"code": 11133})
        self.assertIn("模型名或参数不被接受", res["detail"])

    def test_400_unknown_code_still_explains(self):
        res = self.reply(400, {"error": {"code": "invalid_request"}})
        self.assertIn("请求体被上游拒绝", res["detail"])
        self.assertIn("invalid_request", res["detail"])

    def test_error_code_reads_nested(self):
        self.assertEqual(probe._error_code({"error": {"code": "e1"}}), "e1")
        self.assertEqual(probe._error_code({"code": 0}), "0")
        self.assertEqual(probe._error_code({}), "")
        self.assertEqual(probe._error_code("not a dict"), "")

    def test_clip_collapses_whitespace(self):
        self.assertEqual(probe._clip("  a\n\n b  "), "a b")
        self.assertTrue(probe._clip("x" * 200, 10).endswith("…"))


class MarkerTests(unittest.TestCase):

    def test_looks_restricted(self):
        self.assertTrue(probe.looks_restricted('{"code":11140}'))
        self.assertTrue(probe.looks_restricted("Your account has been restricted"))
        self.assertFalse(probe.looks_restricted("invalid api key"))
        self.assertFalse(probe.looks_restricted(""))


class LocalSkipTests(unittest.TestCase):
    """本地就能判定的不发请求。"""

    def test_no_token(self):
        res = probe.chat_probe(acc(access_token=""))
        self.assertEqual(res["verdict"], probe.RECHECK_SKIPPED)

    def test_api_key(self):
        res = probe.chat_probe(acc(api_key=True))
        self.assertEqual(res["verdict"], probe.RECHECK_SKIPPED)

    def test_envelope(self):
        a = acc()
        a.auth_raw = {"$wbEncrypted": 1, "envelope": "abc"}
        # is_envelope 看的是 access_token 的形态，这里直接构造
        a.access_token = '{"$wbEncrypted":1,"envelope":"abc"}'
        self.assertTrue(a.is_envelope)
        self.assertEqual(probe.chat_probe(a)["verdict"], probe.RECHECK_SKIPPED)


class _ChatServer:
    """起一个假上游，按预设返回。只为了让 chat_probe 走完整 HTTP 路径。"""

    def __init__(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class HttpPathTests(unittest.TestCase):
    """真实 HTTP 路径：路径回退、结论映射。"""

    def test_banned_over_http(self):
        body = {"code": 11140, "msg": "request illegal",
                "displayMsg": {"zh": "账号存在异常行为，已被限制使用"}}
        with _ChatServer(403, body) as srv:
            base = f"http://127.0.0.1:{srv.port}"
            with mock.patch.object(probe, "CHAT_BASES_INTL", (base,)):
                res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(res["verdict"], probe.RECHECK_BANNED)
        # 第一个路径就通了，不该再试第二个
        self.assertEqual(srv.hits, ["/v2/chat/completions"])

    def test_revived_over_http(self):
        with _ChatServer(200, {"choices": [{"message": {"content": "pong"}}]}) as srv:
            base = f"http://127.0.0.1:{srv.port}"
            with mock.patch.object(probe, "CHAT_BASES_INTL", (base,)):
                res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(res["verdict"], probe.RECHECK_REVIVED)

    def test_404_falls_back_to_next_path(self):
        """带 /v2 的路径不通时换不带前缀的，别把 404 当成账号问题。"""
        with _ChatServer(200, {"choices": []}) as srv:
            base = f"http://127.0.0.1:{srv.port}"
            statuses = {"/v2/chat/completions": 404, "/chat/completions": 200}

            original = probe._post_json

            def fake(url, body, headers, timeout, proxy=""):
                code = statuses.get(url.split(str(srv.port))[-1], 404)
                return original(url, body, headers, timeout, proxy) if code == 200 else (404, {})

            with mock.patch.object(probe, "CHAT_BASES_INTL", (base,)), \
                    mock.patch.object(probe, "_post_json", fake):
                res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(res["verdict"], probe.RECHECK_REVIVED)
        self.assertIn("/chat/completions", srv.hits[-1])

    def test_network_error_is_unknown(self):
        """连不通是网络问题，绝不判成「仍被封」。"""
        with mock.patch.object(probe, "CHAT_BASES_INTL", ("http://127.0.0.1:1",)):
            res = probe.chat_probe(acc(), timeout=3)
        self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)


class TrackFlowTests(unittest.TestCase):
    """名单状态流转。"""

    def test_next_track(self):
        self.assertEqual(recheck.next_track("banned", probe.RECHECK_REVIVED), "revived")
        self.assertEqual(recheck.next_track("", probe.RECHECK_BANNED), "banned")
        self.assertEqual(recheck.next_track("banned", probe.RECHECK_AUTH), "auth")
        # 未确认不改判，保留原状态：网络抖动不该把封号洗白
        self.assertEqual(recheck.next_track("banned", probe.RECHECK_UNKNOWN), "banned")
        self.assertEqual(recheck.next_track("", probe.RECHECK_UNKNOWN), "pending")

    def test_summarize_counts(self):
        rows = [
            {"verdict": probe.RECHECK_BANNED},
            {"verdict": probe.RECHECK_REVIVED},
            {"verdict": probe.RECHECK_REVIVED},
            {"verdict": "skipped", "skipped": True},
        ]
        s = recheck.summarize(rows)
        self.assertEqual(s[probe.RECHECK_REVIVED], 2)
        self.assertEqual(s[probe.RECHECK_BANNED], 1)
        self.assertEqual(s["skipped"], 1)
        self.assertEqual(s["total"], 4)


class PortTests(unittest.TestCase):

    def test_ranges(self):
        self.assertEqual(recheck.parse_ports("17901-17903"), [17901, 17902, 17903])
        self.assertEqual(recheck.parse_ports("7890"), [7890])
        self.assertEqual(recheck.parse_ports("1,3,3,5"), [1, 3, 5])
        self.assertEqual(recheck.parse_ports(""), [])
        # 反着写也要认
        self.assertEqual(recheck.parse_ports("17903-17901"), [17901, 17902, 17903])

    def test_bad_input(self):
        self.assertEqual(recheck.parse_ports("abc"), [])
        self.assertEqual(recheck.parse_ports("99999"), [])


class ConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-rc-")
        self.path = os.path.join(self.tmp, "recheck.json")
        self.patch = mock.patch.object(recheck, "CONFIG_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_defaults_when_missing(self):
        cfg = recheck.load_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["min_interval"], 6 * 3600)

    def test_save_and_reload(self):
        recheck.save_config({"enabled": True, "min_interval": 900})
        cfg = recheck.load_config()
        self.assertTrue(cfg["enabled"])
        # 低于 10 分钟会被钳回 600 秒——太密等于持续敲门
        self.assertEqual(cfg["min_interval"], 900)

    def test_min_interval_floor(self):
        recheck.save_config({"min_interval": 5})
        self.assertEqual(recheck.load_config()["min_interval"], 600)

    def test_gap_never_zero(self):
        """gap=0 就是并发敲门，踩 hub 留的连坐红线。"""
        recheck.save_config({"gap_seconds": 0})
        self.assertGreaterEqual(recheck.load_config()["gap_seconds"], 1.0)

    def test_broken_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        cfg = recheck.load_config()
        self.assertFalse(cfg["enabled"])

    def test_env_overrides_mihomo_host(self):
        os.environ["WB_PROXY_DISCOVER_HOST"] = "my-mihomo"
        try:
            self.assertEqual(recheck.load_config()["mihomo"]["host"], "my-mihomo")
        finally:
            os.environ.pop("WB_PROXY_DISCOVER_HOST", None)


class RunnerTests(unittest.TestCase):
    """Runner 的节流与轮换。不发真请求。"""

    def test_min_interval_blocks_second_pass(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, enabled=True, min_interval=3600))
        runner = recheck.Runner(cfg)
        a = acc(recheck_at=int(__import__("time").time()) - 60)
        called = []

        def fake_probe(*args, **kw):
            called.append(1)
            return {"verdict": probe.RECHECK_BANNED, "detail": "", "raw": "",
                    "status": 403, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake_probe):
            results = runner.run([(1, a)], force=False)
        self.assertTrue(results[0]["skipped"])
        self.assertEqual(called, [])

    def test_force_bypasses_interval(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, enabled=True, min_interval=3600))
        runner = recheck.Runner(cfg)
        a = acc(recheck_at=int(__import__("time").time()) - 60)

        with mock.patch.object(probe, "chat_probe",
                               lambda *a, **k: {"verdict": probe.RECHECK_REVIVED,
                                                "detail": "", "raw": "", "status": 200,
                                                "endpoint": "", "latency_ms": 1}):
            results = runner.run([(1, a)], force=True)
        self.assertEqual(results[0]["verdict"], probe.RECHECK_REVIVED)

    def test_batch_limit(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, enabled=True, batch_limit=2,
                                   gap_seconds=1.0))
        runner = recheck.Runner(cfg)
        pairs = [(i, acc(uid=f"u{i}")) for i in range(5)]
        with mock.patch.object(probe, "chat_probe",
                               lambda *a, **k: {"verdict": probe.RECHECK_BANNED,
                                                "detail": "", "raw": "", "status": 403,
                                                "endpoint": "", "latency_ms": 1}):
            results = runner.run(pairs, force=True)
        self.assertEqual(len(results), 2)


class BannedHintTests(unittest.TestCase):
    """导入时的自动入名单判据。"""

    def test_detects_upstream_ban(self):
        a = acc()
        a.extra = {"reason": "account banned by upstream (11140)"}
        self.assertTrue(store.banned_hint_of(a))

    def test_ignores_manual_disable(self):
        """手动停用的健康号绝不能进名单——那是用户自己的选择。"""
        a = acc()
        a.extra = {"reason": "manual disable (panel)"}
        self.assertEqual(store.banned_hint_of(a), "")

    def test_ignores_clean_account(self):
        self.assertEqual(store.banned_hint_of(acc()), "")


class ExitPlanTests(unittest.TestCase):
    """出口三档：同一 / 随机 / 自定义。

    这一层错了不会报错，只会「静默地全从同一个 IP 出去」——正是被封的原因，
    所以要逐档钉死：每档给出什么 proxy、退化路径是什么。
    """

    def _cfg(self, **kw):
        return recheck._coerce(dict(recheck.DEFAULTS, **kw))

    def test_normalize_url(self):
        n = recheck.normalize_url
        self.assertEqual(n("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(n("http://1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(n("https://h.example:443/"), "https://h.example:443")
        # 端口缺失 / 非数字 / 协议不收 / 带空白，一律当作没配
        self.assertEqual(n("1.2.3.4"), "")
        self.assertEqual(n("1.2.3.4:abc"), "")
        self.assertEqual(n("socks5://1.2.3.4:1080"), "")
        self.assertEqual(n("http://1.2.3.4:8080 extra"), "")
        self.assertEqual(n(""), "")

    def test_parse_urls_dedup_and_separators(self):
        got = recheck.parse_urls("http://a:1, b:2;http://a:1\nc:3")
        self.assertEqual(got, ["http://a:1", "http://b:2", "http://c:3"])

    def test_legacy_rotate_flag_migrates(self):
        on = recheck._coerce({"rotate_proxy": True})
        off = recheck._coerce({"rotate_proxy": False})
        self.assertEqual(on["exit_mode"], recheck.EXIT_RANDOM)
        self.assertEqual(off["exit_mode"], recheck.EXIT_SAME)
        # 写回时派生字段跟着 exit_mode 走
        self.assertTrue(on["rotate_proxy"])
        self.assertFalse(off["rotate_proxy"])

    def test_removed_custom_mode_folds_to_random(self):
        """custom 已经从选项里拿掉：老配置读进来要折成一个还存在的档。

        折成 random 而不是 same —— 老 custom 是「逐号轮换」，打散 IP 的意图
        和 random 一致；折成 same 会把一堆号全塞到同一个出口，正好相反。
        """
        cfg = self._cfg(exit_mode="custom", rotate_proxy=True)
        self.assertEqual(cfg["exit_mode"], recheck.EXIT_RANDOM)
        self.assertTrue(cfg["rotate_proxy"])

    def test_same_pin(self):
        cfg = self._cfg(exit_mode="same", exit_pin="9.9.9.9:7890")
        plan = recheck.plan_exit(cfg, [], 3)
        self.assertEqual(plan["proxy"], "http://9.9.9.9:7890")
        self.assertEqual(plan["mode_label"], "同一出口")

    def test_same_without_pin_uses_first_node(self):
        cfg = self._cfg(exit_mode="same", exit_pin="")
        nodes = [{"url": "http://n1:1", "ok": False, "exit_ip": ""},
                 {"url": "http://n2:2", "ok": True, "exit_ip": "5.5.5.5"}]
        plan = recheck.plan_exit(cfg, nodes, 0)
        self.assertEqual(plan["proxy"], "http://n2:2")
        self.assertEqual(plan["exit_ip"], "5.5.5.5")

    def test_same_without_anything_is_direct(self):
        cfg = self._cfg(exit_mode="same", exit_pin="")
        plan = recheck.plan_exit(cfg, [], 0)
        self.assertEqual(plan["proxy"], "")
        self.assertIn("直连", plan["label"])

    def test_random_uses_rng(self):
        class Fixed:
            def choice(self, seq):
                return seq[-1]

        cfg = self._cfg(exit_mode="random", mihomo={"enabled": True})
        nodes = [{"url": "http://a:1", "ok": True, "exit_ip": "1.1.1.1"},
                 {"url": "http://b:2", "ok": True, "exit_ip": "2.2.2.2"}]
        plan = recheck.plan_exit(cfg, nodes, 0, rng=Fixed())
        self.assertEqual(plan["proxy"], "http://b:2")
        self.assertEqual(plan["exit_ip"], "2.2.2.2")

    def test_random_without_nodes_is_direct(self):
        cfg = self._cfg(exit_mode="random")
        self.assertEqual(recheck.plan_exit(cfg, [], 0)["proxy"], "")

    def test_custom_rotates_in_order(self):
        """plan_exit 的 custom 分支还在：老配置文件直接喂进来时要读得懂。

        界面已经不给这档了（指定出口改成按行选端口），但函数不能因为
        「入口没了」就把老配置读成别的意思。
        """
        cfg = dict(recheck.DEFAULTS, exit_mode="custom",
                   exit_custom="http://c1:1\nhttp://c2:2")
        got = [recheck.plan_exit(cfg, [], i)["proxy"] for i in range(5)]
        self.assertEqual(got, ["http://c1:1", "http://c2:2",
                               "http://c1:1", "http://c2:2", "http://c1:1"])

    def test_custom_empty_falls_back_to_same(self):
        """表里一个合法地址都没有时不能谁都直连了事，要按 same 处理。"""
        cfg = dict(recheck.DEFAULTS, exit_mode="custom",
                   exit_custom="socks5://x:1", exit_pin="7.7.7.7:1080")
        plan = recheck.plan_exit(cfg, [], 0)
        self.assertEqual(plan["proxy"], "http://7.7.7.7:1080")
        self.assertEqual(plan["mode"], recheck.EXIT_SAME)


class RunnerExitTests(unittest.TestCase):
    """Runner 真的把「这一行该走的端口」传下去了没有。

    出口从「全局三档」改成「每行一个端口」之后，这一层是唯一会把端口变成
    实际代理串的地方。错了不会抛异常，只会所有号都直连（或都走同一个口），
    所以逐条钉。
    """

    def _cfg(self, **kw):
        mihomo = {"enabled": True, "host": "127.0.0.1", "ports": "17950-17951",
                  "probe_url": "http://probe.local/ip", "timeout": 2.0,
                  "api": "", "secret": "", "pool": "", "pool_token": ""}
        mihomo.update(kw.pop("mihomo", {}) or {})
        return recheck._coerce(dict(recheck.DEFAULTS, enabled=True,
                                    gap_seconds=1.0, mihomo=mihomo, **kw))

    def test_port_becomes_proxy(self):
        """行上写了 17950，这一号就必须从 127.0.0.1:17950 出去。"""
        runner = recheck.Runner(self._cfg())
        seen = []

        def fake(acc_, timeout=0, proxy="", model="", **kw):
            seen.append(proxy)
            return {"verdict": probe.RECHECK_REVIVED, "detail": "", "raw": "",
                    "status": 200, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_prepare", lambda self, p: None):
            results = runner.run([(1, acc())], force=True, ports={1: 17950})
        self.assertEqual(seen, ["http://127.0.0.1:17950"])
        self.assertEqual(results[0]["proxy"], "http://127.0.0.1:17950")
        self.assertEqual(results[0]["exit_mode"], recheck.EXIT_PORT)

    def test_same_port_for_every_row(self):
        runner = recheck.Runner(self._cfg())
        seen = []

        def fake(acc_, timeout=0, proxy="", model="", **kw):
            seen.append(proxy)
            return {"verdict": probe.RECHECK_BANNED, "detail": "", "raw": "",
                    "status": 403, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_prepare", lambda self, p: None):
            runner.run([(1, acc(uid="a")), (2, acc(uid="b"))], force=True,
                       ports={1: 17950, 2: 17950})
        self.assertEqual(seen, ["http://127.0.0.1:17950"] * 2)

    def test_row_without_port_goes_direct(self):
        """没分配出口就是直连，不许回退到「随便挑个节点」——那会让用户
        以为自己选的东西没生效。"""
        runner = recheck.Runner(self._cfg())
        seen = []

        def fake(acc_, timeout=0, proxy="", model="", **kw):
            seen.append(proxy)
            return {"verdict": probe.RECHECK_REVIVED, "detail": "", "raw": "",
                    "status": 200, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_prepare", lambda self, p: None):
            results = runner.run([(1, acc())], force=True, ports={})
        self.assertEqual(seen, [""])
        self.assertEqual(results[0]["exit_mode"], recheck.EXIT_DIRECT)

    def test_prepare_fills_name_and_exit_ip(self):
        runner = recheck.Runner(self._cfg())
        with mock.patch.object(recheck, "_fetch_listeners",
                               lambda *a, **kw: ({17950: {"name": "HUB-E01",
                                                          "proxy": "香港住宅01"}}, "")):
            with mock.patch.object(recheck, "probe_urls",
                                   lambda urls, pu, to: [
                                       {"url": u, "ok": True, "exit_ip": "8.8.8.8",
                                        "latency_ms": 1, "error": ""} for u in urls]):
                runner._prepare({1: 17950})
        self.assertEqual(runner._names[17950], "香港住宅01")
        self.assertEqual(runner._ips["http://127.0.0.1:17950"], "8.8.8.8")
        plan = runner._plan(1, {1: 17950})
        self.assertEqual(plan["label"], "17950 香港住宅01")
        self.assertEqual(plan["exit_ip"], "8.8.8.8")


class ListenerNameTests(unittest.TestCase):
    """节点名来自 mihomo 的 /listeners，出口 IP 分不出线路。"""

    def _fake(self, payload: dict, secret: str = ""):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                if self.path != "/listeners":
                    self.send_error(404)
                    return
                if secret and self.headers.get("Authorization") != "Bearer " + secret:
                    self.send_error(401)
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # addCleanup 是后进先出：先登记 close，后登记 shutdown，才能
        # 保证 shutdown 在 server_close 之前跑（反过来会对着已关闭的
        # socket 调 select，Windows 上直接抛 WinError 10038）
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _cfg(self, api: str, secret: str = "") -> dict:
        return {"mihomo": {"host": "127.0.0.1", "ports": "17901-17902",
                           "probe_url": "https://api.ipify.org", "timeout": 3.0,
                           "api": api, "secret": secret, "enabled": True}}

    def test_api_base_derives_9090_when_empty(self):
        self.assertEqual(recheck.api_base({"host": "cli-proxy-mihomo", "api": ""}),
                         "http://cli-proxy-mihomo:9090")

    def test_api_base_keeps_explicit(self):
        self.assertEqual(recheck.api_base({"host": "x", "api": "http://10.0.0.2:6000"}),
                         "http://10.0.0.2:6000")

    def test_api_base_host_with_port(self):
        self.assertEqual(recheck.api_base({"host": "127.0.0.1:9090", "api": ""}),
                         "http://127.0.0.1:9090")

    def test_fetch_listeners_maps_port_to_proxy(self):
        port = self._fake({"listeners": [
            {"name": "http-17901", "type": "http", "port": 17901, "proxy": "香港01"},
            {"name": "http-17902", "type": "http", "port": 17902, "proxy": "日本02"},
            {"name": "socks-17903", "type": "socks", "port": 0, "proxy": "跳过"},
        ]})
        got = recheck.fetch_listeners(self._cfg(f"http://127.0.0.1:{port}"))
        self.assertEqual(got[17901]["proxy"], "香港01")
        self.assertEqual(got[17902]["proxy"], "日本02")
        self.assertEqual(got[17902]["name"], "http-17902")
        self.assertNotIn(0, got)

    def test_fetch_listeners_sends_secret(self):
        port = self._fake({"listeners": [
            {"name": "a", "port": 17901, "proxy": "节点A"}]}, secret="s3cret")
        got = recheck.fetch_listeners(self._cfg(f"http://127.0.0.1:{port}", "s3cret"))
        self.assertEqual(got[17901]["proxy"], "节点A")

    def test_fetch_listeners_empty_when_unauthorized(self):
        port = self._fake({"listeners": [
            {"name": "a", "port": 17901, "proxy": "节点A"}]}, secret="s3cret")
        got = recheck.fetch_listeners(self._cfg(f"http://127.0.0.1:{port}", "wrong"))
        self.assertEqual(got, {})

    def test_fetch_listeners_empty_when_unreachable(self):
        """控制器没开的话不该抛异常，只是拿不到名字。"""
        self.assertEqual(recheck.fetch_listeners(self._cfg("http://127.0.0.1:1")), {})

    def test_discover_nodes_prefers_proxy_name(self):
        cfg = self._cfg("")
        nodes = [
            {"url": "http://127.0.0.1:17901", "ok": True, "exit_ip": "1.1.1.1",
             "latency_ms": 10, "error": ""},
            {"url": "http://127.0.0.1:17902", "ok": True, "exit_ip": "2.2.2.2",
             "latency_ms": 20, "error": ""},
        ]
        with mock.patch.object(recheck, "_fetch_listeners",
                               return_value=({17901: {"name": "listener-1", "proxy": "香港01"},
                                              17902: {"name": "listener-2", "proxy": ""}}, "")), \
             mock.patch.object(recheck, "probe_urls", return_value=nodes):
            data = recheck.discover_nodes(cfg, probe_now=True)
        self.assertEqual([n["name"] for n in data["nodes"]], ["香港01", "listener-2"])
        self.assertTrue(data["named"])
        self.assertEqual(data["nodes"][0]["port"], 17901)

    def test_discover_nodes_falls_back_to_address(self):
        """控制器不通时 name 是空串，前端退回显示地址。"""
        cfg = self._cfg("")
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "连不上")), \
             mock.patch.object(recheck, "probe_urls",
                               return_value=[{"url": "http://127.0.0.1:17901",
                                              "ok": True, "exit_ip": "1.1.1.1",
                                              "latency_ms": 10, "error": ""}]):
            data = recheck.discover_nodes(cfg, probe_now=True)
        self.assertEqual(data["nodes"][0]["name"], "")
        self.assertFalse(data["named"])

    def test_port_of_handles_garbage(self):
        self.assertEqual(recheck.port_of("http://127.0.0.1:17901"), 17901)
        self.assertEqual(recheck.port_of(""), 0)
        self.assertEqual(recheck.port_of("不对"), 0)


class ControllerErrorTests(unittest.TestCase):
    """控制器拿不到名单时，原因必须能照着改。

    界面上只写「控制器未通」等于没给信息：地址错、缺 secret、端点不存在，
    三种处置完全不同。这里逐条钉住返回的文案。
    """

    def _fake(self, code: int = 200, payload: dict | None = None):
        body = json.dumps(payload or {"listeners": []}).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                if code != 200:
                    self.send_error(code)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _cfg(self, api: str) -> dict:
        return {"mihomo": {"host": "127.0.0.1", "ports": "17901",
                           "probe_url": "https://api.ipify.org", "timeout": 2.0,
                           "api": api, "secret": "", "enabled": True}}

    def test_401_says_need_secret(self):
        port = self._fake(401)
        names, err = recheck._fetch_listeners(self._cfg(f"http://127.0.0.1:{port}"), timeout=2.0)
        self.assertEqual(names, {})
        self.assertIn("secret", err)

    def test_404_says_endpoint_missing(self):
        """404 是常态：官方 mihomo 的路由表里根本没有 /listeners。

        提示不能说成「版本太老」——那会让人去升级内核，升完照样 404。
        """
        port = self._fake(404)
        names, err = recheck._fetch_listeners(
            self._cfg(f"http://127.0.0.1:{port}"), ports=[17901], timeout=2.0)
        self.assertEqual(names, {})
        self.assertIn("/listeners", err)
        self.assertIn("官方 mihomo 不带这个端点", err)

    def test_empty_listeners_is_reported(self):
        """mihomo 通了但一个 listener 都没有，也得说清楚，不能当成「连上了」。"""
        port = self._fake(200, {"listeners": []})
        names, err = recheck._fetch_listeners(self._cfg(f"http://127.0.0.1:{port}"), timeout=2.0)
        self.assertEqual(names, {})
        self.assertIn("空", err)

    def test_no_host_no_api_reports(self):
        names, err = recheck._fetch_listeners(
            {"mihomo": {"host": "", "api": "", "secret": ""}}, 1.0)
        self.assertEqual(names, {})
        self.assertIn("控制器", err)

    def test_unreachable_never_raises(self):
        names, err = recheck._fetch_listeners(self._cfg("http://127.0.0.1:1"), timeout=1.0)
        self.assertEqual(names, {})
        self.assertTrue(err)


class _HoldProxy:
    """假 listener：收 CONNECT、回 200、然后把连接挂着不关。

    挂着才是重点——`/connections` 里能查到的是**活动**连接，一关就没了。
    """

    def __init__(self) -> None:
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(3.0)
                    head = b""
                    while b"\r\n\r\n" not in head and len(head) < 4096:
                        chunk = self.request.recv(512)
                        if not chunk:
                            return
                        head += chunk
                    if not head.upper().startswith(b"CONNECT"):
                        return
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    outer.seen.append(head.split(b"\r\n", 1)[0].decode("latin-1"))
                    # 一直读到对端关闭，保证这条连接在探测期间是活的
                    while self.request.recv(512):
                        pass
                except OSError:
                    pass

        self.seen: list[str] = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _HoldSocks:
    """假 SOCKS5 listener：握手成功后把连接挂着不关。

    有些 listener 的 type 就是 socks，这时 HTTP CONNECT 过去只会被对端
    直接关掉——`_open_tunnels` 必须能退到 SOCKS5 才算真能覆盖。
    """

    def __init__(self) -> None:
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(3.0)
                    if self.request.recv(3) != b"\x05\x01\x00":
                        return
                    self.request.sendall(b"\x05\x00")
                    head = self.request.recv(4)
                    if len(head) < 4:
                        return
                    if head[3] == 0x03:
                        n = self.request.recv(1)[0]
                        self.request.recv(n)
                    elif head[3] == 0x01:
                        self.request.recv(4)
                    else:
                        self.request.recv(16)
                    self.request.recv(2)                  # 端口
                    self.request.sendall(b"\x05\x00\x00\x01"
                                         b"\x00\x00\x00\x00\x00\x00")
                    outer.seen.append("socks")
                    while self.request.recv(512):
                        pass
                except OSError:
                    pass

        self.seen: list[str] = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class SocksTunnelTests(unittest.TestCase):
    """listener 只收 SOCKS 时，CONNECT 那条路走不通，得能退到 SOCKS5。"""

    def test_socks_only_listener_still_maps(self):
        socks = _HoldSocks()
        self.addCleanup(socks.close)

        body = json.dumps({"connections": [{
            "metadata": {"inboundPort": str(socks.port), "inboundName": "socks-in-1"},
            "chains": ["日本-代理-01"],
        }]}).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/listeners"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        cfg = {"mihomo": {"host": "127.0.0.1", "ports": str(socks.port),
                          "probe_url": "https://api.ipify.org", "timeout": 2.0,
                          "api": f"http://127.0.0.1:{srv.server_address[1]}",
                          "secret": "", "enabled": True}}
        names, err = recheck._fetch_listeners(
            cfg, ports=[socks.port], timeout=2.0, deep=True)
        self.assertEqual(err, "")
        self.assertEqual(names[socks.port]["proxy"], "日本-代理-01")
        self.assertEqual(socks.seen, ["socks"])


class ConnectionsFallbackTests(unittest.TestCase):
    """官方 mihomo 没有 `/listeners`，唯一出路是从 `/connections` 反查。

    起一个假控制器（/listeners 404、/connections 带 inboundPort 和 chains）
    加一个假 listener（收 CONNECT 不关），验证端口真能对上节点名。
    """

    def _controller(self, connections: list[dict]) -> int:
        body = json.dumps({"connections": connections}).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/listeners"):
                    self.send_error(404)
                    return
                if self.path.startswith("/connections"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _cfg(self, ctrl_port: int, proxy_port: int) -> dict:
        return {"mihomo": {"host": "127.0.0.1", "ports": str(proxy_port),
                           "probe_url": "https://api.ipify.org", "timeout": 2.0,
                           "api": f"http://127.0.0.1:{ctrl_port}",
                           "secret": "", "enabled": True}}

    def setUp(self) -> None:
        self.proxy = _HoldProxy()
        self.addCleanup(self.proxy.close)

    def test_port_maps_to_chains_head(self):
        ctrl = self._controller([{
            "metadata": {"inboundPort": str(self.proxy.port),
                         "inboundName": "mixed-in-1"},
            "chains": ["香港住宅01", "GLOBAL"],
        }])
        names, err = recheck._fetch_listeners(
            self._cfg(ctrl, self.proxy.port), ports=[self.proxy.port],
            timeout=2.0, deep=True)
        self.assertEqual(err, "")
        self.assertEqual(names[self.proxy.port]["proxy"], "香港住宅01")
        self.assertEqual(names[self.proxy.port]["name"], "mixed-in-1")
        # 隧道真的发出去过，不然上面的映射就是空转
        self.assertTrue(self.proxy.seen and "CONNECT" in self.proxy.seen[0])

    def test_other_ports_are_ignored(self):
        """连接表里一堆别的端口，只能取我们开过隧道的那几个。"""
        ctrl = self._controller([
            {"metadata": {"inboundPort": str(self.proxy.port + 1)}, "chains": ["别的"]},
            {"metadata": {"inboundPort": str(self.proxy.port)}, "chains": ["香港住宅01"]},
        ])
        names, _ = recheck._fetch_listeners(
            self._cfg(ctrl, self.proxy.port), ports=[self.proxy.port],
            timeout=2.0, deep=True)
        self.assertEqual(list(names), [self.proxy.port])

    def test_no_inbound_port_says_version_too_old(self):
        ctrl = self._controller([{"metadata": {}, "chains": ["香港住宅01"]}])
        names, err = recheck._fetch_listeners(
            self._cfg(ctrl, self.proxy.port), ports=[self.proxy.port],
            timeout=2.0, deep=True)
        self.assertEqual(names, {})
        self.assertIn("inboundPort", err)

    def test_deep_false_does_not_open_tunnels(self):
        """列页面时不该去开 N 条长连接——用户还没点「自动检测」。"""
        ctrl = self._controller([])
        names, err = recheck._fetch_listeners(
            self._cfg(ctrl, self.proxy.port), ports=[self.proxy.port],
            timeout=1.0, deep=False)
        self.assertEqual(names, {})
        self.assertIn("自动检测", err)
        self.assertEqual(self.proxy.seen, [])


class HostParseTests(unittest.TestCase):
    """CONNECT 隧道要的是纯主机名，配置里可能带着协议和端口。"""

    def test_host_only(self):
        self.assertEqual(recheck._host_only("127.0.0.1"), "127.0.0.1")
        self.assertEqual(recheck._host_only("http://127.0.0.1:9090"), "127.0.0.1")
        self.assertEqual(recheck._host_only("cli-proxy-mihomo:7890"), "cli-proxy-mihomo")
        self.assertEqual(recheck._host_only(""), "")

    def test_probe_host(self):
        self.assertEqual(recheck._probe_host("https://api.ipify.org"), "api.ipify.org")
        self.assertEqual(recheck._probe_host("api.ipify.org"), "api.ipify.org")
        self.assertEqual(recheck._probe_host(""), "api.ipify.org")


class NodeKindTests(unittest.TestCase):
    """类型标签只按名字推断，名字里没写就不标。"""

    def test_residential(self):
        for name in ("香港住宅01", "家庭宽带-广州", "HK Residential 03"):
            self.assertEqual(recheck.node_kind(name), "住宅", name)

    def test_datacenter(self):
        for name in ("US-Datacenter-02", "IDC-新加坡", "台湾-机房-专线"):
            self.assertEqual(recheck.node_kind(name), "机房", name)

    def test_proxy(self):
        for name in ("日本-代理-01", "socks5-入口"):
            self.assertEqual(recheck.node_kind(name), "代理", name)

    def test_residential_wins_over_proxy(self):
        """「住宅代理」算住宅——那才是它的用处。"""
        self.assertEqual(recheck.node_kind("德国-住宅代理"), "住宅")

    def test_unknown_name_is_blank(self):
        for name in ("节点A", "", "node-01"):
            self.assertEqual(recheck.node_kind(name), "", name)

    def test_label_nodes_fills_port_name_and_kind(self):
        nodes = [{"url": "http://127.0.0.1:17950"}, {"url": "http://127.0.0.1:17951"}]
        names = {17950: {"name": "mixed-in-1", "proxy": "香港住宅01"},
                 17951: {"name": "mixed-in-2", "proxy": ""}}
        got = recheck._label_nodes(nodes, names)
        self.assertEqual(got[0]["port"], 17950)
        self.assertEqual(got[0]["name"], "香港住宅01")
        self.assertEqual(got[0]["kind"], "住宅")
        # proxy 空就退到 listener 名，仍然不是地址
        self.assertEqual(got[1]["name"], "mixed-in-2")
        self.assertEqual(got[1]["kind"], "")


class AssignPortsTests(unittest.TestCase):
    """点端口 / 切模式 → 每行一个端口。

    这一段是「用户以为他配了，实际全直连」最容易发生的地方：分配函数返回
    空表时如果被上层当成「全写 0」，整份名单的出口就静默没了。
    """

    class _Last:
        """固定挑最后一个，用来验证「在范围里」而不是「挑最小/最大」。"""

        def choice(self, seq):
            return seq[-1]

    def test_same_uses_first_picked(self):
        got = recheck.assign_ports("same", [17952, 17950], [], [1, 2])
        self.assertEqual(got, [(1, 17952), (2, 17952)])

    def test_same_falls_back_to_all_ports(self):
        got = recheck.assign_ports("same", [], [17950, 17951], [1])
        self.assertEqual(got, [(1, 17950)])

    def test_random_stays_inside_picked(self):
        got = recheck.assign_ports("random", [17950, 17951], [17950, 17951, 17952],
                                   [1, 2, 3], rng=self._Last())
        self.assertTrue(got)
        for _id, port in got:
            self.assertIn(port, (17950, 17951))
        # 每次都取范围内最后一个：证明真的按 picked 收窄了
        self.assertEqual({p for _, p in got}, {17951})

    def test_random_uses_all_when_nothing_picked(self):
        got = recheck.assign_ports("random", [], [17950, 17951],
                                   [1, 2], rng=self._Last())
        self.assertEqual({p for _, p in got}, {17951})

    def test_no_port_returns_empty(self):
        """没有可选端口时必须返回空表：上层据此报错，而不是把每行写成 0。"""
        self.assertEqual(recheck.assign_ports("same", [], [], [1, 2]), [])
        self.assertEqual(recheck.assign_ports("random", [0], [0], [1]), [])

    def test_duplicates_collapse(self):
        got = recheck.assign_ports("same", [17950, 17950], [], [1])
        self.assertEqual(got, [(1, 17950)])

    def test_plan_port_zero_is_direct(self):
        cfg = {"mihomo": {"host": "127.0.0.1"}}
        plan = recheck.plan_port(cfg, 0)
        self.assertEqual(plan["proxy"], "")
        self.assertEqual(plan["mode"], recheck.EXIT_DIRECT)

    def test_plan_port_builds_proxy_and_label(self):
        cfg = {"mihomo": {"host": "127.0.0.1"}}
        plan = recheck.plan_port(cfg, 17950, {17950: "香港住宅01"},
                                 {"http://127.0.0.1:17950": "1.1.1.1"})
        self.assertEqual(plan["proxy"], "http://127.0.0.1:17950")
        self.assertEqual(plan["exit_ip"], "1.1.1.1")
        self.assertEqual(plan["label"], "17950 香港住宅01")
        self.assertEqual(plan["mode"], recheck.EXIT_PORT)

    def test_plan_port_without_name_keeps_port(self):
        cfg = {"mihomo": {"host": "10.0.0.2"}}
        self.assertEqual(recheck.plan_port(cfg, 17951)["label"], "17951")


class PoolPanelTests(unittest.TestCase):
    """mihomo-pool 面板：一次 GET 拿到「节点 → 独享端口」全表。

    池子的 listeners 名字是 `HUB-E01` 这种序号（端口升序编号，重排就变），
    节点名只在 `proxy` 上。所以既不能拿 listener 名当节点名，也不该为了
    名字去开三十条隧道——面板自己就维护着这张表。
    """

    def _fake_pool(self, nodes: list, token: str = ""):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                seen["path"] = self.path
                seen["token"] = self.headers.get("X-Token") or ""
                if self.path != "/api/v1/nodes":
                    self.send_error(404)
                    return
                if token and seen["token"] != token:
                    self.send_error(401)
                    return
                body = json.dumps({"ok": True, "nodes": nodes}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1], seen

    def _nodes(self):
        return [
            {"name": "香港住宅01", "type": "vmess", "delay_ms": 210,
             "listener_port": 17950, "match": True},
            {"name": "新加坡机房-02", "type": "ss", "delay_ms": 380,
             "listener_port": 17951, "match": False},
            # 没分到端口的节点不进表
            {"name": "日本-未分配", "type": "trojan", "listener_port": None},
            {"name": "", "type": "ss", "listener_port": 17952},
        ]

    def test_base_derives_8006(self):
        self.assertEqual(recheck._pool_base({"host": "127.0.0.1", "pool": ""}),
                         "http://127.0.0.1:8006")

    def test_base_keeps_explicit(self):
        self.assertEqual(
            recheck._pool_base({"host": "127.0.0.1", "pool": "http://10.0.0.2:8080/"}),
            "http://10.0.0.2:8080")

    def test_base_blank_without_host(self):
        self.assertEqual(recheck._pool_base({"host": "", "pool": ""}), "")

    def test_map_skips_unassigned(self):
        got = recheck._pool_map(self._nodes())
        self.assertEqual(set(got), {17950, 17951})
        self.assertEqual(got[17950]["name"], "香港住宅01")
        self.assertEqual(got[17950]["proxy"], "香港住宅01")
        self.assertEqual(got[17950]["type"], "vmess")

    def test_map_tolerates_string_port(self):
        got = recheck._pool_map([{"name": "x", "listener_port": "17953"}])
        self.assertEqual(got[17953]["name"], "x")

    def test_fetch_returns_table(self):
        port, seen = self._fake_pool(self._nodes(), token="tok-1")
        got, why = recheck._fetch_pool_nodes(
            {"pool_token": "tok-1", "secret": ""}, f"http://127.0.0.1:{port}", 2.0)
        self.assertEqual(why, "")
        self.assertEqual(got[17951]["name"], "新加坡机房-02")
        self.assertEqual(seen["path"], "/api/v1/nodes")
        self.assertEqual(seen["token"], "tok-1")

    def test_fetch_falls_back_to_secret(self):
        """面板 Token 和内核 secret 长得一样，填混是常态，拿 secret 再试一次。"""
        port, seen = self._fake_pool(self._nodes(), token="same-words")
        got, why = recheck._fetch_pool_nodes(
            {"pool_token": "", "secret": "same-words"}, f"http://127.0.0.1:{port}", 2.0)
        self.assertEqual(why, "")
        self.assertTrue(got)
        self.assertEqual(seen["token"], "same-words")

    def test_fetch_401_names_env_var(self):
        port, _ = self._fake_pool(self._nodes(), token="right")
        got, why = recheck._fetch_pool_nodes(
            {"pool_token": "wrong", "secret": ""}, f"http://127.0.0.1:{port}", 2.0)
        self.assertEqual(got, {})
        self.assertIn("MPOOL_TOKEN", why)

    def test_fetch_dead_panel_is_silent(self):
        """8006 没开只是这条路走不通，不许它冒充「控制器未通」。"""
        got, why = recheck._fetch_pool_nodes(
            {"pool_token": "", "secret": ""}, "http://127.0.0.1:1", 0.5)
        self.assertEqual(got, {})
        self.assertEqual(why, "")

    def test_listeners_falls_through_to_panel(self):
        """9090 没 /listeners（官方内核常态）时，名字由面板给出。"""
        panel, _ = self._fake_pool(self._nodes(), token="tok-1")

        class Ctrl(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                self.send_error(404)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Ctrl)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        cfg = {"mihomo": {
            "host": "127.0.0.1",
            "ports": "17950-17951",
            "probe_url": "https://api.ipify.org",
            "timeout": 2.0,
            "api": f"http://127.0.0.1:{srv.server_address[1]}",
            "secret": "",
            "pool": f"http://127.0.0.1:{panel}",
            "pool_token": "tok-1",
            "enabled": True,
        }}
        got, why = recheck._fetch_listeners(cfg, ports=[17950, 17951],
                                           timeout=1.5, deep=True)
        self.assertEqual(why, "")
        self.assertEqual(got[17950]["name"], "香港住宅01")

    def test_both_401_explains_both_secrets(self):
        """两条路都 401 时，两个 Token 的出处都要写清楚，否则用户只会来回改。"""

        class Ctrl(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                self.send_error(401)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Ctrl)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        panel, _ = self._fake_pool(self._nodes(), token="right")

        cfg = {"mihomo": {
            "host": "127.0.0.1",
            "ports": "17950",
            "probe_url": "https://api.ipify.org",
            "timeout": 1.5,
            "api": f"http://127.0.0.1:{srv.server_address[1]}",
            "secret": "wrong",
            "pool": f"http://127.0.0.1:{panel}",
            "pool_token": "wrong",
            "enabled": True,
        }}
        _, why = recheck._fetch_listeners(cfg, ports=[17950], timeout=1.0, deep=False)
        self.assertIn("runtime/secret", why)
        self.assertIn("MPOOL_TOKEN", why)


    def test_discover_nodes_paints_names_from_panel(self):
        """整条链路：面板给的端口表要落到卡片的 name/kind 上。"""
        panel, _ = self._fake_pool(self._nodes(), token="tok-1")
        cfg = {"mihomo": {
            "host": "127.0.0.1",
            "ports": "17950-17951",
            "probe_url": "https://api.ipify.org",
            "timeout": 1.0,
            "api": "http://127.0.0.1:1",     # 控制器故意连不上
            "secret": "",
            "pool": f"http://127.0.0.1:{panel}",
            "pool_token": "tok-1",
            "enabled": True,
        }}
        rows = [
            {"url": "http://127.0.0.1:17950", "ok": True, "exit_ip": "1.1.1.1",
             "latency_ms": 120, "error": ""},
            {"url": "http://127.0.0.1:17951", "ok": True, "exit_ip": "2.2.2.2",
             "latency_ms": 300, "error": ""},
        ]
        with mock.patch.object(recheck, "probe_urls", return_value=rows):
            data = recheck.discover_nodes(cfg, probe_now=True)
        self.assertTrue(data["named"])
        self.assertEqual(data["nodes"][0]["name"], "香港住宅01")
        self.assertEqual(data["nodes"][0]["kind"], "住宅")
        self.assertEqual(data["nodes"][1]["kind"], "机房")
        self.assertEqual(data["api_error"], "")

    def test_coerce_keeps_pool_fields(self):
        cfg = recheck._coerce({"mihomo": {"host": "127.0.0.1", "pool": "http://h:8006/",
                                          "pool_token": " abc "}})
        self.assertEqual(cfg["mihomo"]["pool"], "http://h:8006")
        self.assertEqual(cfg["mihomo"]["pool_token"], "abc")


class PortBindTests(unittest.TestCase):
    """给端口换节点：池子只认「节点 → 端口」，端口被占就拒绝。

    用户要的是「17950 改成走这个节点」，所以被占时得先把占用者挪到空端口。
    这一层要是错了，两个后果都很糟：换不动（用户以为功能没做），或者把
    占用者挤到 0 号位（那张卡片从此不显示）。
    """

    LISTEN_BASE = 17950

    def _pool(self, nodes: list[dict], *, reject: set[str] | None = None):
        """起一个能 status / nodes / ports/bind 的假池。

        `nodes` 是 [{name, port}]，bind 会真的改内存里的映射——不真的改就
        测不出「腾位之后目标端口真的空出来了」。
        """
        state = {n["name"]: n["port"] for n in nodes}
        delays = {n["name"]: n.get("delay", 120) for n in nodes}
        calls: list[dict] = []
        reject = reject or set()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _json(self, code, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/v1/status":
                    return self._json(200, {"ok": True, "status": {
                        "listen_base": self.server_base, "listen_max": 32}})
                if self.path == "/api/v1/nodes":
                    return self._json(200, {"ok": True, "nodes": [
                        {"name": name, "type": "ss", "delay_ms": delays[name],
                         "listener_port": port, "pinned": port is not None}
                        for name, port in state.items()]})
                self.send_error(404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                calls.append(body)
                name = str(body.get("node") or "")
                port = body.get("port")
                if name not in state:
                    return self._json(500, {"ok": False, "error": "节点不存在: " + name})
                if name in reject:
                    return self._json(500, {"ok": False, "error": "上游拒绝了这个节点"})
                if port is None:
                    state[name] = None
                    return self._json(200, {"ok": True, "node": name, "port": None})
                port = int(port)
                holder = next((n for n, p in state.items() if p == port and n != name), None)
                if holder:
                    return self._json(500, {"ok": False,
                                            "error": f"端口 {port} 正被 {holder} 使用"})
                state[name] = port
                return self._json(200, {"ok": True, "node": name, "port": port, "pinned": True})

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Handler.server_base = self.LISTEN_BASE
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1], state, calls

    def _cfg(self, port: int) -> dict:
        return {"mihomo": {"host": "127.0.0.1", "pool": f"http://127.0.0.1:{port}",
                           "pool_token": "tok", "secret": "", "ports": "17950-17955",
                           "probe_url": "https://api.ipify.org", "timeout": 5.0,
                           "enabled": True},
                "exit_mode": "random", "exit_ports": [], "exit_pin": "",
                "exit_custom": "", "batch_limit": 20, "min_interval": 0,
                "gap_seconds": 0, "timeout": 5}

    def test_pool_nodes_keeps_unassigned(self):
        """没分到端口的节点也要在候选表里——能换的前提就是它此刻空闲。"""
        port, _state, _calls = self._pool([
            {"name": "香港住宅01", "port": 17950},
            {"name": "日本-未分配", "port": None},
        ])
        got = recheck.pool_nodes(self._cfg(port))
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["nodes"]), 2)
        free = [n for n in got["nodes"] if n["name"] == "日本-未分配"][0]
        self.assertEqual(free["port"], 0)
        self.assertIsNotNone(free["delay_ms"])

    def test_pool_nodes_reports_http_error(self):
        cfg = self._cfg(1)      # 没人监听这个端口
        got = recheck.pool_nodes(cfg, timeout=1.0)
        self.assertFalse(got["ok"])
        self.assertTrue(got["error"])

    def test_bind_free_port(self):
        port, state, calls = self._pool([
            {"name": "香港住宅01", "port": 17950},
            {"name": "日本-01", "port": None},
        ])
        got = recheck.bind_port(self._cfg(port), 17951, "日本-01")
        self.assertEqual(got["port"], 17951)
        self.assertIsNone(got["moved"])
        self.assertEqual(state["日本-01"], 17951)
        self.assertEqual(calls[-1], {"node": "日本-01", "port": 17951})

    def test_bind_evicts_occupant(self):
        """端口上已经有人时，先把占用者挪到空端口，再让目标节点占位。"""
        port, state, calls = self._pool([
            {"name": "香港住宅01", "port": 17950},
            {"name": "日本-01", "port": None},
            {"name": "新加坡-02", "port": None},
        ])
        got = recheck.bind_port(self._cfg(port), 17950, "日本-01")
        self.assertEqual(got["port"], 17950)
        self.assertEqual(got["moved"]["node"], "香港住宅01")
        self.assertEqual(got["moved"]["to"], 17951)   # 17950 之后第一个空位
        self.assertEqual(state["日本-01"], 17950)
        self.assertEqual(state["香港住宅01"], 17951)
        # 顺序不能反：先挪人再绑，否则第一步就被池子拒
        self.assertEqual([c["node"] for c in calls], ["香港住宅01", "日本-01"])

    def test_bind_rolls_back_when_target_rejected(self):
        """目标节点被上游拒了，占用者要挪回原位，别留下两张错位的卡片。"""
        port, state, _calls = self._pool([
            {"name": "香港住宅01", "port": 17950},
            {"name": "坏节点", "port": None},
        ], reject={"坏节点"})
        with self.assertRaises(ValueError):
            recheck.bind_port(self._cfg(port), 17950, "坏节点")
        self.assertEqual(state["香港住宅01"], 17950)

    def test_bind_unknown_node(self):
        port, _state, _calls = self._pool([{"name": "香港住宅01", "port": 17950}])
        with self.assertRaises(ValueError):
            recheck.bind_port(self._cfg(port), 17950, "不存在的节点")

    def test_bind_without_pool_base(self):
        cfg = self._cfg(1)
        cfg["mihomo"]["pool"] = ""
        cfg["mihomo"]["host"] = ""
        with self.assertRaises(ValueError):
            recheck.bind_port(cfg, 17950, "任意")

    def test_bind_rejects_bad_port(self):
        port, _state, _calls = self._pool([{"name": "香港住宅01", "port": 17950}])
        with self.assertRaises(ValueError):
            recheck.bind_port(self._cfg(port), 0, "香港住宅01")


class GroupPortTests(unittest.TestCase):
    """正常区 / 风控区的端口隔离。

    一键分配前先扣掉对方正在用的端口。这里测的是纯函数那一层，服务端
    两个分配接口都调它，不用起 HTTP 也能把「撞端口」钉死。
    """

    def test_free_ports_removes_busy(self):
        self.assertEqual(
            recheck.free_ports([17950, 17951, 17952], {17951}),
            [17950, 17952])

    def test_free_ports_keeps_order_and_dedupes(self):
        # same 模式取第一个：顺序被集合运算打乱，分配结果就跟着变
        self.assertEqual(
            recheck.free_ports([17952, 17950, 17952, 0, None], {17999}),
            [17952, 17950])

    def test_free_ports_drops_non_numeric(self):
        self.assertEqual(recheck.free_ports(["x", 17950, ""], set()), [17950])

    def test_all_busy_leaves_no_plan(self):
        self.assertEqual(recheck.free_ports([17950], {17950}), [])
        # 没端口时保持原值，不能把每行悄悄写成 0
        self.assertEqual(recheck.assign_ports("random", [], [], [1, 2]), [])

    def test_assigned_ports_never_in_busy(self):
        free = recheck.free_ports([17950, 17951, 17952], {17951})
        plan = recheck.assign_ports("random", [], free, list(range(30)))
        self.assertEqual(len(plan), 30)
        self.assertTrue({p for _, p in plan} <= {17950, 17952})

    def test_same_uses_first_free_port(self):
        free = recheck.free_ports([17951, 17952], {17951})
        self.assertEqual(
            recheck.assign_ports("same", free, [], [1, 2, 3]),
            [(1, 17952), (2, 17952), (3, 17952)])


class ChatModelTests(unittest.TestCase):
    """账户验证模型：默认 deepseek-v4.1-flash，可配置，且要传到降级请求。"""

    def test_default_is_deepseek_flash(self):
        self.assertEqual(recheck.DEFAULTS["chat_model"], "deepseek-v4.1-flash")
        self.assertEqual(probe.CHAT_MODEL, "deepseek-v4.1-flash")
        self.assertEqual(recheck.load_config()["chat_model"],
                         "deepseek-v4.1-flash")

    def test_coerce_keeps_custom_model(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, chat_model="  my-model-2  "))
        self.assertEqual(cfg["chat_model"], "my-model-2")

    def test_coerce_blank_falls_back(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, chat_model="   "))
        self.assertEqual(cfg["chat_model"], recheck.DEFAULTS["chat_model"])

    def test_runner_passes_configured_model(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, enabled=True,
                                   gap_seconds=1.0, chat_model="verify-x"))
        seen = []

        def fake(acc_, timeout=0, proxy="", model="", **kw):
            seen.append(model)
            return {"verdict": probe.RECHECK_REVIVED, "detail": "", "raw": "",
                    "status": 200, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_prepare", lambda self, p: None):
            recheck.Runner(cfg).run([(1, acc())], force=True, ports={})
        self.assertEqual(seen, ["verify-x"])

    def test_400_tries_model_and_body_chain(self):
        """400 后按顺序换模型/请求体，不再把同一个模型重发两遍就收工。"""
        bodies = []

        def fake(url, body, headers, timeout, proxy=""):
            bodies.append(dict(body))
            return 400, {"code": 11133, "msg": "model_param_invalid"}

        with mock.patch.object(probe, "_post_json", fake):
            res = probe.chat_probe(acc(), timeout=5, model="verify-x")
        self.assertEqual(len(bodies), probe.CHAT_MAX_ATTEMPTS)
        self.assertEqual(bodies[0]["model"], "verify-x")
        self.assertEqual({b["model"] for b in bodies},
                         {"verify-x", "glm-5.3-flash", "hy3"})
        self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)
        self.assertIn("已试 8 种", res["detail"])
        self.assertEqual(len(res["attempts"]), probe.CHAT_MAX_ATTEMPTS)


class ChatChainTests(unittest.TestCase):
    """400 自适应：intl 别名、候选链、逐次原文、200 业务错误码。"""

    def test_intl_alias_is_second_attempt(self):
        """裸名 400 → 第二次就用 -sg 打通，不再永远 400。"""
        seen = []

        def fake(url, body, headers, timeout, proxy=""):
            seen.append(body["model"])
            if body["model"] == "deepseek-v4.1-flash":
                return 400, {"code": 11133, "msg": "model_param_invalid"}
            if body["model"] == "deepseek-v4.1-flash-sg":
                return 200, {"choices": [{"message": {"content": "pong"}}]}
            return 400, {"msg": "bad model"}

        with mock.patch.object(probe, "_post_json", fake):
            res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(seen[:2], ["deepseek-v4.1-flash", "deepseek-v4.1-flash-sg"])
        self.assertEqual(res["verdict"], probe.RECHECK_REVIVED)
        self.assertEqual(res["model"], "deepseek-v4.1-flash-sg")
        self.assertEqual([a["status"] for a in res["attempts"]], [400, 200])

    def test_cn_keeps_bare_model(self):
        self.assertEqual(probe.intl_model_alias("deepseek-v4.1-flash", "cn"),
                         "deepseek-v4.1-flash")
        self.assertEqual(probe.intl_model_alias("deepseek-v4.1-flash", "intl"),
                         "deepseek-v4.1-flash-sg")
        self.assertNotIn("deepseek-v4.1-flash-sg",
                         probe.model_chain("deepseek-v4.1-flash", None, "cn"))

    def test_custom_chain_and_paths(self):
        self.assertEqual(probe.model_chain("", "m1, m2", "cn"), ["m1", "m2"])
        self.assertEqual(probe.path_chain("/v1/chat/completions, chat/completions"),
                         ("/v1/chat/completions", "/chat/completions"))

    def test_v1_path_fallback(self):
        def fake(url, body, headers, timeout, proxy=""):
            if url.endswith("/v2/chat/completions"):
                return 404, {}
            if url.endswith("/v1/chat/completions"):
                return 200, {"choices": []}
            return 404, {}

        with mock.patch.object(probe, "_post_json", fake):
            res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(res["verdict"], probe.RECHECK_REVIVED)
        self.assertTrue(res["endpoint"].endswith("/v1/chat/completions"))
        self.assertEqual([a["status"] for a in res["attempts"]], [404, 200])

    def test_all_400_keeps_upstream_text(self):
        def fake(url, body, headers, timeout, proxy=""):
            return 400, {"code": 11133, "msg": "model id not found"}

        with mock.patch.object(probe, "_post_json", fake):
            res = probe.chat_probe(acc(), timeout=5)
        self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)
        self.assertEqual(len(res["attempts"]), probe.CHAT_MAX_ATTEMPTS)
        self.assertIn("model id not found", res["detail"])
        self.assertIn("已试 8 种", res["detail"])
        self.assertIn("11133", res["detail"])

    def test_html_400_reason_is_extracted(self):
        html = ('<!DOCTYPE html><html><head><meta name="mcai-preview-error" '
                'code="400" content="model id not found: deepseek-v4.1-flash"/>'
                '<title>400 Bad Request</title><style>.a{color:red}</style>'
                '</head><body>参数错误</body></html>')
        with mock.patch.object(probe, "_post_json",
                               lambda *a, **k: (400, {"_raw": html})):
            res = probe.chat_probe(acc(), timeout=5)
        self.assertIn("model id not found: deepseek-v4.1-flash", res["detail"])
        self.assertIn("参数错误", res["detail"])

    def test_200_with_business_error_is_not_revived(self):
        res = probe._read_chat_reply(
            200, {"code": 6004, "msg": "usage exceeds frequency limit"},
            "https://x/v2/chat/completions", 0.0)
        self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)
        self.assertIn("6004", res["detail"])

    def test_coerce_chat_chain_defaults(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS))
        self.assertEqual(cfg["chat_models"], "")
        self.assertEqual(cfg["chat_paths"], "")
        self.assertEqual(cfg["chat_max_attempts"], 8)
        self.assertEqual(recheck._coerce(dict(
            recheck.DEFAULTS, chat_max_attempts=99))["chat_max_attempts"], 16)

    def test_runner_passes_configured_chain(self):
        cfg = recheck._coerce(dict(recheck.DEFAULTS, enabled=True,
                                   gap_seconds=1.0, chat_models="m1,m2",
                                   chat_paths="/x/chat/completions",
                                   chat_max_attempts=3))
        seen = []

        def fake(acc_, timeout=0, proxy="", model="", **kw):
            seen.append(kw)
            return {"verdict": probe.RECHECK_REVIVED, "detail": "", "raw": "",
                    "status": 200, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_prepare", lambda self, p: None):
            recheck.Runner(cfg).run([(1, acc())], force=True, ports={})
        self.assertEqual(seen[0]["models"], "m1,m2")
        self.assertEqual(seen[0]["paths"], "/x/chat/completions")
        self.assertEqual(seen[0]["max_attempts"], 3)


class DiagnoseApiTests(unittest.TestCase):
    """诊断接口：逐次尝试原样回传，不碰名单状态。"""

    def test_diagnose_returns_attempts_without_writing(self):
        import server

        class Fake:
            def __init__(self):
                self.out = None

            def _json(self, status, data):
                self.out = (status, data)

        fake = Fake()
        cfg = dict(recheck.DEFAULTS, chat_model="m-x", chat_models="m1",
                   chat_paths="/p", chat_max_attempts=4)
        result = {"verdict": probe.RECHECK_UNKNOWN, "detail": "HTTP 400",
                  "raw": "", "status": 400, "endpoint": "u", "latency_ms": 1,
                  "attempts": [{"status": 400, "model": "m-x"}]}
        with mock.patch.object(server.store, "load_for_probe",
                               return_value=[(7, acc())]), \
             mock.patch.object(server.store, "exit_ports_of",
                               return_value={7: 0}), \
             mock.patch.object(server.recheck, "load_config",
                               return_value=cfg), \
             mock.patch.object(server.probe, "chat_probe",
                               return_value=result) as called, \
             mock.patch.object(server.recheck, "plan_port",
                               return_value={"proxy": ""}) as plan:
            server.Handler._recheck_diagnose(fake, {"ids": [7]})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["attempts"], [{"status": 400, "model": "m-x"}])
        self.assertEqual(data["config"]["chat_model"], "m-x")
        self.assertEqual(data["config"]["exit_port"], 0)
        self.assertEqual(called.call_args.kwargs["models"], "m1")
        self.assertEqual(called.call_args.kwargs["max_attempts"], 4)
        # 出口由 plan_port 统一算（固定代理也在里面），诊断与真复检同线
        plan.assert_called_once()
        self.assertEqual(called.call_args.kwargs["proxy"], "")


class OfficialChatShapeTests(unittest.TestCase):
    """2026-11 上游渠道校验：流式 + system 首条 + 官方网关头。

    实测（www.workbuddy.ai，走 127.0.0.1:7891）：非流式请求回 400/11101；
    补 stream 但缺网关头回 400/11128 "first message is not system prompt"；
    补齐网关头 + system 首条后，同一个号回 403/11140 真实风控结论。
    """

    def test_official_body_is_first_and_valid(self):
        self.assertEqual(probe.CHAT_BODY_VARIANTS[0], "official")
        body = probe._chat_body("m-x", "official")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertTrue(body["stream"])
        self.assertEqual(body["model"], "m-x")

    def test_chat_headers_are_official_and_fresh(self):
        h1 = probe._chat_headers(acc())
        h2 = probe._chat_headers(acc())
        for key in ("x-codebuddy-request", "X-Agent-Intent", "X-Conversation-ID",
                    "X-Request-ID", "traceparent", "X-Private-Data", "X-B3-TraceId"):
            self.assertIn(key, h1)
        self.assertEqual(h1["x-codebuddy-request"], "1")
        self.assertEqual(h1["X-Product"], "SaaS")
        self.assertNotEqual(h1["X-Request-ID"], h2["X-Request-ID"])

    def test_channel_400_is_not_banned(self):
        # 11128 的 msg 也写 "request illegal"，走通用判据会误判成仍封
        for code, word in (("11101", "流式"), ("11128", "渠道"), ("11133", "模型名")):
            res = probe._read_chat_reply(
                400, {"code": int(code), "msg": "request illegal"},
                "https://x/v2/chat/completions", 0.0)
            self.assertEqual(res["verdict"], probe.RECHECK_UNKNOWN)
            self.assertIn(word, res["detail"])
            self.assertIn("地址和凭据都通", res["detail"])

    def test_11140_is_banned(self):
        payload = {"code": 11140, "msg": "request illegal",
                   "displayMsg": {"zh": "账号存在异常行为，已被限制使用"}}
        res = probe._read_chat_reply(
            403, payload, "https://x/v2/chat/completions", 0.0)
        self.assertEqual(res["verdict"], probe.RECHECK_BANNED)

    def test_sse_business_error_not_revived(self):
        raw = ('data: {"code":11140,"msg":"request illegal"}\n\n'
               'data: [DONE]\n\n')
        self.assertIn("11140", probe._sse_text(raw))
        res = probe._read_chat_reply(
            200, {"_raw": raw}, "https://x/v2/chat/completions", 0.0)
        self.assertEqual(res["verdict"], probe.RECHECK_BANNED)

    def test_post_json_stops_on_sse_done(self):
        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.chunks = [b'data: {"choices":[{"delta":{"content":"po"}}]}\n\n',
                               b'data: [DONE]\n\n', b'NEVER READ']

            def read1(self, n):
                return self.chunks.pop(0) if self.chunks else b""

            def read(self, n=-1):
                return self.read1(n)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, req, timeout=0):
                return Resp()

        with mock.patch.object(probe, "_opener", return_value=Opener()):
            status, payload = probe._post_json("http://x", {}, {}, 5)
        self.assertEqual(status, 200)
        self.assertIn("[DONE]", payload["_raw"])
        self.assertNotIn("NEVER READ", payload["_raw"])

    def test_model_chain_has_glm(self):
        chain = probe.model_chain("deepseek-v4.1-flash", None, "intl")
        self.assertIn("deepseek-v4.1-flash-sg", chain)
        self.assertIn("glm-5.3-flash", chain)

    def test_verify_proxy_overrides_every_row(self):
        import server

        cfg = dict(recheck.DEFAULTS)
        cfg["verify_proxy"] = "http://127.0.0.1:7891"
        for port in (0, 17901):
            self.assertEqual(recheck.plan_port(cfg, port)["proxy"],
                             "http://127.0.0.1:7891")
        pairs = [(1, acc()), (2, acc())]
        with mock.patch.object(server.recheck, "load_config", return_value=cfg), \
             mock.patch.object(server.store, "exit_ports_of", return_value={}):
            server.attach_exit_proxies(pairs)
        self.assertEqual({a.proxy for _, a in pairs},
                         {"http://127.0.0.1:7891"})

    def test_diagnose_uses_verify_proxy(self):
        import server

        class Fake:
            def _json(self, status, data):
                self.out = (status, data)

        fake = Fake()
        cfg = dict(recheck.DEFAULTS)
        cfg["verify_proxy"] = "http://127.0.0.1:7891"
        result = {"verdict": probe.RECHECK_BANNED, "detail": "仍被风控拦截",
                  "raw": "", "status": 403, "endpoint": "u", "latency_ms": 1,
                  "attempts": []}
        with mock.patch.object(server.store, "load_for_probe",
                               return_value=[(7, acc())]), \
             mock.patch.object(server.store, "exit_ports_of", return_value={7: 0}), \
             mock.patch.object(server.recheck, "load_config", return_value=cfg), \
             mock.patch.object(server.probe, "chat_probe",
                               return_value=result) as called:
            server.Handler._recheck_diagnose(fake, {"ids": [7]})
        self.assertEqual(called.call_args.kwargs["proxy"],
                         "http://127.0.0.1:7891")


class PinIpTests(unittest.TestCase):
    """固定出口 IP：配置、候选探测、每号请求前的漂移闸、接口联动。

    核心口径：端口只是本机编号，节点换线后同一个端口背后的公网 IP 会变。
    钉了 IP 就必须**每号发请求前**核对真实出口，探不到也按不符处理，
    绝不能带着疑问敲门。
    """

    def _cfg(self, **kw):
        cfg = json.loads(json.dumps(recheck.DEFAULTS))
        cfg["enabled"] = True
        cfg["mihomo"]["host"] = "127.0.0.1"
        cfg["mihomo"]["ports"] = "17901-17903"
        # _prepare 里不再去探节点名 / 出口 IP，测试只盯 IP 闸本身
        cfg["mihomo"]["enabled"] = False
        cfg.update(kw)
        return cfg

    @staticmethod
    def _res():
        return {"verdict": probe.RECHECK_REVIVED, "detail": "通了", "raw": "",
                "status": 200, "endpoint": "u", "latency_ms": 1,
                "attempts": [], "model": ""}

    @staticmethod
    def _exit(url, ip):
        return {"url": url, "ok": bool(ip), "exit_ip": ip,
                "latency_ms": 1, "error": "" if ip else "timeout"}

    def test_normalize_ip(self):
        self.assertEqual(recheck.normalize_ip(" 1.2.3.4 "), "1.2.3.4")
        self.assertEqual(recheck.normalize_ip("2001:0db8::1"), "2001:db8::1")
        for bad in ("", "abc", "1.2.3.4:8080", "001.2.3.4", "999.1.1.1",
                    "example.com", None):
            self.assertEqual(recheck.normalize_ip(bad), "", f"{bad!r} 不该算 IP")

    def test_config_coerces_pin_fields(self):
        cfg = recheck._coerce(self._cfg(pin_exit_ip="not-an-ip",
                                        pin_ip_action="whatever"))
        self.assertEqual(cfg["pin_exit_ip"], "")
        self.assertEqual(cfg["pin_ip_action"], recheck.PIN_RESELECT)
        cfg = recheck._coerce(self._cfg(pin_exit_ip="8.8.8.8",
                                        pin_ip_action="block"))
        self.assertEqual(cfg["pin_exit_ip"], "8.8.8.8")
        self.assertEqual(cfg["pin_ip_action"], "block")

    def test_config_save_and_reload_pin(self):
        with tempfile.TemporaryDirectory(prefix="wk-pin-") as tmp:
            with mock.patch.object(recheck, "CONFIG_PATH",
                                   os.path.join(tmp, "recheck.json")):
                recheck.save_config({"pin_exit_ip": "8.8.8.8",
                                     "pin_ip_action": "block"})
                cfg = recheck.load_config()
        self.assertEqual(cfg["pin_exit_ip"], "8.8.8.8")
        self.assertEqual(cfg["pin_ip_action"], "block")

    def test_candidates_include_fixed_and_ports(self):
        cfg = self._cfg(verify_proxy="http://127.0.0.1:7891")
        rows = recheck.pin_candidates(cfg)
        self.assertEqual(rows[0], {"port": 0, "url": "http://127.0.0.1:7891",
                                   "kind": "fixed"})
        self.assertEqual([r["port"] for r in rows[1:]], [17901, 17902, 17903])

    def test_host_with_scheme_matches_plan_port(self):
        """host 写成 http://host 时，钉 IP 探的地址必须和真请求的地址一样。"""
        cfg = self._cfg()
        cfg["mihomo"]["host"] = "http://127.0.0.1"
        url = recheck.plan_port(cfg, 17901)["proxy"]
        self.assertEqual(url, "http://127.0.0.1:17901")
        self.assertIn(url, [r["url"] for r in recheck.pin_candidates(cfg)])

    def test_pin_status_picks_matching_ports(self):
        cfg = self._cfg()
        with mock.patch.object(recheck, "probe_urls", side_effect=lambda urls, *_a, **_k: [
                self._exit(u, "1.2.3.4" if u.endswith(("17902", "17903")) else "9.9.9.9")
                for u in urls]):
            status = recheck.pin_status(cfg, wanted="1.2.3.4", probe_now=True)
        self.assertEqual(status["matched_ports"], [17902, 17903])
        self.assertEqual(status["best_port"], 17902)
        self.assertTrue(status["ok"])
        self.assertFalse(status["fixed_covers"])

    def test_pin_status_fixed_proxy_covers_ports(self):
        cfg = self._cfg(verify_proxy="http://127.0.0.1:7891")
        with mock.patch.object(recheck, "probe_urls", side_effect=lambda urls, *_a, **_k: [
                self._exit(u, "9.9.9.9") for u in urls]):
            status = recheck.pin_status(cfg, wanted="1.2.3.4", probe_now=True)
        self.assertTrue(status["fixed_covers"])
        self.assertFalse(status["fixed_matched"])
        self.assertEqual(status["best_port"], 0)
        self.assertIn("换端口无效", status["reason"])

    def test_pin_status_fixed_proxy_matches(self):
        cfg = self._cfg(verify_proxy="http://127.0.0.1:7891")
        with mock.patch.object(recheck, "probe_urls", side_effect=lambda urls, *_a, **_k: [
                self._exit(u, "1.2.3.4" if u == "http://127.0.0.1:7891" else "9.9.9.9")
                for u in urls]):
            status = recheck.pin_status(cfg, wanted="1.2.3.4", probe_now=True)
        self.assertTrue(status["fixed_ok"])
        self.assertTrue(status["ok"])
        self.assertEqual(status["best_port"], 0)

    def test_pin_check_url_caches_and_fails_closed(self):
        cfg = self._cfg(pin_exit_ip="1.2.3.4")
        cache: dict = {}
        with mock.patch.object(recheck, "probe_exit",
                               return_value=self._exit("u", "")) as called:
            one = recheck.pin_check_url(cfg, "u", cache)
            two = recheck.pin_check_url(cfg, "u", cache)
        self.assertFalse(one["ok"])
        self.assertFalse(two["ok"])
        self.assertEqual(called.call_count, 1, "同一条线一轮只该探一次")
        with mock.patch.object(recheck, "probe_exit",
                               return_value=self._exit("u", "1.2.3.4")):
            self.assertTrue(recheck.pin_check_url(cfg, "u", {})["ok"])

    def test_runner_allows_matching_ip(self):
        cfg = self._cfg(pin_exit_ip="1.2.3.4")
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "")), \
             mock.patch.object(recheck, "probe_exit",
                               return_value=self._exit("u", "1.2.3.4")), \
             mock.patch.object(recheck.probe, "chat_probe",
                               return_value=self._res()) as chat:
            runner = recheck.Runner(cfg)
            out = runner.run([(1, acc())], force=True, ports={1: 17901})
        self.assertEqual(chat.call_args.kwargs["proxy"],
                         "http://127.0.0.1:17901")
        self.assertTrue(out[0]["exit_ip_ok"])
        self.assertEqual(out[0]["exit_ip"], "1.2.3.4")
        self.assertEqual(runner.pin_report["blocked"], 0)

    def test_runner_reselects_drifted_port(self):
        cfg = self._cfg(pin_exit_ip="1.2.3.4")
        fake_exit = lambda url, *_a, **_k: self._exit(
            url, "1.2.3.4" if url.endswith("17902") else "9.9.9.9")
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "")), \
             mock.patch.object(recheck, "probe_exit", side_effect=fake_exit), \
             mock.patch.object(recheck, "probe_urls",
                               side_effect=lambda urls, *_a, **_k: [
                                   fake_exit(u) for u in urls]), \
             mock.patch.object(recheck.store, "exit_ports_in_use", return_value=[]), \
             mock.patch.object(recheck.store, "set_exit_map", return_value=1) as setmap, \
             mock.patch.object(recheck.probe, "chat_probe",
                               return_value=self._res()) as chat:
            runner = recheck.Runner(cfg)
            out = runner.run([(1, acc())], force=True, ports={1: 17901})
        self.assertEqual(chat.call_args.kwargs["proxy"],
                         "http://127.0.0.1:17902")
        self.assertEqual(setmap.call_args.args[0], [(1, 17902)])
        self.assertEqual(out[0]["pin_reselected"], 17902)
        self.assertTrue(out[0]["exit_ip_ok"])
        self.assertEqual(runner.pin_report["reselected"], 1)

    def test_runner_block_does_not_send_request(self):
        cfg = self._cfg(pin_exit_ip="1.2.3.4", pin_ip_action="block")
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "")), \
             mock.patch.object(recheck, "probe_exit",
                               return_value=self._exit("u", "9.9.9.9")), \
             mock.patch.object(recheck.probe, "chat_probe",
                               return_value=self._res()) as chat:
            runner = recheck.Runner(cfg)
            out = runner.run([(1, acc())], force=True, ports={1: 17901})
        chat.assert_not_called()
        self.assertTrue(out[0]["skipped"])
        self.assertFalse(out[0]["exit_ip_ok"])
        self.assertIn("阻止", out[0]["detail"])
        self.assertEqual(runner.pin_report["blocked"], 1)

    def test_runner_fixed_proxy_never_fakes_reselect(self):
        cfg = self._cfg(pin_exit_ip="1.2.3.4",
                        verify_proxy="http://127.0.0.1:7891")
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "")), \
             mock.patch.object(recheck, "probe_exit",
                               return_value=self._exit("u", "9.9.9.9")), \
             mock.patch.object(recheck, "pin_reselect") as scan, \
             mock.patch.object(recheck.store, "set_exit_map") as setmap, \
             mock.patch.object(recheck.probe, "chat_probe",
                               return_value=self._res()) as chat:
            out = recheck.Runner(cfg).run([(1, acc())], force=True,
                                         ports={1: 17901})
        scan.assert_not_called()
        setmap.assert_not_called()
        chat.assert_not_called()
        self.assertTrue(out[0]["skipped"])
        self.assertIn("固定验证代理", out[0]["detail"])

    def test_runner_without_pin_never_probes_ip(self):
        cfg = self._cfg()
        with mock.patch.object(recheck, "_fetch_listeners", return_value=({}, "")), \
             mock.patch.object(recheck, "probe_exit") as called, \
             mock.patch.object(recheck.probe, "chat_probe",
                               return_value=self._res()):
            out = recheck.Runner(cfg).run([(1, acc())], force=True,
                                         ports={1: 17901})
        called.assert_not_called()
        self.assertTrue(out[0]["exit_ip_ok"])
        self.assertEqual(out[0]["exit_ip_expected"], "")

    def test_exit_ip_endpoint_renames_match_flag(self):
        import server

        class Fake:
            path = "/api/recheck/exit-ip?ip=1.2.3.4&probe=1"

            def _json(self, status, data):
                self.out = (status, data)

        status = {"wanted": "1.2.3.4", "action": "reselect", "probed": True,
                  "candidates": [], "matched_ports": [17902], "best_port": 17902,
                  "fixed_covers": False, "fixed_matched": False,
                  "fixed_ok": False, "ok": True, "reason": "匹配"}
        fake = Fake()
        with mock.patch.object(server.recheck, "load_config", return_value=self._cfg()), \
             mock.patch.object(server.recheck, "pin_status", return_value=status):
            server.Handler._recheck_exit_ip(fake)
        self.assertTrue(fake.out[1]["ok"], "接口成功不能被探测结果覆盖")
        self.assertTrue(fake.out[1]["matched"])
        self.assertEqual(fake.out[1]["matched_ports"], [17902])

    def test_assign_by_ip_writes_same_port_to_every_row(self):
        import server

        class Fake:
            def _json(self, status, data):
                self.out = (status, data)

        status = {"wanted": "1.2.3.4", "action": "reselect", "probed": True,
                  "candidates": [], "matched_ports": [17902, 17903],
                  "best_port": 0, "fixed_covers": False, "fixed_matched": False,
                  "fixed_ok": False, "ok": True, "reason": "匹配"}
        fake = Fake()
        with mock.patch.object(server.recheck, "load_config", return_value=self._cfg()), \
             mock.patch.object(server.recheck, "pin_status", return_value=status), \
             mock.patch.object(server.recheck, "discover_nodes",
                               return_value={"nodes": [{"port": 17901}]}), \
             mock.patch.object(server.store, "recheck_ids", return_value=[1, 2]), \
             mock.patch.object(server.store, "exit_ports_in_use", return_value=[]), \
             mock.patch.object(server.store, "set_exit_map", return_value=2) as setmap, \
             mock.patch.object(server.recheck, "save_config",
                               side_effect=lambda patch: {**self._cfg(), **patch}) as save, \
             mock.patch.object(server.store, "list_accounts", return_value=[]), \
             mock.patch.object(server.store, "recheck_stats", return_value={}):
            server.Handler._recheck_assign(fake, {"ip": "1.2.3.4"})
        self.assertEqual(setmap.call_args.args[0], [(1, 17902), (2, 17902)])
        self.assertEqual(save.call_args.args[0]["pin_exit_ip"], "1.2.3.4")
        self.assertEqual(save.call_args.args[0]["exit_mode"], recheck.EXIT_SAME)
        self.assertEqual(fake.out[1]["mode"], "same")

    def test_assign_by_ip_without_match_fails(self):
        import server

        class Fake:
            def _json(self, status, data):
                self.out = (status, data)

        status = {"wanted": "1.2.3.4", "action": "reselect", "probed": True,
                  "candidates": [], "matched_ports": [], "best_port": 0,
                  "fixed_covers": False, "fixed_matched": False,
                  "fixed_ok": False, "ok": False, "reason": "没有端口匹配"}
        with mock.patch.object(server.recheck, "load_config", return_value=self._cfg()), \
             mock.patch.object(server.recheck, "pin_status", return_value=status):
            with self.assertRaises(ValueError):
                server.Handler._recheck_assign(Fake(), {"ip": "1.2.3.4"})

    def test_auto_recheck_passes_port_map(self):
        """自动复检以前不传 ports，名单里配的出口在定时任务里等于没生效。"""
        import datetime
        import server

        cfg = self._cfg(auto=True, auto_hour=3)
        with mock.patch.object(server, "_auto_state", {"last_day": ""}), \
             mock.patch.object(server.clock, "dt",
                               return_value=datetime.datetime(2026, 11, 12, 4, 0)), \
             mock.patch.object(server.recheck, "load_config", return_value=cfg), \
             mock.patch.object(server.store, "list_accounts",
                               return_value=[{"id": 1}]), \
             mock.patch.object(server.store, "load_for_probe",
                               return_value=[(1, acc())]), \
             mock.patch.object(server.store, "exit_ports_of",
                               return_value={1: 17901}), \
             mock.patch.object(server.store, "update_recheck", return_value=0), \
             mock.patch.object(server.recheck.Runner, "run",
                               return_value=[]) as run:
            server._auto_recheck_once()
        self.assertEqual(run.call_args.kwargs["ports"], {1: 17901})


if __name__ == "__main__":
    unittest.main(verbosity=2)
