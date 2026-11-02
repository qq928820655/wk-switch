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

下载 `wk-switch-<ver>-windows.zip`，解压后双击根目录的 `install.bat`。

- 解压目录里有完整代码 → 就地安装，不联网
- 只有 `install.bat` 一个文件 → 自动去 Releases 拉最新包

装完在 `%LOCALAPPDATA%\wk-switch` 生成：

```bat
start.bat        :: 启动（写 wk.pid，开浏览器）
stop.bat         :: 停止（按端口找进程，不按进程名误伤）
uninstall.bat    :: 卸载（保留 data 与 config）
```

端口与口令可用环境变量覆盖：`WK_PORT` / `WK_TOKEN` / `WK_HOME_OVERRIDE`；
离线安装包路径写 `WK_PACKAGE`。

## 升级

页面「升级」标签页 → 检查更新 → 一键升级 → 重启生效。

制品源两种：

- GitHub Releases（默认）：`config/update.json` 的 `repo`，或 `.git` 的 origin
- 任意制品索引：`index_url` 返回 `{"version": "1.0.1", "url": "包地址"}`

**下载拉不动时**（国内直连 `github.com` 的 release 资产常被掐断，而
`api.github.com` 往往还能通，所以「检查更新成功、下载卡住」是常见组合）：

- 页面「下载代理」填本地代理，如 `http://127.0.0.1:7890`；留空则用环境变量
  `HTTPS_PROXY` / `HTTP_PROXY`
- 或用「本地包升级」：浏览器里手动下载 `wk-switch-x.y.z.zip`，在页面上传，
  走浏览器自己的网络栈，与本服务无关

升级只替换 `main.py` `server.py` `wk/` `web/`，`data/` `config/` 不动，
`deploy/` 也不动（安装器里的端口、路径、服务名是装的时候定的，被覆盖会冲掉
用户的部署配置）；每次升级前旧代码自动打包到 `backups/`。

包版本低于当前版本时按回滚处理，结果里会标 `downgraded: true`。

重启走 `--wait-port`：新进程只在原端口上等旧进程退出，不往后找——端口一漂
书签和反代就全失联了。

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
