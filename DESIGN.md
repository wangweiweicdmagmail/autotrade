# autotrade 交易控制台 — 详细设计文档

> 版本：2026-10-13 更新
> 覆盖范围：TradingView 数据订阅 → 指标计算 → 下单执行 → 仓位管理 → 前端控制台的全链路设计。
> 配套文档：`PLAYBOOK.md`（交易规则）、`STRATEGY_RULES.md`（策略 v1）、`PROJECT_NOTES.md`（历史交接）。

---

## 1. 项目概述

autotrade 是一个「人机结合」的日内交易控制台，核心链路：

```
TradingView（数据 + 指标计算）→ 引擎（采集 + 下单计算）→ IBKR（执行）→ Web 控制台（人机交互）
```

**核心设计原则：严格分工（所见即所得）**

- **TradingView / Pine 指标**：负责所有行情类计算（EMA、ST、通道、方向、信号、趋势强度、多头过滤）。
- **控制端（Python）**：只读指标输出值 + 下单执行 + 仓位/风控/账户 + 事件记录。
- **前端（Vue）**：所见即所得展示 + 仓位参数输入 + 信号提醒。

行情指标绝不重复计算；控制端只保留「执行类」计算（仓位、金额、风控）。**所有行情过滤/信号判断都在 Pine 指标里算好，控制端只读信号值。**

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
| 事件日志 | JSONL 追加（按天归档） | 信号/下单/平仓/撤单 持久化，可回溯 |

---

## 4. 数据流设计

### 4.1 实时行情流（订阅模式）

```
TradingView 云端 WS
   │  onUpdate（tick 级推送）
   ▼
tv_cloud_daemon.js（常驻 Node 进程）
   │  每标的 1 个 1M session，session='extended'
   │  订阅 K线 + EMA7 + st_channel_www + st_strength + fast_slow_ma + VWAP + langfilter
   │  内存维护 state，setInterval 1s 节流输出 JSON 行 + 30s 心跳
   ▼  stdout（每行一个 JSON）
realtime_collector.py（Python asyncio subprocess）
   │  逐行读 stdout → 解析 → 覆盖式更新 self._snapshot
   │  记录 latest_bar_time（数据时钟）、bar_update_delay（K线收盘→收到延时）
   ▼
console/app.py build_snapshot()
   │  合并 status（净值/日盈亏/时钟）+ market（44只实时）+ lots + 信号
   ▼
WebSocket /ws（每 1s 广播）
   ▼
Vue3 前端（宫格 + tab，实时计算限价/股数/金额/资金占比 + 信号提醒）
```

> **会话用 'extended'，不是 '24x7'**：24x7 会让美股数据卡在昨日收盘不推进。

### 4.2 实时快照数据结构

```jsonc
// collector.latest() 输出（每标的）
{
  "NASDAQ:SKHY": {
    "bar": { "time": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ... },  // 实时 K 线（未收盘）
    "bar_c": { "time": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ... }, // 上一根已收盘 K 线（稳定）
    "ema7": 169.09,                       // STD;EMA Length=7（实时）
    "st": {                                // 实时值（periods[0]，盘中抖动，仅显示用）
      "up_trend": 169.64, "down_trend": null,
      "up_channel": 169.84, "down_channel": null,
      "body_middle": 170.04,
      "zone_up": 169.70, "zone_down": null,       // 第二个通道 = ST ± 0.2×ATR（回踩检测区）
      "pierce_signal": 0                           // 回踩信号 1/-1/0（实时）
    },
    "st_c": { ... 同上，但是上一根已收盘的「确认值」... },  // 策略判断/下单全用这套，稳定不抖
    "strength": { "move_atr": 4.5, "bars": 20 },   // 趋势强度（推动 ATR 数）
    "filter": {
      "ma_fast": 169.5, "ma_slow": 168.9,          // fast_slow_ma 快线/慢线
      "vwap": 169.2,                                // STD;VWAP
      "long_candidate": 1                           // langfilter 多头过滤信号 0/1
    }
  }
}
```

### 4.3 关键指标

| 指标 | 脚本 ID | 用途 |
|---|---|---|
| `st_channel_www` | `USER;f1157c7728c047069c21e72dcb8f94a2` | ST 止损线（Up/Down_Trend）+ 通道（ST±ATR）+ 中线 + 检测区（ST±0.2×ATR）+ 回踩信号 Pierce_Signal |
| `st_strength` | `USER;aa6f71ec42fc48839479878b4a36965d` | 趋势强度（move_atr，推动 ATR 数） |
| `fast_slow_ma` | `USER;5c1a17e8fd934e30adb3aa7aad30a42c` | 快慢线：MA_1=5分钟 EMA20（快线）、MA_2=1分钟 EMA250（慢线） |
| `langfilter` | `USER;f16a9397f107472584dadd331304907b` | **多头过滤**：signalOutput（0/1），内部综合 ST 多头 + EMA 快慢线发散 + VWAP |
| `ema7` | `STD;EMA`（Length=7） | EMA 限价单价格线 |
| `VWAP` | `STD;VWAP` | 日内均价 |

