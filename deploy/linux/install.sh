#!/usr/bin/env bash
# wk-switch Linux 一键部署 / 更新
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/qq928820655/wk-switch/main/deploy/linux/install.sh | sudo bash
#   sudo bash install.sh                 # 本地运行，进菜单
#   sudo bash install.sh --yes           # 全自动（已装则更新，未装则安装）
#   sudo bash install.sh --token 你的口令 --port 8765
#   sudo bash install.sh --from-file dist/wk-switch-1.0.1.zip   # 离线包
#   sudo bash install.sh --proxy http://127.0.0.1:7890          # 走代理下载
#   sudo bash install.sh --mirror https://ghfast.top            # 走 GitHub 镜像
#
# 通过管道执行时 stdin 是脚本本身，菜单从 /dev/tty 读，所以 curl | bash 也能交互。

set -o pipefail

APP="wk-switch"
# 打包时由 build_release.py 整体替换成真实 owner/name；从 raw 直拉时它仍是占位符，
# resolve_repo 会自己兜底，所以两种分发方式都能跑
REPO_PLACEHOLDER="__REPO__"
FALLBACK_REPO="qq928820655/wk-switch"
HOME_DIR="${WK_HOME:-/opt/wk-switch}"
SERVICE="/etc/systemd/system/${APP}.service"
PORT="${WK_PORT:-8765}"
HOST="${WK_HOST:-127.0.0.1}"
TOKEN="${WK_TOKEN:-qq928820655}"
PROXY="${WK_PROXY:-}"
MIRROR="${WK_MIRROR:-}"
ASSUME_YES=0
FROM_FILE=""
ACTION=""

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
info() { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '%s✗%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

# ---------- 参数 ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)     ASSUME_YES=1 ;;
    --token)      TOKEN="${2:-}"; shift ;;
    --port)       PORT="${2:-}"; shift ;;
    --host)       HOST="${2:-}"; shift ;;
    --home)       HOME_DIR="${2:-}"; shift ;;
    --repo)       REPO="${2:-}"; shift ;;
    --proxy)      PROXY="${2:-}"; shift ;;
    --mirror)     MIRROR="${2:-}"; shift ;;
    --from-file)  FROM_FILE="${2:-}"; shift ;;
    --install)    ACTION="install" ;;
    --update)     ACTION="update" ;;
    --uninstall)  ACTION="uninstall" ;;
    --status)     ACTION="status" ;;
    -h|--help)    sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
  shift
done

need_root() {
  [ "$(id -u)" = "0" ] || die "需要 root：请用 sudo 运行"
}

have() { command -v "$1" >/dev/null 2>&1; }

# 仓库名三级兜底：命令行 --repo > WK_REPO > 打包时替换的占位 > 从 raw 地址解析 > 内置默认。
# 直接从 raw.githubusercontent.com 拉这个脚本时占位符不会被替换，必须能自己认出来。
resolve_repo() {
  local src
  # 真实 owner/name 一定含斜杠；打包前的 __REPO__ 不含，据此识别"没被替换"。
  # 用含斜杠判断而不是比对占位串，是因为打包脚本会全局替换 __REPO__，
  # 一旦这段判断里出现该字面量，替换后逻辑本身就坏了。
  if [ -n "${REPO:-}" ] && [ "${REPO#*/}" != "$REPO" ]; then printf '%s' "$REPO"; return; fi
  if [ -n "${WK_REPO:-}" ]; then printf '%s' "$WK_REPO"; return; fi
  if [ -n "${REPO_PLACEHOLDER:-}" ] && [ "${REPO_PLACEHOLDER#*/}" != "$REPO_PLACEHOLDER" ]; then
    printf '%s' "$REPO_PLACEHOLDER"; return
  fi

  src="${WK_SCRIPT_URL:-${BASH_SOURCE[0]:-}}"
  case "$src" in
    *raw.githubusercontent.com/*/*/*)
      src="${src#*raw.githubusercontent.com/}"
      printf '%s/%s' "${src%%/*}" "$(printf '%s' "${src#*/}" | cut -d/ -f1)"
      return ;;
  esac
  printf '%s' "$FALLBACK_REPO"
}
REPO="$(resolve_repo)"

# ---------- 环境检查 ----------
check_env() {
  have python3 || die "缺少 python3，请先安装：apt install -y python3 / yum install -y python3"
  local ver
  ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$ver" in
    3.8|3.9|3.1*) : ;;
    *) die "Python 版本过低（$ver），需要 3.8+" ;;
  esac
  have curl || have wget || die "缺少 curl / wget"
  ok "环境检查通过（Python $ver）"
}

