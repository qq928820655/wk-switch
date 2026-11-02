#!/usr/bin/env bash
# wk-switch Linux 一键部署 / 更新
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/linux/install.sh | sudo bash
#   sudo bash install.sh                 # 本地运行，进菜单
#   sudo bash install.sh --yes           # 全自动（已装则更新，未装则安装）
#   sudo bash install.sh --token 你的口令 --port 8765
#   sudo bash install.sh --from-file dist/wk-switch-1.0.0.zip   # 离线包
#
# 通过管道执行时 stdin 是脚本本身，菜单从 /dev/tty 读，所以 curl | bash 也能交互。

set -o pipefail

APP="wk-switch"
REPO="${WK_REPO:-__REPO__}"
HOME_DIR="${WK_HOME:-/opt/wk-switch}"
SERVICE="/etc/systemd/system/${APP}.service"
PORT="${WK_PORT:-8765}"
TOKEN="${WK_TOKEN:-qq928820655}"
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
    --home)       HOME_DIR="${2:-}"; shift ;;
    --repo)       REPO="${2:-}"; shift ;;
    --from-file)  FROM_FILE="${2:-}"; shift ;;
    --install)    ACTION="install" ;;
    --update)     ACTION="update" ;;
    --uninstall)  ACTION="uninstall" ;;
    --status)     ACTION="status" ;;
    -h|--help)    sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
  shift
done

need_root() {
  [ "$(id -u)" = "0" ] || die "需要 root：请用 sudo 运行"
}

have() { command -v "$1" >/dev/null 2>&1; }

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
download() { # $1=url $2=输出路径
  if have curl; then
    curl -fsSL --retry 3 --connect-timeout 15 -o "$2" "$1"
  else
    wget -q -O "$2" "$1"
  fi
}

fetch_latest() { # 输出下载地址
  local api tmp url
  api="https://api.github.com/repos/${REPO}/releases/latest"
  tmp="$(mktemp)"
  download "$api" "$tmp" || { rm -f "$tmp"; die "取版本信息失败：$api"; }
  # 不依赖 jq：直接抓 zip 资产的下载地址
  url="$(grep -o '"browser_download_url"[^,]*' "$tmp" \
        | sed 's/.*"\(https[^"]*\)".*/\1/' \
        | grep -E '\.zip$' | head -1)"
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
}

install_service() {
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
ExecStart=${py} ${HOME_DIR}/main.py ${PORT} --no-browser
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
# 只监听本机；要对外暴露请在前面加 nginx 并配 TLS
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
  info ""
  info "安装目录：$HOME_DIR"
  info "监听端口：$PORT（仅 127.0.0.1）"
  info ""

  local zip url root old
  zip="$(mktemp --suffix=.zip)"
  if [ -n "$FROM_FILE" ]; then
    [ -f "$FROM_FILE" ] || die "找不到本地包：$FROM_FILE"
    cp "$FROM_FILE" "$zip"
    info "使用本地包：$FROM_FILE"
  else
    url="$(fetch_latest)"
    info "下载：$url"
    download "$url" "$zip" || die "下载失败"
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
  info "  地址：http://127.0.0.1:${PORT}"
  info "  口令：${TOKEN}（改 ${HOME_DIR}/config/auth.json）"
  info "  状态：systemctl status ${APP}"
  info "  日志：journalctl -u ${APP} -f"
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
  systemctl stop "$APP" >/dev/null 2>&1 || true
  systemctl disable "$APP" >/dev/null 2>&1 || true
  rm -f "$SERVICE"
  systemctl daemon-reload
  rm -rf "$HOME_DIR/wk" "$HOME_DIR/web" "$HOME_DIR/main.py" "$HOME_DIR/server.py"
  ok "服务已卸载；数据保留在 ${HOME_DIR}/data，配置保留在 ${HOME_DIR}/config"
}

do_status() {
  info "安装目录：$HOME_DIR"
  info "当前版本：$(current_version)"
  if have systemctl; then
    systemctl is-active "$APP" >/dev/null 2>&1 && ok "服务运行中" || warn "服务未运行"
  fi
  if have ss; then
    ss -ltnp 2>/dev/null | grep -E ":${PORT}\b" || warn "端口 ${PORT} 没有监听"
  fi
  [ -f "$HOME_DIR/config/auth.json" ] && info "口令文件：$HOME_DIR/config/auth.json"
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
    info "  目录：$HOME_DIR   端口：$PORT"
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
      3) need_root; systemctl restart "$APP" && ok "已重启" ;;
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
