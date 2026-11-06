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

## 进程归属

Linux 上如果根目录有部署脚本生成的 `start.sh`，就用它拉起新进程：它负责
写 `wk.pid`、把日志追到 `logs/wk-switch.log`，跟用户平时 `./start.sh` 起
来的完全一致。否则直接 `python main.py`，日志写 `data/logs/server.log`。
不这么做的话，重启出来的进程 `stop.sh` 停不掉，下次升级又撞端口。
"""

from __future__ import annotations

import json
import os
import re
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
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n"
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
        return {"state": "idle", "steps": [], "log_tail": ""}
    data["log_tail"] = tail(os.path.join(LOG_DIR, "server.log"), 40)
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

    不设 SO_REUSEADDR：Windows 上它会把「正在 LISTEN」误判成空闲，那就失去
    了唯一的判据。这条和 `main.port_free` 是同一个理由，两边不能各写一套。
    """
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    addr = "::" if family == socket.AF_INET6 else (host or "127.0.0.1")
    if addr in ("0.0.0.0", ""):
        addr = "0.0.0.0"
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((addr, port))
            sock.listen(1)
        except OSError:
            return False
        return True


def _wait_port(port: int, host: str, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _bindable(port, host):
            return True
        time.sleep(0.25)
    return False


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


def spawn(port: int, host: str = "127.0.0.1", target_version: str = "",
          backup: str = "", from_version: str = "") -> dict:
    """旧进程调用：写好状态、拉起监督进程，然后调用方自己退出。

    这里**不等**新进程起来——等的话页面请求就超时了。监督进程负责剩下的事，
    结果落在 data/restart.json 里。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    now = time.time()
    state = {
        "state": "preparing",
        "port": int(port),
        "host": host or "127.0.0.1",
        "pid": os.getpid(),
        "from": from_version or _version(),
        "to": target_version or "",
        "backup": backup or "",
        "started": now,
        "updated": now,
        "steps": [{"at": now, "text": "发起重启，拉起监督进程"}],
        "server_log": SERVER_LOG,
    }
    _write(state)

    args = [sys.executable, os.path.join(ROOT, "wk", "relaunch.py"), "--supervise"]
    log = open(LOG_PATH, "ab", buffering=0)
    try:
        subprocess.Popen(
            args,
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

    return {"ok": True, "state": "preparing", "port": int(port),
            "message": "监督进程已拉起，正在重启"}


def _launch(state: dict):
    """拉起新进程。返回 (Popen|None, 说明)。

    Linux 有 start.sh 就走它：它写 wk.pid、日志追加到 logs/wk-switch.log，
    和用户平时启动的方式一致，`stop.sh` 还能停掉。Windows / 无 start.sh
    就直接起 python，日志进 data/logs/server.log。
    """
    port = int(state["port"])
    host = str(state.get("host") or "127.0.0.1")
    start_sh = os.path.join(ROOT, "start.sh")
    os.makedirs(LOG_DIR, exist_ok=True)

    if os.name != "nt" and os.path.isfile(start_sh):
        env = dict(os.environ)
        env["WK_HOST"] = host
        env["PYTHONUNBUFFERED"] = "1"
        log = open(LOG_PATH, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                ["bash", start_sh, str(port)],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, close_fds=True,
                start_new_session=True,
            )
        finally:
            log.close()
        return proc, "start.sh"

    args = [sys.executable, os.path.join(ROOT, "main.py"), str(port),
            "--host", host, "--no-browser", "--wait-port", "10"]
    # 日志不丢：起不来的原因就在这个文件里，页面失败时会读它。
    # PYTHONUNBUFFERED 必须开——关掉缓冲前，进程崩溃时 stdout 还压在管道里，
    # 日志文件是空的，等于又把「失败原因」扔了。
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    log = open(SERVER_LOG, "ab", buffering=0)
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


def _wait_alive(port: int, want: str, timeout: float, proc=None) -> tuple[bool, dict]:
    """等新进程应答，并确认它跑的是目标版本。

    只判「端口通了」不够：旧进程残留、或者启动的是备份里的旧代码，都会让
    端口通而版本不对。版本对不上按失败处理，交给回滚。
    """
    deadline = time.time() + max(0.0, timeout)
    last = {}
    while time.time() < deadline:
        last = _ping(port)
        if last.get("ok"):
            got = str(last.get("version") or "")
            if not want or got == want:
                return True, last
            # 版本对不上：可能是旧进程还占着端口，给它几秒再判
        if proc is not None and proc.poll() is not None:
            # 直接起的子进程已经退了，端口再怎么等都是它自己起来过又死的
            time.sleep(1.0)
            last = _ping(port)
            if last.get("ok") and (not want or str(last.get("version") or "") == want):
                return True, last
            return False, last
        time.sleep(0.4)
    return False, last


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
    _log(f"监督进程启动：等待 {host}:{port} 释放（旧 PID {state.get('pid')}）")

    _patch(state="waiting")
    if not _wait_port(port, host, PORT_WAIT):
        _log(f"端口 {port} 在 {PORT_WAIT:.0f}s 内没有释放，尝试结束旧进程")
        _terminate(state.get("pid"))
        if not _wait_port(port, host, 12):
            _patch(state="failed", error=f"旧进程没有退出，端口 {port} 仍被占用")
            _log("重启失败：端口被占，无法拉起新进程")
            return 1

    _patch(state="starting")
    try:
        proc, mode = _launch(state)
    except OSError as exc:
        _patch(state="failed", error=f"拉起新进程失败：{exc}")
        _log(f"拉起失败：{exc}")
        return 1
    _log(f"已用 {mode} 拉起新进程（PID {getattr(proc, 'pid', '?')}），等待应答")

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
        _log("没有可用的备份，停在这里；日志见 data/logs/")
        return 1

    _patch(state="rolledback", error="")
    _mark_broken(str(state.get("to") or ""))
    _log("已回滚，重新拉起旧版本")
    if not _wait_port(port, host, 20):
        _terminate(getattr(proc, "pid", None))
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


def _terminate(pid) -> None:
    """温柔地结束一个进程。失败就算了，调用方会再判端口。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    if "--supervise" in sys.argv:
        raise SystemExit(supervise())
    print("用法：python wk/relaunch.py --supervise（由升级流程自动调用）")
    raise SystemExit(2)
