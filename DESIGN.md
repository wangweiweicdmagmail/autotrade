# autotrade 交易控制台 — 详细设计文档

> 版本：2026-10-27 更新（盘前报告工作流 + YouTube 视频源章节；TV 切标的冷启动修专用 Chrome profile；FVG 监控窗口 10/15/30 分钟）
> 历史版本：2026-10-21（回踩单 retest + 股数跟随改价、OCA 竞态成交真相根治、幻影守卫、FVG 监控器 + 通报阈值 + 3 分钟倒计时）
> 历史版本：2026-10-20（FVG Finder+ 第二订阅指标：快照 fvg/fvg_c + 策略 FVG 入场线/过滤 + 选股 FVG 维度）
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
   │  订阅 2 个 Study：trend_state + FVG Finder+（K线 + 指标同 session）
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
    "bar_c2": { "time": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ... }, // 上上根已收盘 K 线（回踩单定价基准）
    "ts": {                            // 实时值（periods[0]，盘中抖动，仅显示用）
      "trend_state": 1,                // 1 单边多头 / -1 单边空头 / 0 震荡
      "direction": 1,                  // ST 方向：Up_Trend 有效=1 / Down_Trend 有效=-1 / 都无=0
      "ema_fast": 169.5,               // EMA20
      "ema_9": 169.8,                  // EMA9（由同一 chart 的 5M 收盘价计算，Source=close/Offset=0）
      "ema_slow": 168.9,               // EMA100
      "supertrend": 169.64,            // Up/Down_Trend 中有效者
      "channel_edge": 169.84,          // Uptrend/Downtrend_Channel 中有效者（ST±1×ATR）
      "atr": 0.20,
      "st_duration_bars": 12,          // 当前 ST 方向已延续的 5M bar 数（翻向当根为 0）
      "st_push_count": 8               // 当前方向内 ST 线有利推进累计次数（翻向清零）
    },
    "ts_c": { ... 同上，但是上一根已收盘的「确认值」（periods[1]）... },  // 策略判断/下单全用这套，稳定不抖
    "fvg": {                           // FVG Finder+ 实时值（periods[0]，仅显示用）
      "time": 1792531800,              // 该值所属 bar 的时间戳（与 bar_c.time 对齐校验用）
      "fvg_cum_count": 3,              // ST 同向 FVG 累积计数（ST 转向清零）
      "fvg_last_top": 263.35,          // 最后一个 FVG 上沿
      "fvg_last_bottom": 263.23,       // 最后一个 FVG 下沿
      "fvg_event": 0,                  // 当根 FVG 信号：1 需求区 / -1 供应区 / 0 无
      "fvg_last_dir": 1                // 最后一个 FVG 的方向（daemon 扫历史最后非零 fvg_event 推导；窗口内无 FVG 为 null）
    },
    "fvg_c": { ... 同上，确认值（periods[1]），策略/选股用 ... }
  }
}
```

### 4.3 关键指标

| 指标 | 脚本 ID | 周期 | 用途 |
|---|---|---|---|
| `trend_state` | `USER;e577100fe79942b4830783d1ceaad535` | 5M（extended，range=200） | 趋势状态过滤 + 回踩入场线 + 方向 + 止损 ATR |
| `fvg_finder` | `USER;5ec7d04179b3429ca9286db452352d85` | 5M（同 session 第二 Study） | FVG 累积计数 + 最后 FVG 上下沿/方向 + 当根信号；策略 FVG 入场线/过滤 + 选股维度 |
| `smart_stop` | `USER;ca26e3ab587841e2adb38cfbfd0bec41` | 5M（同 session 第三 Study） | **智能止损价**（源码 smart_stop.pine）：结构止损（3 根极值）+ ATR 钳制（默认 1~2×ATR，指标面板可调）；plot `Smart_Stop_Long`/`Smart_Stop_Short` → ts/ts_c 的 `smart_stop_long`/`smart_stop_short` |

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

FVG Finder+ plot 映射（⚠️ 该指标 plot 标题为中文，WS 订阅键名 = 原标题剥离非 ASCII 字符 + 重名加后缀，**在 TV 上改 plot 标题会让键名静默变化、字段全部变 null**）：

| 实际键名 | 原标题 | 快照字段 | 说明 |
|---|---|---|---|
| `STFVG` | ST同向FVG累积计数 | `fvg_cum_count` | 与当前 ST 方向同向的 FVG 累积（转向清零） |
| `FVG` | 最后FVG上沿 | `fvg_last_top` | 最后一个 FVG 上沿 |
| `FVG_2` | 最后FVG下沿 | `fvg_last_bottom` | 最后一个 FVG 下沿 |
| `KFVG110` | 当前K线FVG信号（1涨/-1跌/0无） | `fvg_event` | 当根信号：1 需求区 / -1 供应区 / 0 无 |
| —（daemon 推导） | — | `fvg_last_dir` | 最后 FVG 方向 = 历史中最后非零 fvg_event；200 根窗口内无 FVG 为 null |

（保存版另有 plot_4/5 `_FVG_`/`_FVG__2`，忽略不用。）

指标内部参数（Pine 里，所见即所得）：emafast=EMA20、emaslow=EMA100、SuperTrend(10,3)、channelEdge=ST±1×ATR；trendState 要求 ST 方向 + close/fast/slow 排列 + EMA 差值 > 1×ATR（回看 6 根）。控制端零逻辑，只读 plot 值。

**ST 推进定义**：多头仅本根 ST 线值高于上一根才累计一次，空头仅低于上一根才累计；ST 不变不计，方向翻向时清零。两个 ST 延续字段只用于选股/候选的人工比较，不参与进出场、止损或排序过滤。远程私有指标未同步新 plot 时字段为 null，界面显示「—」，不能当成 0。

---

## 5. 事件流程

### 5.1 下单流（五种限价单 + STP 子单）

```
宫格五个 tab：EMA快线（ema_fast）/ 通道边沿（channel_edge）/
             中点限价单（midpoint=(bar_c.high+bar_c.low)/2，OCA 双腿）/
             回踩限价单（retest=bar_c2 极值）/ 市价单（market）
前端点「下单」（按钮按 trend_state 方向显示 买入/卖出，震荡禁用）
→ POST /api/lots/limit-stop {symbol, side, qty, limit_px, stop_px, limit_type}
→ 【下单拦截】trend_state 过滤：做多需 ts_c.trend_state==1、做空需 ==-1、
   0（震荡）拒绝；side 必须与 trend_state 同向；force 执意开仓保留
