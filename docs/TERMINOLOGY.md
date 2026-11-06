# 项目术语与模块命名规范

## 项目定位

项目中文名称：**Pine 驱动的量化交易执行系统**。
英文描述：**Pine-driven Quantitative Trading Execution System**。
仓库名称继续使用 `autotrade`。

以下是项目内统一采用的专业术语及其职责映射，不表示新增了组件或已经完成物理拆分。
文档、需求和后续界面文案沿用这些名称；现有代码符号、API、模型 ID、快照键和持久化字段保持兼容，不因术语规范化批量更名。

## 两个核心概念

**策略模型（Strategy Model）**：由 Pine 指标实现的交易决策规则。负责方向、入场条件、入场参考价和策略止损。首个策略模型为 **EMA9-ST-ATR**。

**执行模型（Execution Model）**：Python 中可复用的交易执行规则。规定如何将有效策略输出转换为订单，以及如何处理风险定量、成交、保护、退出和恢复。执行模型通过执行配置选择具体行为，由执行引擎运行。

Python 工程还包含数据接入、控制台、券商连接等支撑模块，因此讨论具体模块时使用下表中的职责名称。

## 模块名称与代码映射

| 统一中文名称 | 英文名称 | 职责 | 当前主要实现 |
|---|---|---|---|
| 策略模型 | Strategy Model | 生成交易方向、入场信号、参考价和止损 | TradingView Pine 指标；首个为 EMA9-ST-ATR |
| 策略模型注册表 | Strategy Model Registry | 声明可用模型、指标 ID、输出映射及执行配置 | `service/pine_strategies.json`、`engine/pine_models.py`、`service/pine_models.js` |
| 策略输出契约 | Strategy Output Contract | 约定输出字段、取值、时间语义与有效性 | `docs/PINE_INDICATOR_CONTRACT.md`、`PineOutput` |
| 执行领域契约 | Execution Domain Contracts | 统一行情、确认 K 线、策略输出、订单计划、成交与交易组的数据语义 | `domain/contracts.py`；旧 `PineOutput` 兼容别名 |
| 订单操作协调器 | Order Operation Coordinator | 在逐订单锁上协调退出、撤单、减仓与改单；建仓串行核对账户风险 | `execution/operations.py`、`OrderManager` |
| 行情与信号接入层 | Market Data & Signal Ingestion Layer | 获取 TradingView K 线，将 PineTS 的本地指标结果组装为实时与确认快照 | `service/tv_cloud_daemon.js`、`service/pinets_live.js`、`engine/realtime_collector.py` |
| 执行编排服务 | Execution Orchestration Service | 启停执行实例、选择模型、检查实例容量并调度输出 | `console/services/auto.py`、`console/runtime/strategy.py` |
| 交易执行引擎 | Trade Execution Engine | 运行执行模型，推进交易状态、去重、建仓、保护和退出 | `engine/pine_execution.py` |
| 执行配置 | Execution Profile | 选择单次建仓或兼容金字塔等具体执行规则 | `execution_profile`、`engine/execution_rules.py` 及执行引擎 |
| 风险控制模块 | Risk Management Module | 计算风险对应的数量、检查账户预算及下单约束 | `execution/risk.py`、`engine/order_calc.py`、订单管理中的风险检查 |
| 订单管理模块 | Order Management Module | 管理订单生命周期、撤改单、成交与关联保护单 | `execution/order_lot.py` 中的 `OrderManager` |
| 交易通道适配层 | Broker Adapter Layer | 为公共引擎提供实盘或模拟执行接口 | `engine/broker_protocols.py`、`engine/brokers/` |
| 券商连接网关 | Broker Connectivity Gateway | 与 IBKR/TWS 通信，传递订单、成交和账户信息 | `execution/ibkr.py` |
| 状态持久化与恢复模块 | State Persistence & Recovery Module | 事务保存执行状态，迁移版本，核对订单与交易组并恢复管理 | `execution/state_store.py`、订单管理及执行引擎中的恢复逻辑、`execution/writer_lock.py` |
| 历史回放与执行回测模块 | Historical Replay & Execution Backtesting Module | 使用历史输入和模拟通道验证公共执行规则 | `engine/backtest_session.py`、`engine/brokers/simulated.py` |
| 夜盘模拟验证模块 | Overnight Paper Validation Module | 独立订阅 24h 行情及 Pine 输出，人工启动实时模拟并保存测试报告 | `console/services/night_test.py`、`engine/paper_feed.py`、`service/pine_paper_daemon.js` |
| 交易运维控制台 | Trading Operations Console | 配置、启停、订单和风险查看、恢复状态展示 | `console/routes/`、`console/services/`、`console/static/` |
| 应用运行时 | Application Runtime | 组装依赖、管理生命周期、后台任务与资源清理 | `console/factory.py`、`console/runtime/bootstrap.py`、`console/runtime/tasks.py` |

