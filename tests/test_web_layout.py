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
SERVER = os.path.join(ROOT, "server.py")


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


class BackupScopeLayoutTest(unittest.TestCase):
    """备份卡「范围跟筛选/勾选走」的结构守门。

    前端算范围、后端复算范围、界面给提示，三处缺一处就会退回「永远备整库」。
    """

    def test_backup_card_has_live_scope_hint(self):
        html = read(INDEX)
        self.assertIn('id="backup-scope"', html, "备份卡缺实时范围提示")
        self.assertIn("按当前筛选 / 勾选打包", html, "卡片说明还写着整库")
        self.assertIn('id="btn-store-backup"', html)

    def test_frontend_posts_scope_filters_and_selection(self):
        app = read(APP)
        for needle in ("function backupScope()", "function renderBackupScope()",
                       "api('/api/store/backup'", "scope: s.scope,",
                       "filters: s.filters,", "ids: s.ids",
                       "data.scope || {}", "renderBackupScope();",
                       "function backupScopeKey(s)", "dataset.scopeKey",
                       "id = 'backup-stale'"):
            self.assertIn(needle, app, f"前端少接了 {needle}")
        self.assertNotIn("apiGet('/api/store/backup')", app,
                         "还在走整库 GET，筛选和勾选不会生效")

    def test_scope_hint_refreshes_on_selection_change(self):
        app = read(APP)
        i = app.index("function refreshSelBar()")
        self.assertIn("renderBackupScope();", app[i:i + 1400],
                      "勾选变了不重算范围提示")

    def test_server_wires_post_scope(self):
        srv = read(SERVER)
        self.assertIn('elif path == "/api/store/backup":\n                self._store_backup(payload)',
                      srv, "POST 路由没接备份范围")
        self.assertIn("store.backup_scope_ids(", srv)
        self.assertIn("store.backup_bytes(row_ids=row_ids, scope=scope)", srv)

    def test_css_has_scope_hint_style(self):
        css = read(CSS)
        self.assertIn(".backup-scope {", css)
        self.assertIn(".backup-scope.bad {", css)
        self.assertIn("#backup-downloads.stale {", css)


class LoginExitPickLayoutTest(unittest.TestCase):
    """登录弹窗必须同时留着手填输入框和端口下拉，删一个都会让另一半功能失效。"""

    def test_modal_has_manual_input_and_port_pick(self):
        html = read(INDEX)
        for needle in ('id="wl-proxy"', 'id="wl-proxy-pick"',
                       'id="btn-wl-exits"', 'id="wl-exits-hint"', '</select>'):
            self.assertIn(needle, html, f"登录弹窗少了 {needle}")
        self.assertNotIn('<label class="field">\n        <span>登录出口代理</span>',
                         html, "里面放了 button，外层不能再用 label")

    def test_app_wires_pick_and_manual_input(self):
        app = read(APP)
        for needle in ("'/api/store/login/exits'", "loadWlExits(false)",
                       "loadWlExits(true)", "function wlRenderExits(",
                       "function wlSyncProxyPick()", "function wlPickProxy()",
                       "wlState.directValue", "data.probed", "<optgroup label="):
            self.assertIn(needle, app, f"前端少接了 {needle}")
        i = app.index("$('#wl-proxy').addEventListener('input'")
        self.assertIn("wlSyncProxyPick();", app[i:i + 300],
                      "手填地址后下拉还高亮旧端口")
        self.assertIn("$('#btn-store-login').addEventListener('click', () => openWebLogin(null));",
                      app, "新增按钮直接传函数会把点击事件当目标行，弹窗进「重新登录」模式")
        self.assertIn("$('#btn-wl-exits').addEventListener('click', () => loadWlExits(true));",
                      app, "检测按钮没接真探测")

    def test_server_wires_login_exits_route(self):
        srv = read(SERVER)
        self.assertIn('if path == "/api/store/login/exits":\n'
                      '            self._store_login_exits()', srv)
        self.assertIn("def _store_login_exits(self)", srv)
        self.assertIn("recheck.port_proxy(cfg, port)", srv)
        self.assertIn("probe_now = (query.get(\"probe\")", srv)

    def test_css_keeps_pick_row_readable_on_mobile(self):
        css = read(CSS)
        self.assertIn(".wl-proxy-pick-row {", css)
        self.assertIn(".wl-form .field select {", css)
        self.assertIn(".wl-proxy-pick-row { flex-wrap: wrap; }", css,
                      "窄屏并排会把端口名挤掉")


class PinIpLayoutTest(unittest.TestCase):
    """固定出口 IP：手填 + 检测下拉 + 策略 + 三个动作，少一个就退化成按端口。"""

    def test_exit_card_has_pin_controls(self):
        html = read(INDEX)
        for needle in ('id="rc-pin-ip"', 'id="rc-pin-ip-pick"',
                       'id="rc-pin-action"', 'id="btn-rc-pin-check"',
                       'id="btn-rc-pin-assign"', 'id="btn-rc-pin-clear"',
                       'id="rc-pin-hint"', 'value="reselect"', 'value="block"'):
            self.assertIn(needle, html, f"复检出口卡少了 {needle}")

    def test_app_wires_pin_endpoints_and_result_line(self):
        app = read(APP)
        for needle in ("'/api/recheck/exit-ip?'", "function renderPinIpOptions(",
                       "async function checkPinIp()", "async function assignPinIp()",
                       "async function clearPinIp()", "pin_exit_ip:",
                       "pin_ip_action:", "data.matched_ports",
                       "fixed_ok", "r.exit_ip_ok === false", "pin.reselected",
                       "const blocked = (data.pin || {}).blocked || 0;",
                       "出口 IP 不符 / 其余未到间隔",
                       "$('#btn-rc-pin-assign').addEventListener('click'"):
            self.assertIn(needle, app, f"前端少接了 {needle}")
        i = app.index("async function assignPinIp()")
        self.assertIn("ip,", app[i:i + 500], "按 IP 分配没把目标 IP 传给后端")
        self.assertIn("renderPinIpOptions(recheckState.nodes);", app,
                      "检测完节点表没刷新出口 IP 下拉")

    def test_server_wires_pin_route_and_report(self):
        srv = read(SERVER)
        for needle in ('if path == "/api/recheck/exit-ip":',
                       "def _recheck_exit_ip(self)",
                       'status["matched"] = status.pop("ok")',
                       '"pin": runner.pin_report',
                       '"pin_exit_ip"] = pinned_ip',
                       "port_map = store.exit_ports_of("):
            self.assertIn(needle, srv, f"后端少了 {needle}")

    def test_css_keeps_pin_box_readable_on_mobile(self):
        css = read(CSS)
        self.assertIn(".rc-pin-box {", css)
        self.assertIn(".rc-pin-box .rc-grid { grid-template-columns: 1fr;", css,
                      "窄屏两列会把 IP 输入框挤到看不见")
        self.assertIn(".rc-pin-box .field select {", css)
        self.assertIn(".rc-pin-box .export-bar .btn { flex: 1 1 100%; }", css,
                      "窄屏按钮并排会把文案折成两行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