→ 止损：ema/channel/midpoint/market/chase = **智能止损（smart_stop 指标，
  所见即所得）**：止损距离 = 现价 − Smart_Stop_Long（多）/ Smart_Stop_Short − 现价（空），
  指标内部 = 最近 3 根 K 线极值逻辑止损点钳制在 1×ATR ~ 2×ATR
  （不太近被波动扫掉、不太远仓位上不去）；指标缺失时控制端按同公式回退计算
  （上限 = 全局 stop_atr_mult，顶栏可调）。
  不锚定含 0.5% 溢价的限价（v2026-10-21 修复：溢价 > ATR 时按限价锚定会
  把止损挂到现价错误一侧，成交瞬间触发，GOOGL/NOW 两单实锤）；
  **market/chase 的风控与股数口径用预期成交价（现价），溢价不进风险**
  （v2026-10-23 修复：0.5% 溢价 ≈ 止损距离时被 10% 容差误杀，BA 实锤；
  服务端以损定量拥有 market/chase 股数，init_entry_px（R 锚）= 现价）；
  retest = bar_c2 另一端极值（不依赖 ATR）。
→ midpoint/retest/chase/market 由服务端重新定价，不信任浏览器价格；
  market/chase 为 marketable 限价（实时 close ×(1±0.5%)，不用裸 MARKET：
  延迟行情会被券商拒单 error 10349）
→ OrderManager.place_limit_stop_lot()
   → RiskGate.check_new_order()
   → IBKRGateway.place_limit_with_stop()
      → LMT(父, DAY, transmit=False) + STP(子, GTC, parentId=父, transmit=True)
      → 显式分配 orderId + 2.5s 主动核对订单状态
→ OrderLot 落盘 logs/lots_state.json
→ 实盘：_watch_entry 轮询成交 → ACTIVE；dry-run：立即虚拟成交 ACTIVE
```

**回踩限价单（retest，v2026-10-21）**：逻辑来自 FVG 定义——上上根 K 线的
高点（多头）/低点（空头）大概率被回踩，不回踩即形成 gap。挂限价单在该极值
等回踩成交，止损挂同一根 K 线的另一端极值（逻辑止损位）。PENDING 期间由
`_retest_limit_reprice_loop` 每根确认 bar 重新定价（bar_c2 随新 bar 滚动），
且**股数跟随改价**：用 `init_risk_usd` 作固定风险预算按新止损距离重新
以损定量（部分成交后只改价不改股数；不足 1 股拒绝本次改价保留原单）。
ACTIVE 后恢复原单向止损管理。事件：`retest_limit_repriced` /
`retest_limit_reprice_failed`。

**中点限价单（v2026-10-20 起）**：OCA 双腿结构（LMT 中点腿 + STP 突破腿，
突破上根 K 线高/低点即成交，一成全撤），一次定价不再逐 bar 改价；
`_midpoint_limit_reprice_loop` 仅作旧式单腿中点单的兼容兜底。

> 止损单只有 **STP** 一种。中点限价单与已删除的旧「收盘价限价单」
> （close-pending 整套）不同；旧 close-pending 与 TRAIL 路径不恢复。

### 5.2 下单后管理流

```
持仓(ACTIVE)
├── 改止损：update_trail（STP，只向有利方向棘轮，1 美分防抖）
├── 平仓：close_lot → CLOSING（记录 close orderId）→ 全部成交后才 CLOSED
├── 撤单：cancel_lot → 等券商终态；已成交部分转 ACTIVE，未成交才 CANCELLED
├── ST 反向自动平仓：_position_scan_loop 只处理本工具 lot；
    外部持仓由独立清理循环处理，不再按账户净额重复补刀
└── OCA 竞态修复（v2026-10-21）：终态判定以成交真相（executions）为准
```

**中点单 OCA 双腿与成交真相（v2026-10-21 根治）**：中点单 = OCA 组双腿
（LMT 中点腿 + STP 突破腿，一成全撤）+ LMT 腿下挂 STP 止损子单。突破腿成交
与 LMT 腿被 OCA 撤单可在同一帧发生，缓存 `orderStatus.filled` 会漏报
（QCOM/SMCI/BABA/SNOW 四次实锤：lot 误判「无实际成交 CANCELLED」，持仓裸奔）。
修复两道防线：
1. `_handle_entry_terminal` 终态判定前先查成交真相
   （`entry_fills_by_order` 按 entry/breakout orderId 聚合 executions）；
   有成交即按真相激活（突破腿转为入场腿、双腿双成自动平掉超计划部分、
   `_restore_protection` 重建保护）；查询失败保持 PENDING 待对账，
   绝不误判 CANCELLED；
2. `reconcile` 复活通道：CANCELLED/FAILED 且本地零成交的 lot，若券商仍有
   同向持仓且本 lot 订单确有成交 → 复活 ACTIVE + 恢复保护（余仓少于成交数
   说明已被人工减仓，账本对齐余仓），每 lot 只复活一次，事件 `lot_revived`；
3. PENDING 跨会话收敛（v2026-10-22）：重启后上一进程的已终结订单不在
   开放快照（trade_map 无句柄）时，查成交真相（executions 跨会话有效）——
   有成交按真相激活；无成交且订单不可见 = 已终结无成交，收敛 CANCELLED，
   绝不永远挂 PENDING（NFLX 两单实锤）；查询失败保持 PENDING（fail-closed）。

**幻影守卫（v2026-10-21）**：`breakeven()` / `_restore_protection` 新建
保护单前强制校验券商侧有足额同向持仓（`_has_broker_position`），防止
TWS 手动平仓后残留的幻影 ACTIVE lot 挂出真实反向单；快照认回失败进入
30s 冷却（`protection_find_blocked_until`），自动保本循环每 lot 5s 节流。

**FVG 事件固化对齐（v2026-10-21）**：daemon 的 K线回调与 FVG Study 回调是独立
通道，跨 1s 推送边界时 fvg_c 可能还是旧 bar 的值。fvg 输出带 `time`（所属 bar
时间戳），监控循环要求 `fvg_c.time == bar_c.time` 才固化，不齐挂起下拍重试
（最多 5 拍，耗尽告警放弃——宁可丢事件记日志，不固化错值）。

**保护单无成交终态自动恢复（v2026-10-21）**：保护单被撤/过期且无成交时，
`_handle_trail_terminal` 置 `protection_restore_needed=True`，下轮 reconcile
经幻影守卫后自动 `_restore_protection` 补挂——裸仓从「只告警」升级为
「告警 + 自动恢复」。另：`_activate_from_ground_truth` 补 `_set_targets`
（复活 lot 的 target_rr 不再为 0）；`close_half` 补保护单句柄守卫；
突破腿 OCA 撤单滞后时防御性补撤。

**回踩单股数封顶（v2026-10-21）**：下单请求与 OrderLot 新增
`max_position_pct`（前端顶部「仓位上限%」透传），回踩改价股数除固定风险预算外
再受净值占比封顶（`qty ≤ equity × pct% ÷ new_limit`），止损距离收窄时名义市值
不超出下单口径。

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

### 5.4 外部订单与持仓处理（每 30s）

```
_external_cleanup_loop
  ├─ EXTERNAL_ORDER_CLEANUP_ENABLED=1（默认，v2026-10-23 用户恢复）：
  │    非 autotrade 工具的挂单撤销、残余持仓**立即市价平仓**（88 号段 +
  │    orderRef=external_cleanup，等待全部成交，去重防重）；
  │    任一快照失败则零写操作；UNKNOWN/账户无法确认只告警
  └─ =0：告警模式——只检测 + external_alerts 快照 + 节流事件 +
       前端常驻弹窗/语音，绝不自动动作（v2026-10-22 曾短暂设为默认）
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
由 plot 值算出）；前端可切换为 ST 持续根数、ST 推进次数或 ST 同向 FVG 累积计数
降序，缺失值排最后、并列时回退 EMA 发散度。多空两列都展示（多头列 = trend_state==1、
空头列 ==-1），每行同时显示 FVG 方向与累积计数。

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
- 挂 **2 个 Study**：trend_state（默认输入即源码默认：ATR 10 / Factor 3.0 /
  Channel 1.0 / 回看 6 / 发散 1×ATR）+ FVG Finder+（默认输入：过滤器 Off /
  ST 3.0×10），均无需 setOption，与图表所见即所得；
