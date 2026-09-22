# 可转债跟踪

一个面向个人研究的可转债行情跟踪与扫描工具。项目会从东方财富延迟行情接口获取盘后数据，生成可转债热度榜和历史快照，并通过一个适合桌面和手机访问的本地 Web 看板展示结果。

## 主要功能

- 按成交额跟踪可转债热度 Top 30
- 查看价格、成交额、涨跌幅、发行规模等行情指标
- 查看历史热度变化、放量扫描和蓄势扫描结果
- 查看个券历史行情、分时数据及对应正股信息
- 日K图标注周几：横轴显示「09-18 周五」（周一加粗）、每周一画浅色竖虚线，悬停显示「日期 周几」，便于观察周内节奏（分时卡片标题也带日期与周几）
- 展示上证指数、深证成指和中证转债等指数
- 内置模拟仓：榜单行尾「＋模拟仓」点一次按现价买入 10 张（1 手，张数可在模拟仓页签的设置里改）；已在模拟仓的券显示「✓ 已加入」，再点只提示、不重复买入；持仓张数可用 −/+ 按 10 张加减，并记录成交与盈亏
- 支持手机通过同一局域网访问，并可添加到主屏幕
- 每半小时把模拟仓持仓（浮动盈亏、逐笔明细、强赎/高溢价/应卖日告警）推送到手机（ntfy，见下文）
- 将每日快照、扫描结果和研究记录保存在 `data/` 及 Markdown 文件中

## 运行环境

- Python 3.10+（仅使用 Python 标准库，无需安装第三方依赖）
- 可访问互联网，用于请求东方财富延迟行情接口

## 快速开始

在项目根目录运行：

```bash
python3 app.py
```

然后在浏览器打开：

```text
http://127.0.0.1:8734
```

程序会在启动时读取 `data/` 中已有的快照，并按需检查和更新数据。终端会同时打印局域网访问地址；手机和电脑连接同一个 Wi-Fi 后，可以使用该地址访问看板。

## 数据更新与扫描

常用脚本：

```bash
# 抓取每日行情快照
python3 fetch_daily.py

# 扫描放量、量价关系并生成报告
python3 scan_volume.py --report

# 扫描蓄势信号
python3 scan_coil.py

# 统计蓄势分层结果
python3 coil_study.py

# 采集行业资金流
python3 sector_flow.py
```

脚本生成的 JSON 结果默认写入 `data/`，扫描报告写入项目根目录。数据源为东方财富延迟行情，适合盘后跟踪和研究，不应作为实时交易数据或投资建议。

## 持仓推送到手机（每半小时）

