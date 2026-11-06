"""升级后的自重启：把「替换完代码」到「新版本真的在服务」这一段做成一个事务。

## 为什么不能只是一次 Popen

旧实现是 `Popen(python main.py ...)` 之后 `os._exit(0)`，三个坑全踩上了：

1. **起得太早**。新进程 `import server` 要几百毫秒，旧进程还在 `os._exit`
   的路上，端口没释放 → 新进程绑不上 → `find_port` 往后找 → 端口漂移，
   书签/反代/防火墙全失效。用户看到的是「重启后打不开了」。
2. **崩了没人知道**。子进程 stdout/stderr 全丢进 DEVNULL，起来失败只剩
   一句「服务没有在 20 秒内回来」。
3. **没有退路**。新版本有语法错误、少了依赖，服务就彻底失联。

所以这里引入一个**监督进程**，由它接管「等旧进程退出 → 起新进程 → 探活 →
失败回滚」的整条链路。旧进程发起后立刻退出，剩下的活全在监督进程里做，
结果写进 `data/restart.json`，页面重启完读它显示。

## 状态机

    preparing → waiting → starting → verifying → ok
                                             ↘ rolledback → ok / failed

`waiting` 等端口能重新 bind（旧进程真退出了），`verifying` 打 `/api/ping`
并校验版本号——版本对不上说明起来的是旧代码，同样算失败。

## 进程归属（Linux 两种部署方式完全不同）

Linux 上这套东西装下去有两种形态，重启方式必须分开处理，混着来必定失败：

**systemd**（`Type=simple`，默认 `KillMode=control-group`）
主进程一退出，systemd 就把**整个 cgroup** 打掉——监督进程和它拉起的子进程
一起没。所以 systemd 模式绝不能自己 Popen 新进程，否则端口在、服务却显示
`inactive`，下次 `systemctl restart` 又会撞端口往后漂移。正确做法是由监督
进程执行 `systemctl --no-block restart <unit>`，让 systemd 自己停、等、起。

而监督进程本身必须待在 cgroup 外，否则它刚发出 restart 就被杀，回滚逻辑
一行都跑不到。用 `systemd-run` 起一个 transient unit 解决（独立 cgroup）。

**nohup**（`start.sh` 里 `nohup ... &`）
没有 systemd 管，进程自己拉起新进程。但**不能经 `start.sh` 转一手**：
`start.sh` 是 `nohup ... &` 之后立刻退出，Popen 拿到的是那个 bash 的 pid，
bash 一死 `proc.poll()` 就非 None，监督进程会误判成「新进程崩了」。

日志统一写 `logs/wk-switch.log`（部署态）。原先 nohup 模式写的是
`data/logs/server.log`，而 `start.sh` 和 systemd unit 都是往
`logs/wk-switch.log` 追加——重启失败时页面读错文件，日志区一片空白，
用户只能看到「重启失败」四个字，等于把唯一线索扔了。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 作为脚本被子进程调用时（--supervise），项目根目录不在 sys.path 里，
# 补上才能 import 兄弟模块。放在最前面，后面所有 `from wk import ...` 才成立。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
STATE_PATH = os.path.join(DATA_DIR, "restart.json")
LOG_PATH = os.path.join(LOG_DIR, "relaunch.log")
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
#: 部署态的日志位置。`start.sh` 与 systemd unit 都往这里追加，重启出来的
#: 进程也必须写同一个文件，否则 `tail -f logs/wk-switch.log` 看不到重启后的
#: 输出，页面失败时读 `data/logs/server.log` 也是空的。
DEPLOY_LOG = os.path.join(ROOT, "logs", "wk-switch.log")
PID_PATH = os.path.join(ROOT, "wk.pid")
START_SH = os.path.join(ROOT, "start.sh")

from wk import clock  # noqa: E402 — 必须在 sys.path 补好之后

#: 等旧进程让出端口的秒数。正常几十毫秒就释放了，留这么大是给「进程被
#: 系统挂起后恢复」这种慢场景兜底。
PORT_WAIT = 45.0
#: 拉起之后等它开始应答的秒数。上游无依赖，纯粹是 import + sqlite 迁移，
#: 冷启动实测 0.4-1.2 秒；60 秒已经远超任何正常情况。
VERIFY_WAIT = 60.0
#: 状态文件里保留多少条进度。页面只显示最近几条，多了没意义。
STEP_KEEP = 40

_lock = threading.Lock()


# ------------------------------------------------------------------ 状态
def _read(path: str = "") -> dict:
    # 路径在调用时取，不写在默认参数里：默认参数在函数定义时就绑定了，
    # 测试里替换 STATE_PATH 会改不动它。
    path = path or STATE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _patch(**kw) -> dict:
    """读-改-写。只覆盖传进来的键，其余字段（端口、版本、备份名）保留。"""
    with _lock:
        data = _read()
        data.update(kw)
        data["updated"] = time.time()
        steps = data.get("steps")
        if isinstance(steps, list) and len(steps) > STEP_KEEP:
            data["steps"] = steps[-STEP_KEEP:]
        _write(data)
        return data


def _log(text: str) -> None:
    """同时写日志文件和状态里的 steps。

    双写不是冗余：日志文件给人排查用，steps 给页面显示进度用——重启期间
    页面连不上服务，能拿到的只有重启完成后读到的这份状态。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{clock.stamp()}] {text}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    with _lock:
        data = _read()
        steps = data.get("steps")
        if not isinstance(steps, list):
            steps = []
        steps.append({"at": time.time(), "text": text})
        data["steps"] = steps[-STEP_KEEP:]
        data["updated"] = time.time()
        _write(data)


