# wk-switch

跨项目账户（token）导入导出转换工具。七个 WorkBuddy 系项目的账号包互转，带本地账户库、版本留存与一键升级。

单机运行，Python 3.8+，零第三方依赖，前端无框架无 CDN。

## 功能

- **多格式识别**：导入时自动嗅探 JSON / ZIP / NDJSON / CSV / 文本行 / 加密信封，七个项目的导出包直接拖进来即可
- **格式互转**：8 种输出格式，按目标项目的目录与字段约定命名
- **本地账户库**：SQLite 留存，唯一键 `(uid, realm)`；同名不同号不会被合并
- **国内版 / 国际版分离**：按 JWT issuer 与域名判定，导出可「全部 / 仅国内 / 仅国际 / 分版本各一份」
- **备份还原**：整库打包 zip（`accounts.db` + `accounts.json`），支持合并与覆盖两种还原
- **一键升级**：页面「升级」标签页检查制品库最新版，下载、备份、替换代码、重启；仅当制品库版本更高时标签页才标红点
- **导出命名可辨**：文件名统一为 `<项目名>-<版本>-<时间戳>.<扩展名>`，例如 `workbuddy-manager-cn-20261103-125433.zip`，下载一堆包不会互相覆盖
- **上游校验与积分**：选中账号后可向上游只读查询，判定凭据是否仍然有效并取回积分余额。**不刷新 token**——refresh token rotation 会让别处正在用同一账号的服务掉线，详见下文
- **封号复检**：被上游风控拦掉的账号单独进「封号复检」标签页，定时发一次最小对话探活；支持串接 mihomo 多出口节点轮换，避免用同一个 IP 反复敲门
- **访问口令**：登录页口令换会话 cookie，默认 `qq928820655`（改 `config/auth.json`）

## 快速开始

```bash
python main.py              # http://127.0.0.1:8765
python main.py 9000         # 换端口
python main.py --version    # 版本号
```

首次访问会跳到登录页，输入口令后进入。

## 支持的格式

导出文件名统一带项目名与时间戳，分版本时再插版本段：

| 目标格式 | 落盘形态 | 文件名示例 |
|---|---|---|
| Hub 导出文档 | `{format,version,count,accounts[]}` 扁平 camelCase | `workbuddy2api-hub-20261103-125433.json` |
| Hub 账户目录 | 每账户一个文件，打成 zip | `workbuddy2api-hub-20261103-125433.zip` |
| Manager 账号包 | 嵌套 `{account,auth,device_token}` | `workbuddy-manager-20261103-125433.zip` |
| Panel 凭据目录 | 同嵌套形 | `workbuddy2api-panel-20261103-125433.zip` |
| Switch 账号库 | 扁平 snake_case 数组 | `workbuddy-switch-20261103-125433.json` |
| Qoder 账号库 | 扁平 snake_case 数组 | `qoder-trae-workbuddy-switch-20261103-125433.json` |
| Daily 登录结果 | `{phone,access_token,refresh_token,env_line,login_at}` | `WorkBuddy-Daily-20261103-125433.json` |
| Daily 环境变量行 | `手机号:AT:RT` | `WorkBuddy-Daily-20261103-125433.txt` |

分版本导出时文件名插版本段：`workbuddy-manager-cn-20261103-125433.zip`、`workbuddy-manager-intl-20261103-125433.zip`。
本地库备份为 `wk-switch-backup-20261103-125433.zip`。

## 上游校验与积分

本地能看到的是 `expiresAt` 和 JWT 里的 `exp`——那只是签名里写的过期时间。服务端有没有封号、token 有没有被换掉、还剩多少积分，只能拿这个 token 去问一次上游。

账户库里有「校验全部」和选中后的「校验选中」。跑完会在「积分」列显示余额与进度条，并把结论写回库里。

**只读边界**

| 会做 | 不会做 |
|---|---|
| `POST /billing/meter/get-user-resource-summary` | 不调用 `/v2/plugin/auth/token/refresh` |
| `POST /billing/meter/get-user-resource` | 不签到、不触发任何写操作 |
| `POST /v2/billing/meter/get-enterprise-user-usage`（静态 Key / 企业号） | 不改上游任何状态 |

