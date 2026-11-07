# wk-switch

跨项目账户（token）导入导出转换工具。七个 WorkBuddy 系项目的账号包互转，带本地账户库、版本留存与一键升级。

单机运行，Python 3.8+，零第三方依赖，前端无框架无 CDN。

## 功能

- **多格式识别**：导入时自动嗅探 JSON / ZIP / NDJSON / CSV / 文本行 / 加密信封，七个项目的导出包直接拖进来即可
- **格式互转**：8 种输出格式，按目标项目的目录与字段约定命名
- **本地账户库**：SQLite 留存，唯一键 `(uid, realm)`；同名不同号不会被合并
- **导入批次可查**：每条记录带「批次号 + 导入时间（北京时间）」，工具栏能按批次筛，同一个包第几次导进来一眼对得上；`first_batch` 记着首次导入，重复导入不会把它冲掉
- **国内版 / 国际版分离**：按 JWT issuer 与域名判定，导出可「全部 / 仅国内 / 仅国际 / 分版本各一份」
- **备份还原**：整库打包 zip（`accounts.db` + `accounts.json`），支持合并与覆盖两种还原
- **一键升级并重启**：下载、备份、替换代码、重启、自检一个动作做完，页面自动等新进程应答后刷新；起不来自动回滚到上一版。仅当制品库版本更高时标签页才标红点
- **导出命名可辨**：文件名统一为 `<项目名>-<版本>-<时间戳>.<扩展名>`，例如 `workbuddy-manager-cn-20261103-125433.zip`，下载一堆包不会互相覆盖
- **上游校验与积分**：选中账号后可向上游只读查询，判定「凭据有效」和「额度可用」两件事，并取回积分余额。**不刷新 token**——refresh token rotation 会让别处正在用同一账号的服务掉线，详见下文
- **风控账号单独成区**：导入时可勾「这批是风控账号」，勾了直接进封号复检名单；不勾就是正常号。**也能逐行标**——结果预览的风控列点一下是「风控」，再点一下是「正常」（把它从风控区拿出来），第三次回到「跟随整批」。导入字段里的风控措辞不再自动判定——以前那样会把整批正常号拖进风控区。后续校验发现风控的，自动移进去并标来源
- **结果预览可勾选、可删行**：预览表支持多选（表头全选），勾中的行能批量标风控 / 标正常，也能直接删掉。删除只作用于这次会话里的副本，拖进来的文件一个字节都不动，重新导入就全回来了
- **导出范围跟勾选走**：勾了一部分就只导勾的（`生成导出文件` 的提示里会写明「勾选的 N 条」）；一条不勾按全部算——空勾选表示「没表态」，不是「都不要」
- **账户库也能设出口**：库里每行一个「出口」下拉（可逐行自定义），工具栏「一键分配出口」按 mihomo 端口随机分，**自动避开风控账号正在用的端口**；配了出口的账号连查积分也走这条线，风控账号被勾选校验时一样
- **状态列区分风控**：账户库的「状态」列以前只画凭据状态，封着的号也显示绿色「有效」。现在进了风控区先显示红色「封号中」，整行淡红底 + 左侧红条，凭据状态降成小字
- **封号复检**：被上游风控拦掉的账号单独进「封号复检」标签页，定时发一次最小对话探活；出口按**每个账号一个 mihomo 端口**分配（单一出口单选、随机出口多选，名单里每行还能单独改，改完就是自定义），支持端口自动检测（显示节点名 / 类型 / 延迟，不只是一串 IP），每个端口还能单独换成别的节点
- **验证模型可配**：复检与账户验证默认送 `deepseek-v4.1-flash`，在「封号复检 → 账户验证模型」里能改；400 之后的降级请求也用同一个模型
- **访问口令**：登录页口令换会话 cookie，默认 `qq928820655`（改 `config/auth.json`）
- **应用图标**：indigo 双向箭头，favicon / 页头 / 任务栏 / PWA 主屏同一张图，随代码生成，`python tools/make_icon.py` 可重出任意尺寸

## 快速开始

```bash
python main.py              # http://127.0.0.1:8765
python main.py 9000         # 换端口
python main.py --version    # 版本号
```

首次访问会跳到登录页，输入口令后进入。

## 时间口径

全站时间都是**北京时间（UTC+8）**，包括：库里所有时间列、批次号、导入记录、校验与复检时间、「每天自动复检」的触发点、导出文件名的时间戳、日志行。

