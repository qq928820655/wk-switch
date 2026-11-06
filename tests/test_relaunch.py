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
            self.assertEqual(relaunch._wait_port(port, "127.0.0.1", 0.6), "busy")
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
        with mock.patch.object(relaunch, "_wait_port", return_value="ok"), \
             mock.patch.object(relaunch, "_launch", return_value=(None, "main.py")), \
             mock.patch.object(relaunch, "_wait_alive",
                               return_value=(True, {"ok": True, "version": "1.4.0", "pid": 2})):
            self.assertEqual(relaunch.supervise(), 0)
        data = relaunch._read()
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["ready_version"], "1.4.0")

    def test_port_never_frees(self):
        with mock.patch.object(relaunch, "_wait_port", return_value="busy"), \
             mock.patch.object(relaunch, "_terminate") as term:
            self.assertEqual(relaunch.supervise(), 1)
        self.assertTrue(term.called)
        self.assertEqual(relaunch._read()["state"], "failed")

    def test_time_wait_does_not_abort_restart(self):
        """旧进程已死、端口只是 TIME_WAIT：必须继续拉起，不能报「旧进程没退出」。

        这就是 Linux 容器里那次事故：旧进程 0.5 秒前就 os._exit(0) 了，
        裸 socket 探测因为 TIME_WAIT 一直 EADDRINUSE，监督进程等满 45 秒
        后判失败，服务从此没人拉起。
        """
        with mock.patch.object(relaunch, "_wait_port", return_value="dead"), \
             mock.patch.object(relaunch, "_launch",
                               return_value=(None, "main.py")) as launch, \
             mock.patch.object(relaunch, "_terminate") as term, \
             mock.patch.object(relaunch, "_wait_alive",
                               return_value=(True, {"ok": True, "version": "1.4.0"})):
            self.assertEqual(relaunch.supervise(), 0)
        launch.assert_called_once()
        term.assert_not_called()
        self.assertEqual(relaunch._read()["state"], "ok")

    def test_rollback_when_new_build_dies(self):
        with mock.patch.object(relaunch, "_wait_port", return_value="ok"), \
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
        with mock.patch.object(relaunch, "_wait_port", return_value="ok"), \
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


