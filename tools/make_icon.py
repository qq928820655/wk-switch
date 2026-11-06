#!/usr/bin/env python3
"""生成 wk-switch 的应用图标（纯标准库，无第三方依赖）。

设计：indigo 渐变圆角方 + 白色双向箭头。双向箭头就是界面顶栏那个 ⇄，
同一个符号在 favicon、任务栏、登录页、PWA 图标里保持一致。

    python tools/make_icon.py            # 输出到 web/assets/
    python tools/make_icon.py --check    # 只校验已有文件尺寸

产出：
    web/assets/icon.svg                矢量源（可手改）
    web/assets/favicon.ico             16/32/48 多尺寸
    web/assets/icon-192.png            PWA
    web/assets/icon-512.png            PWA / 商店
    web/assets/apple-touch-icon.png    180，iOS 不给透明，这里铺满底色

为什么自己写光栅化而不用 Pillow：项目声明「仅标准库」，图标生成脚本也得守住
这条，否则换个环境想改个颜色还得先装包。实现方式是 SDF（有符号距离场）逐像素
求值 + 解析抗锯齿，精度比 4x 超采样降采样还好，代价是这个文件看着长。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from math import hypot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "web", "assets")

# ---------------------------------------------------------------- 设计参数
#: 坐标全部按 512 的设计网格写，渲染时按 scale 缩放到目标像素。
GRID = 512.0

#: 圆角半径。0.215 约等于 iOS squircle 的观感，比纯圆角矩形更有「图标感」。
RADIUS = 110.0

#: 渐变两端。比界面 --brand (#4f46e5) 的明度跨度大一截：图标尺寸小，
#: 色差不够就会看成一个平色块，深色任务栏里尤其明显。
GRAD_FROM = (0x7E, 0x7B, 0xFF)
GRAD_TO = (0x36, 0x2B, 0xAB)

#: 左上高光。纯渐变在深色任务栏里会显得闷，加一层极淡的径向光提亮上缘。
HIGHLIGHT = (1.00, 1.00, 1.00)
HIGHLIGHT_ALPHA = 0.16
HIGHLIGHT_CENTER = (0.28, 0.20)   # 归一化坐标
HIGHLIGHT_RADIUS = 0.80

#: 内描边：贴着底色边缘内侧描一道 3px 的半透明白，做出一点「玻璃壳」的厚度。
#: 只在边缘生效，中间是纯渐变——大图标靠它提质感，小图标看不出来但也不碍事。
RIM_WIDTH = 3.0
RIM_ALPHA = 0.16

INK = (0xFF, 0xFF, 0xFF)

#: 标准箭头几何（≥32px 用）。上杆向右、下杆向左，两个三角各在一端。
_NORMAL = {
    "bar": 25.0,      # 杆的半宽 → 杆宽 50
    "arrow_h": 46.0,  # 三角的半高 → 高 92
    "arrow_w": 68.0,  # 三角的宽
    "up_y": 192.0,    # 上杆中线
    "down_y": 320.0,  # 下杆中线（两杆间距 128，留 78 空隙）
    "left": 104.0,    # 图形左右边界
    "right": 408.0,
    "pad": 0.86,      # 图形占画布比例，四周留白
}

#: 极小几何（≤20px 用）。16px 画布上图形只有 15 个像素可用，标准版那套比例
#: 放到这里三角会被量化吃掉，看上去就是两条白线。这组把两条箭头往外拉开、
#: 三角放大到 4px 高，换来「看得出是箭头」，代价是留白几乎为零。
#: 调参前先算两个数：三角高 = 2*arrow_h*pad*size/512，两箭头净间隙 =
#: (down_y - up_y - 2*arrow_h)*pad*size/512。后者低于 1.5px 就会粘成一条横线。
_TINY = {
    "bar": 34.0,
    "arrow_h": 70.0,
    "arrow_w": 96.0,
    "up_y": 150.0,
    "down_y": 362.0,
    "left": 60.0,
    "right": 452.0,
    "pad": 0.96,
}


def geometry(size: int) -> dict:
    """按目标尺寸挑一套几何。分界定在 24：再小就得用极小版。"""
    return dict(_TINY if size <= 20 else _NORMAL)


def _mix(a: tuple[float, float, float], b: tuple[float, float, float],
         t: float) -> tuple[float, float, float]:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


# ---------------------------------------------------------------- SDF 基元
# 约定：返回值 < 0 表示点在形状内部，数值是到边界的距离（像素，已按 scale 缩放）。

def _sdf_round_rect(px: float, py: float, cx: float, cy: float,
                    half_w: float, half_h: float, r: float) -> float:
    qx = abs(px - cx) - (half_w - r)
    qy = abs(py - cy) - (half_h - r)
    ax = qx if qx > 0 else 0.0
    ay = qy if qy > 0 else 0.0
    outside = hypot(ax, ay)
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


def _sdf_segment(px: float, py: float, x1: float, y1: float,
                 x2: float, y2: float, half_w: float) -> float:
    """圆头线段。"""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-9:
        return hypot(px - x1, py - y1) - half_w
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return hypot(px - (x1 + dx * t), py - (y1 + dy * t)) - half_w


def _sdf_triangle(px: float, py: float,
                  a: tuple[float, float], b: tuple[float, float],
                  c: tuple[float, float]) -> float:
    """三角形的近似 SDF：三条边有符号距离取最大。

    边附近是精确的（抗锯齿只关心边），顶点外侧会略微高估距离，
    表现为尖角稍微钝一点——对图标没影响，那点差别在半像素以内。
    """
    pts = (a, b, c)
    # 用重心符号判断内外，避免三条边的法线方向写反
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    sign = 1.0 if cross(a, b, c) > 0 else -1.0
    worst = -1e9
    for i in range(3):
        p1, p2 = pts[i], pts[(i + 1) % 3]
        ex, ey = p2[0] - p1[0], p2[1] - p1[1]
        ex_len = hypot(ex, ey)
        # 点到边的有符号距离（内部为负）
        d = ((px - p1[0]) * ey - (py - p1[1]) * ex) / ex_len * sign
        if d > worst:
            worst = d
    return worst


# ---------------------------------------------------------------- 渲染
def _shape_sdf(px: float, py: float, g: dict) -> float:
    """白色箭头部分的合并 SDF。g 是 geometry() 给的那套参数。"""
    bar = g["bar"]
    arrow_h = g["arrow_h"]
    arrow_w = g["arrow_w"]
    up_y, down_y = g["up_y"], g["down_y"]
    left, right = g["left"], g["right"]

    up_base = right - arrow_w
    down_base = left + arrow_w

    # 杆往三角里多伸 4，避免抗锯齿在接缝处透出底色
    d = _sdf_segment(px, py, left + bar, up_y, up_base + 4, up_y, bar)
    tri = _sdf_triangle(px, py,
                        (up_base, up_y - arrow_h),
                        (right, up_y),
                        (up_base, up_y + arrow_h))
    if tri < d:
        d = tri

    d2 = _sdf_segment(px, py, right - bar, down_y, down_base - 4, down_y, bar)
    tri2 = _sdf_triangle(px, py,
                         (down_base, down_y - arrow_h),
                         (left, down_y),
                         (down_base, down_y + arrow_h))
    if tri2 < d2:
        d2 = tri2
    return d if d < d2 else d2


def render(size: int, *, padded: bool = False, safe: bool = False) -> bytes:
    """渲染一张 RGBA 图，返回 PNG 字节。

    padded=True 四周留白——favicon 在 16px 下紧贴边缘会糊成一团，
    留白后和其他站点的图标放一起比例才协调。PWA 大图就不留，铺满更醒目。

    safe=True 出 maskable 版：背景照旧铺满整幅（不然被系统裁成圆形时边缘透明），
    但把箭头缩到中心 62%，保证落在 80% 直径的安全圆里不被裁掉。
    """
    g = geometry(size)
    ss = g["pad"] if padded else 1.0
    scale = size / GRID * ss
    offset = (size - size * ss) / 2.0         # 居中留白
    # maskable：箭头缩到 0.82。算一下余量——箭头在 512 网格上宽 304、高 176，
    # 外接圆半径 sqrt(152²+88²)≈176；乘 0.82 后是 144，而系统保证的安全区
    # 半径是 205（画布 40%）。缩太多会在圆形图标里显空，缩到 1.0 又贴着边缘。
    art = 0.82 if safe else 1.0
    art_scale = scale * art
    art_offset = (size - size * art) / 2.0

    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        py = (y + 0.5 - offset) / scale       # 转到 512 网格
        ay = (y + 0.5 - art_offset) / art_scale
        for x in range(size):
            px = (x + 0.5 - offset) / scale
            ax = (x + 0.5 - art_offset) / art_scale

            # --- 底：圆角矩形 ---
            # safe 版（maskable）必须是满幅方块：系统会按自己的形状（圆形、方圆、
            # 水滴）去裁，自己再留圆角就会在四角露出透明，装上桌面是个缺角的图。
            d_bg = _sdf_round_rect(px, py, GRID / 2, GRID / 2,
                                   GRID / 2, GRID / 2, 0.0 if safe else RADIUS)
            a_bg = 0.5 - d_bg * scale         # 一个像素的过渡带做抗锯齿
            if a_bg <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            if a_bg > 1.0:
                a_bg = 1.0

            # --- 渐变 + 高光 ---
            t = (px + py) / (2.0 * GRID)
            r, gg, b = _mix(GRAD_FROM, GRAD_TO, t)
            hx = px / GRID - HIGHLIGHT_CENTER[0]
            hy = py / GRID - HIGHLIGHT_CENTER[1]
            hd = hypot(hx, hy) / HIGHLIGHT_RADIUS
            if hd < 1.0:
                k = (1.0 - hd) ** 2 * HIGHLIGHT_ALPHA
                r += (HIGHLIGHT[0] * 255 - r) * k
                gg += (HIGHLIGHT[1] * 255 - gg) * k
                b += (HIGHLIGHT[2] * 255 - b) * k

            # --- 内描边（只在内侧一个像素带里） ---
            # 必须带 d_bg < 0：外侧正距离也满足 > -RIM_WIDTH，不挡住的话
            # rim 会变成 1 + (-d_bg)/RIM_WIDTH，远处飙到 ≥1，颜色直接爆 255。
            if -RIM_WIDTH < d_bg < 0.0:
                rim = 1.0 + d_bg / RIM_WIDTH      # 贴边 → 1，往里 → 0
                rim = rim * rim * RIM_ALPHA
                r += (255.0 - r) * rim
                gg += (255.0 - gg) * rim
                b += (255.0 - b) * rim

            # --- 箭头 ---
            d_fg = _shape_sdf(ax, ay, g)
            a_fg = 0.5 - d_fg * art_scale
            if a_fg > 0.0:
                a_fg = 1.0 if a_fg > 1.0 else a_fg
                r += (INK[0] - r) * a_fg
                gg += (INK[1] - gg) * a_fg
                b += (INK[2] - b) * a_fg

            # 逐层叠加后理论上不会越界，但还是兜一道——图标生成属于「跑一次就忘」，
            # 宁可多一次 min/max 也不要哪天改个参数就抛 ValueError
            row += bytes((_c255(r), _c255(gg), _c255(b),
                          int(a_bg * 255 + 0.5)))
        rows.append(b"\x00" + bytes(row))
    return _png_bytes(size, size, rows)


def _c255(v: float) -> int:
    """浮点颜色转 0-255 整数。"""
    i = int(v + 0.5)
    return 0 if i < 0 else (255 if i > 255 else i)


def _png_bytes(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def _ico_bytes(pngs: list[tuple[int, bytes]]) -> bytes:
    """多尺寸 ICO。Vista 之后允许条目里直接塞 PNG，省得写 BMP 反色掩码。"""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b""
    blob = b""
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII",
                               0 if size >= 256 else size,
                               0 if size >= 256 else size,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blob += data
    return header + entries + blob


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" role="img" aria-label="wk-switch">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7e7bff"/>
      <stop offset="1" stop-color="#362bab"/>
    </linearGradient>
    <radialGradient id="hl" cx="0.28" cy="0.20" r="0.80">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.16"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="512" height="512" rx="110" fill="url(#bg)"/>
  <rect width="512" height="512" rx="110" fill="url(#hl)"/>
  <rect x="2" y="2" width="508" height="508" rx="108" fill="none"
        stroke="#ffffff" stroke-opacity="0.16" stroke-width="3"/>
  <g fill="#ffffff" stroke="#ffffff" stroke-linecap="round" stroke-width="50">
    <line x1="129" y1="192" x2="344" y2="192"/>
    <line x1="383" y1="320" x2="168" y2="320"/>
  </g>
  <path d="M340 146 L408 192 L340 238 Z" fill="#ffffff"/>
  <path d="M172 274 L104 320 L172 366 Z" fill="#ffffff"/>
</svg>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 wk-switch 应用图标")
    ap.add_argument("--out", default=OUT_DIR, help="输出目录（默认 web/assets）")
    ap.add_argument("--check", action="store_true", help="只检查已有文件是否存在")
    args = ap.parse_args()

    if args.check:
        missing = 0
        for name in ("icon.svg", "favicon.ico", "icon-192.png",
                     "icon-512.png", "apple-touch-icon.png"):
            path = os.path.join(args.out, name)
            ok = os.path.exists(path)
            size = os.path.getsize(path) if ok else 0
            print(f"{'OK ' if ok else 'MISS'} {name:24s} {size} B")
            missing += 0 if ok else 1
        return 1 if missing else 0

    os.makedirs(args.out, exist_ok=True)

    def write(name: str, data: bytes) -> None:
        path = os.path.join(args.out, name)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)       # 原子替换：中途 Ctrl-C 不会留下半个文件
        print(f"  {name:24s} {len(data):>7} B")

    print(f"输出到 {args.out}")
    write("icon.svg", SVG.encode("utf-8"))

    # favicon：三个尺寸共用一张小图逻辑（带留白），16px 才不糊边
    ico_parts = [(s, render(s, padded=True)) for s in (16, 32, 48)]
    write("favicon.ico", _ico_bytes(ico_parts))

    # PWA / 任务栏：铺满，不留白
    write("icon-192.png", render(192))
    write("icon-512.png", render(512))
    # maskable：Android 自适应图标会裁成圆形/方圆，箭头得缩进安全区
    write("icon-maskable-512.png", render(512, safe=True))

    # iOS 圆角由系统裁，图本身必须是满幅方形且不透明 → padded=False
    write("apple-touch-icon.png", render(180))
    return 0


if __name__ == "__main__":
    sys.exit(main())
