"""时间口径：全站只认北京时间（UTC+8）。

这些断言是防回归用的：谁哪天把 `time.strftime` 换回来，跑一遍就能看出来。
服务器时区在这台机器上是 UTC+8 也一样跑不出区别，所以用固定时间戳算，
不看「现在几点」。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wk import clock  # noqa: E402


class ClockTests(unittest.TestCase):
    def test_stamp_is_utc_plus_8(self) -> None:
        # 2026-11-06 09:30:00 UTC → 北京时间 17:30:00
        ts = datetime(2026, 11, 6, 9, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.stamp(ts), "2026-11-06 17:30:00")

    def test_compact_sorts_lexicographically(self) -> None:
        ts = datetime(2026, 11, 6, 9, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.compact(ts), "20261106-173000")
        # 同格式的字符串排序 = 时间排序，批次下拉依赖这一点
        self.assertLess(clock.compact(ts), clock.compact(ts + 60))

    def test_short_format(self) -> None:
        ts = datetime(2026, 11, 6, 9, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.short(ts), "11-06 17:30")

    def test_date_rolls_over_at_beijing_midnight(self) -> None:
        """UTC 16:00 已经是北京次日 00:00——按机器时区算日期会差一天。"""
        ts = datetime(2026, 11, 6, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.day(ts), "2026-11-07")

    def test_day_start_is_beijing_midnight(self) -> None:
        ts = datetime(2026, 11, 6, 9, 30, 0, tzinfo=timezone.utc).timestamp()
        start = clock.day_start(ts)
        self.assertEqual(clock.stamp(start), "2026-11-06 00:00:00")
        self.assertLess(start, ts)

    def test_parse_roundtrip(self) -> None:
        text = clock.stamp(1790000000)
        self.assertEqual(int(clock.parse(text)), 1790000000)

    def test_parse_never_raises(self) -> None:
        """历史数据里可能有手工改过的行，一条坏行不该让整个列表打不开。"""
        for bad in ("", "2026/11/06", "昨天", "2026-13-45 99:99:99"):
            self.assertEqual(clock.parse(bad), 0.0)

    def test_stamp_now_matches_offset(self) -> None:
        """不带参数时取当前时刻，和 UTC 差 8 小时（允许几秒误差）。"""
        got = clock.stamp()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        expect = (datetime.now(timezone.utc)
                  .astimezone(timezone.utc).timestamp())
        delta = datetime.strptime(got, clock.STAMP).replace(
            tzinfo=clock.BEIJING).timestamp() - expect
        self.assertLess(abs(delta), 5)
        # 顺带确认时区名字没写错
        self.assertTrue(now_utc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
