# autotrade 交易控制台 — 详细设计文档

> 版本：2026-10-18 更新（5M trend_state + ST 延续/推进指标 + 开仓候选人工确认 + 中点限价单）
> 覆盖范围：TradingView 数据订阅 → 指标计算 → 下单执行 → 仓位管理 → 前端控制台的全链路设计。
> 配套文档：`PLAYBOOK.md`（交易规则）、`STRATEGY_RULES.md`（策略 v1）、`PROJECT_NOTES.md`（历史交接）。

---

## 1. 项目概述

autotrade 是一个「人机结合」的日内交易控制台，核心链路：

```
TradingView（数据 + 指标计算）→ 引擎（采集 + 下单计算）→ IBKR（执行）→ Web 控制台（人机交互）
```

**核心设计原则：严格分工（所见即所得）**

- **TradingView / Pine 指标**：负责所有行情类计算（EMA 快慢线、SuperTrend、通道、方向、趋势状态过滤）。
- **控制端（Python）**：只读指标输出值 + 下单执行 + 仓位/风控/账户 + 事件记录。
- **前端（Vue）**：所见即所得展示 + 仓位参数输入 + 手动下单（不再有自动信号提醒）。

行情指标绝不重复计算；控制端只保留「执行类」计算（仓位、金额、风控）。**所有行情过滤判断都在 Pine 指标里算好，控制端只读信号值。**

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ ④ 控制层   console/   FastAPI + Vue3                        │
│    REST + WebSocket；实时推送、下单、仓位管理、事件记录、时钟   │
├─────────────────────────────────────────────────────────────┤
│ ③ 执行层   execution/  IBKR 网关 + 订单批次 + 风控           │
│    ibkr.py / order_lot.py / risk.py（ib_async）              │
├─────────────────────────────────────────────────────────────┤
│ ② 引擎层   engine/  采集器 + 下单计算 + 策略                 │
│    realtime_collector.py / cloud_collector.py / order_calc.py│
├─────────────────────────────────────────────────────────────┤
│ ① 数据层   service/  TradingView 数据（Node.js）             │
│    tv_cloud_daemon.js（订阅）/ tv_cloud.js（一次性）          │
└─────────────────────────────────────────────────────────────┘
        ▲ 横切：心跳监控 + 断线检测 + 自动重连（execution/ibkr.py）
```

**语言分工**：数据层用 Node.js（唯一成熟的 TradingView 库 `@mathieuc/tradingview`），引擎/执行/控制层用 Python（`ib_async` 是 IBKR 事实标准）。

---

## 3. 技术选型

| 层 | 技术 | 说明 |
|---|---|---|
| TradingView 数据 | `@mathieuc/tradingview`（npm） | 私有 WS 协议订阅 K 线 + 指标，支持 USER 私有脚本 |
| IBKR 执行 | `ib_async`（pip，1.0.3） | ib_insync 官方后继，TWS API 异步封装 |
| 后端 | FastAPI + uvicorn | REST + WebSocket，asyncio 原生 |
| 前端 | Vue3 + Element Plus（CDN） | 深色主题，宫格交易台 + 底部 tab |
| 数据管道 | asyncio subprocess + JSON 行 | daemon 与 Python 之间解耦 |
| 事件日志 | JSONL 追加（按天归档） | 下单/平仓/撤单 持久化，可回溯 |

---

## 4. 数据流设计

### 4.1 实时行情流（订阅模式）

```
TradingView 云端 WS
   │  onUpdate（tick 级推送）
   ▼
tv_cloud_daemon.js（常驻 Node 进程）
   │  每标的 1 个 5M session，session='extended'
   │  只订阅 1 个 Study：trend_state（K线 + 指标同 session）
   │  内存维护 state，setInterval 1s 节流输出 JSON 行 + 30s 心跳
   ▼  stdout（每行一个 JSON）
realtime_collector.py（Python asyncio subprocess）
   │  逐行读 stdout → 解析 → 覆盖式更新 self._snapshot
   │  记录 latest_bar_time（数据时钟）、bar_update_delay（K线收盘→收到延时）
   ▼
console/app.py build_snapshot()
   │  合并 status（净值/日盈亏/时钟）+ market（44只实时）+ lots
   ▼
WebSocket /ws（每 1s 广播）
   ▼