class DeployModeTests(unittest.TestCase):
    """Linux 两种部署方式的重启路径完全不同，判错就是全盘失败。"""

    def setUp(self):
        relaunch.reset_deploy_cache()
        self.tmp = tempfile.mkdtemp()
        # Linux 的两条分支（systemd / nohup）在 Windows 上跑不到，用这个开关
        # 把平台判断掰成 posix，逻辑本身仍然是同一份。
        self.posix = mock.patch.object(relaunch, "_posix", return_value=True)
        self.posix.start()

    def tearDown(self):
        self.posix.stop()
        relaunch.reset_deploy_cache()

    def test_plain_when_nothing_detected(self):
        with mock.patch.object(relaunch, "ROOT", self.tmp), \
             mock.patch.object(relaunch, "START_SH", os.path.join(self.tmp, "start.sh")), \
             mock.patch.object(relaunch, "DEPLOY_LOG", os.path.join(self.tmp, "logs", "wk-switch.log")), \
             mock.patch.object(relaunch, "find_unit", return_value=""):
            dep = relaunch.deploy_mode()
        self.assertEqual(dep["mode"], "plain")

    def test_nohup_when_start_sh_present(self):
        """有 logs/ 和 start.sh 就是 nohup 部署，日志要写 logs/wk-switch.log。"""
        os.makedirs(os.path.join(self.tmp, "logs"))
        start = os.path.join(self.tmp, "start.sh")
        open(start, "w").close()
        with mock.patch.object(relaunch, "ROOT", self.tmp), \
             mock.patch.object(relaunch, "START_SH", start), \
             mock.patch.object(relaunch, "DEPLOY_LOG", os.path.join(self.tmp, "logs", "wk-switch.log")), \
             mock.patch.object(relaunch, "find_unit", return_value=""):
            dep = relaunch.deploy_mode()
        self.assertEqual(dep["mode"], "nohup")
        self.assertEqual(os.path.normpath(dep["log"]),
                         os.path.normpath(os.path.join(self.tmp, "logs", "wk-switch.log")))

    def test_systemd_wins_over_start_sh(self):
        """systemd 部署同样会生成 start.sh 当应急手柄，但重启必须走 systemctl。"""
        os.makedirs(os.path.join(self.tmp, "logs"))
        start = os.path.join(self.tmp, "start.sh")
        open(start, "w").close()
        with mock.patch.object(relaunch, "ROOT", self.tmp), \
             mock.patch.object(relaunch, "START_SH", start), \
             mock.patch.object(relaunch, "DEPLOY_LOG", os.path.join(self.tmp, "logs", "wk-switch.log")), \
             mock.patch.object(relaunch, "find_unit", return_value="wk-switch"), \
             mock.patch.object(relaunch, "systemd_active", return_value=True):
            dep = relaunch.deploy_mode()
        self.assertEqual(dep["mode"], "systemd")
        self.assertEqual(dep["unit"], "wk-switch")

    def test_unit_found_by_working_directory(self):
        """按 WorkingDirectory 认 unit，不写死服务名。"""
        unit_dir = os.path.join(self.tmp, "units")
        os.makedirs(unit_dir)
        with open(os.path.join(unit_dir, "other.service"), "w") as f:
            f.write("[Service]\nWorkingDirectory=/srv/other\nExecStart=/usr/bin/python3 /srv/other/main.py\n")
        with open(os.path.join(unit_dir, "mine.service"), "w") as f:
            f.write(f"[Service]\nWorkingDirectory={relaunch.ROOT}\nExecStart=/usr/bin/python3 x\n")
        with mock.patch.object(relaunch, "_UNIT_DIRS", (unit_dir,)):
            self.assertEqual(relaunch.find_unit(), "mine")

    def test_launch_systemd_calls_systemctl_not_popen(self):
        """systemd 模式下自己 Popen 会让 is-active 变 inactive，端口却在服务。"""
        state = {"port": 8765, "host": "127.0.0.1", "unit": "wk-switch"}
        done = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(relaunch, "_posix", return_value=True), \
             mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch",
                                             "log": "x"}), \
             mock.patch.object(relaunch.subprocess, "run", return_value=done) as run, \
             mock.patch.object(relaunch.subprocess, "Popen") as popen:
            proc, how = relaunch._launch(state)
        self.assertIsNone(proc)
        self.assertIn("systemctl", how)
        self.assertEqual(run.call_args[0][0][:3], ["systemctl", "--no-block", "restart"])
        self.assertEqual(run.call_args[0][0][3], "wk-switch")
        popen.assert_not_called()

    def test_launch_systemd_failure_raises(self):
        state = {"port": 8765, "host": "127.0.0.1", "unit": "wk-switch"}
        done = mock.Mock(returncode=1, stdout=b"", stderr=b"Unit not found.")
        with mock.patch.object(relaunch, "_posix", return_value=True), \
             mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch",
                                             "log": "x"}), \
             mock.patch.object(relaunch.subprocess, "run", return_value=done):
            with self.assertRaises(OSError) as ctx:
                relaunch._launch(state)
        self.assertIn("Unit not found", str(ctx.exception))


class WaitAliveTests(unittest.TestCase):
    """探活判据。原先「子进程一退就只再等 1 秒」在 Linux 上百分百误判。"""

    def test_wrapped_child_exit_does_not_fail_early(self):
        """包装进程已退出、真进程稍后才应答，不能立刻判死。

        这就是 Linux 上的真实时序：`nohup ... &` 的 bash 秒退，python 还在
        import。旧逻辑给 1 秒，必然判失败 → 每一轮升级都触发回滚。
        """
        port = _free_port()
        proc = mock.Mock()
        proc.poll.return_value = 0          # 包装进程已经退了

        calls = {"n": 0}

        def fake_ping(p, timeout=2.0):
            calls["n"] += 1
            # 前 4 次不通（约 1.6 秒），之后才通 —— 正好越过旧的 1 秒窗口
            if calls["n"] > 4:
                return {"ok": True, "version": "1.5.0"}
            return {}

        with mock.patch.object(relaunch, "_ping", side_effect=fake_ping):
            ok, info = relaunch._wait_alive(port, "1.5.0", timeout=15, proc=proc, grace=8.0)
        self.assertTrue(ok, "过了 1 秒窗口还在等，不应该判失败")
        self.assertEqual(info["version"], "1.5.0")

    def test_dead_child_with_wrong_version_fails(self):
        """进程退干净、端口通但版本不对，仍算失败（否则会把旧代码当新版）。"""
        port = _free_port()
        proc = mock.Mock()
        proc.poll.return_value = 1
        with mock.patch.object(relaunch, "_ping",
                               return_value={"ok": True, "version": "1.0.0"}):
            ok, _ = relaunch._wait_alive(port, "1.5.0", timeout=3, proc=proc, grace=0.5)
        self.assertFalse(ok)

    def test_no_proc_still_waits_full_timeout(self):
        """systemd 模式没有子进程可看，只能靠 ping，别提前返回。"""
        port = _free_port()
        with mock.patch.object(relaunch, "_ping", return_value={}):
            started = time.time()
            ok, _ = relaunch._wait_alive(port, "", timeout=1.5, proc=None)
        self.assertFalse(ok)
        self.assertGreaterEqual(time.time() - started, 1.4)


