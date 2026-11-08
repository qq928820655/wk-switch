"""版本号与构建信息。升级逻辑、部署脚本、页面都从这里读，避免多处写死。"""

from __future__ import annotations

import os
import time

from . import clock

APP_NAME = "wk-switch"
VERSION = "1.9.5"
CHANNEL = "stable"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILD_FILE = os.path.join(_ROOT, "BUILD")
_VERSION_FILE = os.path.join(_ROOT, "VERSION")


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def version() -> str:
    """VERSION 文件优先。

    一键升级会把新版本号写进 VERSION 文件，重启后无需改动本模块就能
    反映真实版本；包里的常量只是源码直跑时的兜底。
    """
    return _read_file(_VERSION_FILE) or VERSION


def build_time() -> str:
    return _read_file(_BUILD_FILE) or "dev"


def info() -> dict:
    return {
        "app": APP_NAME,
        "version": version(),
        "channel": CHANNEL,
        "build": build_time(),
        "python": "%d.%d.%d" % __import__("sys").version_info[:3],
        "started_at": clock.stamp(),
    }


def parse_semver(text: str) -> tuple:
    """'1.2.3' / 'v1.2.3' / '1.2.3-beta.1' → (1,2,3)。

    比较只用数字段：预发布标记不参与排序，发布流程里 stable 通道不会
    拿到 beta 包，多写一套预发布比较逻辑没有收益。
    """
    raw = str(text or "").strip().lstrip("vV")
    head = raw.split("-")[0].split("+")[0]
    parts = []
    for piece in head.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str = "") -> bool:
    return parse_semver(candidate) > parse_semver(current or version())
