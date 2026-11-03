"""启动器：起本地服务并打开浏览器。

用法：
    python main.py                      # 默认 8765 端口，只绑 127.0.0.1
    python main.py 9000                 # 指定端口
    python main.py --no-browser         # 只起服务（部署脚本用这个）
    python main.py --host 0.0.0.0       # 对外监听（容器 / 反代场景）
    python main.py 8765 --wait-port 25  # 升级重启：等端口释放最多 25 秒

端口被占用时会自动往后找（最多 20 个），不会因为上次没关干净就起不来。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

import server
from wk import version


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口能不能真的绑上。

    这里**不设** SO_REUSEADDR：Linux 上它只影响 TIME_WAIT，Windows 上却
    允许绑定一个正在 LISTEN 的端口，会把「已被占用」误判成「空闲」，
    于是升级重启时新进程以为 8765 空着、绑上去、实际抢不到而退出。
    不设它，Windows 的 bind 遇到已监听端口会直接报 WSAEADDRINUSE。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            sock.listen(1)
        except OSError:
            return False
        return True


def find_port(start: int, host: str = "127.0.0.1", tries: int = 20,
              wait: float = 0) -> int:
    """找一个能绑的端口。

    wait > 0 是升级重启场景：此时**只等 start 这一个端口**，不往后找。
    旧进程还在退出的那零点几秒里，8765 被占着而 8766 空着，一往后找
    就绑到 8766 了——用户的书签、反向代理、防火墙规则全都指着 8765，
    端口一漂就等于服务失联。真等不到（比如被别的程序长期占着）才退回
    往后找，避免彻底起不来。
    """
    deadline = time.time() + max(0.0, wait)
    if wait > 0:
        while time.time() < deadline:
            if port_free(start, host):
                return start
            time.sleep(0.2)

    for offset in range(tries):
        port = start + offset
        if port_free(port, host):
            return port
    raise SystemExit(f"从 {start} 起连续 {tries} 个端口都被占用")


def main() -> int:
    parser = argparse.ArgumentParser(prog="wk-switch", add_help=True)
    parser.add_argument("port", nargs="?", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--host", default=os.environ.get("WK_HOST") or "127.0.0.1",
                        help="监听地址，默认 127.0.0.1；对外暴露用 0.0.0.0")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--wait-port", type=float, default=0,
                        help="等端口释放的秒数（升级重启用）")
    parser.add_argument("--version", action="version", version=f"wk-switch {version.version()}")
    args = parser.parse_args()

    host = args.host
    port = find_port(args.port, host=host, wait=args.wait_port)
    httpd = server.serve(host=host, port=port)
    # 绑 0.0.0.0 时本机访问仍用回环地址，别把 0.0.0.0 塞进浏览器
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}"

    print("=" * 58)
    print(f"  wk-switch v{version.version()} · 跨项目账户导入导出转换")
    print(f"  监听：{host}:{port}")
    print(f"  地址：{url}")
    print("  停止：Ctrl+C")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