class SelfcheckTests(unittest.TestCase):
    def test_current_tree_passes(self):
        self.assertEqual(relaunch.selfcheck(), "")

    def test_syntax_error_is_reported(self):
        """临时把 server.py 换成一个语法错的版本，自检必须拦住。"""
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "wk"))
        with open(os.path.join(root, "server.py"), "w", encoding="utf-8") as f:
            f.write("def broken(:\n")
        with open(os.path.join(root, "wk", "__init__.py"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(root, "wk", "version.py"), "w", encoding="utf-8") as f:
            f.write("VERSION = '0.0.1'\ndef version():\n    return VERSION\n")
        with mock.patch.object(relaunch, "ROOT", root):
            problem = relaunch.selfcheck()
        self.assertTrue(problem)
        self.assertIn("SyntaxError", problem)


class WaitPortTests(unittest.TestCase):
    """三态返回值。判错就会把 TIME_WAIT 当「旧进程没退出」。"""

    def test_bindable_returns_ok(self):
        with mock.patch.object(relaunch, "_bindable", return_value=True):
            self.assertEqual(relaunch._wait_port(1, "127.0.0.1", 1), "ok")

    def test_dead_process_returns_dead_without_waiting_full_timeout(self):
        """旧进程没了就立刻返回 dead，别把 45 秒耗完。"""
        with mock.patch.object(relaunch, "_bindable", return_value=False), \
             mock.patch.object(relaunch, "_alive", return_value=False):
            started = time.time()
            got = relaunch._wait_port(1, "127.0.0.1", 30, dead_pid=4242)
        self.assertEqual(got, "dead")
        self.assertLess(time.time() - started, 2.0)

    def test_live_process_returns_busy(self):
        with mock.patch.object(relaunch, "_bindable", return_value=False), \
             mock.patch.object(relaunch, "_alive", return_value=True):
            got = relaunch._wait_port(1, "127.0.0.1", 0.6, dead_pid=4242)
        self.assertEqual(got, "busy")

    def test_no_pid_falls_back_to_timeout(self):
        """state 里没记 pid 时不能靠存活判断，只能等满。"""
        with mock.patch.object(relaunch, "_bindable", return_value=False):
            got = relaunch._wait_port(1, "127.0.0.1", 0.5, dead_pid=0)
        self.assertEqual(got, "busy")

    def test_bindable_sets_reuseaddr_on_posix(self):
        """POSIX 上探测必须和真服务的 allow_reuse_address 对齐。"""
        with mock.patch.object(relaunch, "_posix", return_value=True), \
             mock.patch.object(relaunch.socket, "socket") as factory:
            sock = factory.return_value.__enter__.return_value
            relaunch._bindable(12345, "127.0.0.1")
        sock.setsockopt.assert_called_once_with(
            relaunch.socket.SOL_SOCKET, relaunch.socket.SO_REUSEADDR, 1)

    def test_bindable_skips_reuseaddr_on_windows(self):
        """Windows 上设了会放过 LISTEN 端口，判据直接失效。"""
        with mock.patch.object(relaunch, "_posix", return_value=False), \
             mock.patch.object(relaunch.socket, "socket") as factory:
            sock = factory.return_value.__enter__.return_value
            relaunch._bindable(12345, "127.0.0.1")
        sock.setsockopt.assert_not_called()

    def test_alive_is_false_for_zero_and_self(self):
        self.assertFalse(relaunch._alive(0))
        self.assertFalse(relaunch._alive(-1))
        self.assertFalse(relaunch._alive(os.getpid()))


class TerminateTests(unittest.TestCase):
    def test_pids_on_port_returns_empty_for_zero(self):
        with mock.patch.object(relaunch, "_posix", return_value=True):
            self.assertEqual(relaunch._pids_on_port(0), [])

    def test_terminate_ignores_own_pid(self):
        """杀自己的 pid 会让监督进程在半路消失，回滚再也跑不到。"""
        with mock.patch.object(relaunch.subprocess, "run") as run:
            relaunch._terminate(os.getpid(), 0)
        run.assert_not_called()


class SpawnTests(unittest.TestCase):
    """监督进程怎么起，决定它能不能活过服务重启。"""

    def setUp(self):
        relaunch.reset_deploy_cache()
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(relaunch, "STATE_PATH", os.path.join(self.tmp, "restart.json")),
            mock.patch.object(relaunch, "DATA_DIR", self.tmp),
            mock.patch.object(relaunch, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(relaunch, "LOG_PATH", os.path.join(self.tmp, "logs", "r.log")),
            mock.patch.object(relaunch, "_posix", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        relaunch.reset_deploy_cache()

    def test_systemd_uses_systemd_run_for_independent_cgroup(self):
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch", "log": "x"}), \
             mock.patch.object(relaunch.shutil, "which", return_value="/bin/systemd-run"), \
             mock.patch.object(relaunch, "_spawn_systemd_run",
                               return_value={"ok": True, "via": "systemd-run",
                                             "unit": "wk-relaunch-1"}) as run, \
             mock.patch.object(relaunch.subprocess, "Popen") as popen:
            got = relaunch.spawn(8765, "127.0.0.1", target_version="1.5.2")
        self.assertTrue(got["ok"])
        self.assertEqual(got["via"], "systemd-run")
        run.assert_called_once()
        popen.assert_not_called()

    def test_systemd_without_systemd_run_degrades(self):
        """拿不到独立 cgroup 时不能起一个注定被杀的监督进程。"""
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch", "log": "x"}), \
             mock.patch.object(relaunch.shutil, "which", return_value=None), \
             mock.patch.object(relaunch.subprocess, "Popen") as popen:
            got = relaunch.spawn(8765, "127.0.0.1")
        self.assertTrue(got["ok"])
        self.assertTrue(got["degraded"])
        self.assertEqual(got["unit"], "wk-switch")
        popen.assert_not_called()

    def test_nohup_uses_plain_popen(self):
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "nohup", "unit": "", "log": "x"}), \
             mock.patch.object(relaunch.subprocess, "Popen") as popen:
            got = relaunch.spawn(8765, "127.0.0.1")
        self.assertEqual(got["via"], "popen")
        self.assertFalse(got.get("degraded"))
        popen.assert_called_once()

    def test_state_records_mode_and_unit(self):
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch",
                                             "log": "x"}), \
             mock.patch.object(relaunch.shutil, "which", return_value=None):
            relaunch.spawn(8765, "127.0.0.1")
        state = relaunch._read()
        self.assertEqual(state["mode"], "systemd")
        self.assertEqual(state["unit"], "wk-switch")
        self.assertEqual(state["port"], 8765)


class SuperviseSystemdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(relaunch, "STATE_PATH", os.path.join(self.tmp, "restart.json")),
            mock.patch.object(relaunch, "DATA_DIR", self.tmp),
            mock.patch.object(relaunch, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(relaunch, "LOG_PATH", os.path.join(self.tmp, "logs", "r.log")),
        ]
        for p in self.patches:
            p.start()
        relaunch._write({"port": 8765, "host": "127.0.0.1", "to": "1.5.2",
                         "pid": 999999, "started": time.time(), "steps": []})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_does_not_fight_systemd_restart_policy(self):
        """systemd 模式下不能自己等端口：会跟 Restart= 抢，死循环到 failed。"""
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "systemd", "unit": "wk-switch", "log": "x"}), \
             mock.patch.object(relaunch, "_wait_port") as wait, \
             mock.patch.object(relaunch, "_launch", return_value=(None, "systemctl restart wk-switch")), \
             mock.patch.object(relaunch, "_wait_alive",
                               return_value=(True, {"version": "1.5.2", "pid": 1})):
            code = relaunch.supervise()
        self.assertEqual(code, 0)
        wait.assert_not_called()

    def test_nohup_still_waits_for_port(self):
        with mock.patch.object(relaunch, "deploy_mode",
                               return_value={"mode": "nohup", "unit": "", "log": "x"}), \
             mock.patch.object(relaunch, "_wait_port", return_value="ok") as wait, \
             mock.patch.object(relaunch, "_launch", return_value=(mock.Mock(pid=1), "main.py")), \
             mock.patch.object(relaunch, "_wait_alive",
                               return_value=(True, {"version": "1.5.2", "pid": 1})):
            code = relaunch.supervise()
        self.assertEqual(code, 0)
        wait.assert_called_once()


class RevertTests(unittest.TestCase):
    def test_revert_marks_bad_backup_broken(self):
        """换回旧版时，_install_zip 会把坏代码也备份一份，得标掉。"""
        with mock.patch.object(relaunch, "_rollback", return_value=True) as roll, \
             mock.patch.object(relaunch, "_mark_broken") as mark:
            got = relaunch.revert("9.9.9", "code-1.5.1-x.zip", "1.5.1")
        self.assertTrue(got["ok"])
        mark.assert_called_once_with("9.9.9")
        self.assertEqual(roll.call_args[0][0]["to"], "9.9.9")


class VersionPinTests(unittest.TestCase):
    def test_version_file_matches_constant(self):
        """VERSION 文件和常量写着一模一样的号，打包脚本才不会打错版本名。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), version.VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
