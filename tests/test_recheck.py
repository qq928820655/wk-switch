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


if __name__ == "__main__":
    unittest.main(verbosity=2)
