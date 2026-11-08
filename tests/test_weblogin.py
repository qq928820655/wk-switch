"""官网登录模块的单测：官方三段协议、会话状态、入库与积分回填。

全部走注入的假 requester / probe_fn，不发真实请求、不碰本地账户库。
跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import store, weblogin  # noqa: E402


def jwt(uid: str, exp: int = 4102444800) -> str:
    """造一个只用于本地解析的 JWT（签名段不校验）。"""
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")
    return "%s.%s.sig" % (seg({"alg": "none"}), seg({"uid": uid, "exp": exp}))


class LoginTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_dir, self._old_path = store.DATA_DIR, store.DB_PATH
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "accounts.db")
        self.addCleanup(self._restore)
        self.calls = []
        self.token_calls = 0
        self.login_uid = "u-oauth"
        self.pending_first = False
        self.profile_fails = False
        self.token_payload = None
        self.profile_payload = {"code": 0, "data": {
            "nickname": "官方昵称", "email": "a@example.com",
            "enterpriseId": "corp-1", "tier": "Pro",
        }}
        self.probe_hits = []

    def _restore(self) -> None:
        store.DATA_DIR, store.DB_PATH = self._old_dir, self._old_path

    def requester(self, url, method="GET", headers=None, body=None,
                  timeout=25.0, proxy=""):
        self.calls.append({"url": url, "method": method, "headers": dict(headers or {}),
                           "body": body, "proxy": proxy})
        if weblogin.AUTH_STATE_PATH in url:
            return 200, {"code": 0, "data": {
                "state": "state-1",
                "authUrl": "https://www.workbuddy.ai/login?platform=CLI&state=state-1"}}
        if weblogin.AUTH_TOKEN_PATH in url:
            self.token_calls += 1
            if self.pending_first and self.token_calls == 1:
                return 200, {"code": weblogin.LOGIN_PENDING, "msg": "waiting"}
            if self.token_payload is not None:
                return 200, self.token_payload
            return 200, {"code": 0, "data": {
                "accessToken": jwt(self.login_uid), "refreshToken": "rt-oauth",
                "expiresAt": 4102444800, "domain": "www.workbuddy.ai"}}
        if weblogin.LOGIN_ACCOUNT_PATH in url:
            if self.profile_fails:
                raise OSError("profile endpoint down")
            return 200, self.profile_payload
        return 404, {"msg": "unexpected " + url}

    def probe_fn(self, acc, timeout=0, proxy=""):
        self.probe_hits.append((acc.uid, proxy))
        return {"state": "valid", "remain": 88.5, "used": 1.5, "size": 90.0,
                "tier": "Pro", "checked_at": 1730000000, "error": "", "restrict": False}

    def manager(self, **kw):
        return weblogin.LoginManager(requester=self.requester,
                                    probe_fn=self.probe_fn, **kw)

    def seed_row(self, uid="u-old", realm="intl", exit_port=17953, risk=False):
        """先放一行老账号，带上出口和风控状态，用来验证重登有没有保住它们。"""
        from wk.model import Account

        a = Account()
        a.uid, a.nickname, a.realm = uid, "老账户", realm
        a.access_token, a.refresh_token = "at-old", "rt-old"
        a.expires_at = 4102444800
        store.upsert([a], batch="b1", source="import", risk=risk)
        row_id = store.list_accounts()[0]["id"]
        store.set_exit_port([row_id], exit_port)
        store.update_credits([{"id": row_id, "state": "valid", "remain": 12.0,
                               "used": 1.0, "size": 13.0, "checked_at": 1730000000,
                               "note": "", "restrict": False}])
        return row_id

    def start(self, mgr, **kw):
        kw.setdefault("fetch_credits", False)
        return mgr.start(**kw)

    def poll_ok(self, mgr, **kw):
        kw.setdefault("fetch_credits", False)
        return mgr.poll("state-1", **kw)


class ProtocolTests(LoginTestBase):
    def test_start_returns_official_auth_url(self):
        mgr = self.manager()
        out = self.start(mgr, proxy="127.0.0.1:7891")
        self.assertEqual(out["state"], "state-1")
        self.assertTrue(out["authUrl"].startswith("https://www.workbuddy.ai/login"))
        self.assertEqual(out["proxy"], "http://127.0.0.1:7891")
        self.assertEqual(out["realm"], "intl")
        self.assertEqual(mgr.pending(), 1)
        call = self.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["body"], {})
        self.assertIn("platform=CLI", call["url"])
        self.assertEqual(call["proxy"], "http://127.0.0.1:7891")

    def test_start_uses_official_channel_headers(self):
        self.start(self.manager())
        h = self.calls[0]["headers"]
        self.assertEqual(h["User-Agent"], "WorkBuddy/5.9.4")
        self.assertEqual(h["Origin"], "https://www.workbuddy.ai")
        self.assertEqual(h["Referer"], "https://www.workbuddy.ai/")
        self.assertEqual(h["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(h["Content-Type"], "application/json")

    def test_cn_realm_uses_cn_upstream(self):
        self.start(self.manager(), realm="cn")
        call = self.calls[0]
        self.assertTrue(call["url"].startswith("https://copilot.tencent.com"))
        self.assertEqual(call["headers"]["User-Agent"], "WorkBuddy/5.8.3")
        self.assertEqual(call["headers"]["Origin"], "https://www.codebuddy.cn")

    def test_start_rejects_bad_inputs(self):
        mgr = self.manager()
        with self.assertRaises(weblogin.WebLoginError):
            mgr.start(realm="xx")
        with self.assertRaises(weblogin.WebLoginError):
            mgr.start(proxy="socks5://127.0.0.1:1080")
        self.assertEqual(mgr.pending(), 0)

    def test_start_reports_upstream_error(self):
        def bad(url, method="GET", headers=None, body=None, timeout=0, proxy=""):
            return 200, {"code": 11140, "msg": "channel blocked"}
        mgr = weblogin.LoginManager(requester=bad)
        with self.assertRaisesRegex(weblogin.WebLoginError, "11140"):
            mgr.start()
        self.assertEqual(mgr.pending(), 0)

    def test_start_requires_state_and_auth_url(self):
        def bad(url, method="GET", headers=None, body=None, timeout=0, proxy=""):
            return 200, {"code": 0, "data": {"state": "s"}}
        mgr = weblogin.LoginManager(requester=bad)
        with self.assertRaisesRegex(weblogin.WebLoginError, "state"):
            mgr.start()


class PollTests(LoginTestBase):
    def test_pending_then_ok(self):
        mgr = self.manager()
        self.pending_first = True
        self.start(mgr)
        first = self.poll_ok(mgr)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(len(store.list_accounts()), 0)
        second = self.poll_ok(mgr)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(len(store.list_accounts()), 1)

    def test_ok_writes_full_account(self):
        mgr = self.manager()
        self.start(mgr, proxy="http://127.0.0.1:7891")
        out = self.poll_ok(mgr)
        self.assertEqual(out["status"], "ok")
        a = out["account"]
        self.assertEqual(a["uid"], "u-oauth")
        self.assertEqual(a["name"], "官方昵称")
        self.assertEqual(a["email"], "a@example.com")
        self.assertEqual(a["realm"], "intl")
        self.assertEqual(a["domain"], "www.workbuddy.ai")
        self.assertEqual(a["source"], "oauth")
        self.assertTrue(a["has_rt"])
        self.assertNotIn("accessToken", a)

        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "oauth")
        self.assertTrue(rows[0]["batch"].startswith("oauth-"))
        exported = store.load_for_export()[0]
        self.assertEqual(exported.access_token, jwt("u-oauth"))
        self.assertEqual(exported.refresh_token, "rt-oauth")
        self.assertEqual(exported.enterprise_id, "corp-1")
        self.assertEqual(exported.tier, "Pro")
        self.assertEqual(exported.profile_raw["nickname"], "官方昵称")
        self.assertEqual(exported.expires_at, 4102444800)

    def test_profile_failure_still_imports(self):
        mgr = self.manager()
        self.profile_fails = True
        self.start(mgr)
        out = self.poll_ok(mgr)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["account"]["name"], "u-oauth")
        self.assertEqual(len(store.list_accounts()), 1)

    def test_error_and_pending_codes_never_write(self):
        mgr = self.manager()
        self.start(mgr)
        self.token_payload = {"code": 11140, "msg": "restricted"}
        self.assertEqual(self.poll_ok(mgr)["status"], "error")
        self.token_payload = {"code": weblogin.LOGIN_PENDING, "msg": "wait"}
        self.assertEqual(self.poll_ok(mgr)["status"], "pending")
        self.assertEqual(len(store.list_accounts()), 0)

    def test_unknown_and_expired(self):
        mgr = self.manager()
        self.assertEqual(mgr.poll("no-such")["status"], "unknown")
        self.start(mgr)
        with mgr._lock:
            mgr._sessions["state-1"]["created"] -= weblogin.LOGIN_TTL_SECONDS + 5
        self.assertEqual(self.poll_ok(mgr)["status"], "expired")
        self.assertEqual(mgr.pending(), 0)

    def test_success_clears_session_and_is_idempotent(self):
        mgr = self.manager()
        self.start(mgr)
        self.assertEqual(self.poll_ok(mgr)["status"], "ok")
        self.assertEqual(mgr.poll("state-1")["status"], "unknown")
        self.assertEqual(len(store.list_accounts()), 1)

    def test_cancel(self):
        mgr = self.manager()
        self.start(mgr)
        self.assertTrue(mgr.cancel("state-1"))
        self.assertFalse(mgr.cancel("state-1"))
        self.assertEqual(mgr.poll("state-1")["status"], "unknown")

    def test_auto_credits_written_to_store(self):
        mgr = self.manager()
        self.start(mgr, fetch_credits=True, proxy="http://127.0.0.1:7891")
        out = mgr.poll("state-1")
        self.assertEqual(out["credits"]["remain"], 88.5)
        self.assertEqual(out["credits"]["tier"], "Pro")
        self.assertEqual(self.probe_hits, [("u-oauth", "http://127.0.0.1:7891")])
        row = store.list_accounts()[0]
        self.assertEqual(row["credit_remain"], 88.5)
        self.assertEqual(row["credit_state"], "valid")

    def test_credits_can_be_disabled(self):
        mgr = self.manager()
        self.start(mgr, fetch_credits=False)
        out = mgr.poll("state-1")
        self.assertEqual(out["credits"], {})
        self.assertEqual(self.probe_hits, [])

    def test_poll_network_error_is_pending(self):
        def broken(url, method="GET", headers=None, body=None, timeout=0, proxy=""):
            if weblogin.AUTH_STATE_PATH in url:
                return 200, {"code": 0, "data": {"state": "state-1", "authUrl": "https://x/login"}}
            raise OSError("network down")
        mgr = weblogin.LoginManager(requester=broken)
        mgr.start()
        self.assertEqual(mgr.poll("state-1")["status"], "pending")
        self.assertEqual(len(store.list_accounts()), 0)


class ReLoginTests(LoginTestBase):
    """已有账户重新登录：按行 id 覆盖，元数据保留，串号拒绝。"""

    def test_row_by_id(self):
        row_id = self.seed_row()
        row = store.row_by_id(row_id)
        self.assertEqual(row["uid"], "u-old")
        self.assertEqual(row["exit_port"], 17953)
        self.assertIsNone(store.row_by_id(999999))

    def test_rebind_keeps_metadata_and_clears_stale_credits(self):
        from wk.model import Account

        row_id = self.seed_row(risk=True)
        a = Account()
        a.uid, a.nickname, a.realm = "u-old", "新昵称", "intl"
        a.access_token, a.refresh_token, a.email = "at-new", "rt-new", "new@example.com"
        a.expires_at = 4102444800
        stats = store.rebind(row_id, a, batch="oauth-test", source="oauth")
        self.assertEqual(stats["updated"], 1)
        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row_id)
        self.assertEqual(rows[0]["name"], "新昵称")
        self.assertEqual(rows[0]["exit_port"], 17953)
        self.assertEqual(rows[0]["recheck_state"], "banned")
        self.assertIsNone(rows[0]["credit_remain"])
        self.assertEqual(rows[0]["batch"], "oauth-test")
        self.assertEqual(store.load_for_export()[0].access_token, "at-new")

    def test_poll_target_updates_existing_row(self):
        row_id = self.seed_row(uid="u-oauth")
        mgr = self.manager()
        target = store.row_by_id(row_id)
        out = self.start(mgr, target=target)
        self.assertEqual(out["target"]["id"], row_id)
        res = self.poll_ok(mgr)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["target"]["id"], row_id)
        self.assertEqual(res["store"]["updated"], 1)
        self.assertEqual(res["account"]["id"], row_id)
        rows = store.list_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row_id)
        self.assertEqual(rows[0]["exit_port"], 17953)
        self.assertEqual(rows[0]["source"], "oauth")
        self.assertEqual(store.load_for_export()[0].access_token, jwt("u-oauth"))

    def test_poll_target_mismatch_never_overwrites(self):
        row_id = self.seed_row(uid="u-old")
        self.login_uid = "u-other"
        mgr = self.manager()
        self.start(mgr, target=store.row_by_id(row_id))
        res = self.poll_ok(mgr)
        self.assertEqual(res["status"], "mismatch")
        self.assertEqual(len(store.list_accounts()), 1)
        self.assertEqual(store.load_for_export()[0].access_token, "at-old")

    def test_sha256_placeholder_uid_allows_rebind(self):
        row_id = self.seed_row(uid="sha256-abc")
        mgr = self.manager()
        self.start(mgr, target=store.row_by_id(row_id))
        res = self.poll_ok(mgr)
        self.assertEqual(res["status"], "ok")
        row = store.list_accounts()[0]
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["uid"], "u-oauth")

    def test_missing_target_row_is_error_not_insert(self):
        mgr = self.manager()
        self.start(mgr, target={"id": 999999, "uid": "u-oauth", "realm": "intl",
                                "name": "已删除", "exit_port": 0})
        res = self.poll_ok(mgr)
        self.assertEqual(res["status"], "error")
        self.assertEqual(len(store.list_accounts()), 0)


class HttpRouteTests(LoginTestBase):
    """直接调 Handler 方法，验证路由参数与默认代理，不启 HTTP 服务。"""

    def _fake(self, path=""):
        class Fake:
            def __init__(self, p):
                self.path = p
                self.out = None

            def _json(self, status, payload):
                self.out = (status, payload)

            def _error(self, status, message):
                self.out = (status, {"ok": False, "error": message})

        return Fake(path)

    def test_start_route_target_uses_its_exit_port(self):
        import server

        row_id = self.seed_row(uid="u-oauth", exit_port=17953)
        mgr = self.manager()
        cfg = {"verify_proxy": "", "mihomo": {"host": "127.0.0.1"}}
        with mock.patch.object(server.weblogin, "LOGINS", mgr), \
             mock.patch.object(server.recheck, "load_config", return_value=cfg):
            fake = self._fake()
            server.Handler._store_login_start(fake, {"target_id": row_id})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["target"]["id"], row_id)
        self.assertEqual(data["realm"], "intl")
        self.assertIn("17953", data["proxy"])
        self.assertEqual(self.calls[0]["proxy"], data["proxy"])

    def test_config_route_target_uses_its_exit_port(self):
        import server

        row_id = self.seed_row(uid="u-oauth", exit_port=17953)
        cfg = {"verify_proxy": "", "mihomo": {"host": "127.0.0.1"}}
        with mock.patch.object(server.recheck, "load_config", return_value=cfg):
            fake = self._fake("/api/store/login/config?target_id=%d" % row_id)
            server.Handler._store_login_config(fake)
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["target"]["id"], row_id)
        self.assertIn("17953", data["proxy"])

    def test_start_route_unknown_target_rejected(self):
        import server

        fake = self._fake()
        with self.assertRaises(ValueError):
            server.Handler._store_login_start(fake, {"target_id": 999999})
        self.assertIsNone(fake.out)

    def test_config_route_returns_default_proxy(self):
        import server

        with mock.patch.object(server.recheck, "load_config",
                               return_value={"verify_proxy": "http://127.0.0.1:7891"}):
            fake = self._fake("/api/store/login/config")
            server.Handler._store_login_config(fake)
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["proxy"], "http://127.0.0.1:7891")
        self.assertEqual(data["platform"], "CLI")
        self.assertEqual({r["value"] for r in data["realms"]}, {"intl", "cn"})

    def test_start_route_uses_verify_proxy_by_default(self):
        import server

        mgr = self.manager()
        with mock.patch.object(server.weblogin, "LOGINS", mgr), \
             mock.patch.object(server.recheck, "load_config",
                               return_value={"verify_proxy": "http://127.0.0.1:7891"}):
            fake = self._fake()
            server.Handler._store_login_start(fake, {"realm": "intl"})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["proxy"], "http://127.0.0.1:7891")
        self.assertEqual(self.calls[0]["proxy"], "http://127.0.0.1:7891")

    def test_start_route_explicit_empty_proxy_is_direct(self):
        import server

        mgr = self.manager()
        with mock.patch.object(server.weblogin, "LOGINS", mgr):
            fake = self._fake()
            server.Handler._store_login_start(fake, {"realm": "intl", "proxy": ""})
        status, data = fake.out
        self.assertEqual(status, 200)
        self.assertEqual(data["proxy"], "")
        self.assertEqual(self.calls[0]["proxy"], "")

    def test_start_route_rejects_unknown_realm(self):
        import server

        fake = self._fake()
        with self.assertRaises(ValueError):
            server.Handler._store_login_start(fake, {"realm": "xx"})
        self.assertIsNone(fake.out)

    def test_poll_and_cancel_routes(self):
        import server

        mgr = self.manager()
        self.start(mgr)
        with mock.patch.object(server.weblogin, "LOGINS", mgr):
            fake = self._fake("/api/store/login/poll?state=state-1")
            server.Handler._store_login_poll(fake)
            status, data = fake.out
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(len(store.list_accounts()), 1)

            mgr.start(fetch_credits=False)
            fake2 = self._fake()
            server.Handler._store_login_cancel(fake2, {"state": "state-1"})
            self.assertTrue(fake2.out[1]["cancelled"])

    def test_poll_route_requires_state(self):
        import server

        fake = self._fake("/api/store/login/poll")
        server.Handler._store_login_poll(fake)
        self.assertEqual(fake.out[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
