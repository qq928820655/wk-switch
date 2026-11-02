"""版本号与构建信息。升级逻辑、部署脚本、页面都从这里读，避免多处写死。"""

from __future__ import annotations

import os
import time

APP_NAME = "wk-switch"
VERSION = "1.0.0"
CHANNEL = "stable"

#: 构建时写入；从源码直接跑时用文件 mtime 兜底
_BUILD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "BUILD")


def build_time() -> str:
    try:
        with open(_BUILD_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def info() -> dict:
    return {
        "app": APP_NAME,
        "version": VERSION,
        "channel": CHANNEL,
        "build": build_time(),
        "python": "%d.%d.%d" % __import__("sys").version_info[:3],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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


def is_newer(candidate: str, current: str = VERSION) -> bool:
    return parse_semver(candidate) > parse_semver(current)