把模拟仓的持仓推到手机，用的是 [ntfy](https://ntfy.sh)（开源、免费、可自建服务器，一条 HTTP POST 就够，不需要注册账号）。

### 数据是怎么流动的

```text
index.html 模拟仓(localStorage)  ← 唯一的真源，买卖账目只在这里
      │  每次改动自动 POST 一份副本(防抖 1.5 秒)
      ▼
app.py  POST /api/sim  ──────►  data/portfolio.json   ← 服务端只存只读镜像
      │
      ▼
push_holdings.py（每半小时跑一次）
      ① 读镜像  ② 批量取行情  ③ 算盈亏/风险  ④ POST 到 ntfy
      ▼
手机 ntfy App
```

要点：
- **服务端不参与记账**。它只保存一份镜像，`push_holdings.py` 只读不写。账目永远以浏览器里那份为准。
- **⚠ 换浏览器打开不会冲掉服务端持仓**：新浏览器的 localStorage 是空的，此时启动同步会**拒绝**推送空仓，只在模拟仓页签的「推送镜像」chip 上提示。要发布服务端那份，用页签里的「导出/导入」。
- 推送内容里的数字**全部由 `push_holdings.py` 算出**，没有 LLM 参与，口径与看板的 `renderSim()` 逐字一致（成本含费、停牌按买入价计）。要加"解读"文字时，让模型只写理由、不碰数字。

### 手机端：装 App + 订阅话题

1. Android 装 **ntfy**（[Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/)）；iOS 装 **ntfy**（App Store）。
2. 打开 App → 右上角 `+` → **Subscribe to topic** → 填 `push_config.json` 里 `ntfy.topic` 的值（就是本机已生成的那个 `kzzt-xxxxxxxxxxxx`）。
3. 想省电可只留 ntfy 的后台白名单；F-Droid 版不含 FCM，需在 App 里开"Instant delivery"（走前台长连接）。

> **topic 就是密码**：ntfy 公开服务器上没有账号，谁知道话题名谁就能收到、也能往里发。所以本项目生成的是 12 位随机串，且 `push_config.json` 已加入 `.gitignore` 不提交。介意行情明细过第三方服务器的话，可以 `docker run -p 80:80 -v /var/cache/ntfy:/var/cache/ntfy binwiederhier/ntfy serve` 自建，再把 `ntfy.server` 改成自己的地址。

### 本机操作

```bash
# 1. 第一次会生成配置文件, 编辑它填 ntfy.topic
python3 push_holdings.py --config

# 2. 验证通道(发一条固定测试消息到你手机)
python3 push_holdings.py --test

# 3. 只看消息长什么样, 不发送
python3 push_holdings.py --dry-run --force

# 4. 之后正常启动 app.py 即可 —— 内置调度会自动按时推送, 不需要额外装东西
python3 app.py

# 可选: 想让 app.py 没开时也能推, 才需要装 launchd 任务(先解决 TCC, 见下)
bash install_push.sh

# 其它
bash install_push.sh status      # 看 launchd 状态 / 配置 / 去重状态 / 最近日志
bash install_push.sh uninstall   # 卸载
```

> 安装 launchd 那一步必须在 **Terminal.app** 里执行：`launchctl bootstrap` 在 AI/沙箱会话里会报
> Error 5（见 `.workbuddy/memory/2026-09-07.md`）。沙箱里放行后可以装上，但**装上也跑不通**——
> 卡在 TCC，原因和解决办法见上一节。

### 什么时候会推、什么时候不推

**两条触发路径，去重后你只收到一条：**

| 路径 | 前提 | 状态 |
| --- | --- | --- |
| **`app.py` 内置调度**（`_start_push_scheduler`） | 只要 `app.py` 在跑 | ✅ 开箱可用，**推荐** |
| launchd 定时任务（`install_push.sh`） | 见下面的 TCC 限制 | ⚠️ 默认跑不通 |

> **⚠ 为什么推荐内置调度：** macOS 的隐私保护（TCC）让 launchd 的后台进程**读不了 `~/Documents`**。
> 实测 plist 能加载成功，但一跑就 `exit 126`，日志是
> `getcwd: cannot access parent directories: Operation not permitted`。
> 而 `app.py` 跑在你自己的终端里没有这个限制。又因为**持仓镜像本来就要靠网页同步**，
> 推送离了 `app.py` 没意义，所以调度放它里面零成本、零授权。
>
> 想让 launchd 也生效（这样 `app.py` 没开时也能推），二选一：
> - **A（推荐）** 把整个项目移出 `~/Documents`，例如 `mv ~/Documents/可转债跟踪 ~/可转债跟踪`，再重跑 `bash install_push.sh install`；
> - **B** 系统设置 → 隐私与安全性 → 完全磁盘访问权限，手动加入 `/bin/bash` 与你的 `python3`。
>
> `bash install_push.sh status` 会自动识别并提示这一点。

`app.py` 每 5 分钟粗筛一次（工作日 9:20~15:10），**具体推不推由 `push_holdings.py` 判定**
（时段规则只有 `sector_flow.session_of()` 一处真源）：

| 情况 | 行为 |
| --- | --- |
| 盘中（9:30~11:30、13:00~15:00） | 推送，约 10 条/交易日 |
| 盘前 / 午间休市 / 盘后 / 周末 | 跳过（数据冻结在上一时点，推了是噪音） |
| 距上次推送不足 25 分钟 | 跳过（两条触发路径不会各发一条） |
| 模拟仓为空 | 跳过（不发"空仓"这种废话） |
| 服务端还没有镜像 | 跳过，提示先打开看板 |
| 行情完全取不到 | 跳过（宁可不推，也不推错数） |

推送的成败会直接打在 `app.py` 的终端里（`[推送 10:30:01] ✓ 已发送 (id=...)`），
也可以从 `GET /api/refresh/status` 的 `push_*` 字段读到。

### 消息长什么样

标题：`持仓 10:30 ▲ +128.40元 (+1.20%)`

```text
持仓 7 笔 · 浮动 -1.08 元 (-0.01%)
[腾讯行情, 东财接口不通]

明新转债 111004
  成本 1,360.85 · 最新 136.072 (09-21) · 盈亏 -0.13 (-0.01%)

振26转债 113709
  成本 1,741.07 · 最新 174.089 (09-21) · 盈亏 -0.18 (-0.01%)
…
```

逐笔**只保留四项**（用户 2026-09-22 指定，与看板持仓表打红框的四列一一对应）：
**转债（名称+代码）/ 成本(含费) / 最新价(带行情日期) / 浮动盈亏(额+%)**。

- 顺序**保持看板持仓表的建仓顺序**，不按盈亏排序——这样手机和看板能对着看，列表也不会随价格跳动。
- 带上行情日期是为了能一眼看出价格是不是今天的（比如盘前拿到的是昨收 `(09-21)`）。
- 市值、张数、买入价、涨跌幅、总资产、现金、已实现盈亏都**不进正文**；想加回来改
  `push_holdings.py::format_report()` 即可，数字在 `compute()` 的返回值里都是现成的。
- 另有三类**条件性**提示行（只在触发时出现）：行情源降级、停牌按成本价计、镜像来源/年龄。
  还有告警段（强赎 / 高溢价 / 已到应卖日）——这是唯一带判断的内容，其余全是数字。

### 行情源

东财 `push2delay` 优先（能拿到溢价率、强赎触发价）；**它断连时自动退到腾讯行情**算价格与盈亏（实测两者价/涨跌幅完全一致），慢变字段（溢价率/强赎触发价）用上次东财成功抓到的缓存并在消息里标注时点。消息里会写明当前用的是哪个源——例如 `[腾讯行情, 东财接口不通 · 风险字段用 09:30 的缓存]`。

### 通道换成钉钉/飞书/企业微信

目前只实现了 ntfy。若想换成群机器人 webhook，改 `push_holdings.py::send()` 一个函数即可——`format_report()` 产出的 `title/body/priority/tags` 与通道无关，群机器人只要 markdown 文本，把 `body` 塞进它的 `text.content` 就行。

## 测试

运行行业资金流模块的离线测试：

```bash
python3 tests/sector_flow_test.py
```

`tests/` 中的其他 `*.mjs` 文件是前端功能冒烟测试，可使用 Node.js 运行。例如：

```bash
node tests/trend_smoke.mjs
```

其中 `tests/sim_smoke.mjs` 覆盖模拟仓的建仓/加仓/减仓账目（需要先运行 `python3 app.py`），`tests/weekday_smoke.mjs` 覆盖日K图的周几/每周一标记（纯本地，用真实K线断言日期都落在周一~周五），`tests/voltab_smoke.mjs`、`tests/coil_smoke.mjs`、`tests/qijin_pending_smoke.mjs`、`tests/bond_detail_smoke.mjs` 需要本地服务在跑。

推送相关的两套：

```bash
# 推送逻辑: 盈亏口径/时段门/去重/风险标记/降级/镜像校验 (139 项含 --live, 纯离线部分 134 项)
python3 tests/push_test.py
python3 tests/push_test.py --live    # 额外验证真实行情接口

# 模拟仓 -> 服务端的同步 (纯本地, 含"空仓不许覆盖服务端持仓"的 clobber 防护)
node tests/push_sync_smoke.mjs
```

> 已知既有失败：`voltab_smoke` / `coil_smoke` / `qijin_pending_smoke` / `bond_detail_smoke` 里有若干项把断言写死到了具体数据状态（如「已完成数 = 346」、「09-14 进表的仍是 6 行」、「20/30 只 ≥80 根」），数据推进后就失效。已用对照实验确证与本项目历次改动无关（见 `.workbuddy/memory/2026-09-20.md`），待改成"性质断言"。

## 目录说明

| 路径 | 说明 |
| --- | --- |
| `app.py` | 本地 Web 服务和 API（含 `POST /api/sim` 持仓镜像接口） |
| `index.html` | 前端看板页面（模拟仓改动时自动同步到服务端） |
| `fetch_daily.py` | 行情快照抓取 |
| `scan_volume.py` | 放量及量价扫描 |
| `scan_coil.py` / `coil_study.py` | 蓄势扫描和统计 |
| `bond_detail.py` | 个券详情及按需补抓 |
| `sector_flow.py` | 行业及板块资金流（`session_of()` 是交易时段判定的唯一真源） |
| `push_holdings.py` | 持仓推送脚本（东财优先 / 腾讯兜底 / 时段门 / 去重） |
| `push_run.sh` | launchd 用的包装脚本（挑可用的 python + cd 到项目目录） |
| `install_push.sh` | 生成并安装 launchd 定时任务 |
| `push_config.json` | 推送配置（**含 topic 密码，已 gitignore**） |
| `data/` | 行情快照、缓存、扫描结果、持仓镜像 |
| `data/portfolio.json` | 模拟仓的服务端只读镜像（由前端 POST 写入，**已 gitignore**） |
| `tests/` | 离线测试和前端冒烟测试 |

## 注意事项

- 行情接口可能受网络、接口限流或交易日影响；抓取失败时请稍后重试。
- **`push2delay.eastmoney.com` 会因突发请求对本机 IP 断连**（TLS 握手成功、服务端直接断开，不是网络问题）。遇到时等几分钟通常自行恢复；`push_holdings.py` 已内置腾讯兜底，`fetch_daily.py` 没有兜底。
- 改了 `app.py` 后**必须重启进程**才生效（前端 `index.html` 是每次请求读盘，刷新即可）。模拟仓页签的同步 chip 若显示「接口不存在 —— app.py 是改动前的旧进程」，就是这个原因。
- `data/` 中的历史数据属于本地研究缓存，运行更新脚本会产生新的数据文件。
- 本项目仅用于个人数据整理和量化研究，不构成任何投资建议。