服务器可能开在 UTC 机房（VPS 默认就是）。用 `time.strftime()` 是跟系统时区走的：同一份数据在 UTC 机器上显示 09:30、在北京机器上显示 17:30，两边对不上任何一条记录，自动复检还会在错误的小时触发、把「今天」算错一天。

所有时间只从 `wk/clock.py` 出：

```python
clock.stamp()              # 2026-11-06 17:30:08
clock.compact()            # 20261106-173008  （批次号、文件名）
clock.short(ts)            # 11-06 17:30      （表格里）
clock.day_start()          # 北京时间当天 00:00 的 epoch
```

时区写成固定偏移 +8 而不是 `ZoneInfo("Asia/Shanghai")`：中国不实行夏令时，固定偏移更省事，也不依赖系统的 tzdata 是否存在（Windows 上某些精简版 Python 没有，`ZoneInfo` 直接抛异常）。

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
| 可用 | 有效 **且** 剩余积分 > 0（或查不到明细） | 现在就能拿去跑 |
| 有效 | HTTP 200 且 `code=0` | 上游认这个 token，但不一定有额度 |
| 已失效 | 401 / 403，或 `code≠0` 且提示登录过期 | 需要重新登录 |
| 未确认 | 网络故障、超时、5xx、所有计费前缀都 404 | 不下结论，可再试 |
| 不可校验 | 加密信封 token、空 token | 本地就能判定，不发请求 |

「未确认」和「已失效」必须分开：把网络故障显示成封号，会让人去删一个好账号。

**「可用」和「有效」也是两件事**

有效只说明 token 没过期。余额为 0 的号拿去跑，第一句话就是余额不足，对使用者等于不可用。所以汇总里单独给「可用 / 额度用完」两个数，判据照 hub 的 `available = code == 0 and (remain > 0 or remain is None)`：

- `remain` 查到且 `<= 0` → 有效但不可用
- `remain` 为 `None`（上游没回明细）→ 仍算可用。没有证据说明它没额度，误杀好号的代价比漏放一个大

**被封和过期也要分开**

403 或 `code=11140` 且正文带「request illegal / 账号存在异常行为」这类措辞的，是账号级风控，不是 token 失效：这种号进风控区等解封，只有 `401` 或明确的登录过期提示才算需要重新扫码。判错方向会让人去重扫一个其实只是被封着的号。

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

`concurrency` 会被夹在 1~16，`timeout` 夹在 3~60 秒。

**出站顺序**：账户库「出口」列配了端口（`exit_port > 0`）就用那个 mihomo 端口，覆盖账号自带的 `proxy`；`exit_port = 0` 时保留导入数据里的 `proxy`；两者都没有就显式直连，不读 `HTTPS_PROXY`——批量校验时环境变量会把该走代理和不该走代理的账号混在一起。风控账号在账户库里被勾选校验时走的是同一套规则，用的是它自己那一行配的端口。

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
| 未确认 | 网络故障、超时、429、400、403 但没有风控特征 | 保留原状态，不误判 |

判定顺序是「先看风控特征，再看 401/403，最后才认 200」。上游偶尔在账号被限制时也回 200 带一段业务错误体，先认 200 会把封着的号判成解封。

**「HTTP 400，未确认」是什么意思**

400 是**上游收到了这次请求，但不认它的内容**。它恰好排除了另外两件事：

- 地址填错 → 连不上（状态码 0 / 超时），不会返回 400
- 凭据失效 → 401

所以看到 400 时地址和 token 都是通的，问题在请求体本身：模型名换了、网关给 `max_tokens` 加了最小值、某个字段成了必填。程序的处理是先用最朴素的请求体再敲一次（去掉 `stream`、`max_tokens` 提到 16），还不行就把上游原话和错误码一起写进结论：

```
HTTP 400，模型名或参数不被接受（11133）；地址和凭据都通，不是账号状态；上游：model_param_invalid
```

这条结论判 `unknown` 而不是 `banned`——它说明不了账号状态，判成封号会让人把好号删掉。

**账户验证模型**

复检、以及所有「这个号还能不能用对话」的敲门，默认送 `deepseek-v4.1-flash`。在「封号复检 → 账户验证模型」里能改，落进 `config/recheck.json`：

```json
{
  "chat_model": "deepseek-v4.1-flash"
}
```

写**裸模型名**，别带 `cn:` / `global:` 前缀——那是面板自己分池用的，转发到上游会被判成 `11133 model_param_invalid`，把「还封着」误报成「参数错误」。400 之后那次降级请求也送同一个模型，不会出现「正式请求用新模型、降级还在用旧默认值」的错位。留空时后端回默认值，长度截到 80 字符。

