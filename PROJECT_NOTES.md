# autotrade — Pine 策略驱动的自动化量化执行系统

> 当前交接基线：2026-11-02 第三阶段本地事务账本与故障恢复，见 [第三阶段成果记录](docs/milestones/2026-11-02-phase3.md)、[第二阶段成果记录](docs/milestones/2026-11-02-phase2.md)、[第一阶段里程碑](docs/milestones/2026-11-02-phase1.md) 和 [当前架构](ARCHITECTURE.md)。安装交付暂不在范围内。
> Pine 指标实现策略，Python 实现公共执行。首个模型是用户的 EMA9-ST-ATR；`ema9_pyramid` 是保留的兼容标识。
> 生产入口为 `console.app:app`；`main.py` 和 `auto_runner.py` 保留为历史入口。
> 下方 2026-10 的“当前状态”“已确认”及人工提案/TRAIL 描述为历史记录，不能覆盖上述最新边界。
> 新指标开发的长期规则已记录在根目录 `AGENTS.md` 与 [Pine 指标规范](docs/PINE_INDICATOR_CONTRACT.md)；后续用户只需描述策略逻辑。

> 开发日志 / 交接文档。最后更新：2026-10-02（浏览器模式切换 + v2 全量化订单）。
> **本文档自包含**：所有 IBKR 实战经验已完整收录于第 6 章，不再依赖
> 同级项目 nautilus_ibkr_helloworld。
> 下次继续开发前，请先读完「当前状态」「IBKR 实战手册」「踩坑记录」「待办」四章。

---

## 1. 项目目标与总体架构

三层人机结合架构，每层各司其职（2026-10-01 与用户确认）：

```
TradingView（信号层）
  Pine 策略/指标，K 线可视化调试（人的主场）
  plot() 暴露信号 → 信号有效性由人在图表上验证
      │  采集通道：云端 tv_cloud.js / CDP 活图表（均已实测）
      ▼
autotrade 控制系统（决策 + 仓位层，纯代码，本项目）
  engine/      信号采集、过滤排序、跳变检测、提案管理
  execution/   风控限额、IBKR 网关、仓位状态机
      ▼
IBKR（执行层）
  TWS 桌面版下单执行、成交回报、实际持仓
  人工最终确认（TV 提案卡）/ 随时在 TWS 手动干预
```

### 1.1 架构决策（已确认，勿推翻）

1. **IBKR 接入：ib_async + TWS 桌面版**。选 TWS 而非 IB Gateway：人要在 TWS 里
   看到下单过程并随时干预。TWS 每日自动登出需处理（Auto-restart 或定时重启，待做）。
2. **下单模式：下单前人工确认**。信号触发 → 交易建议卡片直接呈现在 TV 窗口
   右侧停靠栏（CDP DOM 注入，`#tv-prop-dock`），人在同一界面看 K 线做判断，
   点卡片按钮确认。决策粒度：批准 / 拒绝 / 改参数（仓位、止损可编辑后批准）。
3. **验证路径：直接实盘小仓位**（跳过 paper）。因此 execution/risk.py 的硬性
   限额是第一笔真实订单前的唯一兜底；且 `EXECUTION_ENABLED` 默认 0（dry-run），
   批准的提案只记日志不下单。
4. **止损止盈一体化：TRAIL 跟踪止损括号子单（v2 定稿）**。入场单与 TRAIL
   子单组成括号单同时下发（parentId + transmit 链），入场成交即生效，
   broker 端托管，引擎宕机也有效。初始触发价 = 1 分钟 SuperTrend(10,2) 线值，
   跟踪幅度 = 2×ATR(5M)，跟踪止损兼任止盈，不再挂独立 TP 单。
   每 bar 收盘的代码棘轮降级为可选的手动收紧（`update_trailing_stop`）。
5. **仓位状态以 IBKR 实际持仓为准对账**：本地状态机维护仓位视图，周期与
   IBKR 持仓 reconcile；人在 TWS 手动改仓后系统自动校准。
6. **全量化订单（v2 定稿）**：信号量化（5M ST 翻向跳变）→ 提案卡量化
   （仓位 = 净值×0.1%÷|入场−止损|，每一笔止损固定损失账户净值的 0.1%）→
   止损/止盈量化（TRAIL 自动跟踪）。**人只做信号的筛选确认**。
