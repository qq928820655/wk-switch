"""带登录的无头截图：登录 → 切到指定视图 → 截图。

用法：
    python tools/shot_login.py <base_url> <out.png> <width> <height> <view> [expr]

view 是页面左下 tab 的 data-view（convert / store / recheck / update）。
expr 是可选的额外 JS，在截图前执行（用来展开 details、勾选几行等）。
"""
import base64
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

url, out, w, h = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
view = sys.argv[5] if len(sys.argv) > 5 else "store"
extra = sys.argv[6] if len(sys.argv) > 6 else ""
# 口令不写死：仓库是公开的，写默认值等于把口令贴出去。
# 优先第 8 个参数，其次环境变量 WK_TOKEN。
TOKEN = sys.argv[7] if len(sys.argv) > 7 else os.environ.get("WK_TOKEN", "")
if not TOKEN:
    sys.exit("缺少口令：用第 8 个参数传，或设环境变量 WK_TOKEN")
WAIT = float(sys.argv[8]) if len(sys.argv) > 8 else 1.2

CH = r"C:/Program Files/Google/Chrome/Application/chrome.exe"
prof = tempfile.mkdtemp(prefix="wk-cdp-")
PORT = random.randint(9400, 9900)
proc = subprocess.Popen(
    [CH, "--headless=new", "--disable-gpu", "--no-proxy-server",
     f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)
try:
    _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    tabs = json.loads(_op.open(f"http://127.0.0.1:{PORT}/json").read())
    ws = websocket.create_connection(
        [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"], timeout=30)
    mid = [0]

    def cmd(method, **params):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == mid[0]:
                return r.get("result", {})

    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
        deviceScaleFactor=1, mobile=(w < 600))
    # 登录：先填口令提交，等跳转
    cmd("Page.navigate", url=url + "/login")
    time.sleep(2.0)
    cmd("Runtime.evaluate", expression=(
        "(()=>{const i=document.getElementById('token');"
        f"i.value={TOKEN!r};"
        "document.getElementById('form').dispatchEvent(new Event('submit',{cancelable:true}));"
        "})()"))
    time.sleep(3.5)
    cmd("Page.navigate", url=url + "/")
    time.sleep(3.0)
    cmd("Runtime.evaluate", expression=(
        f"document.querySelector('.tab[data-view=\"{view}\"]').click()"))
    time.sleep(2.5)
    if extra:
        cmd("Runtime.evaluate", expression=extra)
        time.sleep(WAIT)
    # 第 9 个参数：截图前滚到某个选择器。长页面（导入页有三段卡片）不滚
    # 就只能拍到头两屏，验收时看不到真正要改的那块。
    focus_sel = sys.argv[9] if len(sys.argv) > 9 else ""
    if focus_sel:
        cmd("Runtime.evaluate", expression=(
            "(()=>{const el=document.querySelector(" + repr(focus_sel) + ");"
            "if(el) el.scrollIntoView({block:'start'});})()"))
        time.sleep(0.6)
    else:
        cmd("Runtime.evaluate", expression="window.scrollTo(0,0)")
        time.sleep(0.5)
    data = cmd("Page.captureScreenshot", format="png")
    open(out, "wb").write(base64.b64decode(data["data"]))
    print("saved", out)
    ws.close()
finally:
    proc.terminate()
    shutil.rmtree(prof, ignore_errors=True)