指标参数：st_channel_www 的 ATR Length=10、ST Factor=**3.2**、Channel ATR Multiplier=1、ST Zone ATR Multiplier=0.2；全部 1M 周期；extended 会话。

---

## 5. 事件流程

### 5.1 下单流（限价 + STOP 子单）

```
前端点「下单」
→ POST /api/lots/limit-stop {symbol, side, qty, limit_px, stop_px, limit_type}
→ 【下单拦截】side=short → 拒绝（纯多头）；long_candidate!=1 → 拒绝（非多头列表）
→ OrderManager.place_limit_stop_lot()
   → RiskGate.check_new_order()   # 以损定量/持仓上限/止损合法性（日熔断已屏蔽）
   → IBKRGateway.place_limit_with_stop()
      → LMT(父, DAY, transmit=False) + STP(子, GTC, parentId=父, transmit=True)
      → 显式分配 orderId（getReqId），保证 parentId 关联正确
      → 2.5s 主动核对订单状态（IBKR 订单错误不一定事件化）
→ OrderLot 落盘 logs/lots_state.json
→ 实盘：_watch_entry 轮询成交 → ACTIVE；dry-run：立即虚拟成交 ACTIVE
```

### 5.2 下单后管理流

```
持仓(ACTIVE)
├── 改 TRAIL：update_trail（只向有利方向棘轮，1 美分防抖）
├── 平仓：close_lot → 撤 STOP 子单 + 反向 marketable LIMIT（place_close）→ CLOSED
└── 撤单：cancel_lot → 撤入场+STOP 挂单 → CANCELLED
```

**父子单关联语义**：父单（限价）撤销/被拒时，子单（STOP）随 parentId 自动取消——即「平仓时子订单自动取消」。

### 5.3 心跳 / 断线流（横切）

```
_heartbeat_loop（每 2s）
   → reqCurrentTimeAsync（3s 超时）→ 检测 TWS 假死
→ 断线判定 ≤5s（socket 断开 disconnectedEvent 毫秒级 / 假死 2s+3s）
→ _on_disconnect → _connected=False
→ _reconnect_loop（每 5s 退避重连）→ connect() 恢复 + 重启心跳
```

---

## 6. 信号体系（核心）

### 6.1 两级信号：预警 + 入场

| 信号 | 数据 | 用途 | 触发 |
|---|---|---|---|
| **预警信号** | 实时 K 线 + 确认指标 | 仅语音提醒「注意观察」 | 盘中实时，尽早 |
| **入场信号** | Pine 确认信号（st_c.pierce_signal）+ 确认 K 线 | 信号 tab 弹出，用户下单 | 收盘确认 |

**交易时段门控**：仅在美股常规交易时段（9:30–16:00 ET，周一~周五）才检测/提醒信号；盘前盘后周末静默。

**多头过滤门控**：所有回踩信号（预警 + 入场）都必须先过 `langfilter` 多头过滤（`long_candidate == 1`），非多头候选不触发信号。

### 6.2 信号检测循环（`_signal_detector_loop`）

```
每 1s：
  非交易时段 → 跳过
  每标的：
    long_candidate != 1 → 跳过（非多头候选）
    ── 入场信号：st_c.pierce_signal == 1（Pine 确认值）→ 记录 _entry_state
    ── 预警信号：实时 bar 形态 + 确认 st_c 判断 → 记录 _alert_state（仅提醒）
```

### 6.3 回踩形态判断（`_check_signal`）

- 多头：确认 up_trend 有效 + `low <= zone_up` + `(close - low)/(high - low) >= 0.4`（长下影线）
- 空头：确认 down_trend 有效 + `high >= zone_down` + `(high - close)/(high - low) >= 0.4`（长上影线）

`ST_WICK_RATIO = 0.4`（影线占比阈值）。

### 6.4 多头候选（langfilter 统一驱动）

**多头列表、回踩信号门槛、回踩事件队列、下单拦截**全部用 `langfilter` 指标的 `signalOutput`（云端字段 `Filter_Output_10`，即 `filter.long_candidate`）：