**端口换节点**

`mihomo 节点` 面板上每张卡片右边有个「改」，点开是池子里的节点列表（只列**测通**的，空闲的排在前面），点一个就把这个端口改走那个节点。

走的是池子的 `POST /api/v1/ports/bind`。那个接口的语义是「把节点钉到端口」，**端口被占时直接拒绝**——所以这里多做一步：目标端口上已经有人时，先把占用者挪到一个空端口，再让目标节点占位。被挪走的节点会在提示里点名（`端口 17950 已改走 直连-美国01；直连-日本01 挪到 17957`），不然下次看到端口表对不上会以为是程序自己乱排。

绑定失败会尽量回滚：占用者已经挪走了就挪回原端口，不留下两张位置都不对的卡片。

**节奏**

串行，不并发，号与号之间固定停 3 秒（`gap_seconds`，最低 1 秒）。这不是偷懒：`workbuddy2api-hub` 在 `wb_proxy.py:15630` 留了一条红线——看到 11140 立即停手，解封前严禁换号继续打同一台设备，设备指纹会连坐。一批被封号同时从同一台机器打出去，特征是一样的。

同一个号默认 6 小时内不重复敲（`min_interval`，最低 10 分钟）。每敲一次都是一次真实请求，敲太密等于自己续上风控。

**出口：每行一个端口**

同一个出口 IP 是当初触发风控的原因之一，复检时继续用同一个 IP 敲门等于告诉上游「还是我」。但也不能一律打散：排查时要看「封的是号还是 IP」，那就得把 IP 这个变量控住。

真正生效的粒度是**每个账号自己的 `exit_port`**（存在库里，`0` = 直连）。复检只认这个数，不再每轮现摇——名单上那一列显示什么，这一趟就走什么，对得上才能核对结论。

界面上的两档只是「批量怎么分」：

| 档 | 点端口面板的行为 | 分配结果 |
|---|---|---|
| 单一出口 | 单选：点第二个自动换过去 | 所有行同一个端口 |
| 随机出口 | 开关：再点一次取消；一个都不点 = 全部端口 | 每行在「勾选的端口」里随机，范围空就是全部端口 |

分配是**一次性写库**的（`POST /api/recheck/assign`），之后名单每行的下拉都能单独改（`POST /api/recheck/exit`）——这就是「自定义出口」：不需要另开一档全局地址表。

```json
{
  "exit_mode": "random",
  "exit_ports": [17950, 17951],
  "exit_pin": ""
}
```

`exit_ports` 是勾选范围（界面高亮的那几张卡片），`exit_mode` 决定分配方式。`exit_pin` / `exit_custom` 只为读老配置留着：`custom` 档已经从界面拿掉，老文件里读到会折成 `random`，`plan_exit` 的分支还在，不会把老配置读成别的意思。

分配函数在没有可用端口时返回空表、接口直接报错，**不会把每行写成 0**——那等于静默把所有人改回直连，正是最不该发生的偏差。

每轮结果会带上这次用的出口 IP（`proxy_ip`），界面上汇总那一行也会列出来。判断「同一个 IP 下的其它号还能不能通」全靠它。

**账户库：一键分配 + 逐行自定义**

账户库表格也有一列「出口」，同一个 `exit_port`，两边看到的数永远是同一个。

| 操作 | 作用 |
|---|---|
| 一键分配出口 | 在 mihomo 端口里给**正常账号**逐行随机分；勾了几个就只分这几个，没勾就是当前全部正常账号 |
| 出口列下拉 | 逐行改成任意端口或「直连」（`0`），只影响这一行 —— 自定义出口就在这里 |
| 全部直连 | 把正常账号全改回 `0`，风控那批不动 |
| 刷新节点 | 重读一次 mihomo 端口表 |

**两区不共用一个端口**：正常区和风控区各自的一键分配都会先扣掉对方正在用的端口（`store.exit_ports_in_use` → `recheck.free_ports`），再调 `assign_ports`。同一个 listener 上既跑积分查询又跑封号敲门，两边的请求特征会互相带过去。下拉里手动选不受这个限制——那是用户自己下的判断，选项会标注「（风控占用）」。

风控账号不在账户库一键分配的范围内：它归「封号复检」页管，勾上了也会被服务端剔掉。分配不出端口时接口报错，不写 `0`。

