"""全站时间源：北京时间（UTC+8）。

服务器可能开在 UTC（VPS 默认）、也可能在美西机房，`time.strftime` 跟系统
时区走，同一份数据换台机器显示的小时数就不一样——「昨天 22:10 导入的」
在两台机器上是两个时间，对不上任何记录。

所以这里把「现在几点」和「把时间戳格式化成字符串」两件事收到一处：

- 存储层（SQLite 的 TEXT 时间列）只写本模块产出的字符串；
- 展示层要显示时间就调 `stamp()` / `short()`，不要自己拼；
- 日志行、批次号、导出文件名里的时间戳同样走这里。

时区写死为固定偏移 +8：中国不实行夏令时，用 `timezone(timedelta(hours=8))`
比 `ZoneInfo("Asia/Shanghai")` 稳，前者不依赖 tzdata 是否存在（Windows 上
某些精简版 Python 没有系统 tzdata，`ZoneInfo` 会抛 `ZoneInfoNotFoundError`）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

#: 固定 +8 偏移。带名字是为了出问题时 repr 里能看出是什么时区。
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

#: 库里存的时间字符串格式。带秒、不带毫秒——秒级足够定位一次操作，
#: 毫秒会让同一秒内的两次写入在字符串比较里分出先后，反而干扰
#: `ORDER BY updated_at` 的稳定排序。
STAMP = "%Y-%m-%d %H:%M:%S"

#: 批次号 / 文件名里用的紧凑格式，无分隔符，可直接排序。
COMPACT = "%Y%m%d-%H%M%S"


def now() -> float:
    """当前 epoch 秒。等价 `time.time()`，存在是为了让调用方只 import 一个模块。"""
    return time.time()


def dt(ts: float | None = None) -> datetime:
    """epoch 秒 → 北京时间 datetime。`ts=None` 取当前时刻。"""
    return datetime.fromtimestamp(now() if ts is None else float(ts), BEIJING)


def stamp(ts: float | None = None, fmt: str = STAMP) -> str:
    """epoch 秒 → 北京时间字符串。默认 `2026-11-06 17:30:08`。"""
    return dt(ts).strftime(fmt)


def compact(ts: float | None = None) -> str:
    """epoch 秒 → `20261106-173008`。批次号、导出文件名用它。"""
    return dt(ts).strftime(COMPACT)


def short(ts: float | None = None) -> str:
    """epoch 秒 → `11-06 17:30`。表格里空间紧张时用，年份由 tooltip 补。"""
    return dt(ts).strftime("%m-%d %H:%M")


def day(ts: float | None = None) -> str:
    """epoch 秒 → `2026-11-06`（北京时间的那一天）。"""
    return dt(ts).strftime("%Y-%m-%d")


def day_start(ts: float | None = None) -> float:
    """北京时间当天 00:00 的 epoch 秒。

    自动复检判断「今天跑过没有」、探针算到期窗口都要这个：
    用本机时区算出来的「今天」在北京时间 08:00 之前是前一天，日更逻辑会
    在错误的时刻触发。
    """
    d = dt(ts)
    midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def parse(text: str, fmt: str = STAMP) -> float:
    """把本模块产出的字符串解析回 epoch 秒。

    只用于读自己写的时间列（备份还原、批次名解析）。解析失败返回 0 而不是
    抛异常：历史数据里可能混着手工改过的行，一条坏行不该让整个列表打不开。
    """
    text = (text or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError:
        return 0.0
    return parsed.replace(tzinfo=BEIJING).timestamp()