7. **信号对接用 pull 不用 push**：把 Pine 下单条件 `plot()` 成变量，系统对齐
   bar 收盘读数值、自己检测 0→±1 跳变。pull 读的是"状态"天然自愈（停机后
   再读仍是最新值）；push（webhook/邮件告警）是"事件"，TV webhook 3 秒超时
   且失败零重试，接收端任何抖动 = 信号永久丢失。仅未来上 1M/tick 级策略时
   再评估 push（届时优先邮件 IMAP 或云端收件箱+本地轮询，不要隧道直连家用 PC）。

### 1.2 信号 → 订单完整链路（v1 已实测）

```
每根 5M bar 收盘（+5s 缓冲）：
  tv_cloud.js 采集 20 标的（~35-55s）
    → ProposalManager.detect_edges()  检测 supertrend 方向 0→±1 跳变
    → build_proposal()                入场=收盘价，止损=∓2×ATR(14)，
                                      仓位=以损定量，止盈=2×风险距离
    → showProposals()                 宫格前 N 格灌入提案标的 +
                                      右侧停靠栏弹卡（格 N 角标一一对应）
盘中（1s 轮询）：
  人点卡 → pollDecisions → on_decision 回调同秒触发（终端日志+macOS 通知）
    → 批准 → RiskGate.check_new_order() → PositionManager.open_position()
      → marketable LIMIT DAY 入场单 → 成交后挂 STOP_MARKET GTC + 半仓 TP（OCA）
    → 拒绝 → 抑制：同 symbol+side 在信号归零前不再弹卡
    → 下一根 bar 收盘未决策 → 自动撤卡（expired，不抑制）
持仓期（每根 bar 收盘）：
  SuperTrend 线值棘轮式上移/下移止损（只向有利方向，1 美分防抖）
```

---

## 2. 当前状态（2026-10-01 v2，全部实测）

**v2 全量化订单链路已落地，dry-run 常驻运行验证通过，未发出任何真实订单。**

- 云端数据线：20 标的、双周期（5M 信号 + 1M ST(10,2) 止损线）一轮
  ~27-32s；K 线 + SuperTrend（双周期方向+线值）+ fast slow ma +
  5M 进攻区域 + ATR(14)。
- 本地控制线：宫格灌入（任意布局自适应，含不可见 widget 过滤）、
  K 线居中、提案卡停靠面板，全部实测。
- 交互链路：真人点击批准/拒绝/改参数全链路通过；回调同秒触发；
  拒绝抑制、过期作废、决策日志（logs/decisions.jsonl）生效。
- 执行层：25 项离线断言全过；IBKR 只读连通实测（TWS 7496，clientId 11，
  FA 24 账户，跨子账户持仓聚合正常）；main.py 多轮冒烟（一轮 ~30s），
  真实 NFLX 跳变信号自动弹卡。
- 订单形态：marketable LIMIT DAY 入场 + TRAIL GTC 子单（初始触发=1M
  ST(10,2) 线值，跟踪幅度=2×ATR）括号单同时下发；仓位=净值×0.1%÷止损距离。
- **TV 前端已切换到浏览器模式（2026-10-02，推荐）**：独立 Chrome 调试实例
  （`~/.tv-chrome-profile`），已登录（wangweiweicdma，pro_premium），
  四宫格主图布局，9222 正常；控制线全部功能实测兼容（宫格/提案卡/停靠面板）。
  **重启恢复两轮实测通过**：杀实例→重启→会话恢复（免登录、布局自动同步、
  单标签）→ main.py 不重连自动恢复（宫格 6/6 校验）。
  八宫格（4x2）仍未实测（待用户切布局）。
- 运行方式：`python3 main.py`（dry-run）；实盘 `EXECUTION_ENABLED=1 python3 main.py`。

---

## 3. 技术选型（开源依赖）

| 线路 | 开源项目 | 用途 |
|---|---|---|
| 云端数据 | `@mathieuc/tradingview`（TradingView-API，npm） | 图表会话订阅 K 线与指标；`getIndicator` 翻译私有指标 |
| 本地控制 | `tradesdontlie/tradingview-mcp`（MIT，vendored 在 `controller/vendor/`） | CDP 连接管理（connection.cjs 已加固） |
| CDP 通信 | `chrome-remote-interface`（npm） | Node 侧 CDP 客户端 |
| IBKR 通信 | `ib_async`（pip，ib_insync 官方后继，实测 1.0.0） | TWS API 异步封装 |