接口：`GET /api/store/exits`（节点表 + 两区占用）、`POST /api/store/assign`、`POST /api/store/exit`。

**mihomo 节点**

分配范围就是这些节点。检测方式和 hub 的代理槽一致：一个端口 = 一个 listener = 一个固定出口，对每个 `http://<host>:<port>` 打一次 `https://api.ipify.org`，拿到出口 IP 就算可用。

复检前会按「本批真用到的端口」补一次出口 IP（去重后并发探），不会为了几个号去扫三十个端口。

```json
{
  "mihomo": {
    "enabled": true,
    "host": "cli-proxy-mihomo",
    "ports": "17901-17910",
    "probe_url": "https://api.ipify.org",
    "timeout": 10.0,
    "api": "http://cli-proxy-mihomo:9090",
    "secret": "",
    "pool": "http://cli-proxy-mihomo:8006",
    "pool_token": ""
  }
}
```

`host` 默认 `cli-proxy-mihomo`（docker 网络里的服务名），`ports` 默认 `17901-17910`，与 hub 的 `PROXY_DISCOVER_HOST` / `PROXY_DISCOVER_PORTS` 同源。

`api` 是 mihomo 的 external-controller，填了才能显示**节点名**。出口 IP 分不出线路——同一订阅里两个节点出口落在同一段是常事，卡片上两格数字一模一样，看着像同一个槽测了两遍。留空时按 `host` 推 `http://<host>:9090`；控制器设了 secret 就填进 `secret`。

**跑 mihomo-pool 的话，`pool` + `pool_token` 是更短的一条路。** 那个池子自己维护「节点 → 独享端口」的映射，`GET /api/v1/nodes` 一次就全拿到，还带节点类型：

```bash
curl -s "http://127.0.0.1:8006/api/v1/nodes" -H "X-Token: $(grep MPOOL_TOKEN .env | cut -d= -f2)" | jq '.nodes[0]'
# {"name":"香港住宅01","type":"vmess","delay_ms":210,"tested":true,
#  "listener_port":17950,"pinned":false,"quality":{...},"match":true}
```

`pool` 留空按 `host` 推 `8006`（面板和内核在同一台机器上）；`pool_token` 是 `.env` 里的 `MPOOL_TOKEN`。**这和 9090 的 `secret` 是两个东西**：前者是面板登录用的，后者是池子写在 `runtime/secret` 里的 32 位十六进制。两个填混会各得一个 401，`pool_token` 留空时程序会拿 `secret` 再当面板 Token 试一次，所以只填一个也可能通。

**节点名是怎么拿到的**（官方 mihomo 的 API 里其实没有「列出所有监听端口」这个端点，`hub/route/server.go` 的路由表里没有）：

1. 先试 `GET /listeners` —— 一步到位，但要面板 / fork 才有，官方 404 是常态。
2. 再问 mihomo-pool 面板的 `GET /api/v1/nodes` —— 一次 HTTP 拿到全部端口和节点名，代价最低，所以排在反查前面。
3. 前两条都不行，才退到 `GET /connections` 反查 —— mihomo 每条连接带 `inboundPort`（从哪个监听进来的）和 `chains`（出站链路，第一个是真正用的节点）。于是对着每个端口各开一条隧道**先别关**（HTTP CONNECT 不通就试 SOCKS5，listener 的 type 从外面看不出来），再读一次连接表，端口和节点名就对上了。连接一断记录就没了，所以必须挂着读。

第 3 步要开 N 条长连接，所以只在点「自动检测」时走；打开页面那一趟只问前两步。

池子的 listener 名是 `HUB-E01` 这种按端口升序编的序号（重排就变），节点名只在 `proxy` 字段上——所以卡片上的名字来自 `proxy`，不要拿 listener 名去反推节点。

每张卡片一行四段：**端口 → 节点名 → 类型 → 延迟**。端口不写全 `127.0.0.1:17901` —— 十张卡片这串前缀一模一样，占掉十几个字符却分不出任何东西，留五位数就够认。类型（住宅 / 机房 / 代理）按节点名推断，名字里没写就不标：同一个订阅里机房 IP 和住宅 IP 混着，这两三个字是唯一能分辨的地方，猜错比不标更误导。出口 IP 退到鼠标悬停的提示里。

控制器拿不到名单时不写「未通」了事 —— 地址错、缺 secret、端点不存在三种情况的处置完全不同，所以分开报（多条原因用「；」拼在一起，不互相冲掉）：