- **ts 输出两套**：`ts`（实时，periods[0]，显示用）+ `ts_c`（确认，periods[1]，策略/下单用）；
  `direction`：Up_Trend 有效=1、Down_Trend 有效=-1、都无=0；
  `supertrend` / `channel_edge` 取 Up/Down 两个 plot 中有效者；
- **fvg 同样两套**：`fvg`（实时，显示用）+ `fvg_c`（确认，策略/选股用）；
  `fvg_last_dir` 由 daemon 自 study periods 历史向前扫描最后一个非零
  `fvg_event` 推导（上下沿只在新 FVG 形成时更新，两者天然对应）；
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
| `place_stop_only` | 独立 STP 兜底；先认回原括号子单，确认缺失后才允许使用 |
| `modify_stop` | 改保护止损触发价（STP 改 auxPrice，券商确认后更新本地） |
| `modify_stop_qty` | 部分成交后调整 STP 覆盖数量（券商确认） |
| `modify_limit_stop` | PENDING 中点单逐 bar 改价（同 orderId 修改 STP 子单 + LMT 父单） |
| `_next_tool_order_id` | 集中分配 88 号段订单号，持久化并支持跨进程互斥 |
| `fa_accounts` | 动态查询 dt_test 组成员，失败回退 config |
| `positions` / `net_liquidation` | dt_test 组口径持仓/净值 |
| `realized_pnl` / `daily_pnl` | 已实现盈亏 / 日盈亏（周末与 09:30 ET 前显示 0；常规时段为当前净值−前一日权益，盘后保留） |
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
| `GET /api/returns/compare?start&end` | 日期范围内账户已实现日收益与 QQQ 收盘收益、累计曲线及超额收益 |
| `GET /api/events` | 历史事件查询（JSONL 回溯） |
| `POST /api/refresh` | 手动刷新 |
| `WebSocket /ws` | 每 1s 广播 snapshot |

（已删除端点：`/api/lots/close-pending`(+/cancel)、`/api/tv/calc`、
`/api/stab/events`、`/api/stab/confirm`、`/api/signal/clear`、
legacy TRAIL 路径 `POST /api/lots`。）

**下单拦截**（两道闸门）：
1. `ALLOW_SHORT` 硬闸门保留但默认放开（config 已改 True，开放双向）；
2. **ST direction 闸门**（现行语义）：做多需 `ts_c.direction == 1`、做空需
   `== -1`；**震荡（trend_state=0）但 ST 方向正确也放行**；side 必须与
   ST direction 同向——可二次确认放行（force 执意开仓，见 6.3）。
   （注：与早期「trend_state==±1 才放行」不同，现行以 direction 为准。）

**事件记录**：`record_event(type, **data)` 追加 `logs/events-YYYYMMDD.jsonl`，覆盖下单/平仓/撤单/失败等所有事件，跨天可回溯。

#### 前端（static/）

- 宫格交易台 + 每格五个 tab：**EMA快线（ema，默认）/ 通道边沿（channel）/
  中点限价单（midpoint）/ 回踩限价单（retest，限价=bar_c2 极值）/
  市价单（market，marketable 限价=现价 ±0.5%，止损=限价 ∓1×ATR）**；
  - EMA/通道按 ts 实时值显示；midpoint 预览与后端一致，使用
    `(bar_c.high+bar_c.low)/2` 和 `ts_c.atr`；止损均为 `limit ∓ 1×ATR`，
    方向按钮按 trend_state 显示，震荡禁用；
- 每格：选股（下拉可搜索）+ 现价大字 + 限价/止损 + 计算（限价/股数/金额/资金占比）+
  FVG 摘要行（方向 + 累积计数 + 上下沿区间）+ 下单按钮；