## 4. 文件结构

```
autotrade/
├── config.py                     # 全部配置（端口/股票池/策略阈值/IBKR/风控/开关）
├── main.py                       # 主入口：bar 对齐调度 + 决策轮询协程 + 全流程
├── requirements.txt              # websockets, ib_async
├── package.json                  # chrome-remote-interface
├── engine/
│   ├── strategy.py               # StockSignalData / select_top_signals / to_decision_payload
│   ├── cloud_collector.py        # subprocess 调 tv_cloud.js → StockSignalData（含 ATR、ST 线值）
│   ├── proposal_manager.py       # 跳变检测 / 抑制 / 过期 / 决策日志 / on_decision 回调
│   └── data_collector.py         # Mock + build_collector() 工厂（早期手写 WS PoC 仅参考）
├── execution/
│   ├── risk.py                   # RiskGate：以损定量 / 持仓上限 / 日亏损熔断（落盘）
│   ├── ibkr.py                   # IBKRGateway：ib_async 封装、FA 组分配、dry-run 门控
│   └── position.py               # PositionManager：仓位状态机 + 棘轮止损 + 对账
├── service/
│   ├── tv_cloud.js               # 云端数据服务（one-shot CLI，TradingView-API）
│   ├── indicators.json           # 指标 ID 注册表
│   ├── creds.json                # ⚠️ sessionid 凭据（600 权限，已 gitignore，禁止提交）
│   └── package.json
├── controller/
│   ├── tv_control.js             # Node CDP 控制器（CLI + daemon）
│   │                             #   listPanes / setGridSymbols / centerAll
│   │                             #   showProposal(s) / pollDecisions / hideProposal / clearProposals
│   ├── tv_control_client.py      # Python daemon 客户端（JSON-RPC over stdio，30s 超时重试 1）
│   ├── tv_cdp.py                 # 纯 Python websockets CDP 备用实现
│   └── vendor/tradingview-mcp/   # vendored 上游，connection.cjs 已加固
└── logs/                         # 运行期生成：decisions.jsonl / orders.jsonl /
                                  #   dryrun_orders.jsonl / halt-YYYYMMDD / equity_sod-*.json
```

## 5. 关键配置（config.py）

**线路开关**：`DATA_SOURCE="cloud"`（mock/cloud/ws）；`CONTROLLER_IMPL="node"`；
`CDP_HTTP_ENDPOINT="http://localhost:9222"`（**必须 localhost，127.0.0.1 会被
DevTools Host 校验拒绝**）。

**IBKR**（详见第 6 章）：`IBKR_HOST/PORT=127.0.0.1:7496`、`IBKR_CLIENT_ID=11`、
`IBKR_ACCOUNT_ID=F10251881`、`IBKR_FA_GROUP=dt_test`、`IBKR_FA_METHOD=""`（**必须留空**）。
均支持环境变量覆盖。

**执行门控**：`EXECUTION_ENABLED`（默认 0=dry-run；=1 或 env `EXECUTION_ENABLED=1`
才真实下单）。

**风控**：`RISK_PER_TRADE_PCT=0.001`（0.1% 净值，以损定量基准）、
`RISK_PER_TRADE_USD=200`（净值未知降级）、`RISK_MAX_ORDER_QTY=200`、
`RISK_MAX_OPEN_POSITIONS=3`、`RISK_DAILY_LOSS_LIMIT_USD=1000`（SOD 锚点口径）、
`ENTRY_LIMIT_PREMIUM_PCT=0.5`、`TRAIL_ATR_MULT=2.0`（TRAIL 跟踪幅度=2×ATR(5M)）、
`TP_RR=0`（不挂独立止盈，TRAIL 兼任）。

**调度**：main.py 内 `ALIGN_TO_BAR_CLOSE=True`、`BAR_SECONDS=300`、
`BAR_BUFFER_SEC=5`、`DECISION_POLL_SEC=1.0`。

---

## 6. IBKR 实战手册（自包含，全部实测/调研确认）

> 来源：nautilus_ibkr_helloworld 项目的实盘经验 + 本项目 2026-10-01 实测。
> 账户结构：FA（顾问）主账号 **F10251881**，下辖 23 个子账户（U11569128、
> U11576827、U11980621、U12032582、U12182759、U14342670、U14361133、
> U15408968、U15991856、U18082969、U18521365、U19788422、U19852501、
> U19883549、U19883558、U19886249、U19886254、U19886258、U19886274、
> U21068708、U9805874、U9809408、U9809946）。
> 账户组（Allocation Group）：**dt_test**。

