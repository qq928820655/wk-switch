"""封号复检：判定口径、名单流转、mihomo 端口解析。"""

from __future__ import annotations

import json
import os
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

    def test_explicit_mode_wins_over_legacy(self):
        cfg = self._cfg(exit_mode="custom", rotate_proxy=True)
        self.assertEqual(cfg["exit_mode"], recheck.EXIT_CUSTOM)
        self.assertFalse(cfg["rotate_proxy"])

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
        cfg = self._cfg(exit_mode="custom",
                        exit_custom="http://c1:1\nhttp://c2:2")
        got = [recheck.plan_exit(cfg, [], i)["proxy"] for i in range(5)]
        self.assertEqual(got, ["http://c1:1", "http://c2:2",
                               "http://c1:1", "http://c2:2", "http://c1:1"])

    def test_custom_empty_falls_back_to_same(self):
        """表里一个合法地址都没有时不能谁都直连了事，要按 same 处理。"""
        cfg = self._cfg(exit_mode="custom", exit_custom="socks5://x:1",
                        exit_pin="7.7.7.7:1080")
        plan = recheck.plan_exit(cfg, [], 0)
        self.assertEqual(plan["proxy"], "http://7.7.7.7:1080")
        self.assertEqual(plan["mode"], recheck.EXIT_SAME)


class RunnerExitTests(unittest.TestCase):
    """Runner 真的把 plan 算出来的代理传下去了没有。"""

    def _cfg(self, **kw):
        return recheck._coerce(dict(recheck.DEFAULTS, enabled=True, gap_seconds=1.0, **kw))

    def test_custom_passes_url_to_chat_probe(self):
        cfg = self._cfg(exit_mode="custom", exit_custom="http://p1:1")
        runner = recheck.Runner(cfg)
        seen = []

        def fake(acc_, timeout=0, proxy=""):
            seen.append(proxy)
            return {"verdict": probe.RECHECK_REVIVED, "detail": "", "raw": "",
                    "status": 200, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake), \
             mock.patch.object(recheck.Runner, "_warm_exits", lambda self: None):
            results = runner.run([(1, acc())], force=True)
        self.assertEqual(seen, ["http://p1:1"])
        self.assertEqual(results[0]["proxy"], "http://p1:1")
        self.assertEqual(results[0]["exit_mode"], recheck.EXIT_CUSTOM)

    def test_same_sends_same_proxy_for_every_row(self):
        cfg = self._cfg(exit_mode="same", exit_pin="http://pin:9")
        runner = recheck.Runner(cfg)
        seen = []

        def fake(acc_, timeout=0, proxy=""):
            seen.append(proxy)
            return {"verdict": probe.RECHECK_BANNED, "detail": "", "raw": "",
                    "status": 403, "endpoint": "", "latency_ms": 1}

        with mock.patch.object(probe, "chat_probe", fake):
            runner.run([(1, acc(uid="a")), (2, acc(uid="b"))], force=True)
        self.assertEqual(seen, ["http://pin:9", "http://pin:9"])

    def test_warm_fills_exit_ip_for_custom(self):
        cfg = self._cfg(exit_mode="custom", exit_custom="http://p1:1")
        runner = recheck.Runner(cfg)
        with mock.patch.object(recheck, "probe_exit",
                               lambda url, pu, to: {"url": url, "ok": True,
                                                    "exit_ip": "8.8.8.8",
                                                    "latency_ms": 1, "error": ""}):
            runner._warm_exits()
        self.assertEqual(runner._ips["http://p1:1"], "8.8.8.8")

    def test_warm_fills_exit_ip_for_pinned_same(self):
        """同一出口也要报 IP——这档的全部意义就是确认「那个 IP 是谁」。"""
        cfg = self._cfg(exit_mode="same", exit_pin="http://pin:9")
        runner = recheck.Runner(cfg)
        with mock.patch.object(recheck, "probe_exit",
                               lambda url, pu, to: {"url": url, "ok": True,
                                                    "exit_ip": "6.6.6.6",
                                                    "latency_ms": 1, "error": ""}):
            runner._warm_exits()
        self.assertEqual(runner._ips["http://pin:9"], "6.6.6.6")
        self.assertEqual(runner._plan(0)["exit_ip"], "6.6.6.6")


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
        with mock.patch.object(recheck, "fetch_listeners",
                               return_value={17901: {"name": "listener-1", "proxy": "香港01"},
                                             17902: {"name": "listener-2", "proxy": ""}}), \
             mock.patch.object(recheck, "probe_urls", return_value=nodes):
            data = recheck.discover_nodes(cfg, probe_now=True)
        self.assertEqual([n["name"] for n in data["nodes"]], ["香港01", "listener-2"])
        self.assertTrue(data["named"])
        self.assertEqual(data["nodes"][0]["port"], 17901)

    def test_discover_nodes_falls_back_to_address(self):
        """控制器不通时 name 是空串，前端退回显示地址。"""
        cfg = self._cfg("")
        with mock.patch.object(recheck, "fetch_listeners", return_value={}), \
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
