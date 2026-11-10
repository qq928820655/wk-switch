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


if __name__ == "__main__":
    unittest.main(verbosity=2)