def status() -> dict:
    """给页面读的重启状态。附带日志尾部，失败时不用让用户去翻文件。"""
    data = _read()
    if not data:
        return {"state": "idle", "steps": [], "log_tail": "", "mode": deploy_mode()["mode"]}
    # 读的必须是这次重启真正写的那份日志。原先写死 data/logs/server.log，
    # 而部署态（start.sh / systemd unit）的输出全在 logs/wk-switch.log，
    # 失败时页面拿到的是一段空白，用户只能看到「重启失败」四个字。
    data["log_tail"] = tail(log_path(), 60)
    data.setdefault("mode", deploy_mode()["mode"])
    return data


def mark_ready(port: int = 0, host: str = "") -> None:
    """新进程启动成功后调用，把状态收尾成 ok。

    只在「这次启动是重启拉起来的」时候改写：`state` 处在中间态且发起时间
    在 10 分钟内才认。用户平时手动 `python main.py` 不该覆盖掉上一次失败
    的记录，否则排查线索就没了。
    """
    data = _read()
    if not data:
        return
    if data.get("state") not in ("preparing", "waiting", "starting", "verifying", "rolledback", "failed"):
        return
    started = float(data.get("started") or 0)
    if started and time.time() - started > 600:
        return
    if port and int(data.get("port") or 0) not in (0, port):
        # 端口对不上说明这次起来的不是它拉的那个进程
        return
    _log(f"新进程已就绪（版本 {_version()}，监听 {host or '?'}:{port or '?'}）")
    _patch(state="ok", ready_at=time.time(), ready_pid=os.getpid(),
           ready_version=_version())


def _version() -> str:
    try:
        from wk import version as ver
        return ver.version()
    except Exception:  # noqa: BLE001 — 版本读不到不影响重启
        return ""


