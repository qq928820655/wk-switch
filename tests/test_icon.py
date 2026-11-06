"""图标资源测试。

图标是「生成一次、很久不动」的东西，坏掉的方式也很安静：PNG 头少了、
ICO 里只有一档、maskable 忘了铺满导致装上桌面是个缺角方块。
所以这里不解码像素细节，只钉住那些「坏了就该报错」的结构事实。
"""

import json
import os
import struct
import sys
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ASSETS = os.path.join(ROOT, "web", "assets")


def _read_png(path):
    """返回 (w, h, rows)。只解析我们自己写出来的 8 位 RGBA，不做通用解码。"""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError(f"{path} 不是 PNG")
    pos, idat, w, h, depth, ctype = 8, b"", 0, 0, 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    if depth != 8 or ctype != 6:
        raise AssertionError(f"{path} 不是 8 位 RGBA（depth={depth} ctype={ctype}）")
    raw = zlib.decompress(idat)
    stride = w * 4
    rows = [raw[y * (stride + 1) + 1:y * (stride + 1) + 1 + stride] for y in range(h)]
    return w, h, rows


def _alpha_at(rows, x, y, stride):
    return rows[y][x * 4 + 3]


class TestAssetsExist(unittest.TestCase):
    def test_all_files_present(self):
        for name in ("icon.svg", "favicon.ico", "icon-192.png", "icon-512.png",
                     "icon-maskable-512.png", "apple-touch-icon.png",
                     "manifest.webmanifest"):
            self.assertTrue(os.path.exists(os.path.join(ASSETS, name)), f"缺 {name}")


class TestPng(unittest.TestCase):
    def test_sizes(self):
        for name, size in (("icon-192.png", 192), ("icon-512.png", 512),
                           ("icon-maskable-512.png", 512),
                           ("apple-touch-icon.png", 180)):
            w, h, _ = _read_png(os.path.join(ASSETS, name))
            self.assertEqual((w, h), (size, size), name)

    def test_round_corners_are_transparent(self):
        """普通图标四角必须透明，否则放深色背景上会顶出四个白角。"""
        w, h, rows = _read_png(os.path.join(ASSETS, "icon-512.png"))
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            self.assertEqual(_alpha_at(rows, x, y, w * 4), 0, f"({x},{y}) 不透明")

    def test_maskable_is_full_bleed(self):
        """maskable 反过来：四角必须不透明，系统裁圆形时才不会露底。"""
        w, h, rows = _read_png(os.path.join(ASSETS, "icon-maskable-512.png"))
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            self.assertEqual(_alpha_at(rows, x, y, w * 4), 255, f"({x},{y}) 透明")

    def test_maskable_art_stays_in_safe_circle(self):
        """箭头必须在半径 40% 的安全圆内，否则圆形裁切会切掉箭头尖。"""
        w, h, rows = _read_png(os.path.join(ASSETS, "icon-maskable-512.png"))
        stride = w * 4
        cx = cy = w / 2.0
        limit = w * 0.40
        worst = 0.0
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                o = x * 4
                if rows[y][o] > 240 and rows[y][o + 1] > 240 and rows[y][o + 2] > 240:
                    d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                    worst = max(worst, d)
        self.assertGreater(worst, w * 0.15, "箭头太小了，看不出是什么")
        self.assertLess(worst, limit, f"箭头超出安全圆：{worst:.0f} > {limit:.0f}")

    def test_center_is_white_ink(self):
        """正中间应该是白的（杆与杆之间不算），这里取上杆中线验证图形没画歪。"""
        w, h, rows = _read_png(os.path.join(ASSETS, "icon-512.png"))
        y = int(512 * 192 / 512)
        o = (w // 2) * 4
        self.assertGreater(rows[y][o], 240)


class TestIco(unittest.TestCase):
    def test_has_three_sizes(self):
        with open(os.path.join(ASSETS, "favicon.ico"), "rb") as f:
            data = f.read()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        self.assertEqual(count, 3)
        sizes = []
        for i in range(count):
            # ICONDIRENTRY 是 16 字节定长：宽/高/色数/保留各 1，planes 与
            # bitcount 各 2，最后是 4 字节长度 + 4 字节偏移
            entry = data[6 + 16 * i:6 + 16 * (i + 1)]
            self.assertEqual(len(entry), 16)
            w = entry[0] or 256
            (length,) = struct.unpack("<I", entry[8:12])
            (offset,) = struct.unpack("<I", entry[12:16])
            self.assertTrue(data[offset:offset + 8].startswith(b"\x89PNG"),
                            f"第 {i} 档不是内嵌 PNG")
            self.assertLessEqual(offset + length, len(data), "条目长度越界")
            sizes.append(w)
        self.assertEqual(sorted(sizes), [16, 32, 48])


class TestManifest(unittest.TestCase):
    def test_valid_and_points_at_real_files(self):
        with open(os.path.join(ASSETS, "manifest.webmanifest"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["start_url"], "/")
        self.assertIn("wk-switch", data["name"])
        self.assertTrue(data["icons"])
        for icon in data["icons"]:
            src = icon["src"]
            self.assertTrue(src.startswith("/static/assets/"), src)
            # 路径要真的对得上磁盘，别写了个不存在的文件名
            on_disk = os.path.join(ASSETS, src[len("/static/assets/"):])
            self.assertTrue(os.path.exists(on_disk), f"manifest 指向不存在的 {on_disk}")


class TestSvg(unittest.TestCase):
    def test_shape_declared(self):
        with open(os.path.join(ASSETS, "icon.svg"), encoding="utf-8") as f:
            svg = f.read()
        self.assertIn('viewBox="0 0 512 512"', svg)
        self.assertIn('rx="110"', svg)
        self.assertEqual(svg.count("<path"), 2, "两个箭头尖各一条路径")


class TestGeometry(unittest.TestCase):
    def test_tiny_branch_used_for_small(self):
        import make_icon
        self.assertEqual(make_icon.geometry(16)["bar"], make_icon._TINY["bar"])
        self.assertEqual(make_icon.geometry(32)["bar"], make_icon._NORMAL["bar"])

    def test_tiny_gap_survives_downscale(self):
        """16px 下两条箭头之间必须还剩 1.5 像素，否则会粘成一条横线。"""
        import make_icon
        g = make_icon.geometry(16)
        gap = (g["down_y"] - g["up_y"] - 2 * g["arrow_h"]) * g["pad"] * 16 / 512
        self.assertGreater(gap, 1.5, f"间隙只有 {gap:.2f}px")


if __name__ == "__main__":
    unittest.main(verbosity=2)
