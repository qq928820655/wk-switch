"""启动器：起本地服务并打开浏览器。

用法：
    python main.py                      # 默认 8765 端口
    python main.py 9000                 # 指定端口
    python main.py --no-browser         # 只起服务（部署脚本用这个）
    python main.py 8765 --wait-port 25  # 升级重启：等端口释放最多 25 秒

端口被占用时会自动往后找（最多 20 个），不会因为上次没关干净就起不来。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

import server
from wk import version


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def find_port(start: int, host: str = "127.0.0.1", tries: int = 20,
              wait: float = 0) -> int:
    """找一个能绑的端口。

    wait > 0 时先原地等端口释放：升级重启场景下旧进程还没退干净，
    立刻往后找端口会让用户的书签指向新端口。
    """
    deadline = time.time() + max(0.0, wait)
    while True:
        for offset in range(tries):
            port = start + offset
            if port_free(port, host):
                return port
        if time.time() >= deadline:
            raise SystemExit(f"从 {start} 起连续 {tries} 个端口都被占用")
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(prog="wk-switch", add_help=True)
    parser.add_argument("port", nargs="?", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--wait-port", type=float, default=0,
                        help="等端口释放的秒数（升级重启用）")
    parser.add_argument("--version", action="version", version=f"wk-switch {version.version()}")
    args = parser.parse_args()

    port = find_port(args.port, wait=args.wait_port)
    httpd = server.serve(port=port)
    url = f"http://127.0.0.1:{port}"

    print("=" * 58)
    print(f"  wk-switch v{version.version()} · 跨项目账户导入导出转换")
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
