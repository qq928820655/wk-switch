"""每日活跃的单测：配置、出口选择、两条腿的判定、落库与调度条件。

跑法：python -m unittest tests.test_activity -v

不打真实网络：`probe._post_json`、`activity._web_call`、`activity.run_turn`
全部打桩。真实链路（建会话 → 沙箱 → ACP）在交付说明里单列，测试只锁行为。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import activity, recheck, store  # noqa: E402
from wk.model import Account  # noqa: E402


class ActivityBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

        self._old_db = (store.DATA_DIR, store.DB_PATH)
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "accounts.db")

        self._old_cfg = activity.CONFIG_PATH
        self._old_rc = recheck.CONFIG_PATH
        cfgdir = os.path.join(self.tmp.name, "config")
        os.makedirs(cfgdir, exist_ok=True)
        activity.CONFIG_PATH = os.path.join(cfgdir, "activity.json")
        recheck.CONFIG_PATH = os.path.join(cfgdir, "recheck.json")
        with open(recheck.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"mihomo": {"host": "cli-proxy-mihomo"}}, fh)
        self._env = mock.patch.dict(os.environ, {
            "WB_PROXY_DISCOVER_HOST": "", "WB_PROXY_DISCOVER_PORTS": "",
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def _restore(self) -> None:
        store.DATA_DIR, store.DB_PATH = self._old_db
        activity.CONFIG_PATH = self._old_cfg
        recheck.CONFIG_PATH = self._old_rc

    def acc(self, uid: str = "u-1", realm: str = "intl", token: str = "tok") -> Account:
        return Account(uid=uid, access_token=token, realm=realm, nickname=uid)


class ConfigTest(ActivityBase):
    def test_defaults_when_file_missing(self) -> None:
        cfg = activity.load_config()
        self.assertTrue(cfg["web"])
        self.assertTrue(cfg["desktop"])
        self.assertEqual(cfg["chat_model"], "deepseek-v4.1-flash")
        self.assertEqual(cfg["timeout"], 120)

    def test_save_roundtrip_and_clamp(self) -> None:
        cfg = activity.save_config({"timeout": 99999, "concurrency": 0,
                                    "enabled": False, "unknown_key": "x"})
        self.assertEqual(cfg["timeout"], 1800)     # 上限收口
        self.assertEqual(cfg["concurrency"], 1)    # 下限收口
        self.assertFalse(cfg["enabled"])
        self.assertNotIn("unknown_key", cfg)
        again = activity.load_config()
        self.assertEqual(again["timeout"], 1800)

    def test_bad_file_falls_back(self) -> None:
        with open(activity.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(activity.load_config()["rounds"], 2)


class ProxyTest(ActivityBase):
    def test_fixed_proxy_wins(self) -> None:
        cfg = {"proxy": "http://127.0.0.1:7891", "use_exit": True,
               "fallback_proxy": "http://127.0.0.1:1080"}
        self.assertEqual(activity.proxy_for(self.acc(), cfg, 17901),
                         "http://127.0.0.1:7891")

    def test_exit_port_builds_mihomo_url(self) -> None:
        cfg = {"proxy": "", "use_exit": True, "fallback_proxy": ""}
        self.assertEqual(activity.proxy_for(self.acc(), cfg, 17903),
                         "http://cli-proxy-mihomo:17903")

    def test_zero_port_falls_back_then_direct(self) -> None:
        cfg = {"proxy": "", "use_exit": True, "fallback_proxy": "http://127.0.0.1:7891"}
        self.assertEqual(activity.proxy_for(self.acc(), cfg, 0), "http://127.0.0.1:7891")
        cfg["fallback_proxy"] = ""
        self.assertEqual(activity.proxy_for(self.acc(), cfg, 0), "")

    def test_use_exit_off_skips_port(self) -> None:
        cfg = {"proxy": "", "use_exit": False, "fallback_proxy": "http://127.0.0.1:7891"}
        self.assertEqual(activity.proxy_for(self.acc(), cfg, 17903),
                         "http://127.0.0.1:7891")


class StoreTest(ActivityBase):
    def _add(self, ok: bool, row_id: int = 1, skipped: bool = False) -> dict:
        return store.activity_add({
            "row_id": row_id, "uid": "u-1", "name": "Avery", "realm": "intl",
            "at": int(__import__("time").time()), "ok": ok, "skipped": skipped,
            "mode": "web", "phase": "turn", "status": 200, "model": "m",
            "proxy": "http://x:1", "elapsed_ms": 1200, "detail": "ok",
        })

    def test_add_and_rows(self) -> None:
        self._add(True)
        self._add(False)
        rows = store.activity_rows("today")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["at_text"])
        self.assertIn("ok", rows[0])

    def test_filter_by_result(self) -> None:
        self._add(True)
        self._add(False)
        self.assertEqual(len(store.activity_rows("today", result="ok")), 1)
        self.assertEqual(len(store.activity_rows("today", result="failed")), 1)

    def test_skipped_is_not_failed(self) -> None:
        self._add(True, skipped=True)
        self.assertEqual(len(store.activity_rows("today", result="failed")), 0)
        self.assertEqual(len(store.activity_rows("today", result="skipped")), 1)

    def test_done_today(self) -> None:
        self.assertFalse(store.activity_done_today(1))
        self._add(False)
        self.assertFalse(store.activity_done_today(1))
        self._add(True)
        self.assertTrue(store.activity_done_today(1))
        self.assertFalse(store.activity_done_today(2))

    def test_yesterday_does_not_count(self) -> None:
        store.activity_add({"row_id": 1, "uid": "u", "ok": 1,
                            "at": int(__import__("time").time()) - 86400 * 2})
        self.assertFalse(store.activity_done_today(1))

    def test_summary_and_last(self) -> None:
        self._add(True)
        self._add(False)
        summary = store.activity_summary()
        self.assertEqual(summary["today_ok"], 1)
        self.assertEqual(summary["today_failed"], 1)
        last = store.activity_last()
        self.assertIn(1, last)
        self.assertFalse(last[1]["ok"])          # 最新那条是失败


class RunOneTest(ActivityBase):
    def test_realm_and_token_guards(self) -> None:
        cfg = activity.load_config()
        cn = activity.run_one(self.acc(realm="cn"), cfg)
        self.assertTrue(cn["skipped"])
        blank = activity.run_one(self.acc(token=""), cfg)
        self.assertTrue(blank["skipped"])

    def test_desktop_ok_web_failed_is_failure(self) -> None:
        cfg = activity.load_config()
        desktop = {"ok": True, "status": 200, "detail": "桌面对话通过",
                   "elapsed_ms": 100}
        web = {"ok": False, "phase": "turn", "error": "沙箱连接被重置",
               "conversation": "c1", "elapsed_ms": 900}
        with mock.patch.object(activity, "desktop_daily", return_value=desktop), \
                mock.patch.object(activity, "web_daily", return_value=web):
            res = activity.run_one(self.acc(), cfg)
        self.assertFalse(res["ok"])              # 网页腿失败就是失败
        self.assertIn("网页腿失败", res["detail"])
        self.assertEqual(res["conversation"], "c1")

    def test_web_ok(self) -> None:
        cfg = activity.load_config()
        web = {"ok": True, "phase": "turn", "conversation": "c2",
               "chunks": 3, "elapsed_ms": 1000, "error": ""}
        with mock.patch.object(activity, "desktop_daily",
                               return_value={"ok": False, "detail": "400"}), \
                mock.patch.object(activity, "web_daily", return_value=web):
            res = activity.run_one(self.acc(), cfg)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "web+desktop")

    def test_desktop_only_mode(self) -> None:
        cfg = activity.load_config()
        cfg["web"] = False
        with mock.patch.object(activity, "desktop_daily",
                               return_value={"ok": True, "status": 200,
                                             "detail": "ok"}):
            res = activity.run_one(self.acc(), cfg)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "desktop")


class DesktopDailyTest(ActivityBase):
    def test_200_is_ok(self) -> None:
        cfg = activity.load_config()
        with mock.patch.object(activity.probe, "_post_json",
                               return_value=(200, {"choices": [{"delta": {}}]})) as post:
            res = activity.desktop_daily(self.acc(), cfg, "", "m1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["variant"], "hub")
        self.assertEqual(post.call_count, 1)

    def test_400_retries_official_body(self) -> None:
        cfg = activity.load_config()
        with mock.patch.object(activity.probe, "_post_json",
                               side_effect=[(400, {"code": 11128}),
                                            (200, {"choices": []})]):
            res = activity.desktop_daily(self.acc(), cfg, "", "m1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["variant"], "official")

    def test_403_restricted_is_failure(self) -> None:
        cfg = activity.load_config()
        body = {"code": 11140, "msg": "Your account has been restricted due to unusual activity"}
        with mock.patch.object(activity.probe, "_post_json", return_value=(403, body)):
            res = activity.desktop_daily(self.acc(), cfg, "", "m1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 403)


class WebDailyTest(ActivityBase):
    def test_create_failure_is_not_retried_on_business_error(self) -> None:
        cfg = activity.load_config()
        with mock.patch.object(activity, "conversation_create",
                               return_value=("", {"message": "code=1001 msg=bad",
                                                  "retryable": False, "limit": False})) as create:
            res = activity.web_daily(self.acc(), cfg, "", "m1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["phase"], "create")
        self.assertEqual(create.call_count, 1)   # 业务拒绝不再试

    def test_happy_path_releases_conversation(self) -> None:
        cfg = activity.load_config()
        sess = {"data": {"link": "https://x/y", "token": "t", "sessionId": "s",
                         "cwd": "/workspace"}}
        turn = {"ok": True, "status": "completed", "chunks": 2, "elapsed_ms": 500,
                "error": ""}
        with mock.patch.object(activity, "conversation_create",
                               return_value=("c1", None)), \
                mock.patch.object(activity, "conversation_get", return_value=sess), \
                mock.patch.object(activity, "run_turn", return_value=turn), \
                mock.patch.object(activity, "conversation_release") as release:
            res = activity.web_daily(self.acc(), cfg, "", "m1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["conversation"], "c1")
        release.assert_called_once()
        self.assertFalse(release.call_args.kwargs["delete"])

    def test_failed_turn_deletes_conversation(self) -> None:
        cfg = activity.load_config()
        cfg["rounds"] = 1
        sess = {"data": {"link": "https://x/y", "token": "t"}}
        turn = {"ok": False, "status": "working", "chunks": 0,
                "elapsed_ms": 100, "error": "连接被重置"}
        with mock.patch.object(activity, "conversation_create",
                               return_value=("c9", None)), \
                mock.patch.object(activity, "conversation_get", return_value=sess), \
                mock.patch.object(activity, "run_turn", return_value=turn), \
                mock.patch.object(activity, "conversation_release") as release:
            res = activity.web_daily(self.acc(), cfg, "", "m1")
        self.assertFalse(res["ok"])
        self.assertTrue(release.call_args.kwargs["delete"])

    def test_missing_sandbox_is_session_phase(self) -> None:
        cfg = activity.load_config()
        cfg["rounds"] = 1
        with mock.patch.object(activity, "conversation_create",
                               return_value=("c2", None)), \
                mock.patch.object(activity, "conversation_get",
                                  return_value={"data": {}}), \
                mock.patch.object(activity, "conversation_release"):
            res = activity.web_daily(self.acc(), cfg, "", "m1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["phase"], "session")
        self.assertIn("link/token", res["error"])


class SweepTest(ActivityBase):
    def test_disabled_does_nothing(self) -> None:
        activity.save_config({"enabled": False})
        self.assertEqual(activity.sweep_once()["why"], "disabled")

    def test_before_window(self) -> None:
        activity.save_config({"enabled": True, "daily_at": "23:59"})
        out = activity.sweep_once(now=__import__("time").time())
        self.assertIn(out["why"], ("before_window", "interval"))

    def test_runs_pending(self) -> None:
        activity.save_config({"enabled": True, "daily_at": "00:00",
                              "interval_minutes": 5})
        activity._SCHED["last"] = 0.0
        acc = self.acc()
        store.upsert([acc])
        pairs = [(1, acc, 17901)]
        with mock.patch.object(activity, "pending_pairs", return_value=pairs), \
                mock.patch.object(activity, "run_batch",
                                  return_value={"started": 1}) as run:
            out = activity.sweep_once()
        self.assertTrue(out["ran"])
        self.assertEqual(run.call_args.kwargs["trigger"], "auto")

    def test_nothing_pending_marks_scanned(self) -> None:
        activity.save_config({"enabled": True, "daily_at": "00:00"})
        activity._SCHED["last"] = 0.0
        with mock.patch.object(activity, "pending_pairs", return_value=[]):
            out = activity.sweep_once()
        self.assertFalse(out["ran"])
        self.assertEqual(out["why"], "nothing_pending")


class PairFilterTest(ActivityBase):
    """哪些号不该被跑：账户级开关关掉的、风控账号。

    这两条都是「不参与」而非「失败」——不产生记录，也不占并发额度。
    """

    def _seed(self) -> list[int]:
        store.upsert([self.acc("u-a"), self.acc("u-b"), self.acc("u-c")])
        return [int(r["id"]) for r in store.list_accounts(limit=10)]

    def test_is_risk_only_counts_banned(self) -> None:
        self.assertTrue(activity.is_risk({"recheck_state": "banned"}))
        # 已解封的号要能重新参与：risk_src 只是来源标记，不能当判据
        self.assertFalse(activity.is_risk({"recheck_state": "revived",
                                          "risk_src": "recheck"}))
        self.assertFalse(activity.is_risk({}))

    def test_pairs_skips_banned_and_disabled(self) -> None:
        ids = self._seed()
        store.set_track([ids[0]], "banned", "test")
        store.set_daily_active([ids[1]], False)
        got = [row_id for row_id, _acc, _port in activity.pending_pairs()]
        self.assertEqual(got, [ids[2]])

    def test_all_pairs_still_skips_them(self) -> None:
        """「全部重跑」也不能把风控号和关掉的号拉进来。"""
        ids = self._seed()
        store.set_track([ids[0]], "banned", "test")
        store.set_daily_active([ids[1]], False)
        got = [row_id for row_id, _acc, _port in activity.all_pairs()]
        self.assertEqual(got, [ids[2]])

    def test_run_batch_skips_risk_even_when_selected(self) -> None:
        """勾选跑走的是 load_for_export，绕过了 _pairs —— run_batch 必须再挡一次。"""
        ids = self._seed()
        store.set_track([ids[0]], "banned", "test")
        rows = store.load_for_export(ids=ids, with_ids=True)
        with mock.patch.object(activity, "_run_and_record",
                               return_value={"ok": True}) as run:
            activity.run_batch(rows, trigger="manual", background=False)
        ran = [c.args[0] for c in run.call_args_list]
        self.assertNotIn(ids[0], ran)
        self.assertEqual(len(ran), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