installed() { [ -f "$HOME_DIR/server.py" ] && [ -d "$HOME_DIR/wk" ]; }

current_version() {
  if [ -f "$HOME_DIR/VERSION" ]; then
    tr -d ' \n' < "$HOME_DIR/VERSION"
  elif [ -f "$HOME_DIR/wk/version.py" ]; then
    sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HOME_DIR/wk/version.py" | head -1
  else
    echo "未知"
  fi
}

# ---------- 下载 ----------
# 镜像前缀（如 https://ghfast.top）只拼在 release 资产地址前面，API 请求不走镜像
mirror_url() {
  if [ -n "$MIRROR" ]; then printf '%s/%s' "${MIRROR%/}" "$1"; else printf '%s' "$1"; fi
}

apply_proxy() {
  [ -n "$PROXY" ] || return 0
  export https_proxy="$PROXY" http_proxy="$PROXY" all_proxy="$PROXY"
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" ALL_PROXY="$PROXY"
  info "使用代理：$PROXY"
}

download() { # $1=url $2=输出路径
  if have curl; then
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 -o "$2" "$1" && return 0
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 "$1" 2>/dev/null)"
    warn "下载失败（HTTP ${code:-无响应}）：$1"
    return 1
  fi
  wget -q -O "$2" "$1"
}