Vue3 前端（宫格 + tab，实时计算限价/股数/金额/资金占比）
```

> **会话用 'extended'，不是 '24x7'**：24x7 会让美股数据卡在昨日收盘不推进。

### 4.2 实时快照数据结构

```jsonc
// collector.latest() 输出（每标的）
{
  "NASDAQ:SKHY": {
    "bar": { "time": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ... },  // 实时 5M K 线（未收盘）
    "bar_c": { "time": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ... }, // 上一根已收盘 K 线（稳定）
    "ts": {                            // 实时值（periods[0]，盘中抖动，仅显示用）
      "trend_state": 1,                // 1 单边多头 / -1 单边空头 / 0 震荡
      "direction": 1,                  // ST 方向：Up_Trend 有效=1 / Down_Trend 有效=-1 / 都无=0
      "ema_fast": 169.5,               // EMA20
      "ema_slow": 168.9,               // EMA100
      "supertrend": 169.64,            // Up/Down_Trend 中有效者
      "channel_edge": 169.84,          // Uptrend/Downtrend_Channel 中有效者（ST±1×ATR）
      "atr": 0.20,
      "st_duration_bars": 12,          // 当前 ST 方向已延续的 5M bar 数（翻向当根为 0）
      "st_push_count": 8               // 当前方向内 ST 线有利推进累计次数（翻向清零）
    },
    "ts_c": { ... 同上，但是上一根已收盘的「确认值」（periods[1]）... }  // 策略判断/下单全用这套，稳定不抖
  }
}
```

### 4.3 关键指标

| 指标 | 脚本 ID | 周期 | 用途 |
|---|---|---|---|
| `trend_state` | `USER;e577100fe79942b4830783d1ceaad535` | 5M（extended，range=200） | **唯一订阅指标**：趋势状态过滤 + 回踩入场线 + 方向 + 止损 ATR 全部来自它 |

plot 映射（订阅字段名，空格转下划线）：

| plot | 快照字段 | 说明 |
|---|---|---|
| `Trend_State` | `trend_state` | 1 单边多头 / -1 单边空头 / 0 震荡（过滤器） |
| `EMA_Fast` | `ema_fast` | EMA 快线（EMA 快线限价单的限价） |
| `EMA_Slow` | `ema_slow` | EMA 慢线（发散度排序用） |
| `Up_Trend` / `Down_Trend` | `supertrend` + `direction` | supertrend 值，谁有效即方向（Up 有效=1 / Down 有效=-1 / 都无=0） |
| `Uptrend_Channel` / `Downtrend_Channel` | `channel_edge` | channelEdge（通道边沿限价单的限价） |
| `Atr_Value` | `atr` | ATR（用户已在脚本加 `plot(atr, "Atr Value", display=display.data_window)`） |
| `ST_Duration_Bars` | `st_duration_bars` | 当前 ST 方向延续的 5M bar 数，翻向当根为 0 |
| `ST_Push_Count` | `st_push_count` | 当前方向内 ST 线有利推进累计次数，翻向清零 |
| `Body_Middle` | — | 中线（plot 输出之一，当前快照未使用） |

指标内部参数（Pine 里，所见即所得）：emafast=EMA20、emaslow=EMA100、SuperTrend(10,3)、channelEdge=ST±1×ATR；trendState 要求 ST 方向 + close/fast/slow 排列 + EMA 差值 > 1×ATR（回看 6 根）。控制端零逻辑，只读 plot 值。

**ST 推进定义**：多头仅本根 ST 线值高于上一根才累计一次，空头仅低于上一根才累计；ST 不变不计，方向翻向时清零。两个 ST 延续字段只用于选股/候选的人工比较，不参与进出场、止损或排序过滤。远程私有指标未同步新 plot 时字段为 null，界面显示「—」，不能当成 0。

---

## 5. 事件流程

### 5.1 下单流（三种限价单 + STP 子单）

```
宫格三个 tab：EMA快线（ema_fast）/ 通道边沿（channel_edge）/
             中点限价单（midpoint=(bar_c.high+bar_c.low)/2）
前端点「下单」（按钮按 trend_state 方向显示 买入/卖出，震荡禁用）
→ POST /api/lots/limit-stop {symbol, side, qty, limit_px, stop_px, limit_type}
→ 【下单拦截】trend_state 过滤：做多需 ts_c.trend_state==1、做空需 ==-1、
   0（震荡）拒绝；side 必须与 trend_state 同向；force 执意开仓保留
→ 止损统一 = 限价 ∓ 1×atr（多单减、空单加）
→ midpoint 由服务端以 bar_c.high/low + ts_c.atr 重新定价，不信任浏览器价格
→ OrderManager.place_limit_stop_lot()
   → RiskGate.check_new_order()
   → IBKRGateway.place_limit_with_stop()
      → LMT(父, DAY, transmit=False) + STP(子, GTC, parentId=父, transmit=True)
      → 显式分配 orderId + 2.5s 主动核对订单状态
→ OrderLot 落盘 logs/lots_state.json
→ 实盘：_watch_entry 轮询成交 → ACTIVE；dry-run：立即虚拟成交 ACTIVE
```

**中点限价单的 PENDING 改价**：独立 `_midpoint_limit_reprice_loop` 每秒检查；
当 `bar_c.time > reprice_bar_time` 时，以刚收盘 bar 的 `(high+low)/2` 和同套
`ts_c.atr` 重算限价/止损，经 `modify_limit_stop` 以相同 orderId 同时修改
STP 子单与 LMT 父单。每根 bar 每 lot 最多尝试一次，股数不变；失败保留原
挂单并记录，下一根 bar 重试。进入 ACTIVE 后不再中点改价，恢复原单向
止损管理。自动策略开关不影响手动中点单改价。

> 止损单只有 **STP** 一种。中点限价单与已删除的旧「收盘价限价单」
> （close-pending 整套）不同；旧 close-pending 与 TRAIL 路径不恢复。

### 5.2 下单后管理流

```
持仓(ACTIVE)
├── 改止损：update_trail（STP，只向有利方向棘轮，1 美分防抖）
├── 平仓：close_lot → CLOSING（记录 close orderId）→ 全部成交后才 CLOSED
├── 撤单：cancel_lot → 等券商终态；已成交部分转 ACTIVE，未成交才 CANCELLED
└── ST 反向自动平仓：_position_scan_loop 只处理本工具 lot；
    外部持仓由独立清理循环处理，不再按账户净额重复补刀