- `long_candidate == 1` → 多头候选（显示在多头列表、可触发信号、可下单）
- `long_candidate == 0` → 非多头候选（不显示、不触发、下单拦截）

langfilter 内部逻辑（Pine 里，所见即所得）：ST 多头（SuperTrend 因子 3.2）+ 快慢线发散（快线 EMA20 > 慢线 EMA100 + 1×ATR）+ VWAP。控制端零逻辑，只读信号。

---

## 7. 模块详细设计

### 7.1 数据层（service/）

| 文件 | 职责 |
|---|---|
| `tv_cloud_daemon.js` | 常驻订阅：每标的 1M session，1s 节流输出 JSON 行 + 30s 心跳 |
| `tv_cloud.js` | 一次性采集 CLI（`--symbols --timeframe --session --range`） |
| `indicators.json` | 指标注册表（key/id/timeframe/inputs） |
| `sync_watchlist.py` | 从 TV 图表 CDP 同步自选列表（44 只）写回 config.py |

**daemon 关键逻辑**：
- 每标的建 1 个 `Session.Chart`，`setMarket(symbol, {timeframe:'1', range:150, session:'extended'})`；
- 订阅 7 个 Study：K线(bar/bar_c) + EMA7 + st_channel_www(st/st_c) + st_strength + fast_slow_ma + VWAP + langfilter；
- `fast_slow_ma` 用 `setOption('1_Resolution','5')` + `setOption('1_Length',20)` + `setOption('2_Length',250)`；
- `langfilter` 用 `setOption('SuperTrend__Multiplier', 3.2)`（和 st_channel_www 一致）；
- **st 输出两套**：`st`（实时，periods[0]，显示用）+ `st_c`（确认，periods[1]，策略用）。

### 7.2 引擎层（engine/）

| 文件 | 职责 |
|---|---|
| `realtime_collector.py` | subprocess 托管 daemon；`_reader_loop` 逐行解析；维护 `_snapshot`、`latest_bar_time`、`bar_update_delay` |
| `cloud_collector.py` | 一次性采集 → `StockSignalData`；解析指标到 `extra` |
| `order_calc.py` | `compute_order_params`、`calc_position`（以损定量）、`compute_live_order` |
| `strategy.py` | `StockSignalData` 数据结构、过滤打分（历史遗留） |

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
| `place_close` | 反向 marketable LIMIT 平仓 |
| `modify_stop` | 改 TRAIL 触发价 |
| `fa_accounts` | 动态查询 dt_test 组成员，失败回退 config |
| `positions` / `net_liquidation` | dt_test 组口径持仓/净值 |
| `realized_pnl` / `daily_pnl` | 已实现盈亏 / 日盈亏 |
| `last_price` / `cancel_order` | snapshot 现价 / 撤单 |

**账户口径**：全部只针对 FA 组 `dt_test`（U11576827 + U18082969）。

#### order_lot.py — OrderLot / OrderManager

**OrderLot**：`lot_id / symbol / side / qty / entry_px / stop_px / limit_type / state / ...` + 复盘字段（`entry_time / exit_px / closed_at / realized_pnl / exit_reason / signal_snapshot / take_profit_px / target_rr / actual_rr / mae / mfe`）。

**OrderManager**：
- `place_limit_stop_lot`（LMT+STOP，带 risk_usd + signal_snapshot）
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
| `POST /api/lots/limit-stop` | 下单（LMT+STOP，含下单拦截） |
| `POST /api/tv/calc` | 拉 TV 数据算下单参数 |
| `POST /api/lots/{id}/trail` | 改 TRAIL |
| `POST /api/lots/{id}/cancel` | 撤单 |
| `POST /api/lots/{id}/close` | 平仓 |
| `GET /api/screen` | 选股（多头/空头/回踩事件，langfilter 过滤） |
| `GET /api/stab/events` | 入场信号列表 |
| `POST /api/stab/confirm` | 回踩信号确认下单 |
| `POST /api/signal/clear` | 一键清除所有信号 |
| `GET /api/performance` | 绩效统计（已平仓交易） |
| `GET /api/events` | 历史事件查询（JSONL 回溯） |
| `POST /api/refresh` | 手动刷新 |
| `WebSocket /ws` | 每 1s 广播 snapshot |

**下单拦截**（两道硬闸门）：
1. `side == "short"` → 拒绝「当前仅允许做多」（`ALLOW_SHORT=False`）
2. `long_candidate != 1` → 拒绝「不在多头列表」

**事件记录**：`record_event(type, **data)` 追加 `logs/events-YYYYMMDD.jsonl`，覆盖信号/下单/平仓/撤单/失败等所有事件，跨天可回溯。

