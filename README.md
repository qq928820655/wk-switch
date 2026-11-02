# wk-switch

跨项目账户（token）导入导出转换工具。七个 WorkBuddy 系项目的账号包互转，带本地账户库、版本留存与一键升级。

单机运行，Python 3.8+，零第三方依赖，前端无框架无 CDN。

## 功能

- **多格式识别**：导入时自动嗅探 JSON / ZIP / NDJSON / CSV / 文本行 / 加密信封，七个项目的导出包直接拖进来即可
- **格式互转**：8 种输出格式，按目标项目的目录与字段约定命名
- **本地账户库**：SQLite 留存，唯一键 `(uid, realm)`；同名不同号不会被合并
- **国内版 / 国际版分离**：按 JWT issuer 与域名判定，导出可「全部 / 仅国内 / 仅国际 / 分版本各一份」
- **备份还原**：整库打包 zip（`accounts.db` + `accounts.json`），支持合并与覆盖两种还原
- **一键升级**：页面「升级」标签页检查制品库最新版，下载、备份、替换代码、重启
- **访问口令**：登录页口令换会话 cookie，默认 `qq928820655`（改 `config/auth.json`）

## 快速开始

```bash
python main.py              # http://127.0.0.1:8765
python main.py 9000         # 换端口
python main.py --version    # 版本号
```

首次访问会跳到登录页，输入口令后进入。

## 支持的格式

| 目标格式 | 落盘形态 | 文件名 |
|---|---|---|
| Hub 导出文档 | `{format,version,count,accounts[]}` 扁平 camelCase | `workbuddy-accounts.json` |
| Hub 账户目录 | 每账户一个文件 | `<uid>.json` |
| Manager 账号包 | 嵌套 `{account,auth,device_token}` | `accounts.zip` |
| Panel 凭据目录 | 同嵌套形 | `<uid>.json` |
| Switch 账号库 | 扁平 snake_case 数组 | `accounts.json` |
| Qoder 账号库 | 扁平 snake_case 数组 | `accounts.json` |
| Daily 登录结果 | `{phone,access_token,refresh_token,env_line,login_at}` | `wb_login_result.json` |
| Daily 环境变量行 | `手机号:AT:RT` | `wb_env.txt` |

## 部署

### Linux

```bash
# 菜单式（自动判断新装 / 更新）
curl -fsSL https://raw.githubusercontent.com/<owner>/wk-switch/main/deploy/linux/install.sh | sudo bash

# 全自动
curl -fsSL .../install.sh | sudo bash -s -- --yes --token 你的口令 --port 8765
```

装完是 systemd 服务：`systemctl status wk-switch` / `journalctl -u wk-switch -f`。
默认目录 `/opt/wk-switch`，`data/` 与 `config/` 在更新时不会被覆盖。

### Windows

```bat
deploy\windows\install.bat      :: 安装 / 更新（管理员）
deploy\windows\start.bat        :: 启动
deploy\windows\stop.bat         :: 停止
deploy\windows\uninstall.bat    :: 卸载（保留数据）
```

默认目录 `%LOCALAPPDATA%\wk-switch`。

## 升级

页面「升级」标签页 → 检查更新 → 一键升级 → 重启生效。

制品源两种：

- GitHub Releases（默认）：`config/update.json` 的 `repo`，或 `.git` 的 origin
- 任意制品索引：`index_url` 返回 `{"version": "1.0.1", "url": "包地址"}`

升级只替换 `main.py` `server.py` `wk/` `web/` `deploy/`，`data/` `config/` 不动；
每次升级前旧代码自动打包到 `backups/`。

## 打包发布

```bash
python build_release.py --repo <owner>/wk-switch
gh release create v1.0.0 dist/*.zip dist/*.tar.gz dist/SHA256SUMS.txt
```

产物：`wk-switch-<ver>.zip`（升级包）、`-linux.tar.gz`、`-windows.zip`。

## 目录

```
main.py              启动器（端口占用自动后移，--wait-port 供升级重启）
server.py            本地 HTTP + API（只绑 127.0.0.1，登录态校验）
build_release.py     打包 dist/
wk/
  model.py           统一 Account、字段别名、时间归一
  formats.py         8 种输出 / 多形态输入 / ZIP 安全解包
  pipeline.py        校验 → 补 uid → 去重 → 统计
  token.py           JWT 载荷、realm 推断、凭据状态
  store.py           本地账户库（SQLite）、备份还原
  auth.py            访问口令与会话
  update.py          在线升级
  version.py         版本号
web/                 index.html / login.html / app.js / style.css
deploy/linux/        install.sh / uninstall.sh
deploy/windows/      install.bat / start.bat / stop.bat / uninstall.bat
samples/             九个样例包与生成脚本
tests/               单元测试
```

## 测试

```bash
python -m unittest discover -s tests -v
```

## 许可

MIT