- 顶栏：DRY-RUN/LIVE 徽章、单笔风险输入（300）、仓位上限%（10-200%）、TV/IBKR 时钟、更新延时、日盈亏；
- 底部 tab：**FVG 监控（默认）/ 选股 / 影子 / 自动策略 / 回测 / 运行日志 / 收益对比 / 绩效 / 事件**（「信号」tab 已删除）；
  - FVG 监控（v2026-10-20 起）：三列——多头 FVG / 空头 FVG / **FVG 回踩（首次）**；
    事件行含时间、标的、缺口区间、ATR 倍数（缺口高 ÷ 同 bar ATR）、状态
    （未回踩/回踩中/已回补/已回踩）；槽位无事件显示空占位；15/30/60 分钟窗口；
    通报 = 常驻通知（方向底色：多绿/空红，3 分钟倒计时自动消失）+ 语音，
    阈值「通报 ≥ X×ATR」可调（localStorage）；数据时钟每前进一根 bar 播 3s 钟声；
    **FVG 预警（v2026-10-21）**：指标在第三根 K 线形成过程中实时计算缺口条件
    （fvg = periods[0] 实时值），监控循环每拍校验 `fvg.time == bar.time` 且
    `fvg_event == ±1` 即预警一次（每 bar 每方向仅一次，盘中回补翻回 0 不影响，
    预警本就是提前量）；预警通知为虚线蓝框 + 「形成中」徽标，语音「F V G 预警」，
    与确认事件（实线框）明确区分，同受 ATR 阈值过滤；事件 `fvg_warn` 落事件流；
    **FVG 回踩列只报每个 FVG 的第一次回踩**（多：实时 bar.low ≤ 缺口上沿；
    空：bar.high ≥ 下沿），终身一次、第二次不再报；
    **FVG 追价一键下单（chase，v2026-10-21）**：事件行在「现价 − 缺口边沿 ≤
    FVG_CHASE_MAX_ATR_RATIO（默认 0.5）×ATR」时显示「追」按钮 → 确认框 →
    marketable 限价单（现价 ±0.5% 溢价）+ STP（限价 ∓ 1×ATR），以损定量，
    服务端按实时价重新定价并二次校验距离（`_fvg_chase_prices`），
    trend_state 闸门与 force 执意开仓与其他订单一致；
  - 选股：多头列 / 空头列（双向展示），每行显示发散度、ST 延续根数、ST 推进次数、
    FVG 方向与累积计数；可按 EMA 发散度、ST 持续根数、ST 推进次数或 FVG 累积
    降序，宫格默认标的跟随当前排序；
  - 自动策略：启动表单 + 待确认候选列表 + 实例列表；候选并排展示发散度、
    ST 延续/推进、FVG 方向/累积/上下沿、入场/止损、估算股数/市值/仓位占比，
    人工点击「确认开仓」或「忽略」；
  - 事件：历史事件查询（按类型过滤）；
  - 收益对比：起止日期可选；账户日收益 = 平仓归档已实现盈亏 ÷ 日起始净值，
    QQQ 日收益 = 常规时段收盘价 ÷ 前收盘 − 1；SVG 展示两条区间累计收益曲线、
    超额收益和逐日明细。净值锚点落 `logs/equity_daily.json`，缺失锚点的历史
    日期按平仓盈亏推算并标「估」；QQQ 日线缓存 `logs/qqq_daily.json`；
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
| init_entry_px | float | 下单时计划限价，R 风险基准；改价/成交后不改变 |
| init_stop_px | float | 下单时计划止损，R 风险基准；跟踪移动后不改变 |
| init_risk_usd | float | 下单时计划风险金额 = qty × abs(init_entry_px − init_stop_px) |
| limit_type | str | ema / channel / midpoint / retest / market / chase / auto_* / adopted |
| trader | str | 订单归属交易员（master / 交易员名 / auto；v2026-10-22 多人协作） |
| reprice_bar_time | float | midpoint 最近一次逐 bar 改价尝试时间（用于幂等与重启恢复） |
| state | str | PENDING / ACTIVE / CLOSING / CANCELLED / FAILED / CLOSED |
| close_order_id / close_permId | int | 平仓单 ID（88 号段）及券商永久 ID，供 CLOSING 对账恢复 |
| filled_qty / avg_fill | | 成交数据 |
| realized_pnl / actual_rr / mae / mfe | | 复盘字段 |

### 8.2 实时快照

见 4.2 节。

### 8.3 事件日志（events-*.jsonl）

每行一个 JSON：`{ts, type, ...}`。type：`order_placed / order_close_requested / order_closed / order_cancelled / order_close_failed / order_cancel_failed / position_scan_alert / auto_close_st_flip / missing_stop_alert / midpoint_limit_repriced / midpoint_limit_reprice_failed / retest_limit_repriced / retest_limit_reprice_failed / lot_revived / auto_breakeven / manual_breakeven / manual_breakeven_failed / external_order_cancelled / external_order_cancel_failed / external_position_closed / external_position_close_failed / external_position_close_pending`。

（已删除事件类型：`pierce_signal / entry_signal / alert_signal / signals_cleared / close_limit_*`。策略引擎另有一组 `auto_*` 事件类型，见 12.5。）

---

