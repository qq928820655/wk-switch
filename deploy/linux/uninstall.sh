#!/usr/bin/env bash
# 卸载 wk-switch（保留 data 与 config）。等价于 install.sh --uninstall
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$DIR/install.sh" --uninstall "$@"