### 6.1 连接

- **端口**：TWS 实盘=**7496**，TWS 模拟=7497，IB Gateway 实盘=4001，模拟=4002。
- **client_id 同一 TWS 实例必须全局唯一**，否则 IBKR 拒绝第二个连接。
  本机分配表：**autotrade=11**（config.IBKR_CLIENT_ID），临时测试=12；
  nautilus 项目占用 Data=2 / Exec=3，其抓取脚本=46。**新增连接先查此表。**
- 连接后**必须等握手完成（nextValidId）再发请求**，socket 一通就发会被 504 拒。
  ib_async 的 `connectAsync` 已内含等待。
- TWS 需开启：Settings → API → Enable ActiveX and Socket Clients，
  socket port 7496，信任 127.0.0.1。
- **TWS 每日自动登出**：日内系统需在 TWS 设置 Auto-restart，或启动脚本定时
  重启（待做，v1 未处理）。
- 断连 error 504 偶发，重连重试一次即可。

### 6.2 FA 账户组下单（重点，全部踩过坑）

- 在订单对象上直接设字段（ib_async Order 原生支持）：
  ```python
  order.faGroup = "dt_test"
  # order.faMethod 不设置（留空）！！
  ```
- **`faMethod` 带值会被 IBKR 拒绝（error 10226 / 321）**；留空则使用
  FA Group 在 TWS 里配置的默认分配方法，分配生效的确认是 **code 2184**
  （TWS 日志可见）。faProfile 与 faGroup 互斥，不要同时用。
- **分配后仓位落在子账户，主账号净额可能为 0**。读持仓必须跨子账户聚合：
  `ib.positions()` 返回全部账户的 `Position(account, contract, position, avgCost)`，
  按 `contract.symbol` 求和，且**必须过滤 `contract.secType == "STK"`**
  （组里有 SNDK 期权，股/期混加会得到错误净额）。
- **账户摘要**：`await ib.accountSummaryAsync("")`（空串）返回全部账户逐条 +
  "All" 汇总行；只传主账号只得主账号（净值 ~111 USD，几乎不持仓，会误判）。
  注意 ib_async 的 `accountSummaryAsync()` **没有 timeout 参数**，
  用 `asyncio.wait_for` 包。
- **FA 盲区护栏**：对账时若"broker 净仓=0 但保护止损单仍活跃"，疑似子账户
  持仓引擎看不见——**只告警，绝不自动撤止损**，否则真实持仓变裸仓。

### 6.3 订单规格（v2 定稿：量化括号单，execution/ibkr.py）

**括号单同时下发**（父单 `transmit=False` + 子单 `parentId` + `transmit=True`
传输链，整组原子提交）：

| 单 | 类型 | TIF | 说明 |
|---|---|---|---|
| 入场（父） | marketable **LIMIT**（限价=参考价×(1±0.5%)） | **DAY** | 不用 bare MARKET：延迟行情被拒 error 10349；不用 GTC：未成交单不隔夜，引擎宕机 IBKR 收盘自动作废（双保险） |
| 跟踪止损（子） | **TRAIL**（`trailStopPrice`=初始触发价，`auxPrice`=跟踪幅度美元） | **GTC** | 初始触发价 = **1M SuperTrend(10,2) 线值**；跟踪幅度 = **2×ATR(5M)**；止损止盈一体（盈利时跟踪线上移即止盈）；broker 端自动跟踪，引擎宕机也有效 |

- 仓位公式：**qty = floor(账户净值 × 0.1% ÷ |入场价 − 止损价|)**，
  每笔止损固定损失净值的 0.1%；净值取不到（dry-run 未连）降级固定 $200。
- **IBKR 的 order error 不一定事件化**（submit 看似成功但已被静默拒）：
  submit 后 **sleep 2.5s 主动核对**父子两单的 `orderStatus.status` +
  errorEvent 收集的致命码（10226 / 10349 / 201 / 202），
  Cancelled/Inactive/ApiCancelled 一律视为失败。
- 入场未成交（DAY 过期/被撤）→ 主动撤 TRAIL 子单防残留。
- 修改 TRAIL 触发价 = 改 `trade.order.trailStopPrice` 后同 orderId 重新
  placeOrder（ib_async 的 modify 语义）。