## 9. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| **价格源** | **全部 TV**（行情/K线/指标/定价/mark/平仓参考价），任何地方不用 TWS 行情数据（v2026-10-24 定稿） | TWS GUI 与 API 行情权限分离（FA 子账户 API 全线 10089 延时），TV 是唯一实时可靠源；`last_price` 已切换 TV provider + 撤销误伤黑名单（SMH/PANW） |
| 数据语言 | Node.js（数据）/ Python（执行） | 上游库语言绑定 |
| 周期 | 5 分钟（5M），K 线与指标同周期 | 噪音小（v2026-10-17 由 1M 调整） |
| 会话 | extended | 24x7 会让美股卡在昨日收盘 |
| 指标 | 双指标同 session：trend_state（USER;e577100fe79942b4830783d1ceaad535）+ FVG Finder+（USER;5ec7d04179b3429ca9286db452352d85），5M，range=200 | 过滤 + 入场线 + 方向 + ATR 一处算好（原 6 个指标合并）；FVG 提供累积计数/上下沿/方向 |
| 确认值 | 策略/下单用 ts_c（periods[1]，上一根已收盘）；实时值 ts 仅显示 | 避免盘中假转向 |
| 过滤 | trend_state：做多需 ==1、做空需 ==-1、0 拦截；side 与 trend_state 同向 | 所见即所得，控制端零逻辑；下单方向=ST 方向，无反向订单 |
| 信号提醒 | 无（两级信号 + 信号 tab 已删除） | 过滤器 + 人工下单 |
| 订单类型 | EMA快线 / 通道边沿 / 中点限价单（midpoint=(bar_c.high+bar_c.low)/2，OCA 双腿）/ 回踩限价单（retest=bar_c2 极值，多:限价=高/止损=低）/ 市价单（market=marketable 限价，现价 ±0.5%）/ FVG追价（chase，距边沿 ≤0.5×ATR 才放行） | 中点单 OCA 一成全撤；回踩单 PENDING 逐 bar 改价 + 股数跟随（固定风险预算）；市价/追价一次定价 |
| OCA 竞态 | 终态判定以 executions 成交真相为准 + reconcile 复活通道 | 突破腿成交与 LMT 撤单同帧时缓存状态漏报（四次实锤），成交真相是唯一可靠来源 |
| 幻影守卫 | 新建保护单前校验券商持仓（_has_broker_position）+ 快照认回 30s 冷却 | 防止幻影 lot 挂出真实反向单（AMD 实锤） |
| 止损 | 限价 ∓ 1×ATR，STP(GTC) 子单（parentId 关联 LMT(DAY) 父单） | 止损单只有 STP 一种；平仓/撤父单时子单自动取消 |
| 双向交易 | ALLOW_SHORT=True，多空两列选股，空头开仓开放 | 多空都做（v2026-10-17 起） |
| 强弱排序 | EMA 发散度 \|ema_fast−ema_slow\|/atr | 指标内部同款度量 |
| 持仓 | 以损定量（risk_usd 最大风险 + maxPositionPct 上限）+ **总风险敞口预算 1500**（保本敞口归 0 释放额度）；顶栏实时显示总风险敞口与总仓位占比 | 每笔固定风险 + 敞口总量封顶，笔数不限 |
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
| `EXTERNAL_ORDER_CLEANUP_ENABLED` | 1 | 1=（默认）号段外挂单撤销、非工具持仓立即市价平仓；0=告警模式只弹框提醒（v2026-10-23 恢复默认开启） |
| `RISK_PER_TRADE_PCT/USD` | 0.001 / 200 | 以损定量（净值/降级） |
| `RISK_MAX_ORDER_QTY` | 100000 | 单笔股数硬顶（仅极端兜底） |
| `RISK_MAX_OPEN_POSITIONS` | 0 | 最大持仓数；0 = 不限制（v2026-10-22 取消三笔上限） |
| `RISK_MAX_TOTAL_RISK_USD` | 3000 | **总风险敞口预算**（0=不限）：Σ 各 open lot 敞口（PENDING=qty×\|入场−止损\|；ACTIVE=filled×max(0,成本∓止损)，**保本归 0**）+ 新单风险 > 预算即拦截；笔数不限，能开几笔由敞口决定（v2026-10-22 引入，v2026-10-23 调整至 3000） |
| `RISK_DAILY_LOSS_LIMIT_USD` | 1000 | 日亏损熔断（已屏蔽） |
| `ALLOW_SHORT` | True | 是否允许做空（v2026-10-17 起开放双向） |
| `ENTRY_LIMIT_PREMIUM_PCT` | 0.5 | marketable 限价溢价 |
| `TRAIL_ATR_MULT` | 2.0 | **已废弃**（TRAIL 路径已删除，止损固定 1×ATR） |
| `TP_RR` | 0.0 | **已废弃**（随 TRAIL 路径一并废弃） |
| `STOP_GUARD_ENABLED` | True | 止损单守卫：每分钟核对持仓有无 STP 保护单，裸仓即提醒 |
| `AUTO_CLOSE_ON_ST_FLIP` | True | 持仓方向与 ts_c.direction 相反（多仓 ==-1 / 空仓 ==1）时自动平仓；False 退化为弹窗提醒 |
| `AUTO_BREAKEVEN_R` | 2.0 | ACTIVE 持仓浮盈达到 2R 时自动保本（按当前 bar 极值检查） |
| `SLOW_BREAK_SECONDS` | 900 | 开仓 15 分钟仍未保本时提醒减仓 |
| `AUTO_MAX_INSTANCES` | 5 | 全自动策略引擎的实例上限（见第 12 章） |
| `FVG_CHASE_MAX_ATR_RATIO` | 0.5 | FVG 追价允许的最大「现价−缺口边沿」距离（ATR 倍数，chase 一键下单阈值） |
| `CLIENT_TOKEN_TTL_HOURS` | 10 | 客户端 token 有效期（小时，自审批起算；过期须重新登记并重新审批） |
| `STOP_ATR_MULT` | 1.0 | **止损 ATR 倍数全局默认**（env 覆盖）；运行时经「止损 ATR×」顶栏输入修改，持久化 logs/settings.json，快照 status.stop_atr_mult 同步全客户端（v2026-10-22） |

---

## 11. 运行与部署

```bash
# dry-run（默认，虚拟成交）
EXECUTION_ENABLED=0 python3 -m uvicorn console.app:app --host 0.0.0.0 --port 8000

# 实盘（先确认 TWS 在线 + 账户有资金 + 风控参数）
EXECUTION_ENABLED=1 python3 -m uvicorn console.app:app --host 0.0.0.0 --port 8000

# 同步自选列表（TV Chrome 9222 开着）
python3 service/sync_watchlist.py
```

**局域网多客户端（v2026-10-22）**：默认绑 0.0.0.0。本机（127.0.0.1）= master
免确认；非本机打开需输入**交易员名称**注册（无密码），后端签 token 存
localStorage，**master 审批条确认后**该客户端才能交易（未审批可读不可写，
写端点 403；审批/拒绝仅本机可调）。**token 有效期 `CLIENT_TOKEN_TTL_HOURS`
（默认 10 小时，自审批时刻起算）**：过期后写操作 403、self 返回
`expired:true`，客户端自动清凭证回到登记页（可同名重新登记，旧 token 作废，
须重新走 master 审批——相当于每日开工确认一次身份）。订单归属记
`lot.trader`（master / 交易员名 / auto），订单表含交易员列，各端自己的
在手订单排前、他人排后（不同交易员块间有空白间隔带）。客户端注册表持久化
`logs/clients.json`（重启有效）。⚠️ 无密码体系，仅在可信内网开放。

`logs/autotrade_writer.lock` 由 console、auto_runner、main.py 启动时获取；
任一获取失败说明已有执行写进程在运行，禁止第二个实例重复撤改平同一订单。
`logs/tool_order_id.json` 则负责 88 号段发号持久化与跨进程互斥。

本机启动后自动打开 http://127.0.0.1:8000。首次启动先显示轻量加载页，
`/api/startup` 每秒报告交易服务、IBKR、全股票池确认指标和图表工作区进度；
全部就绪后才在原标签页进入主控制台。实盘必须等 TWS 连接，dry-run 不强制
等待 TWS。首次就绪状态锁定，后续短暂断线仍由主页面内的健康告警处理，不退回
加载页。控制台与选股联动的 TradingView 图表复用同一个 9222 调试 Chrome 进程，
各占独立标签页；冷启动一次打开两个 URL，已有浏览器时只补缺失标签页。设置
`CONSOLE_AUTO_OPEN_BROWSER=0` 可关闭自动打开浏览器（测试/无头运行）。

