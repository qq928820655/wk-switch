"""自重启：状态机、端口判据、回滚候选、会话续命。

这几条都是「曾经坏过」的地方，所以逐条钉住：
- 旧进程没退出就开始拉新进程 → 端口漂移；
- 探活只判端口通 → 把旧进程当新进程，页面刷新撞上退出；
- 回滚挑错备份 → 退回到坏代码，原地打转；
- 会话只在内存 → 升级重启后掉登录，看起来像升级把会话弄坏了。
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wk import auth, relaunch, update, version  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "restart.json")
        self.patches = [
            mock.patch.object(relaunch, "STATE_PATH", self.state),
            mock.patch.object(relaunch, "DATA_DIR", self.tmp),
            mock.patch.object(relaunch, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(relaunch, "LOG_PATH", os.path.join(self.tmp, "logs", "r.log")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_write_read_roundtrip(self):
        relaunch._write({"state": "preparing", "port": 8765})
        self.assertEqual(relaunch._read()["port"], 8765)

    def test_patch_keeps_other_keys(self):
        """_patch 只该改传进来的键——端口、备份名被覆盖掉，重启就找不到路了。"""
        relaunch._write({"state": "preparing", "port": 8765, "backup": "a.zip"})
        relaunch._patch(state="waiting")
        data = relaunch._read()
        self.assertEqual(data["state"], "waiting")
        self.assertEqual(data["port"], 8765)
        self.assertEqual(data["backup"], "a.zip")

    def test_log_appends_steps(self):
        relaunch._write({"state": "preparing", "port": 1, "steps": []})
        relaunch._log("第一步")
        relaunch._log("第二步")
        steps = relaunch._read()["steps"]
        self.assertEqual([s["text"] for s in steps], ["第一步", "第二步"])

    def test_mark_ready_from_intermediate_state(self):
        relaunch._write({"state": "verifying", "port": 8765,
                         "started": time.time(), "steps": []})
        relaunch.mark_ready(8765, "127.0.0.1")
        self.assertEqual(relaunch._read()["state"], "ok")

    def test_mark_ready_ignores_port_mismatch(self):
        """手动在别的端口起一个实例，不该把上一次重启的记录改成成功。"""
        relaunch._write({"state": "verifying", "port": 8765,
                         "started": time.time(), "steps": []})
        relaunch.mark_ready(9999, "127.0.0.1")
        self.assertEqual(relaunch._read()["state"], "verifying")

    def test_mark_ready_ignores_stale(self):
        relaunch._write({"state": "waiting", "port": 8765,
                         "started": time.time() - 3600, "steps": []})
        relaunch.mark_ready(8765, "127.0.0.1")
        self.assertEqual(relaunch._read()["state"], "waiting")

    def test_mark_ready_ignores_finished_state(self):
        relaunch._write({"state": "ok", "port": 8765, "started": time.time()})
        relaunch.mark_ready(8765, "127.0.0.1")
        self.assertNotIn("ready_pid", relaunch._read())


class PortTests(unittest.TestCase):
    def test_bindable_on_free_port(self):
        self.assertTrue(relaunch._bindable(_free_port(), "127.0.0.1"))

    def test_not_bindable_while_listening(self):
        port = _free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            self.assertFalse(relaunch._bindable(port, "127.0.0.1"))
        finally:
            srv.close()

    def test_wait_port_times_out(self):
        port = _free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            self.assertFalse(relaunch._wait_port(port, "127.0.0.1", 0.6))
        finally:
            srv.close()

    def test_detach_flags(self):
        if os.name != "nt":
            self.assertEqual(relaunch._detach_flags(), 0)
        else:
            # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP。
            # 旧实现用的是 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，
            # 那个组合在 Windows 上会让子进程直接起不来。
            flags = relaunch._detach_flags()
            self.assertEqual(flags, 0x08000000 | 0x00000200)
            self.assertFalse(flags & 0x00000008)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(update, "BACKUP_DIR", self.tmp)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _mk(self, name: str, mtime: float):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"PK")
        os.utime(path, (mtime, mtime))
        return path

    def test_picks_closest_lower_version(self):
        now = time.time()
        self._mk("code-1.0.0-20260101-000000.zip", now - 3000)
        self._mk("code-1.3.0-20260102-000000.zip", now - 100)
        got = relaunch._latest_backup("1.4.0")
        self.assertTrue(got.endswith("code-1.3.0-20260102-000000.zip"))

    def test_excludes_current_and_newer(self):
        """失败的新版本也会留一份备份，它绝不能成为下一次的回滚目标。"""
        now = time.time()
        self._mk("code-1.0.0-20260101-000000.zip", now - 100)
        self._mk("code-9.9.9-20260102-000000.zip", now)
        got = relaunch._latest_backup("1.4.0")
        self.assertTrue(got.endswith("code-1.0.0-20260101-000000.zip"))

    def test_prefer_version_wins(self):
        now = time.time()
        self._mk("code-1.0.0-20260101-000000.zip", now - 100)
        self._mk("code-1.3.0-20260102-000000.zip", now - 50)
        got = relaunch._latest_backup("1.4.0", prefer_version="1.0.0")
        self.assertTrue(got.endswith("code-1.0.0-20260101-000000.zip"))

    def test_empty_when_nothing_older(self):
        self._mk("code-1.4.0-20260101-000000.zip", time.time())
        self.assertEqual(relaunch._latest_backup("1.4.0"), "")


class SpawnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "restart.json")
        self.patches = [
            mock.patch.object(relaunch, "STATE_PATH", self.state),
            mock.patch.object(relaunch, "DATA_DIR", self.tmp),
            mock.patch.object(relaunch, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(relaunch, "LOG_PATH", os.path.join(self.tmp, "logs", "r.log")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_spawn_records_target_and_returns(self):
        with mock.patch.object(relaunch.subprocess, "Popen") as popen:
            out = relaunch.spawn(8765, "0.0.0.0", target_version="1.4.0",
                                 backup="code-1.3.0-x.zip", from_version="1.3.0")
        self.assertTrue(out["ok"])
        self.assertTrue(popen.called)
        args = popen.call_args[0][0]
        self.assertIn("--supervise", args)
        data = relaunch._read()
        self.assertEqual(data["port"], 8765)
        self.assertEqual(data["host"], "0.0.0.0")
        self.assertEqual(data["to"], "1.4.0")
        self.assertEqual(data["backup"], "code-1.3.0-x.zip")

    def test_spawn_reports_launch_failure(self):
        with mock.patch.object(relaunch.subprocess, "Popen", side_effect=OSError("nope")):
            out = relaunch.spawn(8765)
        self.assertFalse(out["ok"])
        self.assertEqual(relaunch._read()["state"], "failed")


class SuperviseTests(unittest.TestCase):
    """整条链路的顺序与失败分支。全部 mock 掉外部动作。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "restart.json")
        self.patches = [
            mock.patch.object(relaunch, "STATE_PATH", self.state),
            mock.patch.object(relaunch, "DATA_DIR", self.tmp),
            mock.patch.object(relaunch, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(relaunch, "LOG_PATH", os.path.join(self.tmp, "logs", "r.log")),
        ]
        for p in self.patches:
            p.start()
        relaunch._write({"state": "preparing", "port": 8765, "host": "127.0.0.1",
                         "pid": 1, "to": "1.4.0", "from": "1.3.0",
                         "backup": "code-1.3.0-x.zip", "started": time.time(), "steps": []})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_happy_path(self):
        with mock.patch.object(relaunch, "_wait_port", return_value=True), \
             mock.patch.object(relaunch, "_launch", return_value=(None, "main.py")), \
             mock.patch.object(relaunch, "_wait_alive",
                               return_value=(True, {"ok": True, "version": "1.4.0", "pid": 2})):
            self.assertEqual(relaunch.supervise(), 0)
        data = relaunch._read()
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["ready_version"], "1.4.0")

    def test_port_never_frees(self):
        with mock.patch.object(relaunch, "_wait_port", return_value=False), \
             mock.patch.object(relaunch, "_terminate") as term:
            self.assertEqual(relaunch.supervise(), 1)
        self.assertTrue(term.called)
        self.assertEqual(relaunch._read()["state"], "failed")

    def test_rollback_when_new_build_dies(self):
        with mock.patch.object(relaunch, "_wait_port", return_value=True), \
             mock.patch.object(relaunch, "_launch", return_value=(None, "main.py")), \
             mock.patch.object(relaunch, "_wait_alive",
                               side_effect=[(False, {}), (True, {"ok": True, "version": "1.3.0"})]), \
             mock.patch.object(relaunch, "_rollback", return_value=True) as rb, \
             mock.patch.object(relaunch, "_mark_broken") as broken:
            self.assertEqual(relaunch.supervise(), 0)
        self.assertTrue(rb.called)
        self.assertTrue(broken.called)
        data = relaunch._read()
        self.assertEqual(data["state"], "ok")
        self.assertTrue(data.get("rolled_back"))

    def test_failed_when_nothing_to_roll_back_to(self):
        with mock.patch.object(relaunch, "_wait_port", return_value=True), \
             mock.patch.object(relaunch, "_launch", return_value=(None, "main.py")), \
             mock.patch.object(relaunch, "_wait_alive", return_value=(False, {})), \
             mock.patch.object(relaunch, "_rollback", return_value=False):
            self.assertEqual(relaunch.supervise(), 1)
        self.assertEqual(relaunch._read()["state"], "failed")


class SessionPersistTests(unittest.TestCase):
    """重启后会话仍在。升级完被踢回登录页，是这个功能最直观的「坏了」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sessions.json")
        self.patches = [mock.patch.object(auth, "SESSIONS_PATH", self.path)]
        for p in self.patches:
            p.start()
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False

    def tearDown(self):
        self.patches[0].stop()
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False

    def test_session_survives_process_restart(self):
        sid = auth.issue_session()
        self.assertTrue(auth.check_session(sid))
        # 模拟重启：内存清空，只剩磁盘
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False
        self.assertTrue(auth.check_session(sid))

    def test_revoke_persists(self):
        sid = auth.issue_session()
        auth.revoke(sid)
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False
        self.assertFalse(auth.check_session(sid))

    def test_expired_session_not_restored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"oldsid": 1}')
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False
        self.assertFalse(auth.check_session("oldsid"))

    def test_broken_file_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        with auth._lock:
            auth._sessions.clear()
            auth._loaded = False
        self.assertFalse(auth.check_session("whatever"))
        self.assertTrue(auth.issue_session())


class VersionPinTests(unittest.TestCase):
    def test_version_file_matches_constant(self):
        """VERSION 文件和常量写着一模一样的号，打包脚本才不会打错版本名。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), version.VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