- 日志/回报：`trade.orderStatus` 有 filled / avgFillPrice。

### 6.3.1 云端多周期采集（v2，tv_cloud.js）

- indicators.json 的指标可带 `timeframe` 与 `inputs` 字段：按周期分组，
  每周期一个 chart session（共享一条 WS 连接），bars 取主周期（5M）。
- inputs 覆盖用 `PineIndicator.setOption(key, value)`，key 支持
  `in_N` / **inline 名** / internalID。⚠️ STD;Supertrend 的 ATR 周期
  inline 名是 `ATR_Length`（下划线），显示名 "ATR Length"（空格）会报
  not found；Factor 是 float。1M ST(10,2) 注册为 `supertrend_1m`。
- 采集器输出 `extra.st_1m_line`（1M ST 线值）与 `extra.supertrend_line`
  （5M ST 线值）。

### 6.4 跟踪止损（v2：broker 端 TRAIL 原生跟踪）

- TRAIL 子单随括号单下发后即由 **broker 按 trail_amount 逐 tick 自动跟踪**，
  无需代码每轮改价；引擎/网络中断不影响止损效力。
- `PositionManager.update_trailing_stop()` 保留为**手动收紧**通道
  （如用指标线主动上移触发价），棘轮只向有利方向移动 + 1 美分防抖。
- （v1 的"每 bar 收盘 SuperTrend 棘轮 modify"方案已弃用——native TRAIL
  更及时且更省。）

### 6.5 风控（execution/risk.py，实盘唯一兜底）

1. **以损定量（v2 净值口径）**：`qty = floor(净值 × RISK_PER_TRADE_PCT(0.1%)
   ÷ |entry − stop|)`，封顶 `RISK_MAX_ORDER_QTY`；净值取不到降级
   `RISK_PER_TRADE_USD` 固定额。每一笔止损 = 固定 0.1% 净值损失。
2. 持仓数 ≤ `RISK_MAX_OPEN_POSITIONS`。
3. **日亏损熔断**：日盈亏 = 当前净值汇总 − 当日锚点净值（SOD anchor，
   `logs/equity_sod-YYYYMMDD.json`，跨重启一致）。触发即写
   `logs/halt-YYYYMMDD` 文件，当日不再开新仓，重启不丢。
   ⚠️ 盘中首次启动锚点失真（已发生的盈亏看不到），会显式 WARN；
   账户摘要的 RealizedPnL/UnrealizedPnL 是**持仓累计口径不是当日**，
   不能用于日熔断（实测）。
4. 硬校验：qty>0、价格必须为正、多仓 stop<entry、空仓 stop>entry。
   （tv_cloud 字段名 bug 曾算出负止损价，靠这条拦住。）

### 6.6 dry-run 门控

`EXECUTION_ENABLED=0`（默认）时 IBKRGateway 的所有下单方法只记
`logs/dryrun_orders.jsonl` 并返回伪造 orderId（900000+），入场即"虚拟成交"，
PositionManager 全流程（保护单、棘轮、对账）照常走——**全链路可零风险演练**。
dry-run 下仍会连接 TWS（只读：持仓、净值），连接失败仅告警不阻塞。

### 6.7 运维与已知边界

- ib_async 的 INFO 日志会把每个持仓/成交回报都打出来（噪声极大），
  main.py 已压到 WARNING；错误由 `IBKRGateway._on_error` 接管。
- `daily_pnl` 当前是全部管理账户汇总（未按 dt_test 组成员过滤，偏保守）。
- `close_position` v1 只记状态；真实平仓 = 撤 OCA 挂单 + 反向 marketable 单（v1.1）。
- EOD 强平未实现（参考做法：15:45 ET 后第一根 M1 触发全平，未成交 DAY 单
  隔夜自动作废）。

---

## 7. TradingView 踩坑记录（都是实测换来的，勿重新推导）

### 云端数据线

**故障速查（数据通道是本项目命脉，先跑诊断再动手）**：

1. `python3 service/tv_diag.py` —— 一键分层诊断（凭据文件 → 凭据有效性 →
   正文提取 → WS 端到端真实采集 → 运行态就绪率），哪层失败直接给修复命令。