本机 master 顶栏提供「关闭系统」按钮，`POST /api/system/shutdown` 仅允许回环
地址调用并需二次确认。关闭顺序：永久关闭 IBKRGateway 交易门（13 个下单/改单/
撤单入口拒绝新调用，等待进行中的写调用结束并断开 API）→ 停止全部跟单子进程
→ 关闭 Trader Workstation（先正常退出，超时 TERM/KILL）→ uvicorn 优雅退出。
关闭期间其他 API 写请求返回 503。此操作只停止软件后续控制，**不会撤销券商已有
订单或止损**；需要恢复时重新运行启动脚本。

---

## 12. 策略引擎

**当前生产策略（v2026-10-31）：`ema9_pyramid` 全自动金字塔。** 核心代码
`engine/pyramid_engine.py`：使用 `ema9_autotrade` v7 的实时 `e9` 触线值，
无需人工候选确认。首仓风险预算取启动时顶栏「单笔资金」R，第二/三次分别为
2R/3、R/3，最多三次；每次达到首仓固定 R 的 2 倍后解锁下一次同向加仓。
成交后按股数加权平均成本重新计算 2R，旧 MFE 清零；第三次加仓除整组
均价 2R 外，还要求第二次仓位独立 MFE ≥1R（风险距离持久化在订单元数据），
避免相邻 K 线在上一仓未盈利时连续加仓。整组止损随解锁移至平均成本。
启动时以当前 `e9.direction` 锁定 `start_direction`，后续只接受该方向；
真实仓位止损/全平后实例 DONE，不自动翻向或重开（未成交撤单可重试）。
确认 `e9_c` 反向时整组平仓。Tab 展示
建仓次数、均价、下一步条件、第二仓 MFE 和最大仓位百分比，支持单票停止加仓/
整组平仓及订单筛选。手动 ACTIVE 仓可显式「转全自动」；策略等待首仓时，
同向手动市价实际成交自动作为 `manual_seed` 首仓接管（仅整组为空、实际成交、
合法止损），实际成本与首仓 R 用于后续管理，元数据持久化支持重启恢复。

以下为历史 `trend_pullback` 设计，仅用于旧回测对照。

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
6. **移动保本/跟踪止盈**：ACTIVE tick/回测极值达到 `breakeven_r`（2R）立即移成本（券商确认才标记，失败节流重试）；ST 线越过实际成本后即可按 `ts_c.supertrend` 单向棘轮，已保本后继续跟踪（1 美分防抖 + 不越过现价）。

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

### 12.6 回测

**当前回测 Tab**：`engine/backtest_session.py` 直接驱动与实盘同一个
`PyramidEngine + SimBroker`，使用历史 `e9`（不再使用旧 StrategyEngine）。
保留加载、单步、全跑；展示每次建仓风险预算、整组解锁/退出、平均成本、
总盈亏和最大仓位百分比。首仓预算与净值分别取当前顶栏单笔资金和账户净值。

以下为旧 `backtest_auto.py` 历史口径：把历史 5M bar（含 trend_state 指标值）逐根喂给 StrategyEngine（+ SimBroker，确认模式关闭），验证「同一策略 + 同一数据 = 同一结果」。

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

- `direction == 1`（ST 多头）：实时成交价触及 `ema_fast`、`ema_9` 或 `channel_edge`（price ≤ 线值）→ 做多；多线同触取更低价（更优入场）；
- `direction == -1`（ST 空头）：price ≥ 线值 → 做空；取更高价；
- **EMA9 入场线**：与图表内置 `STD;EMA` 参数一致（Length=9 / Source=close / Offset=0），daemon 和历史导出由同一组 5M 收盘价计算；已与图表实际 EMA9 数值对拍；
- **FVG 入场线**（`use_fvg_line`，默认开）：fvg_c 确认值中，多头方向存在需求区 FVG（`fvg_last_dir == 1`）时 `fvg_last_top` 作为第四条入场线（price ≤ 上沿 → 限价 = 上沿）；空头方向存在供应区 FVG（`fvg_last_dir == -1`）时 `fvg_last_bottom` 作线（price ≥ 下沿 → 限价 = 下沿）；与 EMA/通道线同触时同样取更优价；
- **FVG 过滤**（可选，默认关）：`require_same_dir_fvg=true` 要求存在同向 FVG（`fvg_last_dir == direction`）才允许开仓；`min_fvg_cum=N`（N>0）要求 `fvg_cum_count >= N`；字段缺失一律视为不满足（拦截，不静默放行）；
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
| `use_fvg_line` | True | FVG 边沿作为第三条入场线（v2026-10-20） |
| `require_same_dir_fvg` | False | 要求存在同向 FVG 才允许开仓（v2026-10-20） |
| `min_fvg_cum` | 0 | 要求 ST 同向 FVG 累积计数 ≥ N（0=不过滤，v2026-10-20） |

新策略在 `engine/strategies/__init__.py` 注册表登记（`build_strategy` 按名构建 + 参数覆盖）即可被引擎/回测/控制台使用。

---

## 14. 跟单系统（copy_trader，v2026-10-22）

一主多从的跟单架构：master 控制台（交易员操作，**零改动**）→ 每个 IBKR 服务器
一个独立跟单进程（互不影响，全部跑在 master Mac）。

```
master /ws 快照（lot 全生命周期）
   ├─ copy_trader.py --name A → TWS A（clientId 13）
   ├─ copy_trader.py --name B → TWS B（clientId 14）
   └─ ...
```

**跟单语义（定稿）**：
- master lot PENDING → 无动作（**master 没成交 = follower 永远无仓**，硬保证）
- 首次转 ACTIVE → follower 市价入场（marketable 限价 = master avg_fill ×(1±0.5%)）
  **+ STP 子单同发**（触发价 = master 当前 stop_px）——绝无裸仓；入场后立即
  对齐一次 master 最新 stop_px（防秒级窗口）；部分成交不追加
- master stop_px 变动（保本/跟踪/手动）→ follower 同步改 STP 到同一价格
- master CLOSED（任何原因）→ 先撤 GTC STP 再市价平仓
- master CANCELLED（未成交）→ 无动作 ✓

**股数**（`size_mode` × `size_multiplier` 放大系数，调试 0.1 → 正常 1.0 → 稳定 1.5）：
`risk`（默认）= risk_usd × 系数 ÷ |avg_fill − stop_px|；`multiplier` = master qty × 系数；
`balance` = master qty × (follower净值/master净值) × 系数。qty<1 记失败不重试。

**保险**：日回撤熔断（`max_drawdown_pct` 默认 3%，只拦新开仓，平仓/改止损同步照常，
页面手动解除）；entry/modify 指数退避 3 次进失败队列；**close 每 30s 无限重试**；
`direction=long_only` 方向过滤；`copy_existing_on_start=false` 不追历史仓。