fetch_latest() { # 输出 zip 下载地址
  local api tmp url
  api="https://api.github.com/repos/${REPO}/releases/latest"
  tmp="$(mktemp)"
  if ! download "$api" "$tmp"; then
    rm -f "$tmp"
    die "取版本信息失败：$api（检查仓库名 ${REPO} 与网络；可加 --repo owner/name 指定）"
  fi
  # 不依赖 jq：抓 zip 资产地址，优先主包 wk-switch-x.y.z.zip，排除 windows 专用包
  url="$(grep -o '"browser_download_url"[^,]*' "$tmp" \
        | sed 's/.*"\(https[^"]*\)".*/\1/' \
        | grep -E "/${APP}-[0-9]+\.[0-9]+\.[0-9]+\.zip$" | head -1)"
  if [ -z "$url" ]; then
    url="$(grep -o '"browser_download_url"[^,]*' "$tmp" \
          | sed 's/.*"\(https[^"]*\)".*/\1/' \
          | grep -E '\.zip$' | grep -v -- '-windows\.zip$' | head -1)"
  fi
  rm -f "$tmp"
  [ -n "$url" ] || die "仓库 ${REPO} 没有可用的 zip 发布包"
  printf '%s' "$url"
}

# ---------- 安装 / 更新 ----------
write_config() {
  mkdir -p "$HOME_DIR/config" "$HOME_DIR/data" "$HOME_DIR/backups"
  if [ ! -f "$HOME_DIR/config/auth.json" ]; then
    cat > "$HOME_DIR/config/auth.json" <<JSON
{
  "token": "${TOKEN}",
  "session_hours": 12
}
JSON
    chmod 600 "$HOME_DIR/config/auth.json"
    ok "已写入访问口令（config/auth.json）"
  else
    info "${C_DIM}保留已有口令：config/auth.json${C_OFF}"
  fi

  if [ ! -f "$HOME_DIR/config/update.json" ]; then
    cat > "$HOME_DIR/config/update.json" <<JSON
{
  "repo": "${REPO}",
  "api": "https://api.github.com",
  "asset": "^wk-switch-[0-9.]+zip$",
  "index_url": "",
  "timeout": 30,
  "allow_prerelease": false
}
JSON
  fi

  # 上游校验的并发与节奏。不写也能跑（代码里有同样的默认值），写上是为了
  # 让人知道这几个旋钮存在——账号多的时候想调慢一点，改这个文件即可。
  if [ ! -f "$HOME_DIR/config/probe.json" ]; then
    cat > "$HOME_DIR/config/probe.json" <<JSON
{
  "concurrency": 4,
  "timeout": 12.0,
  "min_gap": 0.20,
  "max_gap": 0.50
}
JSON
  fi

  # 封号复检。默认关闭——它会真的往上游发对话请求，一次一点额度，
  # 不该在用户还没看过说明的时候自己开跑。要用就在页面上打开总开关。
  # mihomo 的 host/ports 可以留空让环境变量接管（WB_PROXY_DISCOVER_HOST /
  # WB_PROXY_DISCOVER_PORTS），这里写的是 hub 那份的默认值。
  if [ ! -f "$HOME_DIR/config/recheck.json" ]; then
    cat > "$HOME_DIR/config/recheck.json" <<JSON
{
  "enabled": false,
  "auto": true,
  "auto_hour": 3,
  "min_interval": 21600,
  "gap_seconds": 3.0,
  "timeout": 25.0,
  "rotate_proxy": true,
  "batch_limit": 20,
  "mihomo": {
    "enabled": true,
    "host": "cli-proxy-mihomo",
    "ports": "17901-17910",
    "probe_url": "https://api.ipify.org",
    "timeout": 10.0
  }
}
JSON
  fi
}

# ---------- 服务进程 ----------
# 有 systemd 就走 unit，没有（容器、WSL1、Alpine 非系统 init）就走 nohup。
# 两种方式都生成 start.sh / stop.sh，systemd 模式下也留着当应急手柄。
has_systemd() {
  [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1
}

write_start_script() {
  local py
  py="$(command -v python3)"
  cat > "$HOME_DIR/start.sh" <<SH
#!/usr/bin/env bash
# 由 install.sh 生成。用法：./start.sh [端口]
set -e
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
PORT="\${1:-${PORT}}"
HOST="\${WK_HOST:-${HOST}}"
PIDF="\$DIR/wk.pid"
LOGF="\$DIR/logs/wk-switch.log"
mkdir -p "\$DIR/logs"

# 已在跑就别再起一个，避免端口冲突。
# 光看 `kill -0` 不够：pid 会被复用，升级重启后 wk.pid 里那个号可能已经
# 变成别的进程（甚至是 systemd 拉起的另一个实例），误判成「已在运行」就
# 直接 exit 0，监督进程等到超时，整条升级链路判失败。所以再核一次命令行。
if [ -f "\$PIDF" ]; then
  OLD="\$(cat "\$PIDF" 2>/dev/null)"
  if [ -n "\$OLD" ] && kill -0 "\$OLD" 2>/dev/null \
     && grep -qa "main.py" "/proc/\$OLD/cmdline" 2>/dev/null; then
    echo "已在运行 PID \$OLD"
    exit 0
  fi
  rm -f "\$PIDF"
fi

cd "\$DIR"
nohup ${py} main.py "\$PORT" --host "\$HOST" --no-browser >> "\$LOGF" 2>&1 &
echo \$! > "\$PIDF"
sleep 1.5
PID="\$(cat "\$PIDF")"
if kill -0 "\$PID" 2>/dev/null; then
  echo "已启动 PID \$PID  监听 \$HOST:\$PORT"
  echo "日志 \$LOGF"
else
  echo "启动失败，最近日志："
  tail -20 "\$LOGF" 2>/dev/null || true
  rm -f "\$PIDF"
  exit 1
fi
SH
  chmod +x "$HOME_DIR/start.sh"
}

write_stop_script() {
  cat > "$HOME_DIR/stop.sh" <<SH
#!/usr/bin/env bash
# 由 install.sh 生成。用法：./stop.sh [端口]
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
PORT="\${1:-${PORT}}"
PIDF="\$DIR/wk.pid"
have() { command -v "\$1" >/dev/null 2>&1; }
stopped=0

if [ -f "\$PIDF" ]; then
  PID="\$(cat "\$PIDF")"
  if kill -0 "\$PID" 2>/dev/null; then
    kill "\$PID" 2>/dev/null && echo "已发停止信号 PID \$PID"
    # 优雅退出给 3 秒，没退就强杀，别让僵尸占着端口
    for _ in 1 2 3 4 5 6; do
      kill -0 "\$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "\$PID" 2>/dev/null && kill -9 "\$PID" 2>/dev/null && echo "强制结束 PID \$PID"
    stopped=1
  fi
  rm -f "\$PIDF"
fi

# 兜底：pid 文件丢了或进程换过，按端口找
if have ss; then
  PID2="\$(ss -ltnp 2>/dev/null | grep ":\${PORT} " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  [ -n "\$PID2" ] && [ "\$PID2" != "\${PID:-}" ] && kill "\$PID2" 2>/dev/null && { echo "已停止端口 \$PORT 上的 PID \$PID2"; stopped=1; }
fi

[ "\$stopped" = "1" ] || echo "没有在运行"
SH
  chmod +x "$HOME_DIR/stop.sh"
}

install_service() {
  write_start_script
  write_stop_script
  if has_systemd; then
    install_systemd
  else
    install_nohup
  fi
}

install_systemd() {
  local py
  py="$(command -v python3)"
  cat > "$SERVICE" <<UNIT
[Unit]
Description=wk-switch 跨项目账户转换
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${HOME_DIR}
ExecStart=${py} ${HOME_DIR}/main.py ${PORT} --host ${HOST} --no-browser
Restart=on-failure
RestartSec=3
# 页面「一键升级」的重启由监督进程执行（wk/relaunch.py）。KillMode 默认是
# control-group，服务一停 systemd 会 SIGTERM 整个 cgroup，监督进程和它拉起的
# 新进程会被一起打掉——升级窗口里服务就彻底空了。改成 process：只杀主进程，
# 剩下的交给监督进程自己收尾。少这一行，Linux 上「升级失败自动回滚」是废的。
KillMode=process
Environment=PYTHONUNBUFFERED=1
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$APP" >/dev/null 2>&1
  systemctl restart "$APP"
  ok "systemd 服务已安装并启动：${SERVICE}"
}

install_nohup() {
  # 旧实例（可能是旧 pid 或旧 systemd 残留）先停掉，避免新进程绑不上端口
  bash "$HOME_DIR/stop.sh" >/dev/null 2>&1 || true
  # 这台机器可能连 systemctl 都没有，先判一下，别往 stderr 甩 command not found
  if have systemctl; then systemctl stop "$APP" >/dev/null 2>&1 || true; fi
  bash "$HOME_DIR/start.sh"
  ok "已用 nohup 启动（start.sh / stop.sh 在 ${HOME_DIR}）"
}

unpack() { # $1=zip
  local zip="$1" tmp
  tmp="$(mktemp -d)"
  if have unzip; then
    unzip -q "$zip" -d "$tmp"
  else
    python3 - "$zip" "$tmp" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
  fi
  # 包内可能有一层顶层目录
  local root="$tmp"
  if [ "$(find "$tmp" -maxdepth 1 -mindepth 1 | wc -l)" = "1" ] && [ -d "$(find "$tmp" -maxdepth 1 -mindepth 1 | head -1)" ]; then
    root="$(find "$tmp" -maxdepth 1 -mindepth 1 | head -1)"
  fi
  [ -f "$root/server.py" ] || { rm -rf "$tmp"; die "包里没有 server.py，不是本程序的发布包"; }
  printf '%s' "$root"
}

do_install() {
  need_root
  check_env
  apply_proxy
  info ""
  info "安装目录：$HOME_DIR"
  info "监听：${HOST}:${PORT}"
  info ""

  local zip url root old
  zip="$(mktemp --suffix=.zip)"
  if [ -n "$FROM_FILE" ]; then
    [ -f "$FROM_FILE" ] || die "找不到本地包：$FROM_FILE"
    cp "$FROM_FILE" "$zip"
    info "使用本地包：$FROM_FILE"
  else
    # fetch_latest 在子 shell 里，die 只退子 shell，这里必须自己拦一次
    url="$(fetch_latest)" || exit 1
    [ -n "$url" ] || die "没有取到下载地址（仓库 ${REPO}）"
    url="$(mirror_url "$url")"
    info "下载：$url"
    download "$url" "$zip" || die "下载失败：$url"
  fi

  old="$(current_version)"
  root="$(unpack "$zip")" || exit 1

  # 更新场景：先备份代码，data/ 与 config/ 不动
  if installed; then
    local stamp backup
    stamp="$(date +%Y%m%d-%H%M%S)"
    backup="$HOME_DIR/backups/code-${old}-${stamp}.tar.gz"
    mkdir -p "$HOME_DIR/backups"
    tar -czf "$backup" -C "$HOME_DIR" main.py server.py wk web 2>/dev/null || true
    info "已备份旧代码：$backup"
  fi

  mkdir -p "$HOME_DIR"
  # 只替换代码，保留 data/ config/ backups/
  rm -rf "$HOME_DIR/wk" "$HOME_DIR/web"
  cp -a "$root"/. "$HOME_DIR"/
  rm -rf "$root" "$zip"
  write_config
  install_service

  local ver
  ver="$(current_version)"
  info ""
  ok "安装完成，版本 v${ver}"
  info "  地址：http://127.0.0.1:${PORT}（对外用 ${HOST}）"
  info "  口令：${TOKEN}（改 ${HOME_DIR}/config/auth.json）"
  if has_systemd; then
    info "  状态：systemctl status ${APP}"
    info "  日志：journalctl -u ${APP} -f"
  else
    info "  启停：${HOME_DIR}/start.sh ${PORT}  /  ${HOME_DIR}/stop.sh"
    info "  日志：${HOME_DIR}/logs/wk-switch.log"
  fi
  [ -n "$old" ] && [ "$old" != "未知" ] && info "  升级：v${old} → v${ver}"
}

do_update() {
  if ! installed; then
    warn "还没安装，改为全新安装"
    do_install
    return
  fi
  do_install
}

do_uninstall() {
  need_root
  if [ "$ASSUME_YES" != "1" ]; then
    printf '卸载会停止服务，但保留 %s/data 与 %s/config。继续？[y/N] ' "$HOME_DIR" "$HOME_DIR"
    read -r ans < "${TTY:-/dev/stdin}" || ans="n"
    case "$ans" in y|Y|yes|YES) : ;; *) info "已取消"; return ;; esac
  fi
  # 先停进程：nohup 模式走 stop.sh，systemd 模式走 systemctl
  bash "$HOME_DIR/stop.sh" >/dev/null 2>&1 || true
  systemctl stop "$APP" >/dev/null 2>&1 || true
  systemctl disable "$APP" >/dev/null 2>&1 || true
  rm -f "$SERVICE"
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$HOME_DIR/wk" "$HOME_DIR/web" "$HOME_DIR/main.py" "$HOME_DIR/server.py" \
         "$HOME_DIR/start.sh" "$HOME_DIR/stop.sh" "$HOME_DIR/wk.pid" "$HOME_DIR/logs"
  ok "服务已卸载；数据保留在 ${HOME_DIR}/data，配置保留在 ${HOME_DIR}/config"
}

do_status() {
  info "安装目录：$HOME_DIR"
  info "当前版本：$(current_version)"
  info "更新源：${REPO}"
  if has_systemd && systemctl is-active "$APP" >/dev/null 2>&1; then
    ok "服务运行中（systemd）"
  elif [ -f "$HOME_DIR/wk.pid" ]; then
    local pid
    pid="$(cat "$HOME_DIR/wk.pid" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      ok "服务运行中（PID $pid）"
    else
      warn "pid 文件存在但进程已退出，请重启"
    fi
  else
    warn "服务未运行"
  fi
  if have ss; then
    ss -ltn 2>/dev/null | grep -E ":${PORT}\b" || warn "端口 ${PORT} 没有监听"
  fi
  [ -f "$HOME_DIR/config/auth.json" ] && info "口令文件：$HOME_DIR/config/auth.json"
  return 0
}

menu() {
  # 管道执行时 stdin 被脚本占用，菜单必须走 tty
  if [ -r /dev/tty ]; then TTY=/dev/tty; else TTY=""; fi
  if [ -z "$TTY" ] && [ "$ASSUME_YES" != "1" ]; then
    warn "没有可交互终端，按自动模式执行"
    ASSUME_YES=1
  fi

  while :; do
    info ""
    info "=============================="
    info "  wk-switch 部署菜单"
    info "  已安装：$(installed && echo "是（v$(current_version)）" || echo "否")"
    info "  目录：$HOME_DIR   端口：$PORT   监听：${HOST}"
    info "  更新源：$REPO"
    info "=============================="
    info "  1) 安装 / 更新（自动判断）"
    info "  2) 查看状态"
    info "  3) 重启服务"
    info "  4) 卸载（保留数据）"
    info "  0) 退出"
    printf '请选择 [0-4]: '
    read -r choice < "$TTY" || choice="0"
    case "$choice" in
      1) do_update; return ;;
      2) do_status ;;
      3) need_root
         if has_systemd; then systemctl restart "$APP" && ok "已重启"
         else bash "$HOME_DIR/stop.sh"; bash "$HOME_DIR/start.sh"; fi ;;
      4) do_uninstall; return ;;
      0|"") return ;;
      *) warn "无效选项" ;;
    esac
  done
}

main() {
  case "$ACTION" in
    install)   do_install ;;
    update)    do_update ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
    *)
      if [ "$ASSUME_YES" = "1" ]; then
        installed && do_update || do_install
      else
        menu
      fi
      ;;
  esac
}

main
