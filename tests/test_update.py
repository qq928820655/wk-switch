# -*- coding: utf-8 -*-
"""升级链路里不依赖网络的纯逻辑：端口选择、包内路径过滤、版本比较。

下载/替换/重启这些要真起进程的步骤由端到端脚本覆盖，不进单测——
单测里动真实代码目录风险太大。
"""

from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import find_port, port_free  # noqa: E402
from wk import update, version  # noqa: E402


class PortTests(unittest.TestCase):
    """升级重启时端口不能漂移——书签和反代都指着原端口。"""

    @staticmethod
    def _hold(port: int, seconds: float) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        threading.Thread(
            target=lambda: (time.sleep(seconds), sock.close()), daemon=True).start()
        return sock

    def test_port_free_sees_listening_socket(self):
        sock = self._hold(18765, 1.0)
        try:
            self.assertFalse(port_free(18765),
                             "被监听的端口必须判定为占用；Windows 下设了 "
                             "SO_REUSEADDR 会把已监听端口误判成空闲")
        finally:
            sock.close()

    def test_wait_mode_stays_on_requested_port(self):
        sock = self._hold(18766, 0.8)
        try:
            started = time.time()
            picked = find_port(18766, wait=6)
            waited = time.time() - started
            self.assertEqual(picked, 18766, "wait 模式下不该顺延到下一个端口")
            self.assertGreater(waited, 0.5, "应该真的等了一会儿，而不是立刻返回")
        finally:
            sock.close()

    def test_no_wait_falls_through(self):
        sock = self._hold(18767, 2.0)
        try:
            picked = find_port(18767, wait=0)
            self.assertEqual(picked, 18768)
        finally:
            sock.close()


class SafeMembersTests(unittest.TestCase):
    """升级包来自网络，路径必须过滤。"""

    @staticmethod
    def _zip(entries: dict) -> zipfile.ZipFile:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        buf.seek(0)
        return zipfile.ZipFile(buf)

    def test_flat_package(self):
        zf = self._zip({"server.py": "x", "wk/token.py": "y", "web/app.js": "z"})
        rels = sorted(rel for _arc, rel in update._safe_members(zf))
        self.assertEqual(rels, ["server.py", "web/app.js", "wk/token.py"])

    def test_strips_single_top_dir(self):
        zf = self._zip({"wk-switch-1.0.1/server.py": "x",
                        "wk-switch-1.0.1/wk/token.py": "y"})
        rels = sorted(rel for _arc, rel in update._safe_members(zf))
        self.assertEqual(rels, ["server.py", "wk/token.py"])

    def test_skips_outside_whitelist(self):
        zf = self._zip({"server.py": "x", "config/auth.json": "secret",
                        "data/accounts.db": "db", ".git/config": "g"})
        rels = [rel for _arc, rel in update._safe_members(zf)]
        self.assertEqual(rels, ["server.py"])

    def test_rejects_path_traversal(self):
        zf = self._zip({"server.py": "x", "../../evil.py": "bad", "/abs.py": "bad"})
        rels = [rel for _arc, rel in update._safe_members(zf)]
        self.assertEqual(rels, ["server.py"])

    def test_rejects_unrelated_zip(self):
        zf = self._zip({"photos/a.jpg": "x", "readme.txt": "y"})
        with self.assertRaises(ValueError):
            update._safe_members(zf)

    def test_rejects_empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        buf.seek(0)
        with self.assertRaises(ValueError):
            update._safe_members(zipfile.ZipFile(buf))


class VersionCompareTests(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(version.parse_semver("1.2.3"), (1, 2, 3))
        self.assertEqual(version.parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(version.parse_semver("1.2.3-beta.1"), (1, 2, 3))
        self.assertEqual(version.parse_semver("2.0"), (2, 0, 0))
        self.assertEqual(version.parse_semver(""), (0, 0, 0))

    def test_newer(self):
        self.assertTrue(version.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(version.is_newer("1.1.0", "1.0.9"))
        self.assertFalse(version.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(version.is_newer("0.9.9", "1.0.0"))

    def test_version_file_wins(self):
        """VERSION 文件优先于源码常量，升级后不改代码就能反映新版本。"""
        self.assertTrue(callable(version.version))
        self.assertRegex(version.version(), r"^\d+\.\d+\.\d+$")


class ConfigTests(unittest.TestCase):

    def test_defaults_present(self):
        cfg = update.config()
        for key in ("repo", "api", "asset", "index_url", "proxy", "timeout"):
            self.assertIn(key, cfg, f"配置缺字段 {key}")

    def test_asset_pattern_only_matches_plain_package(self):
        pattern = update.DEFAULT_CONFIG["asset"]
        import re
        self.assertTrue(re.search(pattern, "wk-switch-1.0.0.zip"))
        self.assertFalse(re.search(pattern, "wk-switch-1.0.0-windows.zip"))
        self.assertFalse(re.search(pattern, "wk-switch-1.0.0-linux.tar.gz"))


if __name__ == "__main__":
    unittest.main()