这条不是为了保守。上游用 refresh token rotation：每次 refresh 签发新 RT，旧 RT 当场作废。本工具持有的是从别处导入的副本，一旦拿 `refresh_token` 去换 `access_token`，真正管辖这个账号的那套系统（hub / manager / panel）手里的 RT 就废了，它下次刷新直接 401，正在服务的账号掉线。所以遇到 401 只记「失效」，不续命。要续期请到原系统里做。

**判定口径**

| 结论 | 触发条件 | 含义 |
|---|---|---|
| 有效 | HTTP 200 且 `code=0` | 上游认这个 token |
| 已失效 | 401 / 403，或 `code≠0` 且提示登录过期 | 需要重新登录 |
| 未确认 | 网络故障、超时、5xx、所有计费前缀都 404 | 不下结论，可再试 |
| 不可校验 | 加密信封 token、空 token | 本地就能判定，不发请求 |

「未确认」和「已失效」必须分开：把网络故障显示成封号，会让人去删一个好账号。

**并发与节奏**

默认 4 并发、每个请求前随机停 200~500ms。一批几十个账号同时打同一个上游是标准的风控触发姿势，这两个值都能在 `config/probe.json` 里调：

```json
{
  "concurrency": 4,
  "timeout": 12.0,
  "min_gap": 0.20,
  "max_gap": 0.50
}
```

`concurrency` 会被夹在 1~16，`timeout` 夹在 3~60 秒。代理按账号自己的 `proxy` 字段走；没配就显式直连，不读 `HTTPS_PROXY`——批量校验时环境变量会把该走代理和不该走代理的账号混在一起。

**会不会影响在用的服务**

- 本机端口：不影响。校验是主动出站 TCP，不占本地端口，和 8765 不冲突
- 上游账号：只读查询不掉线。唯一会掉线的是刷新 token，本工具不做
- 频率：共用该账号的 rate limit。并发压到 4、间隔 200ms 以上就是为了不撞这条线

接口路径、请求头、响应字段名照抄两个已在生产跑的项目：`ardeyouxipianyi/workbuddy2api-hub`（`wb_accounts.py:388-398`、`3081-3236`）和 `L0NE-6/WorkBuddy-Daily`（`credits_report.py:56-70`、`207-216`），没有自己发明。

## 封号复检

账号被封时，计费接口照样返回 `code=0`、积分照样查得到——上游封的是**对话**那条链路。所以「解封了没有」只能通过发一次真实对话请求来判断，这就是封号复检做的事。

逻辑搬自 `GenLiya/workbuddy-registration-notes` 的 `banned-retry.mjs`，它实测出一条关键结论：**重新 OAuth 登录解不了封**。登录能成功、拿回同一个 uid，但一发请求仍被拦，说明封在账号层而不是会话层，只能等上游自动解除。

**名单怎么来**

| 来源 | 触发 |
|---|---|
| 自动 | 导入时来源系统把禁用原因写成 `banned by upstream` / `request illegal` / `11140` 一类 |
| 手工 | 「把账号加进名单」下拉里选一个 |
| 复检 | 上一轮复检判定仍被封的留在名单里 |

手动停用（`manual disable`）的号**不会**被扫进名单——那是用户自己的选择，不该去敲门。

**判定口径**

| 结论 | 触发条件 | 后续 |
|---|---|---|
| 已解封 | HTTP 200 | 移出名单，保留一条「已解封」记录 |
| 封号中 | 403 且正文含 `11140` / `request illegal` / `账号存在异常行为` | 留在名单，等下一轮 |
| 凭据失效 | 401 | 标为 auth，不再敲门——这不是封号，等重新导入 |
| 未确认 | 网络故障、超时、429、403 但没有风控特征 | 保留原状态，不误判 |

判定顺序是「先看风控特征，再看 401/403，最后才认 200」。上游偶尔在账号被限制时也回 200 带一段业务错误体，先认 200 会把封着的号判成解封。

**节奏**

串行，不并发，号与号之间固定停 3 秒（`gap_seconds`，最低 1 秒）。这不是偷懒：`workbuddy2api-hub` 在 `wb_proxy.py:15630` 留了一条红线——看到 11140 立即停手，解封前严禁换号继续打同一台设备，设备指纹会连坐。一批被封号同时从同一台机器打出去，特征是一样的。