#### 前端（static/）

- 宫格交易台（纯多头模式只显示多头宫格）+ 每格「EMA / ST回踩」tab；
- 每格：选股（下拉可搜索）+ 现价大字 + 限价/止损 + 计算（限价/股数/金额/资金占比）+ 下单按钮；
- 顶栏：DRY-RUN/LIVE 徽章、单笔风险输入（300）、仓位上限%（10-200%）、TV/IBKR 时钟、更新延时、日盈亏；
- 底部 tab：**信号 / 选股 / 运行日志 / 绩效 / 事件**；
  - 信号：入场信号列表 + 「下单」按钮 + 「清除信号」按钮（一键清除）；预警仅语音提醒；
  - 选股：多头列 / 空头列（屏蔽）/ 回踩事件队列（只显示多头回踩）；
  - 事件：历史事件查询（按类型过滤）；
  - 绩效：已平仓交易统计。
- 提醒：入场信号 → 语音 + 自动切信号 tab + 弹窗（1 分钟自动消失）；预警 → 语音「注意观察」。

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
| stop_px | float | STOP 止损价 |
| limit_type | str | ema / channel / st_pierce |
| state | str | PENDING / ACTIVE / CANCELLED / FAILED / CLOSED |
| filled_qty / avg_fill | | 成交数据 |
| realized_pnl / actual_rr / mae / mfe | | 复盘字段 |

### 8.2 实时快照

见 4.2 节。

### 8.3 事件日志（events-*.jsonl）

每行一个 JSON：`{ts, type, ...}`。type：`pierce_signal / entry_signal / alert_signal / order_placed / order_closed / order_cancelled / order_close_failed / order_cancel_failed / signals_cleared`。

---

## 9. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据语言 | Node.js（数据）/ Python（执行） | 上游库语言绑定 |
| 周期 | 1 分钟（1M） | 用户交易节奏 |
| 会话 | extended | 24x7 会让美股卡在昨日收盘 |
| 指标 | st_channel_www + st_strength + fast_slow_ma + VWAP + EMA7 + langfilter | 信号 + 过滤来源 |
| ST 因子 | 3.2（st_channel_www 与 langfilter 一致） | 统一方向判断 |
| 确认值 | 策略用 st_c（periods[1]，上一根已收盘）；实时值 st 仅显示 | 避免盘中假转向 |
| 信号 | 两级：预警（实时 K线，提醒）+ 入场（Pine 确认值，下单） | 尽早提醒 + 稳定入场 |
| 多头过滤 | langfilter 指标 signalOutput 统一驱动 | 所见即所得，控制端零逻辑 |
| 交易时段 | 9:30–16:00 ET 周一~五才提醒 | 过滤盘前盘后噪音 |
| 止损结构 | STOP 子单（parentId 关联限价父单） | 平仓/撤父单时子单自动取消 |
| 仓位 | 以损定量（risk_usd 最大风险 + maxPositionPct 上限） | 每笔固定风险 + 仓位封顶 |
| 账户口径 | dt_test 组 | 只操作目标组 |
| 运行模式 | dry-run 默认，EXECUTION_ENABLED=1 实盘 | 先演练后实盘 |
| 纯多头 | ALLOW_SHORT=False，空单拦截 | 当前只做多 |
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
| `RISK_PER_TRADE_PCT/USD` | 0.001 / 200 | 以损定量（净值/降级） |
| `RISK_MAX_ORDER_QTY` | 100000 | 单笔股数硬顶（仅极端兜底） |
| `RISK_MAX_OPEN_POSITIONS` | 3 | 最大持仓数 |
| `RISK_DAILY_LOSS_LIMIT_USD` | 1000 | 日亏损熔断（已屏蔽） |
| `ALLOW_SHORT` | False | 是否允许做空（当前只做多） |
| `ENTRY_LIMIT_PREMIUM_PCT` | 0.5 | marketable 限价溢价 |
| `TRAIL_ATR_MULT` | 2.0 | TRAIL 幅度 |

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

浏览器打开 http://127.0.0.1:8000。

---

## 12. 待完善 / 未验证

| 项 | 状态 |
|---|---|
| 真实成交闭环（下单→成交→持仓→止损→平仓） | ⏳ 等开盘测试 |
| 止损单真实触发 | ⏳ 未验证 |
| TRAIL 跟踪止损真实跟踪 | ⏳ 未验证 |
| EOD 强平（15:45） | 未实现 |
| 多日压测 | 未做 |
| 空头模式恢复 | ALLOW_SHORT=True 即可，未验证 |