表中风险、订单、恢复等模块按职责划分，当前部分逻辑跨文件协作；不能仅据此表推断它们已是独立服务。
回测模块复用执行引擎，但 OHLC 回放不能还原真实根内成交顺序。

## 容易混淆的业务对象

| 术语 | 定义与用法 |
|---|---|
| 策略模型 / Strategy Model | 一套 Pine 决策规则，例如 EMA9-ST-ATR；不指一次运行 |
| 执行模型 / Execution Model | 公共执行规则的整体；不为每个 Pine 指标复制一个 Python 策略 |
| 执行配置 / Execution Profile | 执行行为的命名选择；当前为 `single_entry`、`ema9_legacy_pyramid` |
| 执行实例 / Execution Instance | 某策略模型的一次独立运行身份，对应 `strategy_instance_id`；结合模型、标的等归属管理状态 |
| 策略输出快照 / Strategy Output Snapshot | 某一采集时刻的输出及时间信息；分实时快照与确认快照 |
| 入场信号 / Entry Signal | 是否存在可申请建仓的条件；不是订单或成交 |
| 入场计划 / Entry Plan | 有效信号、方向、参考价和对应初始止损构成的交易意图；仍须通过执行检查 |
| 交易组 / Trade Group | 一轮交易中相关建仓单、加仓单和保护单的逻辑归属，对应 `group_id`；不等于账户净持仓 |
| 策略止损价 / Strategy Stop Price | Pine 输出的理论止损价格 |
| 保护止损单 / Protective Stop Order | 执行系统根据规则提交和维护的券商订单；价格以实际订单状态为准 |
| 执行账本 / Execution Ledger | SQLite 中持久化的本地订单、交易组与历史归档；券商订单和成交仍是外部交易事实 |
| 受理意图 / Submission Intent | 发往券商前保存的本地操作记录；存在意图不代表券商已受理或已成交 |
| 实时快照 / Realtime Snapshot | 当前未收盘 bar 的最新状态；当前用于入场判断 |
| 确认快照 / Confirmed Snapshot | 已确认 bar 的策略输出；当前用于反向退出及止损管理 |

归属使用 `strategy_id + strategy_instance_id + symbol + group_id`；账户风险统一汇总。
策略输出、内部执行状态和券商订单事实应分别表述，避免将信号出现写成已成交。

## 沟通与界面用词

- “开发一个新策略模型”：实现新的 Pine 决策规则并满足输出契约。
- “接入一个策略模型”：登记真实指标、字段映射和执行配置，并验证接入链路。
- “新增一种执行配置”：扩展下单、加仓、保护或退出规则，不改变 Pine 策略含义。
- “启动一个执行实例”：运行已注册策略模型，建立独立执行身份。
- 界面优先使用“策略模型”“执行实例”“执行配置”“交易组”“保护止损单”等具体名称，避免统称“自动策略”。
- “模型”表示规则集合，不隐含机器学习；本项目不因此改称 AI 交易系统。

现有 `StrategyRuntime` 对应执行编排职责，`PineExecutionEngine` 对应交易执行引擎；旧名称保留兼容。
历史 `main.py`、`auto_runner.py` 及独立跟单流程的定位以 `ARCHITECTURE.md` 为准，不混同为当前 Pine 主执行链路。