def tail(path: str, lines: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        return "".join(rows[-lines:])
    except OSError:
        return ""


# ------------------------------------------------------------------ 端口
def _bindable(port: int, host: str) -> bool:
    """端口现在能不能绑。

    **POSIX 上必须设 SO_REUSEADDR**，否则 TIME_WAIT 会被当成「端口还被占」。
    服务的 socket 是 `ThreadingHTTPServer`，它 `allow_reuse_address = 1`，
    上一个请求的连接由服务端主动关闭、留下约 60 秒 TIME_WAIT 时，它照样能绑；
    裸 socket 不设这个选项就必然 `EADDRINUSE`。探测比现实更严，结论就是
    「旧进程没退出」——而旧进程其实早就没了，用户被指去杀一个不存在的进程。

    **Windows 上不能设**：那边 SO_REUSEADDR 会放过正在 LISTEN 的端口，判据
    直接失效，重启时新进程以为端口空着、绑上去、实际抢不到。

    TIME_WAIT 本身是隔离的：`SO_REUSEADDR` 只放过「对端已关闭的旧连接」，
    挡不住另一个进程的 LISTEN，所以设了它不会把「端口真被占」漏判。
    """
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    addr = "::" if family == socket.AF_INET6 else (host or "127.0.0.1")
    if addr in ("0.0.0.0", ""):
        addr = "0.0.0.0"
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if _posix():
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((addr, port))
            sock.listen(1)
        except OSError:
            return False
        return True


def _alive(pid) -> bool:
    """这个 pid 现在还活着吗。

    Windows 上 **不能** 用 `os.kill(pid, 0)`：那边只认 CTRL_C/CTRL_BREAK，
    其它值会走 TerminateProcess——探测活着的直接把人杀了。所以分开处理。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (res.stdout or b"").decode("utf-8", "replace")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_port(port: int, host: str, timeout: float, dead_pid: int = 0) -> str:
    """等端口能绑。返回 `"ok"` / `"dead"` / `"busy"`。

    `"dead"`：旧进程已经不在了，端口还是绑不上——那不是它占着，是 TIME_WAIT
    之类的东西缠着。真实服务带 `allow_reuse_address`，绑得上，所以这时候
    该继续往下走，不该报「旧进程没有退出」。

    `"busy"`：等满了、旧进程还活着。这才是真的被占。
    """
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _bindable(port, host):
            return "ok"
        if dead_pid and not _alive(dead_pid):
            # 再试一次，避免「刚好在这一刻释放」被漏掉
            if _bindable(port, host):
                return "ok"
            return "dead"
        time.sleep(0.25)
    return "busy" if not dead_pid or _alive(dead_pid) else "dead"


# ------------------------------------------------------------------ 部署形态
#: 上哪儿找 unit。`/run/systemd/system` 里是运行时的，前面几个是安装的。
_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system",
              "/lib/systemd/system", "/usr/lib/systemd/system")
_deploy_cache: dict | None = None


def _posix() -> bool:
    """当前是不是 Unix。

    单独抽出来是为了能被测试驱动：`os.name` 是只读属性，Windows 上没法把
    它改成 posix，Linux 那两条分支（systemd / nohup）就只能靠真机验证。
    有了这个开关，两边逻辑都能在同一个进程里跑测试。
    """
    return os.name != "nt"


def reset_deploy_cache() -> None:
    """测试和「用户刚改过部署方式」时用。"""
    global _deploy_cache
    _deploy_cache = None


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def find_unit() -> str:
    """找出启动本项目的 systemd unit 名。

    不写死服务名：install.sh 用的是 `wk-switch`，但用户可能改过名，也可能
    同一台机器装了两份。按 `WorkingDirectory=<ROOT>` 匹配最准，退一步看
    `ExecStart` 里有没有本项目的 `main.py`。
    """
    if not _posix():
        return ""
    root = os.path.realpath(ROOT)
    for base in _UNIT_DIRS:
        try:
            names = sorted(n for n in os.listdir(base) if n.endswith(".service"))
        except OSError:
            continue
        for name in names:
            body = _read_text(os.path.join(base, name))
            if not body:
                continue
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("WorkingDirectory="):
                    val = line.split("=", 1)[1].strip()
                    if val and os.path.realpath(val) == root:
                        return name[:-len(".service")]
                elif line.startswith("ExecStart="):
                    if os.path.join(root, "main.py") in line:
                        return name[:-len(".service")]
    return ""


def systemd_active(unit: str) -> bool:
    """服务此刻是不是 systemd 在管着且活着。

    unit 文件在、服务却没跑，说明用户是手动 `python main.py` 起的，这时候
    按 systemd 去 restart 会凭空多起一个进程、撞端口。所以必须问一次。
    """
    if not unit or not _posix():
        return False
    try:
        res = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return (res.stdout or b"").strip() in (b"active", b"reloading")


def deploy_mode() -> dict:
    """当前进程是怎么跑起来的：systemd / nohup / plain。

    三个字段：`mode`、`unit`（systemd 时）、`log`（新进程 stdout 写哪）。
    """
    global _deploy_cache
    if _deploy_cache is not None:
        return _deploy_cache

    info = {"mode": "plain", "unit": "", "log": SERVER_LOG}
    if _posix():
        # 部署态一定有 logs/：start.sh 里 mkdir -p，systemd 的日志也在那儿。
        # 开发态只有 data/logs/。用目录存在与否区分，比读配置文件可靠。
        if os.path.isdir(os.path.dirname(DEPLOY_LOG)):
            info["log"] = DEPLOY_LOG
        unit = find_unit()
        if unit and systemd_active(unit):
            info.update(mode="systemd", unit=unit, log=DEPLOY_LOG)
        elif os.path.isfile(START_SH):
            info.update(mode="nohup", log=DEPLOY_LOG)
    _deploy_cache = info
    return info


def log_path() -> str:
    """新进程的 stdout/stderr 落到哪个文件。

    必须和部署方式用的日志是同一个：`start.sh` 和 systemd unit 都往
    `logs/wk-switch.log` 追加，重启出来的进程写到别处，用户 `tail` 的时候
    就「重启后没有任何输出」，页面失败时读的也是空的。
    """
    return deploy_mode()["log"]


# ------------------------------------------------------------------ 启动
def _detach_flags() -> int:
    """子进程脱离当前控制台的 flags。

    Windows 上 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 这个组合是坏的：
    NEW_PROCESS_GROUP 要有控制台，配上 DETACHED 子进程会直接起不来（0xC0000142）。
    正确写法是 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`——不建窗口、但
    有自己的进程组，父进程 Ctrl+C 或窗口关闭都带不走它。
    """
    if os.name != "nt":
        return 0
    return 0x08000000 | 0x00000200   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _supervisor_args() -> list[str]:
    return [sys.executable, os.path.join(ROOT, "wk", "relaunch.py"), "--supervise"]


def _spawn_systemd_run(unit_name: str) -> dict:
    """用 systemd-run 把监督进程扔进独立的 cgroup。

    这是 Linux systemd 部署能自愈的**前提**：服务的 `KillMode` 默认是
    `control-group`，`systemctl restart` 会 SIGTERM 掉整个 cgroup。监督进程
    要是也在里面，它刚发出 restart 就被自己一起杀掉，回滚逻辑一行都跑不到，
    表现就是「升级失败后服务再没起来」。
    """
    cmd = ["systemd-run", "--collect", "--no-block", "--quiet",
           "--unit", unit_name,
           "--working-directory", ROOT,
           "--setenv", "PYTHONUNBUFFERED=1",
           "--setenv", "WK_SUPERVISOR=1",
           *_supervisor_args()]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"systemd-run 起不来：{exc}"}
    if res.returncode == 0:
        return {"ok": True, "via": "systemd-run", "unit": unit_name}
    err = (res.stderr or b"").decode("utf-8", "replace").strip()
    return {"ok": False, "error": err.splitlines()[-1][:200] if err else
            f"systemd-run 退出码 {res.returncode}"}


def kick_systemd(unit: str) -> dict:
    """让 systemd 重启服务。`--no-block` 是为了不被自己这条命令卡住。

    调用方必须**先发完 HTTP 响应**再调它：systemd 停服务时会 SIGTERM 整个
    cgroup（除非 unit 里写了 `KillMode=process`），当前进程就在里面。

    返回 `{"ok": bool, "error": str}`——失败原因要能传到页面上，
    「systemctl 没成功」这种话等于没说。
    """
    if not unit:
        return {"ok": False, "error": "没有拿到服务名"}
    try:
        res = subprocess.run(["systemctl", "--no-block", "restart", unit],
                             capture_output=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"systemctl restart {unit} 执行失败：{exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    err = (res.stderr or b"").decode("utf-8", "replace").strip()
    _log(f"systemctl --no-block restart {unit} → 退出码 {res.returncode}"
         + (f"（{err}）" if err else ""))
    return {"ok": res.returncode == 0,
            "error": err.splitlines()[-1][:200] if err else ""}


def spawn(port: int, host: str = "127.0.0.1", target_version: str = "",
          backup: str = "", from_version: str = "") -> dict:
    """旧进程调用：写好状态、拉起监督进程，然后调用方自己退出。

    这里**不等**新进程起来——等的话页面请求就超时了。监督进程负责剩下的事，
    结果落在 data/restart.json 里。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    dep = deploy_mode()
    now = time.time()
    state = {
        "state": "preparing",
        "port": int(port),
        "host": host or "127.0.0.1",
        "pid": os.getpid(),
        "mode": dep["mode"],
        "unit": dep.get("unit") or "",
        "from": from_version or _version(),
        "to": target_version or "",
        "backup": backup or "",
        "started": now,
        "updated": now,
        "steps": [{"at": now, "text": f"发起重启（{dep['mode']} 部署），拉起监督进程"}],
        "server_log": log_path(),
    }
    _write(state)

    if dep["mode"] == "systemd":
        if shutil.which("systemd-run"):
            got = _spawn_systemd_run(f"wk-relaunch-{os.getpid()}")
            if got.get("ok"):
                _log("监督进程已在独立 cgroup 里启动")
                return {"ok": True, "state": "preparing", "port": int(port),
                        "via": "systemd-run", "unit": got.get("unit"),
                        "message": "监督进程已拉起（独立 cgroup），正在重启"}
            _log(f"systemd-run 不可用：{got.get('error')}")

        # 降级：监督进程只能待在服务自己的 cgroup 里，systemd 一停服务就会
        # 把它一起 SIGTERM 掉，回滚逻辑一行都跑不到。与其让页面干等 60 秒
        # 再报一个必然的失败，不如让旧进程直接按下重启键，前端靠
        # `/api/ping` 判就绪——没有自动回滚，但有确定的结局。
        _log("没有独立 cgroup 可用，改为由 systemd 直接重启（无自动回滚）")
        return {"ok": True, "state": "preparing", "port": int(port),
                "via": "systemctl-direct", "degraded": True,
                "unit": dep.get("unit") or "",
                "message": "由 systemd 直接重启"}

    log = open(LOG_PATH, "ab", buffering=0)
    try:
        subprocess.Popen(
            _supervisor_args(),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=_detach_flags(),
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        _patch(state="failed", error=f"监督进程没起来：{exc}")
        return {"ok": False, "error": f"监督进程没起来：{exc}"}
    finally:
        log.close()
    return {"ok": True, "state": "preparing", "port": int(port), "via": "popen",
            "message": "监督进程已拉起，正在重启"}


def _launch(state: dict):
    """拉起新进程。返回 (Popen|None, 说明)。

    两种部署方式分开走：

    - **systemd**：`systemctl --no-block restart <unit>`。进程归 systemd 管，
      `is-active`、日志、开机自启才对得上。**绝不能**在这里自己 Popen：那样
      端口上有服务、systemd 却认为服务已停，下次 `systemctl restart` 会再起
      一个，抢不到端口就往后漂。
    - **nohup / plain**：直接 Popen python。**不经 `start.sh`**——它是
      `nohup ... &` 后立刻退出，Popen 拿到的是那个 bash 的 pid，bash 一死
      `proc.poll()` 就非 None，监督进程会误判成「新进程崩了」。这是我们自己
      写 `wk.pid`、日志写 `logs/wk-switch.log`，效果和 `start.sh` 一致，
      `stop.sh` 照样停得掉。
    """
    port = int(state["port"])
    host = str(state.get("host") or "127.0.0.1")
    dep = deploy_mode()
    os.makedirs(LOG_DIR, exist_ok=True)

    if dep["mode"] == "systemd":
        unit = str(state.get("unit") or dep.get("unit") or "")
        if not unit:
            raise OSError("检测到 systemd 部署，但找不到对应的服务名")
        got = kick_systemd(unit)
        if not got["ok"]:
            raise OSError(got.get("error") or f"systemctl restart {unit} 返回非 0")
        return None, f"systemctl restart {unit}"

    # --wait-port：监督进程已经确认端口空了，这里只起保险作用。等不到就
    # SystemExit，绝不往后挪端口——端口一漂书签和反代全失联。
    args = [sys.executable, os.path.join(ROOT, "main.py"), str(port),
            "--host", host, "--no-browser", "--wait-port", "10"]
    # 日志不丢：起不来的原因就在这个文件里，页面失败时会读它。
    # PYTHONUNBUFFERED 必须开——关掉缓冲前，进程崩溃时 stdout 还压在管道里，
    # 日志文件是空的，等于又把「失败原因」扔了。
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log = open(path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=_detach_flags(),
            start_new_session=(os.name != "nt"),
        )
    finally:
        log.close()

    # 写 wk.pid：部署态的 `stop.sh` 只认这个文件，没有它就只能靠端口扫描兜底。
    if _posix() and os.path.isdir(ROOT):
        try:
            with open(PID_PATH, "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
        except OSError:
            pass
    return proc, "main.py"


def _ping(port: int, timeout: float = 2.0) -> dict:
    """打 /api/ping。免鉴权、只回版本号，专门给探活用。

    强制直连：环境里的 HTTPS_PROXY 指向本地代理时，urllib 会把 127.0.0.1
    也塞给代理，探活就永远失败。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/api/ping"
    try:
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return {}


def _wait_alive(port: int, want: str, timeout: float, proc=None,
                grace: float = 8.0) -> tuple[bool, dict]:
    """等新进程应答，并确认它跑的是目标版本。

    只判「端口通了」不够：旧进程残留、或者启动的是备份里的旧代码，都会让
    端口通而版本不对。版本对不上按失败处理，交给回滚。

    `proc` 退出**不能立刻判死**。原先那版是 `poll()` 非 None 就只再等 1 秒
    然后返回失败——Linux 上服务是 `nohup ... &` 或 `systemctl` 拉起的，
    Popen 拿到的往往是个包装进程（bash），它交棒完就退，真正干活的 python
    还在 import。1 秒够不着，于是每一轮升级都被判成「新版本没有起来」，
    百分百触发回滚。现在给 `grace` 秒宽限，只认 ping 和版本号。
    """
    deadline = time.time() + max(0.0, timeout)
    last: dict = {}
    died_at = 0.0
    while time.time() < deadline:
        last = _ping(port)
        if last.get("ok"):
            got = str(last.get("version") or "")
            if not want or got == want:
                return True, last
            # 版本对不上：可能是旧进程还占着端口，给它几秒再判
        if proc is not None and proc.poll() is not None:
            if not died_at:
                died_at = time.time()
            elif time.time() - died_at > grace:
                # 进程退了、宽限也给完了，最后一探还是不通才算失败
                last = _ping(port)
                if last.get("ok") and (not want or str(last.get("version") or "") == want):
                    return True, last
                return False, last
        time.sleep(0.4)
    return False, last


# ------------------------------------------------------------------ 自检
def selfcheck() -> str:
    """用刚替换上去的代码跑一次 import + 读版本。返回空串表示通过。

    挡的是「包里 server.py 有语法错 / 少了个模块」这种一眼能看出来、却要等
    重启白等 60 秒再回滚才暴露的问题。花 0.4 秒，把 60 秒的失败窗口和一次
    拿不到服务的空窗换掉，很划算。
    """
    code = ("import sys; sys.path.insert(0, %r);"
            "import server; from wk import version;"
            "sys.stdout.write(version.version())" % ROOT)
    try:
        res = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                             stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"自检没跑起来：{exc}"
    if res.returncode == 0:
        return ""
    err = (res.stderr or b"").decode("utf-8", "replace").strip()
    if not err:
        return f"自检退出码 {res.returncode}"
    # 最后一行才是真正的错误（SyntaxError / ImportError），前面都是栈
    return err.splitlines()[-1][:300]


def revert(failed_version: str, backup: str, from_version: str) -> dict:
    """把代码换回备份，不动进程。

    自检没过时用这条：新代码根本 import 不了，没必要再走一遍「拉起 → 等
    60 秒 → 回滚」，原地换回去再重启就行。
    """
    state = {"to": failed_version, "backup": backup, "from": from_version}
    ok = _rollback(state)
    if ok:
        # 换回去的时候 _install_zip 会把「刚才那份坏代码」也备份成
        # code-<坏版本>-*.zip。不标一下，它就一直躺在候选里，下次按文件名
        # 挑备份的人（或者半年后的自己）会挑中它。
        _mark_broken(failed_version)
    return {"ok": ok}


# ------------------------------------------------------------------ 回滚
def _rollback(state: dict) -> bool:
    """用升级前的备份把代码换回去。失败返回 False。"""
    from wk import update as updater

    backup = str(state.get("backup") or "")
    path = os.path.join(updater.BACKUP_DIR, backup) if backup else ""
    if not backup or not os.path.isfile(path):
        # 备份名可能没传下来（早期版本的升级路径），退到最新的非当前版本备份
        path = _latest_backup(str(state.get("to") or ""), str(state.get("from") or ""))
    if not path or not os.path.isfile(path):
        _log("回滚失败：找不到升级前的备份包")
        return False
    try:
        with open(path, "rb") as f:
            blob = f.read()
        _log(f"开始回滚：{os.path.basename(path)}")
        updater._install_zip(blob, str(state.get("from") or _version()), _log)
        return True
    except Exception as exc:  # noqa: BLE001 — 回滚失败要留证据，不能吞
        _log(f"回滚出错：{type(exc).__name__}: {exc}")
        return False


def _latest_backup(exclude_version: str, prefer_version: str = "") -> str:
    """在 backups/ 里找一份可用的代码备份。

    两条硬规则，都是被坑出来的：

    1. **优先 `code-<prefer_version>-*.zip`**，那正是升级前那一版；
    2. **只认版本号低于 exclude_version 的备份**。回滚失败后，`_install_zip`
       会把「坏代码」也备份成 `code-9.9.9-*.zip`；按修改时间挑最新的，下一次
       回滚就会挑回那个坏包，原地打转。按版本号卡住，坏版本永远进不了候选。
    """
    from wk import update as updater
    from wk import version as ver

    try:
        names = [n for n in os.listdir(updater.BACKUP_DIR)
                 if n.startswith("code-") and n.endswith(".zip")]
    except OSError:
        return ""
    if not names:
        return ""

    if prefer_version:
        hits = sorted(n for n in names if n.startswith(f"code-{prefer_version}-"))
        if hits:
            return os.path.join(updater.BACKUP_DIR, hits[-1])

    ceiling = ver.parse_semver(exclude_version) if exclude_version else None
    usable = []
    for name in names:
        match = re.match(r"code-(\d+\.\d+\.\d+)-", name)
        if not match:
            continue
        sem = ver.parse_semver(match.group(1))
        if ceiling and sem >= ceiling:
            continue
        usable.append((sem, os.path.getmtime(os.path.join(updater.BACKUP_DIR, name)), name))
    if not usable:
        return ""
    # 按「版本号优先、时间次之」取最后一个：回滚要退到**离当前最近的那一版**，
    # 不能因为目录里躺着一个一年前的备份就把代码退回去。
    usable.sort()
    return os.path.join(updater.BACKUP_DIR, usable[-1][2])


# ------------------------------------------------------------------ 监督进程
def supervise() -> int:
    state = _read()
    if not state or not state.get("port"):
        print("没有找到重启状态文件，无事可做", file=sys.stderr)
        return 2

    port = int(state["port"])
    host = str(state.get("host") or "127.0.0.1")
    want = str(state.get("to") or "")
    dep = deploy_mode()
    systemd = dep["mode"] == "systemd"
    _log(f"监督进程启动：{dep['mode']} 部署，等待 {host}:{port} 释放"
         f"（旧 PID {state.get('pid')}）")

    _patch(state="waiting")
    if systemd:
        # systemd 自己负责「停 → 等 → 起」。这里插一手去等端口只会跟它的
        # `Restart=` 打架：它按策略刚拉起一个，我们判定「端口还被占」再去
        # kill，它又拉一个，死循环到 failed。停和起都交给它，失败原因在
        # journalctl 里，比我们瞎猜准。
        pass
    else:
        old_pid = int(state.get("pid") or 0)
        got = _wait_port(port, host, PORT_WAIT, dead_pid=old_pid)
        if got == "dead":
            # 旧进程早没了，端口绑不上只是 TIME_WAIT 缠着。真实服务是
            # ThreadingHTTPServer（allow_reuse_address=1），绑得上。停在这里
            # 报「旧进程没有退出」，等于让用户去杀一个不存在的进程。
            _log(f"旧进程 {old_pid} 已退出；端口 {port} 绑不上是 TIME_WAIT，"
                 f"直接拉起新进程")
        elif got == "busy":
            _log(f"端口 {port} 在 {PORT_WAIT:.0f}s 内没有释放，旧进程仍在，结束它")
            _terminate(old_pid, port)
            again = _wait_port(port, host, 12, dead_pid=old_pid)
            if again == "busy":
                _patch(state="failed",
                       error=f"旧进程 {old_pid} 仍活着，端口 {port} 被占用")
                _log("重启失败：旧进程没有退出，端口被占用")
                return 1
            if again == "dead":
                _log("旧进程已结束；端口只剩 TIME_WAIT，继续拉起")

    _patch(state="starting")
    try:
        proc, mode = _launch(state)
    except OSError as exc:
        _patch(state="failed", error=f"拉起新进程失败：{exc}")
        _log(f"拉起失败：{exc}")
        return 1
    who = f"PID {proc.pid}" if proc is not None else "（交给 systemd 拉起）"
    _log(f"已用 {mode} 拉起新进程 {who}，等待应答")

    _patch(state="verifying")
    ok, info = _wait_alive(port, want, VERIFY_WAIT, proc)
    if ok:
        _log(f"新版本已应答：v{info.get('version')}")
        _patch(state="ok", ready_at=time.time(), ready_pid=info.get("pid"),
               ready_version=str(info.get("version") or ""))
        return 0

    _log("新进程没有在限定时间内应答，准备回滚")
    _patch(state="failed", error="新版本没有起来")
    if not _rollback(state):
        _log("没有可用的备份，停在这里；日志见 logs/")
        return 1

    _patch(state="rolledback", error="")
    _mark_broken(str(state.get("to") or ""))
    _log("已回滚，重新拉起旧版本")
    if not systemd:
        # systemd 模式下不用插手动刀：`systemctl restart` 自己会先停掉那个
        # 还在磨蹭的新版本（TERM，超时 KILL），再把旧版本起来。
        old = int(getattr(proc, "pid", 0) or 0)
        if _wait_port(port, host, 20, dead_pid=old) == "busy":
            _terminate(old, port)
            _wait_port(port, host, 15)
    try:
        proc, mode = _launch(state)
    except OSError as exc:
        _log(f"回滚后拉起失败：{exc}")
        _patch(state="failed", error=f"回滚后仍起不来：{exc}")
        return 1
    ok, info = _wait_alive(port, "", VERIFY_WAIT, proc)
    if ok:
        _log(f"已回滚到 v{info.get('version')}，服务恢复")
        _patch(state="ok", rolled_back=True, ready_at=time.time(),
               ready_version=str(info.get("version") or ""))
        return 0
    _patch(state="failed", error="回滚后服务仍未起来")
    _log("回滚后仍然起不来，需要人工介入")
    return 1


def _mark_broken(failed_version: str) -> None:
    """把「坏版本」的备份改名，别让它留在候选里当回滚目标。

    `_latest_backup` 已经按版本号挡了一层，这里再加一层：文件真躺在
    backups/ 里，下一个人（或者半年后的自己）按文件名挑的时候不会挑错。
    """
    if not failed_version:
        return
    from wk import update as updater

    try:
        names = [n for n in os.listdir(updater.BACKUP_DIR)
                 if n.startswith(f"code-{failed_version}-") and n.endswith(".zip")]
    except OSError:
        return
    for name in names:
        src = os.path.join(updater.BACKUP_DIR, name)
        try:
            os.replace(src, src + ".broken")
        except OSError:
            pass


def _pids_on_port(port: int) -> list[int]:
    """端口上现在是谁。pid 文件不可信时的唯一抓手。

    `wk.pid` 会被 `stop.sh` 删、会被别的进程复用、也可能根本没写成；
    端口扫描不看这些，谁在 LISTEN 就是谁。非 root 看不到别人的 pid，
    拿不到就返回空，由调用方决定要不要报错。
    """
    if not _posix() or not port:
        return []
    found: list[int] = []
    for cmd in (["ss", "-ltnp"], ["lsof", "-ti", f"tcp:{port}"]):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=6)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (res.stdout or b"").decode("utf-8", "replace")
        if res.returncode != 0 or not text.strip():
            continue
        if cmd[0] == "lsof":
            found += [int(x) for x in text.split() if x.strip().isdigit()]
        else:
            found += [int(m) for m in re.findall(r"pid=(\d+)", text)]
        if found:
            break
    return sorted({p for p in found if p > 0 and p != os.getpid()})


def _terminate(pid=None, port: int = 0) -> None:
    """温柔地结束一个进程。失败就算了，调用方会再判端口。"""
    victims: list[int] = []
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and pid != os.getpid():
        victims.append(pid)
    victims += [p for p in _pids_on_port(port) if p not in victims]
    if not victims:
        return

    for victim in victims:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(victim), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                os.kill(victim, 15)
        except (OSError, subprocess.SubprocessError):
            pass

    # 15 秒的 TERM 没反应就 KILL。只对 Unix：Windows 那条已经 /F 了。
    if os.name == "nt":
        return
    time.sleep(1.5)
    for victim in victims:
        try:
            os.kill(victim, 0)      # 还在？
            os.kill(victim, 9)
        except OSError:
            pass


if __name__ == "__main__":
    if "--supervise" in sys.argv:
        raise SystemExit(supervise())
    print("用法：python wk/relaunch.py --supervise（由升级流程自动调用）")
    raise SystemExit(2)
