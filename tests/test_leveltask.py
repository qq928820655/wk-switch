"""国际版等级任务的单测：任务解析、三源合并、动作链路、领奖回读、开关与落库。

跑法：python -m unittest tests.test_leveltask -v

不打真实网络：`leveltask._http` 与 `leveltask._stream_chat` 全部打桩。
真实链路（网页会话 + /v2/report）在交付说明里单列，这里只锁行为：
没回读到 completed 就不领奖，微信那条永远不伪造。
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

from wk import leveltask, store  # noqa: E402
from wk.model import Account  # noqa: E402


class FakeNet:
    """一张按路径分发的假上游。状态可变，用来验证「动作 → 回读 → 领奖」。"""

    def __init__(self) -> None:
        #: 任务行。`_src` 表示这一行只从哪个口径下发（level/desktop/mp）；
        #: 同一个任务码可以在多个口径各有一行（合并逻辑就靠这个测）。
        self.rows: list[dict] = []
        self.calls: list[tuple[str, str, object]] = []
        self.reports: list[list[dict]] = []
        self.claims: list[str] = []
        self.accepts: list[str] = []
        self.settles = True          # 领奖后回读是否变成 claimed
        self.restricted = False      # /v2/report 回 11140
        self.after_report = "completed"
        self.installed = False
        self.stream_ok = True
        #: 每次 stream 收到的 body，用来断言 messages 结构（首条必须 system）
        self.bodies: list[dict] = []
        #: True 时上游按「首条不是 system」拒绝，回 400/11128
        self.stream_bad_request = False
        #: True 时任务列表三个口径全回 401（凭据失效）
        self.tasks_denied = False

    # -- 分派 ------------------------------------------------------
    def http(self, acc, proxy, method, path, body=None, timeout=25.0, headers=None):
        self.calls.append((method, path, body))
        if path in ("/activity/growth/tasks", "/v2/activity/growth/tasks"):
            return self._tasks(path, headers or {})
        if path == leveltask.ACCEPT_PATH:
            code = ((body or {}).get("task_codes") or [""])[0]
            self.accepts.append(code)
            for row in self._find(code):
                row["accept_status"] = "accepted"
            return self._ok({"results": [{"task_code": code, "status": "accepted"}]})
        if path.startswith("/activity/growth/tasks/") and path.endswith("/claim"):
            code = path.split("/")[-2]
            self.claims.append(code)
            if self.settles:
                for row in self._find(code):
                    row["accept_status"] = "claimed"
                    row["claimed"] = True
            return self._ok({"credit": 100} if self.settles else {})
        if path == leveltask.REPORT_PATH:
            self.reports.append(body if isinstance(body, list) else [])
            if self.restricted:
                return {"status": 403, "payload": {"code": 11140, "msg": "request illegal"},
                        "text": ""}
            if self.after_report and isinstance(body, list):
                for row in self.rows:
                    if row.get("task_code") in leveltask.CLIENT_ONLY:
                        continue
                    if str(row.get("accept_status") or "") in ("accepted", ""):
                        row["accept_status"] = self.after_report
            return self._ok({})
        if path == leveltask.WEB_CONVERSATIONS_PATH:
            return self._ok({"conversationId": "c-1"})
        if path.startswith(leveltask.SCENES_PATH.split("?")[0]):
            return self._ok({"scenes": [{"id": "s-1", "name": "Blog"}]})
        if path == leveltask.EXPERT_LIST_PATH:
            return self._ok({"experts": [{"expert_id": "ex_1", "display_name_zh": "阿岩",
                                          "profession_zh": "工程师", "version": "1.0.0"}]})
        if path == leveltask.SKILL_LIST_PATH:
            return self._ok({"skills": [{"skill_id": "sk_1", "name": "Writer",
                                         "slug": "writer"}]})
        if path == leveltask.PLUGINS_INSTALLED_PATH:
            rows = [{"plugin_name": "writer"}] if self.installed else []
            return self._ok({"plugins": rows})
        if path == leveltask.MARKET_SOURCES_PATH:
            return self._ok({"sources": [{"id": "mkt-1"}]})
        if path == leveltask.PLUGINS_INSTALL_PATH:
            self.installed = True
            return self._ok({})
        return self._ok({})

    def stream(self, acc, proxy, body, timeout=90.0):
        self.bodies.append(body)
        if self.stream_bad_request:
            return {"status": 400, "finished": False, "text": "", "message_id": "",
                    "code": leveltask.CODE_BAD_REQUEST,
                    "error": 'HTTP 400 {"code":11128,"msg":"first message is not system prompt"}'}
        if not self.stream_ok:
            return {"status": 500, "finished": False, "text": "", "message_id": "",
                    "error": "HTTP 500"}
        return {"status": 200, "finished": True, "text": "OK", "message_id": "m-1",
                "error": ""}

    # -- 工具 ------------------------------------------------------
    def _ok(self, data):
        return {"status": 200, "payload": {"code": 0, "data": data}, "text": ""}

    def _find(self, code):
        return [row for row in self.rows if row.get("task_code") == code]

    def add(self, code, status="accepted", source="level", **extra):
        row = {"task_code": code, "accept_status": status, "_src": source}
        if status == "claimed":
            row["claimed"] = True
        row.update(extra)
        self.rows.append(row)
        return row

    def _tasks(self, path, headers):
        if self.tasks_denied:
            return {"status": 401, "payload": {},
                    "text": "<html><center>401</center></html>"}
        if path == "/activity/growth/tasks":
            source = "level"
        elif headers.get("X-Client-Platform") == "miniprogram":
            source = "mp"
        else:
            source = "desktop"
        rows = [{k: v for k, v in row.items() if k != "_src"}
                for row in self.rows if (row.get("_src") or "level") == source]
        return self._ok({"tasks": rows})


class LevelBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

        self._old_db = (store.DATA_DIR, store.DB_PATH)
        store.DATA_DIR = self.tmp.name
        store.DB_PATH = os.path.join(self.tmp.name, "accounts.db")

        self._old_cfg = leveltask.CONFIG_PATH
        cfgdir = os.path.join(self.tmp.name, "config")
        os.makedirs(cfgdir, exist_ok=True)
        leveltask.CONFIG_PATH = os.path.join(cfgdir, "leveltask.json")

        self.net = FakeNet()
        self._patch = mock.patch.multiple(
            leveltask, _http=self.net.http, _stream_chat=self.net.stream)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _restore(self) -> None:
        store.DATA_DIR, store.DB_PATH = self._old_db
        leveltask.CONFIG_PATH = self._old_cfg

    def acc(self, uid: str = "u-1", realm: str = "intl", token: str = "at-1") -> Account:
        a = Account()
        a.uid = uid
        a.nickname = "阿岩"
        a.realm = realm
        a.access_token = token
        return a

    def cfg(self, **patch) -> dict:
        cfg = dict(leveltask.DEFAULTS)
        cfg.update(patch)
        return leveltask._clean(cfg)


class TaskParseTests(unittest.TestCase):
    def test_status_and_claim(self) -> None:
        self.assertTrue(leveltask.is_claimed({"claim": "x", "claimed": True}))
        self.assertTrue(leveltask.is_claimed({"accept_status": "claimed"}))
        self.assertFalse(leveltask.is_claimed({"accept_status": "completed"}))
        self.assertTrue(leveltask.is_ready({"accept_status": "completed"}))
        self.assertTrue(leveltask.is_ready(
            {"accept_status": "accepted", "progress": {"current": 1, "target": 1}}))
        self.assertFalse(leveltask.is_ready(
            {"accept_status": "accepted", "progress": {"current": 0, "target": 3}}))
        self.assertFalse(leveltask.is_ready({"accept_status": "completed", "locked": True}))

    def test_progress_text(self) -> None:
        row = {"accept_status": "accepted", "progress": {"current": 2.0, "target": 5.0}}
        self.assertEqual(leveltask.progress_text(row), "accepted 2/5")
        self.assertEqual(leveltask.progress_text({"accept_status": "completed"}), "completed")

    def test_auto_codes_exclude_client_only(self) -> None:
        self.assertNotIn("wechat_linked", leveltask.AUTO_CODES)
        self.assertEqual(len(leveltask.AUTO_CODES), 4)


class FetchTests(LevelBase):
    def test_merges_three_sources_and_prefers_claimed(self) -> None:
        self.net.add("first_chat", "accepted", "level")
        self.net.add("first_chat", "claimed", "desktop")
        self.net.add("template_used", "completed", "desktop")
        rows, errors = leveltask.fetch_tasks(self.acc(), "")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(leveltask.task_status(rows["first_chat"]), "claimed")
        self.assertEqual(rows["first_chat"]["_source"], "desktop")
        self.assertEqual(rows["template_used"]["_source"], "desktop")

    def test_unknown_codes_are_ignored(self) -> None:
        self.net.add("checkin", "available")
        self.net.add("first_chat", "accepted")
        rows, _ = leveltask.fetch_tasks(self.acc(), "")
        self.assertEqual(list(rows), ["first_chat"])

    def test_restricted_is_reported_not_raised(self) -> None:
        self.net.add("first_chat", "accepted")
        denied = lambda *a, **k: {"status": 403, "payload": {"code": 11140}, "text": ""}
        with mock.patch.object(leveltask, "_http", side_effect=denied):
            rows, errors = leveltask.fetch_tasks(self.acc(), "")
        self.assertEqual(rows, {})
        self.assertTrue(any("账号受限" in e for e in errors))


class RunOneTests(LevelBase):
    def test_first_chat_action_report_and_claim(self) -> None:
        self.net.add("first_chat", "accepted",
                     progress={"current": 0, "target": 1})
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertTrue(res["ok"], res["detail"])
        self.assertEqual(self.net.claims, ["first_chat"])
        # 上报里必须带 chat_request_send 且带 userId（缺了会被静默丢弃）
        self.assertTrue(self.net.reports)
        codes = [e.get("eventCode") for e in self.net.reports[0]]
        self.assertIn("chat_request_send", codes)
        self.assertTrue(all(e.get("userId") == "u-1" for e in self.net.reports[0]))
        snap = {t["code"]: t for t in res["tasks"]}
        self.assertTrue(snap["first_chat"]["claimed"])

    def test_wechat_row_from_server_is_ignored(self) -> None:
        """服务端还会下发 wechat_linked，但这一项不在 CODES 里，整条链路不碰它。"""
        self.net.add("wechat_linked", "completed")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertEqual(self.net.claims, [])
        self.assertEqual(self.net.reports, [])
        snap = {t["code"]: t for t in res["tasks"]}
        self.assertNotIn("wechat_linked", snap)
        self.assertEqual(len(snap), 4)

    def test_already_claimed_rows_are_skipped(self) -> None:
        self.net.add("first_chat", "claimed")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertTrue(res["ok"])
        self.assertEqual(self.net.claims, [])
        self.assertEqual(res["done"], 1)

    def test_claim_not_confirmed_by_readback_is_failure(self) -> None:
        self.net.add("first_chat", "completed")
        self.net.settles = False
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertFalse(res["ok"])
        self.assertIn("领奖未确认", res["detail"])

    def test_restricted_account_stops_after_first_failure(self) -> None:
        self.net.add("first_chat", "accepted")
        self.net.add("template_used", "accepted")
        self.net.restricted = True
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertFalse(res["ok"])
        self.assertIn("账号受限", res["detail"])
        self.assertEqual(self.net.claims, [])
        snap = {t["code"]: t for t in res["tasks"]}
        self.assertIn("账号受限", snap["template_used"]["note"])

    def test_desktop_source_accepts_before_action(self) -> None:
        self.net.add("first_chat", "not_accepted", "desktop")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertIn("first_chat", self.net.accepts)
        self.assertTrue(res["ok"], res["detail"])

    def test_mp_only_row_is_not_executed(self) -> None:
        self.net.add("expert_summoned", "available", "mp")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertEqual(self.net.reports, [])

    def test_locked_row_is_skipped(self) -> None:
        self.net.add("template_used", "available", locked=True,
                     valid_start="2026-12-01T00:00:00")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertEqual(self.net.claims, [])
        snap = {t["code"]: t for t in res["tasks"]}
        self.assertIn("未解锁", snap["template_used"]["note"])

    def test_skill_task_installs_before_chat(self) -> None:
        self.net.add("skill_installed", "accepted")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg(install_skill=True))
        self.assertTrue(res["ok"], res["detail"])
        self.assertTrue(self.net.installed)
        paths = [p for _m, p, _b in self.net.calls]
        self.assertIn(leveltask.PLUGINS_INSTALL_PATH, paths)

    def test_skill_install_can_be_disabled(self) -> None:
        self.net.add("skill_installed", "accepted")
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg(install_skill=False))
        self.assertFalse(res["ok"])
        self.assertIn("关闭了技能安装", res["detail"])
        self.assertFalse(self.net.installed)

    def test_skips_domestic_and_tokenless(self) -> None:
        res = leveltask.run_one(1, self.acc(realm="cn"), cfg=self.cfg())
        self.assertTrue(res["skipped"])
        res = leveltask.run_one(1, self.acc(token=""), cfg=self.cfg())
        self.assertTrue(res["skipped"])
        env = self.acc()
        env.access_token = json.dumps({"$wbEncrypted": 1, "envelope": "xx"})
        res = leveltask.run_one(1, env, cfg=self.cfg())
        self.assertTrue(res["skipped"])

    def test_empty_task_list_is_not_success(self) -> None:
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertFalse(res["ok"])
        self.assertIn("任务列表为空", res["detail"])
        self.assertEqual(len(res["tasks"]), len(leveltask.CODES))


class ListFailTests(LevelBase):
    """列表读不到时，格子要写清是凭据问题，不能一律写「未下发」。"""

    def test_401_marks_credentials_not_missing(self) -> None:
        self.net.tasks_denied = True
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        notes = [t["note"] for t in res["tasks"]]
        self.assertEqual(len(notes), len(leveltask.CODES))
        self.assertTrue(all("401" in n for n in notes), notes)
        self.assertNotIn("未下发", notes)

    def test_empty_list_still_says_not_issued(self) -> None:
        res = leveltask.run_one(1, self.acc(), cfg=self.cfg())
        self.assertEqual({t["note"] for t in res["tasks"]}, {"未下发"})

    def test_wechat_task_is_gone(self) -> None:
        self.assertNotIn("wechat_linked", leveltask.CODES)
        self.assertEqual(len(leveltask.CODES), 4)


class ConversationTests(LevelBase):
    """网页对话的请求体：首条必须是 system（上游 11128 拦的就是这一条）。"""

    def test_first_message_is_system_prompt(self) -> None:
        res = leveltask.web_conversation(self.acc(), "", "hi", self.cfg())
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.net.bodies), 1)
        msgs = self.net.bodies[0]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], leveltask.SYSTEM_PROMPT)
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "hi")

    def test_bad_request_stops_after_first_model(self) -> None:
        """11128 跟模型无关：不能拿同一句错误把五个模型全试一遍。"""
        self.net.stream_bad_request = True
        cfg = self.cfg(models="a,b,c,d,e")
        res = leveltask.web_conversation(self.acc(), "", "hi", cfg)
        self.assertFalse(res["ok"])
        self.assertEqual(len(self.net.bodies), 1)
        self.assertIn("system prompt", res["note"])
        self.assertIn(str(leveltask.CODE_BAD_REQUEST), res["note"])

    def test_body_code_reads_non_2xx_payload(self) -> None:
        self.assertEqual(leveltask._body_code('{"code":11128,"msg":"x"}'), 11128)
        self.assertIsNone(leveltask._body_code("<html>500</html>"))
        self.assertIsNone(leveltask._body_code(""))


class StoreTests(LevelBase):
    def _seed(self, uid="u-1", realm="intl", payload_patch=None):
        a = self.acc(uid=uid, realm=realm)
        if payload_patch:
            a.level_tasks_enabled = payload_patch.get("level_tasks_enabled", True)
        store.upsert([a])
        row = store.list_accounts(realm=realm)[0]
        return int(row["id"])

    def _task(self, code, claimed=False, status="accepted", note=""):
        return {"code": code, "label": leveltask.LABELS[code], "status": status,
                "claimed": claimed, "locked": False, "progress": "", "current": None,
                "target": None, "source": "level", "note": note, "at": 0}

    def test_snapshot_is_replaced_not_merged(self) -> None:
        row_id = self._seed()
        store.level_tasks_save(row_id, [self._task("first_chat")])
        store.level_tasks_save(row_id, [self._task("template_used")])
        snaps = store.level_tasks_map()[row_id]
        self.assertEqual(set(snaps), {"template_used"})

    def test_level_run_rows_and_summary(self) -> None:
        row_id = self._seed()
        store.level_run_add({"row_id": row_id, "uid": "u-1", "name": "阿岩",
                             "ok": True, "claimed": 2, "detail": "ok"})
        rows = store.level_run_rows("today")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(store.level_summary()["today_claimed"], 2)

    def test_toggle_is_separate_from_daily_active(self) -> None:
        row_id = self._seed()
        store.set_daily_active([row_id], False)
        row = store.list_accounts(realm="intl")[0]
        self.assertFalse(row["daily_active"])
        self.assertTrue(row["level_tasks"])
        store.set_level_tasks([row_id], False)
        row = store.list_accounts(realm="intl")[0]
        self.assertFalse(row["daily_active"])
        self.assertFalse(row["level_tasks"])
        # 导出给 hub 的账户快照里也要关上，不然换台机器又跑起来
        dumped = store.dump_records([row_id])[0]["account"]
        dumped = json.loads(dumped) if isinstance(dumped, str) else dumped
        self.assertFalse(dumped.get("level_tasks_enabled", True))

    def test_domestic_rows_are_not_toggled(self) -> None:
        row_id = self._seed(uid="cn-1", realm="cn")
        self.assertEqual(store.set_level_tasks([row_id], False)["changed"], 0)

    def test_pairs_skip_done_risk_and_disabled(self) -> None:
        done = self._seed(uid="u-done")
        todo = self._seed(uid="u-todo")
        risk = self._seed(uid="u-risk")
        store.level_tasks_save(done, [self._task(c, claimed=True, status="claimed")
                                      for c in leveltask.AUTO_CODES])
        with store.connect() as conn:
            conn.execute("UPDATE accounts SET recheck_state = 'banned' WHERE id = ?", (risk,))
        ids = [r[0] for r in leveltask.pairs(only_pending=True)]
        self.assertIn(todo, ids)
        self.assertNotIn(done, ids)
        self.assertNotIn(risk, ids)

    def test_absent_tasks_do_not_block_completion(self) -> None:
        """服务端没下发的任务不该让号永远被捞出来重跑。"""
        row_id = self._seed()
        store.level_tasks_save(row_id, [
            self._task("first_chat", claimed=True, status="claimed"),
            self._task("skill_installed", status=""),
            self._task("expert_summoned", status=""),
            self._task("template_used", status=""),
            self._task("wechat_linked", status="completed"),
        ])
        self.assertEqual(leveltask.pairs(only_pending=True), [])

    def test_pairs_respects_account_switch(self) -> None:
        row_id = self._seed()
        store.set_level_tasks([row_id], False)
        self.assertEqual(leveltask.pairs(only_pending=True), [])


class ConfigTests(LevelBase):
    def test_save_clamps_and_round_trips(self) -> None:
        cfg = leveltask.save_config({"concurrency": 99, "interval_minutes": 1,
                                     "daily_at": "07:30", "unknown": 1})
        self.assertEqual(cfg["concurrency"], 6)
        self.assertEqual(cfg["interval_minutes"], 15)
        self.assertEqual(cfg["daily_at"], "07:30")
        self.assertNotIn("unknown", cfg)
        self.assertEqual(leveltask.load_config()["daily_at"], "07:30")

    def test_bad_file_falls_back_to_defaults(self) -> None:
        with open(leveltask.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(leveltask.load_config(), leveltask.DEFAULTS)


class WebLayoutTests(unittest.TestCase):
    """静态守门：新标签页、视图、脚本、样式都得在，别只做后端。"""

    @classmethod
    def setUpClass(cls) -> None:
        base = os.path.join(ROOT, "web")
        with open(os.path.join(base, "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(base, "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()
        with open(os.path.join(base, "style.css"), encoding="utf-8") as fh:
            cls.css = fh.read()

    def test_tab_and_view_exist(self) -> None:
        self.assertIn('data-view="level"', self.html)
        self.assertIn('id="view-level"', self.html)
        self.assertIn('id="lv-accounts"', self.html)

    def test_view_switching_and_loading(self) -> None:
        self.assertIn("view !== 'level'", self.js)
        self.assertIn("loadLevel()", self.js)
        self.assertIn("bindLevel()", self.js)

    def test_status_chips_and_mobile_layout(self) -> None:
        for cls in (".lv-chip.ok", ".lv-chip.ready", ".lv-chip.client", ".lv-chip.absent"):
            self.assertIn(cls, self.css)
        self.assertIn("#lv-accounts tbody tr {", self.css)


if __name__ == "__main__":
    unittest.main()