**master 侧（console/app.py）**：配置 CRUD（`logs/copy_servers.json`，clientId 13 起
自动分配避开保留号段）+ CopySupervisor（每 enabled 服务器 spawn 子进程，崩溃指数
退避重启 10 次/小时封顶，配置变更自动 reload，EXECUTION_ENABLED=0 追加 --dry-run）
+ 快照 `copy` 段（配置 + `logs/copy-{name}-status.json` 合并 + process_alive）
+ 人工指令 `logs/copy-{name}-cmd.json`（retry/dismiss/unhalt）。

**前端「跟单管理」tab**：服务器配置表（参数/放大系数/熔断/方向/启停/增删）｜
状态卡（连接灯/净值/已跟/待跟/失败/回撤/熔断徽章）｜跟踪订单对照表
（master lot ↔ follower lot 状态）｜失败订单表（重放/忽略）。

**已知坑**：websockets 16 新 asyncio 客户端在本环境（Python 3.11 + uvicorn）
握手即 EOF（连最小 uvicorn ws 也失败）——WS 客户端一律用
`websockets.legacy.client.connect`（实测稳定）。

**daemon 稳定性（v2026-10-31：核心行情与 attack 隔离）**：
- 分批启动（默认 6 chart/批 × 3s，`--batch-size`/`--batch-delay-ms` 可调）——
  一次性发起 54×(1 chart+4 study) 会被 TV 限流卡死且不自愈（实测两次复现）；
- 巡检自愈：每 30s 检查，仅 chart 超 90s 无核心 K 线时才销毁重建
  （间隔 1.5s，单 chart 上限 10 次）；attack 无值只每 5 分钟聚合告警，
  不再重建 chart。attack 正文加载失败时降级为无区间数据，trend/FVG/
  smart_stop 与 K 线继续运行；
- `pine_text.js` 缓存回退强制校验远端/缓存版本（忽略末尾 `.0`），版本错配
  明确报错，禁止旧脚本正文拼新元数据导致 plot 静默缺失；
- `realtime_collector` 两个管道坑：① stderr 接 PIPE 必须持续排空
  （`_stderr_drain`），否则 daemon console.error 写满 64KB 管道永久阻塞；
  ② `create_subprocess_exec` 必须 `limit=4MB`——tick JSON 单行 54 标的
  实测 71KB > asyncio 默认 64KB 流上限，readline 抛 ValueError，
  读取异常曾静默 break 导致「daemon 健康但零数据」（现已改为记日志退出）。

---

## 15. 盘前突破实时信号（breakout_tracker，v2026-10-24）

把 `research_breakout.py` 的批量研究算法改造为实盘逐 bar 流式信号，
口径与批量版**逐条对拍一致**（六天回放全绿：10-13/19/20/21/22/23）。

**算法（多空双出，v2026-10-25）**：盘前区间 = 当天 04:00–09:25 ET 的 5M
高低点 → 09:45 ET 起收盘越界（站上：前收 ≤ 盘前高 且 本收 > 盘前高；
跌破：前收 ≥ 盘前低 且 本收 < 盘前低，每次越界都算）→ 下一根须同向
FVG（多 fvg_event=1 / 空 -1）且同向 K 线 → 同一确认时刻候选 ≤3 只立即
出信号，>3 只升级再等一根要求三连阳/三连阴镜像（确认时间延后到第三根）
→ 确认时刻 ≤10:30 ET；BLACKBULL:/CAPITALCOM: 期货剔除。

**架构**：
- `engine/breakout_tracker.py` — 纯逻辑状态机（无 IO）：每 symbol 累积/锁定
  盘前区间、越界挂起、FVG 确认、拥挤组终判（`flush(bar_time)`）、三连阳/阴升级。
  **拥挤计数多空混算**（与批量版 Counter 口径一致——对拍一致的关键），
  信号多空双出（side=站上高点/跌破低点，字段结构一致）。
- `console/app.py` — `_breakout_loop`（1s）：bar_c.time 前进 = 新 bar 确认，
  带对齐校验后的 fvg_c.fvg_event 喂 tracker；信号 → `record_event("breakout_signal")`
  + `_breakout_signals` 缓冲（4h TTL）+ 快照 `breakout_signals` 段。
- 前端信号栏新增「突破」/「跌破」卡片（多绿/空红亮底 + 徽标，显示
  ticker/确认时间/盘前低~高/区间宽/确认方式），固定排栏顶，复用逐条
  删除与 4 分钟过期机制；语音播报「站上盘前高点」/「跌破盘前低点」。

**盘前区间双来源（v2026-10-24 二期）**：图表「5M 进攻区域」指标
（`USER;8ab4f534e5874b47b24a5ce5c49acace`，daemon 第 4 个 Study，注册表
`attack_zone`）plot 出 `premarketHigh/premarketLow/Bar_Color`（na=1e100→null），
快照带 `attack/attack_c`；实测与 research.db 盘前口径（04:00–09:25 ET）精确一致。
`_breakout_loop` 在 `attack_c.time == bar_c.time` 且 pmh>pml>0 时经
`tracker.set_premarket()` 注入（每 symbol 每天一次），**注入优先于自累积**，
无效值自动回落自累积——盘中重启控制台也能拿到正确盘前区间，原
「必须 04:00 ET 前启动」限制解除。**注入门控 bar 时间 hm≥570**（09:30 ET）：
指标的 premarketHigh/Low 盘前时段是累积中的半成品（实测 pml 一路下探，
09:30 首根常规 K 线才锁定终值）——盘前启动等 09:30 首根注入，开盘后启动
第一拍即注；否则盘前启动会把半成品区间锁死一整天（2026-10-24 用户指出）。
`bar_color`（0/1 方向过滤）已下发快照，暂不使用。

---

## 16. 盘前热点新闻统计（news_digest，v2026-10-25）

盘前新闻摘要：RSS 采集（无 key）+ LLM 主题归纳 + 自选股热点识别。

- `engine/news_digest.py` — 纯逻辑 + 两个 IO 边界（fetcher/llm 可注入）。
  数据源（2026-10-25 实测）：MarketWatch 头条 RSS + Google News RSS
  主题查询（premarket movers / stock market today / Fed rate / earnings
  各前 10 条）；个股层 watchlist 每 ticker 一次 Google 查询（0.25s 限速，
  18h 时间窗，每票留 5 条）。⚠️ Yahoo 个股 RSS 已失效（返回 HTML），禁用。
  标题归一化去重（Google 尾挂来源剥离）；摘要喂 agent_llm（JSON 约束）
  产出 {market_mood, themes, hot_symbols{ticker:{headline,gist,sentiment,
  catalyst}}}；LLM 不可用 → summary=None 降级纯标题列表，不阻塞。
  落盘 logs/news-YYYYMMDD.json（tmp+replace 原子写）。