2. 分层修复速查：凭据失效 → `python3 service/dump_creds.py`（需 TV Chrome
   登录态）；正文失效 → 主路径 pine_text.js 每次启动自动取最新，备用
   `python3 service/refresh_pine_texts.py`；疑似 TV 再改协议 → 先查上游
   <https://github.com/Mathieu2301/Tradingview-API/issues>，再用
   `python3 service/sniff_tv_ws.py` 抓浏览器真实包逐字段对比（浏览器永远是对的）。
3. 运行期监控：console 健康巡检每 5s 检查 study 通道（错误风暴 >20 条/60s
   或 ts_c 就绪率 <50% 持续 120s）→ critical 告警 + iMessage（配置收件人后
   手机直达）+ 快照 `status.tv_study`；恢复自动解除。data_stale 只管 K线，
   tv_study_dead 管指标，两者互补。
4. 依赖纪律：`@mathieuc/tradingview` 锁死 3.5.2（exact pin）；升级前先在
   测试目录验证 `node service/tv_cloud.js --symbols NASDAQ:QQQ` 四指标出数。

- **TV 服务端协议变更（2026-10-27）**：`create_study` 必须携带脚本加密正文
  `text` 字段；translate 端点不再返回 `result.ilTemplate`（正文挪到
  `metaInfo.inputs` 里 `id="text"` 的 defval，见上游 TradingView-API
  issue #354），否则**所有** study（含内置、匿名连接）报 `Can't parse pine`。
  旧长连接被豁免（当日 12:48 起的进程正常到被杀）。
  **最终修复（service/pine_text.js，零 node_modules 补丁）**：加载指标时
  自抓 translate 提取 text defval，用库导出的 `TradingView.PineIndicator`
  重建实例注入——每次启动都拿最新正文，TV 重新登录密钥轮换也不怕；
  提取失败仅回退**同版本** `pine_texts.json` 缓存；版本错配明确报错，禁止旧正文拼新
  元数据。tv_cloud.js / tv_cloud_daemon.js 统一走 `loadIndicatorWithText()`。
  `python3 service/refresh_pine_texts.py` 是一键刷新工具：translate 无正文时自动
  抓官方 create_study 帧，校验 pineId/pineVersion，原子更新缓存并做真实订阅验证，
  失败自动回滚；加 `--restart` 会在验证后重启实盘并检查 attack 53/53。
  daemon 中 attack 为可选增强：加载失败只降级区间数据，不影响核心行情；
  attack 无值只聚合告警，不再销毁重建整个 chart。依赖锁死 `@mathieuc/tradingview@3.5.2`
  （package.json exact pin；v4 是 ESM 破坏性重写，移植版存于
  service/tv_cloud_daemon_v4_port.js 备用，未启用）。
- **凭据**：`service/creds.json` 内含 sessionid/sessionid_sign，从 CDP 提取
  （`window.user.auth_token` JWT 或 sessionid cookie）。提取脚本
  `/tmp/dump_creds.py`。sessionid 有效期未知，过期需重新提取。
- **`chart.periods` / `study.periods` 是倒序**（[0] 最新），tv_cloud.js 已转正序。
- **bar 的字段是 `max`/`min` 不是 `high`/`low`**（v1 在此翻车：ATR 读错键
  ≈收盘价，止损算成负数）。volume/close/open/time 字段名正常。
- **图形型指标（5M 进攻区域）必须 `session: 'extended'`**，regular 返回空。
- `study.graphic` getter 未就绪时抛异常，**必须 try/catch**。
- Node `process.exit()` 截断 >64KB 未 flush 的 stdout，必须在 write 回调里退出。
- supertrend 方向 = 最新 period 的 Up_Trend/Down_Trend 谁不是 1e100；
  线值同理（谁有效取谁），是天然追踪止损线。
- **attack_zone（5M 进攻区域）**：昨日日 K 高低 + 当日盘前高低构成静态区域，
  开盘定型全天不变；要的是 `study.graphic` 的 boxes/lines 价位；
  每天开盘后采一次即可（美东 9:35 后）。
- **「超级信号」（USER;f1157c...）pine-facade 404**：受邀保护脚本云端不可得，
  降级方案 CDP 读活图表（需 visible=true）。
- 已实测指标 ID（service/indicators.json）：`STD;Supertrend`、
  `USER;5c1a17e8fd934e30adb3aa7aad30a42c`（fast slow ma）、
  `USER;8ab4f534e5874b47b24a5ce5c49acace`（5M 进攻区域）。
- **并发采集可行性**：一条 WS 可挂多个 chart session（Premium 每 tab 8 图），
  20 只分 3 批并发可从 ~150s 降到 ~25s（待实现，参数默认 8）。