```

**父子单关联语义**：父单（限价）撤销/被拒时，子单（STOP）随 parentId 自动取消——即「平仓时子订单自动取消」。

### 5.3 持仓止损单守卫（每分钟）

```
_stop_guard_loop（每 60s，需 IBKR 已连接）
   → gateway.positions()（FA 组持仓）× gateway.open_protective_qty()
     （IBKR 开放 STP 单按 ticker 聚合，reqAllOpenOrders）
   → 每个持仓核对反方向保护单股数：多仓需 SELL 覆盖、空仓需 BUY 覆盖
   → 保护单股数 < 持仓股数 = 裸仓 → 日志 WARN + missing_stop_alert 事件
     + 前端语音/常驻弹窗，每分钟重复直到补齐（只告警，绝不自动补单/撤单）
```

### 5.4 外部订单与持仓清理（每 30s）

```
_external_cleanup_loop（需 EXTERNAL_ORDER_CLEANUP_ENABLED + 实盘连接）
   → 同时取得开放订单、持仓和旧工具 ID 白名单；任一快照失败则零写操作
   → 开放订单：88 号段或旧白名单保留；号段外挂单撤销并等待 Cancelled
   → 持仓：broker 净仓 − 工具 lot 净仓 = 外部残余
     → 用 88 号段 + orderRef=external_cleanup 的平仓单平掉并等待全部成交
   → 清理中的平仓单去重；UNKNOWN/账户无法确认只告警，不误动工具订单
```

### 5.5 心跳 / 断线流（横切）

```
_heartbeat_loop（每 2s）
   → reqCurrentTimeAsync（3s 超时）→ 检测 TWS 假死