- `console/app.py`（只追加）：`_news_loop` 每天 07:30 ET 自动生成一次；
  GET /api/news（空态 {"empty": true}）；POST /api/news/refresh
  （require_trader，冷却 10 分钟）；快照 status.news 小摘要
  {generated_at, theme_count, hot_count, hot{ticker→gist}}；独立
  @app.on_event("startup") 注册，不侵入既有函数体。生成走
  asyncio.to_thread（阻塞 RSS/LLM 不进事件循环）。
- 前端「盘前新闻」tab（FVG 监控后第二位）：大盘情绪+主题列表 / 自选股
  热点卡（ticker 可点 → tvShow，情绪色 pos绿/neg红/neu灰 + 催化徽标）/
  原始头条 / 手动刷新。徽标联动：突破卡/FVG 卡/选股行上标的命中当日
  hot_symbols → 琥珀「新闻」小徽标（tooltip=gist）。
- 测试 tests/test_news_digest.py（fixture XML + mock LLM，零网络）；
  真网冒烟在 tests/live/test_news_live.py（mark live，默认不跑）。

---

## 17. 盘前报告工作流 + YouTube 视频源（v2026-10-25）

新闻摘要（§16）之上再加一层「盘前交易分析报告」：LLM 生成中文 markdown
（固定五章节：隔夜综述/主题/自选股催化/盘前异动/风险提示；有视频源时加
第六章「盘前视频观点」），存档供复盘。

- **报告层**（engine/news_digest.py）：`news_window`（D-1 16:00 ~ D 09:30
  ET 窗口）/ `backfill_suffix`（Google News after:/before: 日期运算符回填）
  / `generate_report`（markdown 章节校验，缺章回退降级汇编）/ `save_report`
  （报告头 + 原始条目附录，tmp+replace 原子写 → logs/reports/
  premarket-YYYYMMDD.md）。降级路径：LLM 不可用仍产出标题汇编版并标注。
- **视频源**（engine/youtube_digest.py）：用户贴 YouTube 链接（watch?v=/
  youtu.be//shorts/ 三种形态），本机 yt-dlp（env YT_DLP 覆盖路径）抓元数据
  + 字幕（元数据直链优先，回退 --write-auto-subs；VTT→纯文本去时间戳、
  滚动重复行合并、6000 词截断）；⚠️ 必须带 Chrome cookies（env
  YT_DLP_COOKIES，默认 "chrome"，空串关闭）否则被反爬拦「not a bot」。
  发布时间窗口过滤（timestamp 精确优先，upload_date 日级兜底）；LLM 中文
  摘要 ~200 字；缓存 logs/yt_cache/<video_id>.json（字幕哈希一致不重复调
  LLM）。单视频任何失败只标记 status/error，不阻塞报告。
  ⚠️ YouTube 对字幕端点有 IP 级 429 限流（Google「Sorry」页），密集重试
  会加重；触发后等一段时间再试，报告侧自动降级跳过该视频。
- **CLI**：`python3 premarket_report.py [--day YYYY-MM-DD]`（--day=回填
  模式：窗口过滤 + 跳过 MarketWatch 无历史；缺省=当日前 18h）。
- **console API**（只追加）：`GET /api/reports`（列表倒序）/
  `GET /api/reports/{date}`（全文，日期格式硬校验防路径穿越）/
  `GET /api/reports/status` / `POST /api/reports/generate`（require_trader；
  在跑 409、冷却 10 分钟 429；asyncio task 后台跑，完成/失败记事件）；
  视频源管理 `GET|POST /api/report/sources`、`/{video_id}/toggle|delete`
  （落盘 logs/report_sources.json，添加后异步补元数据，生成后回写
  last_status/last_error）。
- **前端「盘前报告」tab**（FVG 监控后第二位，盘前新闻顺延第三）：
  「生成今日报告」按钮（running 倒计时 + 上次结果）/ 左列历史列表 /
  右列轻量 markdown 渲染（ticker 可点 → tvShow）/ 视频源输入框与列表。
- 测试：tests/test_reports_api.py（API/工作流/并发锁/冷却/路径穿越）、
  tests/test_youtube_digest.py（链接解析/VTT/窗口/缓存/编排，全 mock）；
  真网冒烟 tests/live/test_youtube_live.py（mark live）。

---

## 18. 客户端看盘：「每人一格」+ 截图回传（v2026-10-27）

其他交易员要看 master 同款 TV 图（私有指标 trend_state/FVG/smart_stop 只有
master 的屏幕能渲染），且多客户端会同时看不同标的 → 按宫格分画面位：

- **trader → pane 绑定**：master 固定 pane 0（只切不截图，面前就是屏幕）；
  已审批客户端按首次使用分 pane 1..N（映射落盘 logs/tv_panes.json，
  两小时未用可回收，布局缩小自动清越界映射）；pane 分完 → `pane_full`
  「画面位已满」。布局由人工保证，启动时校验 pane 数并在快照
  `status.tv_view`（panes/slots）展示（选股 tab 头部小字）。
- **链路**：`POST /api/tv/view`（require_trader）→ master 直通
  `_tv_show_symbol`；客户端 → `_tv_ensure_browser` → `_tv_assign_pane`
  → 每 pane 一把 asyncio 锁 → `_TV_PANE_SHOW_JS` 切该格（已是目标标的
  跳过切换 = 刷新 fast-path）→ `_TV_PANE_READY_JS` 轮询数据就绪（最多
  12s + 1.2s 渲染稳定）→ `tv_cdp.screenshot(clip=pane rect)` 只截该格
  → base64 JPEG 回传。
- **前端**：`tvShow()` 统一改调 `/api/tv/view`；客户端收到图弹
  「TV 图表」面板（el-dialog，10s 自动刷新 + 手动刷新，关闭停表）；
  master 行为不变（无弹窗）。
- 实测：单格布局正确报 pane_full；借 pane 0 验证全链路（切 TSLA →
  截图含 SuperTrend/FVG 区域私有指标，2328×1540 @dpr2）。
- 测试 tests/test_tv_view.py（分配/回收/满员/master 直通/fast-path/
  未就绪透传，CDP 全 fake）。
- 后续候选：TV widget 自绘（用采集数据在前端画 K 线，摆脱单屏限制）——
  视截图版使用效果再定。

---

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
