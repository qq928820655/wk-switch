"""界面布局的静态守门。

布局回归靠截图看不出来（列宽压到 54px 时页面照样能渲染），而 CDP 验证
不会跑在每次 CI 里。这里只钉住三条「删了就必然复发」的约束：

- 复检名单必须有 colgroup，且出口列是固定像素宽
- 桌面必须 table-layout: fixed，否则 auto 布局会把列压回 min-content
- 窄屏卡片布局必须把 colgroup 藏掉，否则固定像素宽会撑破 grid

不测具体像素值，只测结构存在性 —— 调宽度不该让测试变红。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "web", "index.html")
CSS = os.path.join(ROOT, "web", "style.css")
APP = os.path.join(ROOT, "web", "app.js")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class RecheckTableLayoutTest(unittest.TestCase):
    def test_rc_table_has_colgroup_with_ten_cols(self) -> None:
        html = read(INDEX)
        table = html[html.index('<table class="grid" id="rc-table">'):]
        table = table[:table.index("</table>")]
        group = re.search(r"<colgroup>(.*?)</colgroup>", table, re.S)
        self.assertIsNotNone(group, "复检名单缺 colgroup，桌面列宽会回到 auto")
        cols = re.findall(r"<col\b[^>]*>", group.group(1))
        self.assertEqual(len(cols), 10, "10 列对应 10 个 col，多一个少一个都会错位")
        self.assertTrue(any("156px" in c for c in cols),
                        "出口列必须是固定像素宽，不然长结论列会把它压到 min-content")

    def test_fixed_layout_and_min_width(self) -> None:
        css = read(CSS)
        m = re.search(r"#rc-table\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "缺 #rc-table 规则")
        self.assertIn("table-layout: fixed", m.group(1),
                      "auto 布局会把出口列压到 54px / select 30px")
        self.assertIn("min-width", m.group(1),
                      "没有 min-width，名称和结论列会被挤成 0")

    def test_narrow_layout_hides_colgroup(self) -> None:
        css = read(CSS)
        # 720px 断点在文件里有好几处（通用窄屏 / 账户库 / 复检卡片），
        # 卡片布局是后面那一块，不能只取第一个。
        blocks = re.findall(r"@media \(max-width: 720px\)\s*\{(.*?)\n\}",
                            css, re.S)
        self.assertTrue(blocks, "窄屏卡片布局的媒体查询不见了")
        joined = "\n".join(blocks)
        self.assertIn("#rc-table colgroup { display: none; }", joined,
                      "卡片布局下 colgroup 的固定像素宽会把 grid 撑破")
        self.assertIn("#rc-table { table-layout: auto; min-width: 0; }", joined,
                      "卡片布局必须撤掉桌面列宽约束")


class AccountSearchLayoutTest(unittest.TestCase):
    """账户搜索两分区的结构守门。

    分区筛选是「账户库 / 每日活跃 / 复检下拉」三处共用的一套口径：
    normal = 不在复检名单，risk = 名单里任意档位。少接一处，界面上就会出现
    「按钮点了没反应」或者「同一个号在两页显示成不同身份」。
    """

    def test_store_has_zone_segment_and_counts(self):
        html = read(INDEX)
        seg = re.search(r'<div class="seg" id="store-zone-seg".*?</div>', html, re.S)
        self.assertIsNotNone(seg, "账户库缺正常/风控分段")
        radios = re.findall(r'name="store-zone" value="([^"]*)"', seg.group(0))
        self.assertEqual(radios, ["", "normal", "risk"])
        for key in ("store-zone-all", "store-zone-normal", "store-zone-risk"):
            self.assertIn(f'id="{key}"', html, "分段计数没有落点")

    def test_activity_has_account_search_and_zone_segment(self):
        html = read(INDEX)
        self.assertIn('id="da-account-search"', html, "每日活跃账号卡缺搜索框")
        seg = re.search(r'<div class="seg" id="da-zone-seg".*?</div>', html, re.S)
        self.assertIsNotNone(seg, "每日活跃账号卡缺正常/风控分段")
        self.assertEqual(re.findall(r'name="da-zone" value="([^"]*)"', seg.group(0)),
                         ["", "normal", "risk"])

    def test_recheck_picker_has_zone_scope(self):
        html = read(INDEX)
        sel = re.search(r'<select id="rc-add-zone".*?</select>', html, re.S)
        self.assertIsNotNone(sel, "加名单下拉缺搜索范围")
        self.assertIn('value="normal"', sel.group(0))
        self.assertIn('value="risk"', sel.group(0))

    def test_frontend_wires_zone_filter_and_grouping(self):
        app = read(APP)
        for needle in ("storeState.zone", "daVisibleRows()", "daPruneSelection()",
                       "zoneFlag(r)", "<optgroup label=", "params.set('zone', zone)"):
            self.assertIn(needle, app, f"前端少接了 {needle}")

    def test_zone_flag_has_both_colors_and_narrow_dot(self):
        css = read(CSS)
        self.assertIn(".zone-flag.risk {", css)
        self.assertIn(".zone-flag.normal {", css)
        blocks = re.findall(r"@media \(max-width: 720px\)\s*\{(.*?)\n\}",
                            css, re.S)
        self.assertTrue(any("#da-accounts .zone-flag" in b for b in blocks),
                        "窄屏没把分区徽标收成圆点，名称列会被胶囊挤破")


if __name__ == "__main__":
    unittest.main(verbosity=2)