→ 断线判定 ≤5s（socket 断开 disconnectedEvent 毫秒级 / 假死 2s+3s）
→ _on_disconnect → _connected=False
→ _reconnect_loop（每 5s 退避重连）→ connect() 恢复 + 重启心跳
```

---

## 6. 过滤体系（trend_state，核心）

### 6.1 不再有自动信号提醒

原两级信号体系（预警 + 入场信号）已整体删除（v2026-10-17）：
`_signal_detector_loop`、`_entry_state`/`_alert_state`、`_check_signal`、
`/api/stab/*`、`/api/signal/clear`、前端「信号」tab 全部移除。
现在没有任何自动信号提醒，**过滤器 + 人工下单**是唯一的入场路径。

### 6.2 过滤器 = trend_state

所有下单先过 `ts_c.trend_state`（Pine 确认值）：

- `trend_state == 1`（单边多头）→ 允许多单；`== -1`（单边空头）→ 允许空单
- `trend_state == 0`（震荡）→ 拦截
- **side 必须与 trend_state 同向**——trend_state 内部已含 ST 方向要求，
  两者天然一致，**不存在反向订单**

选股默认按 EMA 发散度 `|ema_fast − ema_slow| / atr` 降序（指标内部同款度量，
由 plot 值算出）；前端可切换为 ST 持续根数或 ST 推进次数降序，缺失值排最后、
并列时回退 EMA 发散度。多空两列都展示（多头列 = trend_state==1、空头列 ==-1）。

### 6.3 force 执意开仓

过滤拦截返回结构化错误 `{code: "not_long_candidate"}`（语义扩展为
「不在多头/空头列表」），前端弹「执意开仓」确认框，**必须填写开仓原因**
（必填输入框）后带 `force=true` + 原因重发。force 放行记 WARN 日志（含原因），
事件中带 `force=true` + `reason`，lot 备注为「执意开仓：<原因>」。

---

## 7. 模块详细设计

### 7.1 数据层（service/）

| 文件 | 职责 |
|---|---|
| `tv_cloud_daemon.js` | 常驻订阅：每标的 5M session + 1 个 Study，1s 节流输出 JSON 行 + 30s 心跳 |
| `tv_cloud.js` | 一次性采集 CLI（`--symbols --timeframe --session --range`） |
| `indicators.json` | 指标注册表（key/id/timeframe/inputs） |
| `sync_watchlist.py` | 从 TV 图表 CDP 同步自选列表（44 只）写回 config.py |

**daemon 关键逻辑**：
- 每标的只建 **1 个** `Session.Chart`：5M session（`{timeframe:'5', range:200, session:'extended'}`，
  200 根保证 EMA100 + 回看 6 根的 warmup）；
- 只挂 **1 个 Study（trend_state）**，默认输入即源码默认
  （ATR 10 / Factor 3.0 / Channel 1.0 / 回看 6 / 发散 1×ATR），无需 setOption；
- **ts 输出两套**：`ts`（实时，periods[0]，显示用）+ `ts_c`（确认，periods[1]，策略/下单用）；
  `direction`：Up_Trend 有效=1、Down_Trend 有效=-1、都无=0；
  `supertrend` / `channel_edge` 取 Up/Down 两个 plot 中有效者；
- `atr == null` 时下单被显式拦截（「ATR 缺失，请检查图表上的 trend_state 指标」），不静默算错。

### 7.2 引擎层（engine/）

| 文件 | 职责 |
|---|---|
| `realtime_collector.py` | subprocess 托管 daemon；`_reader_loop` 逐行解析；维护 `_snapshot`、`latest_bar_time`、`bar_update_delay` |
| `cloud_collector.py` | 一次性采集 → `StockSignalData`；解析指标到 `extra` |
| `order_calc.py` | `compute_order_params`、`calc_position`（以损定量）、`compute_live_order` |
| `strategy.py` | `StockSignalData` 数据结构、过滤打分（历史遗留） |

`order_calc` 字段：`ema_fast` / `channel_edge` / `atr` / `direction`；
止损 = `限价 ∓ 1×atr`。

**以损定量**：

```
股数 = min( 单笔风险金额 ÷ |限价 − 止损| ,  净值 × 仓位上限% ÷ 限价 )
```

风险金额由顶部「单笔风险 $」输入（默认 300，步长 100）；`maxPositionPct`（10–200%，默认 100%）为仓位上限。

### 7.3 执行层（execution/）

#### ibkr.py — IBKRGateway

| 方法 | 职责 |
|---|---|
| `connect` / `disconnect` | 连接 + 心跳任务 + 自动重连管理 |
| `place_limit_with_stop` | **LMT 父 + STP 子**（parentId 关联，显式 orderId） |
| `place_close` | 反向 marketable LIMIT 平仓（88 号段显式 orderId，支持 orderRef） |
| `place_stop_only` | 平仓失败/余仓变化后的独立 STP 保护单 |
| `modify_stop` | 改保护止损触发价（STP 改 auxPrice，券商确认后更新本地） |
| `modify_stop_qty` | 部分成交后调整 STP 覆盖数量（券商确认） |
| `modify_limit_stop` | PENDING 中点单逐 bar 改价（同 orderId 修改 STP 子单 + LMT 父单） |
| `_next_tool_order_id` | 集中分配 88 号段订单号，持久化并支持跨进程互斥 |
| `fa_accounts` | 动态查询 dt_test 组成员，失败回退 config |
| `positions` / `net_liquidation` | dt_test 组口径持仓/净值 |
| `realized_pnl` / `daily_pnl` | 已实现盈亏 / 日盈亏 |
| `last_price` / `cancel_order` | snapshot 现价 / 撤单 |

（`place_limit_with_trail` / `place_bracket` 已随 TRAIL 路径删除。）

**账户口径**：全部只针对 FA 组 `dt_test`（U11576827 + U18082969）。
**订单所有权**：新订单一律由集中发号器分配 88 号段 ID；旧订单用本地 lot 的
entry/stop/close ID 白名单兼容。快照查询失败返回 None，不得当成空仓。

#### order_lot.py — OrderLot / OrderManager

**OrderLot**：`lot_id / symbol / side / qty / entry_px / stop_px / limit_type / state / reprice_bar_time / ...` + 复盘字段（`entry_time / exit_px / closed_at / realized_pnl / exit_reason / signal_snapshot / take_profit_px / target_rr / actual_rr / mae / mfe`）。`reprice_bar_time` 持久化中点单最近改价尝试的 bar，重启后不重复修改旧 bar。

**OrderManager**：
- `place_limit_stop_lot`（LMT+STP，带 risk_usd + signal_snapshot）——唯一下单入口
  （`place_lot`、`place_limit_trail_lot` 已删除）
- `reprice_midpoint_lot`：仅 PENDING 中点单，逐 bar 同步改 LMT+STP，股数不变
- `close_lot` / `cancel_lot` / `update_trail` / `refresh_marks` / `aggregate`
- 持久化 `logs/lots_state.json`；平仓归档 `logs/trades-YYYYMMDD.json`（append）

#### risk.py — RiskGate

- 以损定量（`size_position`，risk_usd 为最大风险 + 10% 容差）
- 持仓数上限（按 distinct symbol 计）
- **日亏损熔断已屏蔽**（`daily_loss_halt_enabled=False`）
- 止损合法性（多空方向、正价）

### 7.4 控制层（console/）

#### app.py — FastAPI

单例持有 `collector` + `gateway` + `risk_gate` + `order_mgr`。

| 端点 | 说明 |
|---|---|
| `GET /api/status` | 状态（dry_run/连接/净值/日盈亏/时钟/延时/allow_short/halted） |
| `GET /api/market` | 实时行情快照 |
| `GET /api/lots` | 订单批次列表 |
| `GET /api/positions` | 聚合仓位 |
| `POST /api/lots/limit-stop` | 下单（ema/channel/midpoint；midpoint 由服务端按确认 bar 中点重新定价） |
| `POST /api/lots/{id}/trail` | 改保护止损价（STP） |
| `POST /api/lots/{id}/cancel` | 撤单 |
| `POST /api/lots/{id}/close` | 平仓 |
| `GET /api/screen` | 选股（多头列 / 空头列，trend_state 过滤 + EMA 发散度排序） |
| `POST /api/auto/start` | 启动策略实例（strategy/symbols/params，见第 12 章） |
| `POST /api/auto/stop` | 停止策略（可选全部平仓；未决候选失效） |
| `GET /api/auto/status` | 策略状态（开关/策略名/待确认候选/实例列表） |
| `POST /api/auto/proposals/{id}/approve` | 人工批准开仓候选，批准后才创建 LMT+STP |
| `POST /api/auto/proposals/{id}/reject` | 人工忽略候选（可选原因，同 bar 不重复提醒） |
| `GET /api/performance` | 绩效统计（已平仓交易） |
| `GET /api/events` | 历史事件查询（JSONL 回溯） |
| `POST /api/refresh` | 手动刷新 |
| `WebSocket /ws` | 每 1s 广播 snapshot |

（已删除端点：`/api/lots/close-pending`(+/cancel)、`/api/tv/calc`、
`/api/stab/events`、`/api/stab/confirm`、`/api/signal/clear`、
legacy TRAIL 路径 `POST /api/lots`。）

**下单拦截**（两道闸门）：
1. `ALLOW_SHORT` 硬闸门保留但默认放开（config 已改 True，开放双向）；
2. `trend_state` 过滤：做多需 `ts_c.trend_state==1`、做空需 `==-1`、`0` 拒绝，
   且 side 必须与 trend_state 同向——**可二次确认放行**（force 执意开仓，
   见 6.3）。

**事件记录**：`record_event(type, **data)` 追加 `logs/events-YYYYMMDD.jsonl`，覆盖下单/平仓/撤单/失败等所有事件，跨天可回溯。

#### 前端（static/）

- 宫格交易台 + 每格三个 tab：**EMA快线（ema，默认）/ 通道边沿（channel）/
  中点限价单（midpoint）**；
  - EMA/通道按 ts 实时值显示；midpoint 预览与后端一致，使用
    `(bar_c.high+bar_c.low)/2` 和 `ts_c.atr`；止损均为 `limit ∓ 1×ATR`，
    方向按钮按 trend_state 显示，震荡禁用；
- 每格：选股（下拉可搜索）+ 现价大字 + 限价/止损 + 计算（限价/股数/金额/资金占比）+ 下单按钮；
- 顶栏：DRY-RUN/LIVE 徽章、单笔风险输入（300）、仓位上限%（10-200%）、TV/IBKR 时钟、更新延时、日盈亏；
- 底部 tab：**选股 / 自动策略 / 运行日志 / 绩效 / 事件**（「信号」tab 已删除）；
  - 选股：多头列 / 空头列（双向展示），每行显示发散度、ST 延续根数与 ST 推进次数；
    可按 EMA 发散度、ST 持续根数或 ST 推进次数降序，宫格默认标的跟随当前排序；
  - 自动策略：启动表单 + 待确认候选列表 + 实例列表；候选并排展示发散度、
    ST 延续/推进、入场/止损、估算股数/市值/仓位占比，人工点击「确认开仓」或「忽略」；
  - 事件：历史事件查询（按类型过滤）；
  - 绩效：已平仓交易统计。
- 提醒：新开仓候选按 proposal_id 去重，常驻通知 + 汇总语音；保留自动平仓、
  裸仓、下单结果提醒。

---

## 8. 数据模型

### 8.1 OrderLot（订单批次）

| 字段 | 类型 | 说明 |
|---|---|---|
| lot_id | str | 唯一批次 ID |
| symbol | str | 完整代码（NASDAQ:SKHY） |
| side | str | long / short |
| qty | int | 目标股数 |
| entry_px | float | 限价 |
| stop_px | float | STP 止损价（= 限价 ∓ 1×ATR） |
| limit_type | str | ema / channel / midpoint / auto_* |
| reprice_bar_time | float | midpoint 最近一次逐 bar 改价尝试时间（用于幂等与重启恢复） |
| state | str | PENDING / ACTIVE / CLOSING / CANCELLED / FAILED / CLOSED |
| close_order_id / close_permId | int | 平仓单 ID（88 号段）及券商永久 ID，供 CLOSING 对账恢复 |
| filled_qty / avg_fill | | 成交数据 |
| realized_pnl / actual_rr / mae / mfe | | 复盘字段 |

### 8.2 实时快照

见 4.2 节。

### 8.3 事件日志（events-*.jsonl）

每行一个 JSON：`{ts, type, ...}`。type：`order_placed / order_close_requested / order_closed / order_cancelled / order_close_failed / order_cancel_failed / position_scan_alert / auto_close_st_flip / missing_stop_alert / midpoint_limit_repriced / midpoint_limit_reprice_failed / external_order_cancelled / external_order_cancel_failed / external_position_closed / external_position_close_failed / external_position_close_pending`。

（已删除事件类型：`pierce_signal / entry_signal / alert_signal / signals_cleared / close_limit_*`。策略引擎另有一组 `auto_*` 事件类型，见 12.5。）

---

## 9. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据语言 | Node.js（数据）/ Python（执行） | 上游库语言绑定 |
| 周期 | 5 分钟（5M），K 线与指标同周期 | 噪音小（v2026-10-17 由 1M 调整） |
| 会话 | extended | 24x7 会让美股卡在昨日收盘 |
| 指标 | 单指标 trend_state（USER;e577100fe79942b4830783d1ceaad535，5M，range=200） | 过滤 + 入场线 + 方向 + ATR 一处算好（原 6 个指标合并） |
| 确认值 | 策略/下单用 ts_c（periods[1]，上一根已收盘）；实时值 ts 仅显示 | 避免盘中假转向 |
| 过滤 | trend_state：做多需 ==1、做空需 ==-1、0 拦截；side 与 trend_state 同向 | 所见即所得，控制端零逻辑；下单方向=ST 方向，无反向订单 |
| 信号提醒 | 无（两级信号 + 信号 tab 已删除） | 过滤器 + 人工下单 |
| 订单类型 | EMA快线 / 通道边沿 / 中点限价单（midpoint=(bar_c.high+bar_c.low)/2） | 中点单 PENDING 每根 5M bar 同步改 LMT+STP，ACTIVE 后恢复统一管理 |
| 止损 | 限价 ∓ 1×ATR，STP(GTC) 子单（parentId 关联 LMT(DAY) 父单） | 止损单只有 STP 一种；平仓/撤父单时子单自动取消 |
| 双向交易 | ALLOW_SHORT=True，多空两列选股，空头开仓开放 | 多空都做（v2026-10-17 起） |
| 强弱排序 | EMA 发散度 \|ema_fast−ema_slow\|/atr | 指标内部同款度量 |
| 仓位 | 以损定量（risk_usd 最大风险 + maxPositionPct 上限） | 每笔固定风险 + 仓位封顶 |
| 账户口径 | dt_test 组 | 只操作目标组 |
| 订单所有权 | 新单统一 88 号段，旧单使用本地 ID 白名单 | 号段外挂单/持仓一律清理，避免误动工具订单 |
| 运行模式 | dry-run 默认，EXECUTION_ENABLED=1 实盘 | 先演练后实盘 |
| 事件记录 | JSONL 追加，按天归档 | 凡事有记录、可回溯 |
| 日熔断 | 已屏蔽（daily_loss_halt_enabled=False） | 用户要求关闭 |

---

## 10. 配置（config.py）

| 配置 | 默认值 | 说明 |
|---|---|---|
| `WATCHLIST` | 44 只自选 | 订阅股票池 |
| `IBKR_HOST/PORT` | 127.0.0.1:7496 | TWS 连接 |
| `IBKR_CLIENT_ID` | 11 | 唯一 clientId |
| `IBKR_FA_GROUP` | dt_test | FA 账户组 |
| `IBKR_FA_ACCOUNTS` | U11576827,U18082969 | 组账户兜底 |
| `EXECUTION_ENABLED` | 0 | 0=dry-run / 1=实盘 |
| `TOOL_ORDER_ID_BASE/MAX` | 88000000 / 88999999 | 工具订单专用 88 号段；其他系统不得复用 |
| `TOOL_ORDER_ID_STATE_PATH` | logs/tool_order_id.json | 发号持久化（原子写 + 跨进程锁） |
| `EXTERNAL_ORDER_CLEANUP_ENABLED` | 1 | 号段外且不在旧工具白名单的挂单撤销、持仓平仓 |
| `RISK_PER_TRADE_PCT/USD` | 0.001 / 200 | 以损定量（净值/降级） |
| `RISK_MAX_ORDER_QTY` | 100000 | 单笔股数硬顶（仅极端兜底） |
| `RISK_MAX_OPEN_POSITIONS` | 3 | 最大持仓数 |
| `RISK_DAILY_LOSS_LIMIT_USD` | 1000 | 日亏损熔断（已屏蔽） |
| `ALLOW_SHORT` | True | 是否允许做空（v2026-10-17 起开放双向） |
| `ENTRY_LIMIT_PREMIUM_PCT` | 0.5 | marketable 限价溢价 |
| `TRAIL_ATR_MULT` | 2.0 | **已废弃**（TRAIL 路径已删除，止损固定 1×ATR） |
| `TP_RR` | 0.0 | **已废弃**（随 TRAIL 路径一并废弃） |
| `STOP_GUARD_ENABLED` | True | 止损单守卫：每分钟核对持仓有无 STP 保护单，裸仓即提醒 |
| `AUTO_CLOSE_ON_ST_FLIP` | True | 持仓方向与 ts_c.direction 相反（多仓 ==-1 / 空仓 ==1）时自动平仓；False 退化为弹窗提醒 |
| `AUTO_MAX_INSTANCES` | 5 | 全自动策略引擎的实例上限（见第 12 章） |

---

## 11. 运行与部署

```bash
# dry-run（默认，虚拟成交）
EXECUTION_ENABLED=0 python3 -m uvicorn console.app:app --host 127.0.0.1 --port 8000

# 实盘（先确认 TWS 在线 + 账户有资金 + 风控参数）
EXECUTION_ENABLED=1 python3 -m uvicorn console.app:app --host 127.0.0.1 --port 8000

# 同步自选列表（TV Chrome 9222 开着）
python3 service/sync_watchlist.py
```

`logs/autotrade_writer.lock` 由 console、auto_runner、main.py 启动时获取；
任一获取失败说明已有执行写进程在运行，禁止第二个实例重复撤改平同一订单。
`logs/tool_order_id.json` 则负责 88 号段发号持久化与跨进程互斥。

浏览器打开 http://127.0.0.1:8000。

---

## 12. 策略引擎与开仓人工确认（auto_engine）

策略负责一致地产生进场计划和执行后续管理，人负责从多个股票候选中挑选趋势与交易价值更好的标的。核心代码在 `engine/auto_engine.py` + `engine/strategies/`。

### 12.1 运行模型：一个引擎多实例

- `StrategyEngine` 常驻，管理多个 `StrategyInstance`（symbol → 实例）；实际上限 `AUTO_MAX_INSTANCES`（5）。
- 实时确认模式状态机：**WATCHING** → **AWAITING_CONFIRM**（候选未下单）→ **PENDING**（已挂限价单）→ **ACTIVE** → **DONE**；回测的非确认模式跳过 AWAITING_CONFIRM。
- **进场 tick 触发**：实时成交价触及线值，策略用上一根已收盘 5M 的 `ts_c` 生成 `EntryPlan`；确认模式只创建候选，不创建 OrderLot、不调用 broker。
- 候选仅在产生它的 5M bar 内有效；下一根确认 bar 到达或超时即过期。拒绝/过期不增加 `entries_today`，同一 bar 不重复产生候选。
- **ACTIVE tick 保本**：最新价/极值达到 2R 立即移动止损到实际入场成本；PENDING 管理与 ST 跟踪/反向平仓等其余动作只在 5M bar 收盘执行。

### 12.2 人工确认与订单管理

1. **建仓信号**：`entry_trigger(ctx, symbol, price)` 返回 `EntryPlan`（side / 限价 / 止损 / 踩的线 / reason）或 None；
2. **候选比较**：展示趋势发散度、ST 延续根数、ST 线推进次数、入场/止损、估算股数、初始风险、市值和净值占比；这些字段只用于人工比较，批准时重新定量；
3. **人工决策**：`approve_entry` 原子防双击，批准后才走原 `_open → LiveBroker → OrderManager`；`reject_entry` 只忽略候选。停止策略、迟到或重复批准均不能下单；
4. **限价单**：LMT 父 + STP 子；`expire_bars`（3）根 bar 未成交 → 撤单回 WATCHING；
5. **STP 止损** = 限价 ∓ 1×ATR（ts_c.atr）；
6. **移动保本/跟踪止盈**：ACTIVE tick/回测极值达到 `breakeven_r`（2R）立即移成本（券商确认才标记，失败节流重试）；之后每根 bar 收盘按 `ts_c.supertrend` 单向棘轮（1 美分防抖 + 不越过现价）。

外加两条终结路径：**趋势反向平仓**（ts_c.direction 与持仓相反 → 平仓）；
**手动取消/平仓**（含 TWS 上操作，由 lot 终态检测/对账发现）→ 实例 DONE。

### 12.3 人工仓收养

`adopt_manual_lots`：收养 OrderManager 本地且未被当前实例管理的 ACTIVE lot（含重启后的 auto_ lot 与人工 ema/channel/midpoint lot），直接 ACTIVE 并套用保本/ST 跟踪/反向平仓；不读取或收养外部 TWS 持仓。CLOSING 由 close watcher 完成，实例只在实际 CLOSED 后结束。

### 12.4 确定性原则（策略与管理回测口径）

**结果 = 数据 × 策略代码 × 参数。** 回测与实盘共用**同一个** StrategyEngine，可插拔的是两端：

- 数据源：LiveFeed（实时快照）/ BacktestFeed（历史 bar）；
- 执行网关：`SimBroker`（回测，touch 成交模型：LMT 触价成交、STP 触价成交）/ `LiveBroker`（= OrderManager → IBKR / dry-run 虚拟成交），两者同接口。

**tick 进场的回测口径 = bar 极值建模**（`on_bar_extremes`）：bar 内 tick 路径不可知，多头看 low（触线/触止损）、空头看 high；两个极值都喂给 broker——**有利极值先行**（MFE 达到 2R 先触发保本）、**不利极值随后**（再检查保本止损/限价成交），顺序固定 = 确定性。回测关闭人工确认以验证原始信号和后续管理；实盘人工选股是其上游过滤，不被历史回测自动模拟。

### 12.5 启停、确认与监控

| 入口 | 说明 |
|---|---|
| `POST /api/auto/start` | 启动：{strategy, symbols, params}，为每个 symbol 建 WATCHING 实例（≤ AUTO_MAX_INSTANCES） |
| `POST /api/auto/stop` | 停止：不再产生/批准候选；存量实例默认继续管理（`close_positions=true` 可全部平仓） |
| `GET /api/auto/status` | 开关 / 策略名 / 待确认候选 / 全部实例 |
| `POST /api/auto/proposals/{id}/approve` | 批准服务端保存的候选；浏览器不能覆盖价格/方向 |
| `POST /api/auto/proposals/{id}/reject` | 忽略候选，可选记录原因 |
| `auto_runner.py` | headless 同样要求确认：TTY 输入 `approve <id>` / `reject <id>`；非 TTY fail-closed，绝不自动批准 |

控制台内由 `_auto_loop`（每 1s）分发：bar 收盘推进 → `on_bar` 候选过期和持仓管理；tick 价 → `on_price` 产生候选；每 5s 扫一次人工仓收养。候选通过 snapshot 每秒推送，前端常驻通知 + 汇总语音提醒。

**事件**：控制台写 `logs/events-YYYYMMDD.jsonl`，新增 `auto_entry_proposed / auto_entry_approved / auto_entry_rejected / auto_entry_expired`；实际下单仍由 `auto_order_placed` 表示。`auto_runner` 写 `logs/auto-YYYYMMDD.jsonl`。

### 12.6 回测（backtest_auto.py）

把历史 5M bar（含 trend_state 指标值）逐根喂给与实盘**同一个** StrategyEngine（+ SimBroker，确认模式关闭），验证「同一策略 + 同一数据 = 同一结果」。

```bash
python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --day 2026-10-16
python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --range 5
python3 backtest_auto.py --strategy trend_pullback --symbol NASDAQ:NVDA --day 2026-10-15,2026-10-16 --param breakeven_r=1.5 --json
```

- 参数：`--strategy / --symbol / --day`（逗号多日，默认上个交易日）/ `--range`（近 N 个交易日）/ `--bars` / `--param`（覆盖策略参数，可多次）/ `--json`；
- 口径：bar 极值模拟 tick（12.4）；收盘（15:55 ET 的 bar）后仍持仓/挂单按最后收盘价了结（eod / eod_cancel）；
- 报告字段：每笔 `entry / stop / exit / reason / pnl / rr / mfe_r / entry_time / exit_time`；汇总 `count / wins / win_rate / total_pnl / avg_rr / expectancy / avg_mfe_r / stopped / no_1r_count / no_1r_pct / dist_mfe`（MFE ≥1R~5R 分布）。

### 12.7 首个策略：trend_pullback

进场规则（tick 触发，指标用 ts_c 确认值）：

- `trend_state == 1`（单边多头）：实时成交价触及 `ema_fast` 或 `channel_edge`（price ≤ 线值）→ 做多；两线同触取更低价（更优入场）；
- `trend_state == -1`（单边空头）：price ≥ 线值 → 做空；取更高价；
- 限价 = 线值；止损 = 限价 ∓ 1×ATR（ts_c.atr，ATR 缺失不进场）；方向 = ST 方向，无反向订单；
- 时段闸：`entry_start_hm`（默认 09:45 ET）前、`entry_end_hm`（默认 15:00 ET）后不开新仓。

默认参数（`AutoStrategy.params`，可用 `--param` 或 API `params` 覆盖）：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `expire_bars` | 3 | 埋单 N 根 bar 未成交撤单回 WATCHING |
| `breakeven_r` | 2.0 | 浮盈 ≥ 2R（1:2 盈亏比）移保本 |
| `entry_start_hm` | 09:45 ET | 开盘前 15 分钟不开新仓 |
| `entry_end_hm` | 15:00 ET | 最后一小时不开新仓 |
| `max_entries_per_day` | 2 | 每标的每日进场上限 |

新策略在 `engine/strategies/__init__.py` 注册表登记（`build_strategy` 按名构建 + 参数覆盖）即可被引擎/回测/控制台使用。

---

## 13. 待完善 / 未验证

| 项 | 状态 |
|---|---|
| 候选人工确认 + 自动管理实盘（EXECUTION_ENABLED=1）验证 | ⏳ 未验证 |
| 开仓候选实时提醒与点击确认 | ⏳ 等实时数据/dry-run 复核 |
| trend_state 5M 双向链路实盘验证 | ⏳ 等开盘验证 |
| 真实成交闭环（下单→成交→持仓→止损→平仓） | ⏳ 等开盘测试 |
| 止损单真实触发 | ⏳ 未验证 |
| 中点单真实 IBKR 父子单逐 bar 改价及成交竞争 | ⏳ 未验证 |
| EOD 强平（15:45 ET） | ✅ 已实现（`_eod_force_close_loop`） |
| 多日压测 | 未做 |