同一个号默认 6 小时内不重复敲（`min_interval`，最低 10 分钟）。每敲一次都是一次真实请求，敲太密等于自己续上风控。

**mihomo 节点**

同一个出口 IP 是当初触发风控的原因之一，复检时继续用同一个 IP 敲门等于告诉上游「还是我」。配了节点就按号轮换。

检测方式和 hub 的代理槽一致：一个端口 = 一个 listener = 一个固定出口，对每个 `http://<host>:<port>` 打一次 `https://api.ipify.org`，拿到出口 IP 就算可用。

```json
{
  "mihomo": {
    "enabled": true,
    "host": "cli-proxy-mihomo",
    "ports": "17901-17910",
    "probe_url": "https://api.ipify.org",
    "timeout": 10.0
  }
}
```

`host` 默认 `cli-proxy-mihomo`（docker 网络里的服务名），`ports` 默认 `17901-17910`，与 hub 的 `PROXY_DISCOVER_HOST` / `PROXY_DISCOVER_PORTS` 同源。已经配过 hub 的机器直接就能复用，也可以用环境变量 `WB_PROXY_DISCOVER_HOST` / `WB_PROXY_DISCOVER_PORTS` 覆盖。

节点全不通时复检会直连——不会因为代理挂了就不检查，只是失去了换 IP 这层保护。

**自动复检**

默认每天 3 点跑一轮（`auto_hour`），服务在跑就会执行，不用挂 crontab。总开关 `enabled` 默认关闭：复检会真的往上游发请求、真扣一点额度，不该在你还没看过说明的时候自己开跑。

## 部署

### Linux

```bash
# 菜单式（自动判断新装 / 更新）
curl -fsSL https://raw.githubusercontent.com/qq928820655/wk-switch/main/deploy/linux/install.sh | sudo bash

# 全自动
curl -fsSL https://raw.githubusercontent.com/qq928820655/wk-switch/main/deploy/linux/install.sh \
  | sudo bash -s -- --yes --token 你的口令 --port 8765

# 网络受限：走代理或 GitHub 镜像
curl -fsSL .../install.sh | sudo bash -s -- --proxy http://127.0.0.1:7890
curl -fsSL .../install.sh | sudo bash -s -- --mirror https://ghfast.top
```

脚本自己认仓库：`--repo` > `WK_REPO` > 打包时写入的占位 > raw 地址解析 > 内置默认，
所以直接 `curl` 仓库里的原始脚本也能跑，不会拼出 `repos/__REPO__/releases/latest`。

装完自动选运行方式，不用手工判断：

- 有 systemd（`/run/systemd/system` 存在且 `systemctl` 可用）→ 装 unit，`systemctl status wk-switch` / `journalctl -u wk-switch -f`
- 没有 systemd（容器、WSL1、Alpine 非系统 init）→ 生成 `start.sh` / `stop.sh` 用 nohup 起，日志在 `logs/wk-switch.log`

两种方式都会在安装目录留 `start.sh` / `stop.sh` 当应急手柄。默认目录 `/opt/wk-switch`，`data/` 与 `config/` 在更新时不会被覆盖。

默认只监听 `127.0.0.1`。要对外提供访问（反代后面或直连）加 `--host 0.0.0.0`：

```bash
curl -fsSL .../install.sh | sudo bash -s -- --host 0.0.0.0 --port 8765
```

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
离线安装包路径写 `WK_PACKAGE`；换仓库写 `WK_REPO=owner/name`。

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

**数据库结构**

`data/accounts.db` 的 `PRAGMA user_version` 当前是 4：

| 版本 | 变更 |
|---|---|
| 2 | 唯一键从 `(name, realm)` 改成 `(uid, realm)` |
| 3 | 加探针列（积分三元组、校验状态、校验时间） |
| 4 | 加封号复检列（名单状态、次数、时间、结论） |

升级时逐列判断缺什么补什么，不按版本号整表重建——有人可能跑过中间版本、也
可能手工改过库。老数据原样保留。

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
  probe.py           只读探针：计费查询 + 对话探活
  recheck.py         封号复检：名单流转、mihomo 节点发现
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
