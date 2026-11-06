#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包发布制品。

用法：
    python build_release.py                 # 版本取 wk/version.py
    python build_release.py --version 1.0.1 # 临时指定版本（不写回源码）
    python build_release.py --repo owner/name

产物（dist/）：
    wk-switch-<ver>.zip          升级包，在线升级下载的就是这个
    wk-switch-<ver>-linux.tar.gz 含 systemd 单元与安装脚本
    wk-switch-<ver>-windows.zip  含 bat 安装/启停脚本
    SHA256SUMS.txt               校验和

不打包：data/ config/ backups/ .git/ __pycache__/ .workbuddy/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")

#: 进包的顶层条目
INCLUDE = (
    "main.py", "server.py", "wk", "web", "deploy", "samples", "tests", "tools",
    "README.md", "requirements.txt", "VERSION", "BUILD", "LICENSE",
)
EXCLUDE_DIRS = {"__pycache__", ".git", ".workbuddy", "data", "config", "backups", "dist", ".idea"}
EXCLUDE_EXT = {".pyc", ".pyo", ".db", ".log"}


#: 北京时间。打包脚本是独立入口，不 import wk，所以这里自带一份偏移量。
_BJ = timezone(timedelta(hours=8))


def _bj_stamp() -> str:
    """构建时间戳，北京时间。和运行时 wk/clock.py 同一个时区。"""
    return datetime.now(_BJ).strftime("%Y-%m-%d %H:%M:%S")


def read_version() -> str:
    """VERSION 文件优先，回退到 wk/version.py 的常量。

    和 `wk.version.version()` 同一套优先级。只读常量的话，在线升级把新版本号
    写进 VERSION 之后，再打包出来的包名还停在常量上——包名和包内容对不上。
    """
    path = os.path.join(ROOT, "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    path = os.path.join(ROOT, "wk", "version.py")
    with open(path, "r", encoding="utf-8") as f:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not match:
        raise SystemExit("wk/version.py 里找不到 VERSION")
    return match.group(1)


def git_repo() -> str:
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=ROOT,
            stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    url = re.sub(r"\.git$", "", url)
    match = re.search(r"github\.com[/:]([^/]+/[^/]+)$", url)
    return match.group(1) if match else ""


def iter_files():
    for top in INCLUDE:
        path = os.path.join(ROOT, top)
        if os.path.isfile(path):
            yield path, top
        elif os.path.isdir(path):
            for base, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for name in files:
                    if os.path.splitext(name)[1].lower() in EXCLUDE_EXT:
                        continue
                    full = os.path.join(base, name)
                    yield full, os.path.relpath(full, ROOT).replace("\\", "/")


def patched_bytes(path: str, repo: str) -> bytes:
    """部署脚本里的 __REPO__ 占位换成真实仓库名，并按扩展名归一换行。

    Windows 工作区的 .sh 常是 CRLF，打进 Linux 包会变成
    `/bin/bash^M: bad interpreter`；反过来 .bat 必须是 CRLF，
    cmd 解析 LF-only 的 goto/label 会错行。所以这里强制归一并
    不依赖 git 的 eol 设置——打包脚本要能单独跑对。
    """
    with open(path, "rb") as f:
        raw = f.read()
    if b"__REPO__" in raw and repo:
        raw = raw.replace(b"__REPO__", repo.encode("utf-8"))

    ext = os.path.splitext(path)[1].lower()
    if ext in (".sh", ".py", ".md", ".html", ".css", ".js", ".json", ".txt", ".yml", ".yaml"):
        raw = raw.replace(b"\r\n", b"\n")
    elif ext in (".bat", ".cmd"):
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return raw


def build_zip(target: str, repo: str, extra: list[tuple[str, str]] | None = None) -> None:
    """extra: [(源相对路径, 包内路径)]，用来把部署脚本摊到包根目录。"""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for full, rel in iter_files():
            zf.writestr(rel, patched_bytes(full, repo))
        for src, arc in extra or []:
            full = os.path.join(ROOT, src)
            if os.path.isfile(full):
                zf.writestr(arc, patched_bytes(full, repo))


def build_tar(target: str, repo: str) -> None:
    with tarfile.open(target, "w:gz") as tf:
        for full, rel in iter_files():
            data = patched_bytes(full, repo)
            info = tf.gettarinfo(full, arcname=rel)
            info.mtime = int(time.time())
            # 换行归一后长度会变（CRLF→LF 少字节），必须按实际数据重置
            info.size = len(data)
            # Windows 上 gettarinfo 拿不到 unix 权限，统一给个合理值
            info.mode = 0o755 if rel.endswith(".sh") else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tf.addfile(info, io.BytesIO(data))


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_release")
    parser.add_argument("--version", help="覆盖版本号（只影响包名与 VERSION 文件）")
    parser.add_argument("--repo", help="GitHub owner/name，用于替换部署脚本里的 __REPO__")
    parser.add_argument("--no-stamp", action="store_true", help="不写 BUILD 时间戳")
    args = parser.parse_args()

    version = (args.version or read_version()).strip().lstrip("vV")
    repo = (args.repo or git_repo() or "").strip()

    if not args.no_stamp:
        with open(os.path.join(ROOT, "BUILD"), "w", encoding="utf-8") as f:
            f.write(_bj_stamp())
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
        f.write(version + "\n")

    os.makedirs(DIST, exist_ok=True)
    for old in os.listdir(DIST):
        if old.startswith("wk-switch-") or old == "SHA256SUMS.txt":
            os.remove(os.path.join(DIST, old))

    plain = os.path.join(DIST, f"wk-switch-{version}.zip")
    linux = os.path.join(DIST, f"wk-switch-{version}-linux.tar.gz")
    windows = os.path.join(DIST, f"wk-switch-{version}-windows.zip")

    print(f"版本 {version}  仓库 {repo or '（未设置，部署脚本保留 __REPO__）'}")
    build_zip(plain, repo)
    print("  写出", os.path.basename(plain), f"{os.path.getsize(plain) // 1024} KB")
    build_tar(linux, repo)
    print("  写出", os.path.basename(linux), f"{os.path.getsize(linux) // 1024} KB")
    # Windows 包把 bat 摊到根目录：解压后双击 install.bat 就能装，不用进两层子目录
    build_zip(windows, repo, extra=[
        ("deploy/windows/install.bat", "install.bat"),
        ("deploy/windows/start.bat", "start.bat"),
        ("deploy/windows/stop.bat", "stop.bat"),
        ("deploy/windows/uninstall.bat", "uninstall.bat"),
    ])
    print("  写出", os.path.basename(windows), f"{os.path.getsize(windows) // 1024} KB")

    sums = []
    for path in (plain, linux, windows):
        sums.append(f"{sha256(path)}  {os.path.basename(path)}")
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sums) + "\n")
    print("  SHA256SUMS.txt 已生成")
    print("\n上传：gh release create v%s dist/*.zip dist/*.tar.gz dist/SHA256SUMS.txt" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