- 信号只在 bar 收盘有意义：已对齐 5M bar + 5s 缓冲，只用已收盘 bar。

### 本地控制线（CDP）

- 图表页两条等价路径：`window.TradingViewApi._chartWidgetCollection`（主）、
  `window._exposed_chartWidgetCollection._chartWidgetsDefs[i].chartWidget`（备）。
- **defs 顺序 ≠ 视觉行列顺序**：tv_control.js 用 `_mainDiv.getBoundingClientRect()`
  按 y 聚行、x 排序。
- **切布局后被移除的 pane widget 残留在 collection**（rect 0x0 但 symbol 读写
  正常，回读校验照样通过，极具迷惑性）——COLLECT_SRC 必须过滤
  `rect.w/h <= 50` 的不可见 widget。
- **TV 窗口在后台时 Chromium 把页内 setTimeout 节流到分钟级**：页内只做同步
  操作，所有等待放 Node/Python 侧。
- vendored `connection.cjs` 的存活探测已加超时加固 + evaluate 超时重连重试。
- 交易所前缀归一化：灌 `NASDAQ:AAPL` 回读可能 `BATS:AAPL`，校验只比 ticker。
- setSymbol 后切换需 5~20s，**校验必须查 API 层 `symbol().value()`**。
- 视图居中：`timeScale().setRightOffset(width()/barSpacing()/2)`；
  恢复默认 `scrollToRealtime()`。
- 隐藏（visible=false）的指标 TV 不计算，`data()._items` 为空。
- CDP 读活图表指标：`dataSources()` 过滤 `typeof s.metaInfo==='function'`；
  数值在 `s.data()._items`（正序，value=[time, plot0, ...]）；
  图形在 `s.graphics()`（内部结构，价位解析待做）。
- 策略测试器数据（上游 tradingview-mcp 已探明，可移植）：
  策略数据源识别用 `metaInfo().isTVScriptStrategy || is_strategy`；
  订单列表 `s.ordersData()`、报告 `s.reportData()`；**报告只在策略测试器
  面板打开且策略未隐藏时才计算**。

### 提案卡交互（CDP DOM 注入）

- 定稿形态：右侧停靠面板 `#tv-prop-dock`，实时测量 `.layout__area--right`
  （自选表容器）rect 精确覆盖，窗口 resize 事件驱动重定位；自选表关闭时
  退化右缘 300px。演进教训：fixed 钉视口（换标的错位）→ pane 内 absolute
  （遮挡 K 线）→ 右侧停靠面板（定稿）。
- 按钮事件必须 `addEventListener`（页内 CSP 可能拦截内联 onclick）。
- 点击结果写 `window.__tvDecisions`，pollDecisions 读即清空并自动移除对应卡片。
- 僵尸卡坑：showProposal 页内"先 appendChild 后 return"，return 抛错时 CLI
  报失败但卡片已注入。批量场景一律用 showProposals（清场重来）。
- daemon 通道 args 已是解析好的对象，dispatch 里不能再 JSON.parse
  （CLI 路径才是字符串）——此类 bug CLI 直调不暴露，必须走 Python 客户端实测。
- injectSrc 的 return 里不能引用 Node 侧变量（页内不存在），要注入字面量。

### TradingView 客户端运维

**浏览器模式（2026-10-02 起，推荐——桌面版 Electron 套壳刷新慢，真实 Chrome 更快）**：

```bash
# 独立配置文件，不影响日常 Chrome（也不暴露日常浏览数据给调试端口）
open -na "Google Chrome" --args --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.tv-chrome-profile" --no-first-run \
  "https://cn.tradingview.com/chart/"
```

- 网页版与桌面版是**同一套前端**：`_chartWidgetCollection` 内部 API、
  宫格控制、提案卡停靠面板全部实测兼容（2026-10-02 验证）。
- **桌面版和浏览器只能有一个占用 9222**——切换时先关另一个。
- 关闭/启动：`pkill -f "tv-chrome-profile"`（按 profile 路径匹配，
  **不会误杀日常 Chrome**）→ 上面 open 命令重启。
- 首次使用需在该 Chrome 实例里**登录 TV 账号**一次（云端布局自动同步过来），
  并关掉 Cookie 横幅。**之后 profile 持久化，重启免登录**；会话恢复会
  自动带回上次的图表标签（含布局）。