| 现象 | 提示 |
|---|---|
| 9090 返回 401 / 403 | secret 不对或没填，并指明 mihomo-pool 的在 `runtime/secret`（32 位十六进制，不是面板 Token） |
| 8006 返回 401 / 403 | 面板 Token 不对，并指明是 `.env` 的 `MPOOL_TOKEN` |
| 连接被拒绝 | 控制器没开，或端口不对 |
| 域名解析不到 | 跨容器要写服务名，宿主机上要写 127.0.0.1 |
| `/listeners` 404 且反查也没结果 | 说清是「官方不带这个端点」+ 反查卡在哪一步 |
| 通了但 `listeners` 为空 | 端口不是用 `listeners` 段开的，一个端口一个节点这件事不成立 |
| 8006 连不上 | 不报错（面板没开只是这条捷径走不通，不冒充成控制器故障） |

已经配过 hub 的机器直接就能复用，也可以用环境变量 `WB_PROXY_DISCOVER_HOST` / `WB_PROXY_DISCOVER_PORTS` 覆盖。

没分配端口（`exit_port=0`）的行就是直连——不会因为代理挂了就跳过检查，也不会偷偷回退到「随便挑个节点」，那样用户会以为自己选的没生效。

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
所以直接 `curl` 仓库里的原始脚本也能跑，不会拼出 `repos/qq928820655/wk-switch/releases/latest`。

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

页面「升级」标签页 → 检查更新 → **一键升级并重启**。一个请求里做完下载 →
替换 → 重启 → 自检，页面自动等新进程应答后刷新，不再需要点第二次。

分两个按钮的话，用户点完升级忘了点重启，就是「代码新的、进程旧的」，版本号
对不上更让人迷惑。

**重启为什么值得单独写一节**

旧实现是「`Popen(新进程)` 然后 `os._exit(0)`」，三个坑全踩过：

| 症状 | 原因 | 现在 |
|---|---|---|
| 重启后打不开 / 换了端口 | 新进程起太早，旧进程还没让出端口，`find_port` 往后找 | 监督进程先确认端口空，再拉起；`--wait-port` 等不到就报错退出，绝不漂移 |
| 服务没回来，也不知道为什么 | 子进程 stdout/stderr 丢进 DEVNULL，或写进了没人读的文件 | 日志写当前部署实际在用的那份（部署态 `logs/wk-switch.log`），失败时页面直接摊开日志尾部 |
| 新版本起不来就彻底失联 | 没有回退路径 | 60 秒没应答自动回滚到升级前的备份，再拉起一次 |
| 升级完要重新登录 | 会话只放内存，重启即失效 | 会话落盘 `data/sessions.json`，重启后仍然有效 |

重启由 `wk/relaunch.py` 的**监督进程**执行，状态机写在 `data/restart.json`
（`preparing → waiting → starting → verifying → ok / rolledback → failed`），
页面轮询 `/api/ping`（免鉴权）判断新进程是否就绪，判据是「pid 换了 + 版本对
得上」——只判端口通会把还没退出的旧进程认成新进程，页面刷新的瞬间正好撞上
它退出，浏览器看到的就是连接被重置。

回滚挑备份的规则是「版本号低于当前、且最接近当前」的那一份。失败的新版本自己
也会在 `backups/` 里留一份，按时间挑最新的会把坏代码滚回来；这类备份会被改名
成 `.zip.broken`。

**Linux 上的两条重启路径**

部署形态不同，重启方式必须分开，混着来一定会失败：

| 形态 | 怎么重启 | 为什么 |
|---|---|---|
| systemd（`Type=simple`） | 监督进程执行 `systemctl --no-block restart <unit>` | 进程归 systemd 管，`is-active`、日志、开机自启才对得上。自己 Popen 会让端口有服务、systemd 却认为已停，下次 restart 再起一个就撞端口 |
| nohup（`start.sh`） | 监督进程直接 Popen `main.py` | 不经 `start.sh` 转手：它是 `nohup ... &` 后立刻退出，Popen 拿到的是那个 bash 的 pid，bash 一死就会误判「新进程崩了」 |

systemd 的 `KillMode` 默认是 `control-group`，停服务时会 SIGTERM 整个 cgroup。
监督进程要是也在里面，它刚发出 restart 就被自己一起杀掉，回滚一行都跑不到。
所以 `install.sh` 生成的 unit 带 `KillMode=process`，同时监督进程优先用
`systemd-run` 起一个 transient unit（独立 cgroup）。**已经装过的机器要重跑一次
`install.sh` 才会更新 unit**；没更新的机器上，若拿不到 `systemd-run`，会降级成
「旧进程直接按重启键」，没有自动回滚，但有确定的结局。

**探测端口时必须带 `SO_REUSEADDR`（POSIX）**

服务的 socket 是 `ThreadingHTTPServer`，`allow_reuse_address = 1`。上一个连接由
服务端主动关闭后，端口有约 60 秒 TIME_WAIT，服务自己照样能绑；裸 socket 不设
`SO_REUSEADDR` 就必然 `EADDRINUSE`。探测比现实更严，得到的结论就是「旧进程没
退出」——而它其实早就没了。

Windows 上反过来，**不能设**：那边 `SO_REUSEADDR` 会放过正在 LISTEN 的端口，
判据直接失效。所以两边分开写，`main.port_free()` 与 `relaunch._bindable()` 一致。

另外，等端口超时后会先 `kill -0` 看一眼旧进程：**已经死了就继续拉起**，不当成
「端口被占」。`_wait_port()` 因此返回三态 `ok / dead / busy`，`dead` 走正常流程，
`busy` 才去结束旧进程。

**制品源**

- GitHub Releases（默认）：`config/update.json` 的 `repo`，或 `.git` 的 origin
- 任意制品索引：`index_url` 返回 `{"version": "1.0.1", "url": "包地址"}`

**下载拉不动时**（国内直连 `github.com` 的 release 资产常被掐断，而
`api.github.com` 往往还能通，所以「检查更新成功、下载卡住」是常见组合）：

- 页面「下载代理」填本地代理，如 `http://127.0.0.1:7890`；留空则用环境变量
  `HTTPS_PROXY` / `HTTP_PROXY`
- 或用「本地包升级」：浏览器里手动下载 `wk-switch-x.y.z.zip`，在页面上传，
  走浏览器自己的网络栈，与本服务无关；同样是一步到位（替换 + 重启）

升级只替换 `main.py` `server.py` `wk/` `web/`，`data/` `config/` 不动，
`deploy/` 也不动（安装器里的端口、路径、服务名是装的时候定的，被覆盖会冲掉
用户的部署配置）；每次升级前旧代码自动打包到 `backups/`。

包版本低于当前版本时按回滚处理，结果里会标 `downgraded: true`。

**出错时的手动兜底**

- 页面失败后会出现「回滚到升级前的备份」，点一下回到上一版并重启
- 「只重启服务」按钮不依赖升级，任何时候都能用
- 日志：`data/logs/relaunch.log`（监督进程）、`data/logs/server.log`（服务本身）
- 状态：`data/restart.json`

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

## 图标

图标是代码生成的，不存二进制源文件——改个颜色或比例重跑一次就行，不用找设计文件：

```bash
python tools/make_icon.py          # 输出到 web/assets/
python tools/make_icon.py --check  # 只检查文件齐不齐
```

打开 `tools/icon-preview.html` 能看各尺寸效果：标签栏、深色任务栏、16～192 阶梯、圆形/方圆/水滴三种系统裁切。

```
web/assets/icon.svg                矢量源
web/assets/favicon.ico             16 / 32 / 48 三档
web/assets/icon-192.png            PWA
web/assets/icon-512.png            PWA / 商店
web/assets/icon-maskable-512.png   Android 自适应图标（满幅背景 + 箭头缩进安全区）
web/assets/apple-touch-icon.png    iOS 180
web/assets/manifest.webmanifest
```

图形是 indigo 渐变圆角方 + 白色双向箭头，和页头 logo、favicon、任务栏用同一张图。低于 24px 会自动切到「极小版」几何（杆更粗、三角更大、留白更少），否则 16px 下三角会被量化掉，看上去只剩两条白线。

光栅化是脚本自己算的（SDF + 解析抗锯齿），不依赖 Pillow——项目声明仅标准库，图标脚本也守住这条。

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
  clock.py           时间源：北京时间（UTC+8），全站唯一出口
  auth.py            访问口令与会话
  update.py          在线升级
  version.py         版本号
web/                 index.html / login.html / app.js / style.css
web/assets/          应用图标与 manifest（由 tools/make_icon.py 生成）
deploy/linux/        install.sh / uninstall.sh
deploy/windows/      install.bat / start.bat / stop.bat / uninstall.bat
samples/             九个样例包与生成脚本
tests/               单元测试
tools/               图标生成脚本、预览页、带登录的无头截图脚本
```

## 测试

```bash
python -m unittest discover -s tests -v
```

## 许可

MIT