- **重启恢复已实测（两轮）**：杀实例→重启→免登录/布局自动同步/单标签
  →main.py 无需重启，daemon 重连逻辑（重新拉 /json/list 拿新 target）
  自动恢复，下一轮宫格校验通过。
- 坑：Chrome 154 刚启动时 `/json/list` 可能返回空（页面还没建），此时用
  `curl -X PUT "http://localhost:9222/json/new?<url>"` 主动开图表页
  （新版 Chrome /json/new 必须 PUT）。**注意：启动命令已带 URL 时先查
  /json/list 再决定是否 PUT，否则会开出两个相同的图表标签页**
  （关掉多余的用 `/json/close/{targetId}`）。
- 日常 Chrome 若已在运行，不影响（独立 user-data-dir 是另一个实例）。

**桌面版模式（备选）**：必须先杀掉所有存活实例再带参启动，否则 Electron
单实例机制丢弃调试参数：

```bash
pkill -f TradingView
open -a TradingView --args --remote-debugging-port=9222
```

- 图表页 target：page 类型、URL 含 `tradingview.com/chart` 或 `cn.tradingview.com/chart`。
- CDP target 的 ws URL 会变，重连必须重新 `GET http://localhost:9222/json/list`。

---

## 8. 验证命令备忘

```bash
# 语法检查
python3 -m py_compile config.py main.py engine/*.py controller/*.py execution/*.py
node --check service/tv_cloud.js controller/tv_control.js

# 云端数据线冒烟
cd service && node tv_cloud.js --symbols NASDAQ:QQQ --out /tmp/x.json

# 本地控制线冒烟
node controller/tv_control.js listPanes --json
node controller/tv_control.js showProposals '[{"symbol":"NASDAQ:QQQ","side":"long","entry":598.2,"stop":594.5,"qty":50,"risk":185}]' --json
node controller/tv_control.js pollDecisions --json

# 执行层离线测试（/tmp 会丢，丢失后按 8.1 节断言描述重建）
python3 /tmp/test_execution_offline.py

# IBKR 只读连通（不下单）
python3 /tmp/ibkr_readonly_test.py

# 全流程常驻（dry-run）
python3 main.py
# 实盘（先确认 config.RISK_* 参数！）
EXECUTION_ENABLED=1 python3 main.py
```

## 9. 临时测试脚本（/tmp 下，重启会丢，重要逻辑已并入正式代码）

- `/tmp/test_execution_offline.py` — 执行层 30 项离线断言（风控/状态机/棘轮/抑制）
- `/tmp/ibkr_readonly_test.py` — IBKR 只读连通验证
- `/tmp/live_proposal_test.py` — 提案卡回调实时验证（含 macOS 通知）
- `/tmp/tv_ws_poc/`、`/tmp/tvjs_poc/`、`/tmp/dump_creds.py`、`/tmp/tv_grid_*.py` — 历史 PoC

## 10. 待办 / 未验证项（按优先级）

1. **EXECUTION_ENABLED=1 实盘首单**：先按账户规模确认 `RISK_*`，最小仓位提案
   走真实下单，验证 FA 分配生效（TWS 日志 code 2184）与 TRAIL 括号子单生效。
2. **八宫格（4x2）实测**：用户切布局后跑 listPanes + showProposals 验证。
3. main.py 长时间常驻稳定性（TWS 每日登出处理；**浏览器重启恢复已实测
   通过**，更长周期待观察）。
4. 实盘入场 watcher 改事件驱动（trade.fillEvent）；close_position 真实平仓
   （撤 TRAIL 子单 + 反向 marketable 单）；EOD 强平（15:45 ET）。
5. 日盈亏按 FA 组 dt_test 成员过滤（当前全账户汇总，偏保守）。
6. tv_cloud.js 并发采集（--concurrency 8；双周期后一轮 ~30s 已够 5M 用）。
7. creds.json 过期自动从 CDP 重新提取。
8. 「超级信号」指标 CDP 降级读取（rejection_score 当前禁用）。
9. attack_zone 每日开盘后只采一次（数据全天静态）。

## 11. 决策模块接口（预留）

`engine/strategy.py` 的 `to_decision_payload()` 输出标准 JSON（version=1
schema），字段含 symbol / supertrend / rejection_score / atr / close / volume /
timestamp / extra。Decision Agent 直接消费该 payload，无需关心数据来源。
