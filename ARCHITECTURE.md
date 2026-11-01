# Pine 策略与公共执行模型

## 项目边界

Pine 指标是策略模型：指标输出入场信号、方向、入场参考价和止损。
Python 是执行模型：读取输出、校验、去重、按风险定量、管理订单和保护单、对账恢复。
Python 不用均线或价格形态重新生成 Pine 入场信号。

当前第一个生产模型是用户的 **EMA9-ST-ATR** 指标，指标 ID 保持
`USER;a17a52ae5b6642e0a3ddbba23edc9ff3`。已有 API/订单中的
`ema9_pyramid` 标识、`e9/e9_c` 快照键以及旧状态文件继续兼容。

## 当前链路

```text
service/pine_strategies.json（模型与 plot 映射，共用声明）
  ├─ Node pine_models.js → TradingView Study → 每秒行情快照
  └─ Python PineModelRegistry → PineOutput
       → AutoService（模型、执行实例、全局实例容量）
       → StrategyRuntime（按模型/实例/标的分发实时值和确认值）
       → PineExecutionEngine（公共订单执行状态机）
       → LiveBroker → OrderManager → RiskGate / IBKRGateway

相同 PineExecutionEngine + SimBroker → BacktestSession
```

## 模型声明与接入

唯一的模型声明文件是 `service/pine_strategies.json`。每个声明包含：

| 字段 | 含义 |
|---|---|
| `id` | 稳定的策略标识，持久化后不要随意更名 |
| `name` | 控制台显示名称 |
| `indicator_id` | 用户的 TradingView Pine 指标 ID |
| `realtime_key` / `confirmed_key` | 实时/收盘确认输出的独立快照键 |
| `plots` | 快照字段名到真实 Pine plot 名的映射 |
| `fields` | 公共执行属性到快照字段名的映射 |
| `execution_profile` | 明确选择的执行配置 |

公共输出属性：`entry_signal`、`direction`、`entry_price`、`long_stop`、
`short_stop`；输出时间来自 Pine `$time`。`entry_signal=1` 表示入场，
`direction=1/-1` 表示多/空。价格必须为有限正数，止损必须在正确一侧。
信号时间缺失、字段非法或实时输出与当前 bar 不匹配时，不开新仓并记录拒绝原因。
确认指标尚未与收盘 bar 对齐时，等待下一次快照，不提前消耗 bar 游标。

接入后续超级趋势回踩指标时：

1. 在 TradingView 提供真实 Pine 实现和稳定的输出 plot。
2. 在声明文件中添加指标 ID、字段映射及独立快照键；不要覆盖 `bar/ts/fvg` 等核心字段。
3. 选择执行配置。Node 实时订阅和历史导出读取同一声明，无需复制 Python 策略代码。
4. 用录制或合成输出验证信号、方向、止损、反向退出和恢复；指标实际可用后再启动。
5. 重启加载新配置后，控制台模型列表会显示它。未注册模型不能启动。

仓库只注册已有的 EMA9-ST-ATR。测试中的第二模型只是字段映射夹具，不是已实现的超级趋势策略。

## 执行配置与原有行为

`ema9_legacy_pyramid` 显式保留原 EMA9 行为：09:45–15:00 ET 入场、
3-2-1 风险预算、独立加仓/保本门槛、共享止损和确认方向反转退出。
现有全局金字塔设置只应用于默认 EMA9 执行实例，保持旧接口兼容。
旧 ST/FVG 叠加守卫也仅作用于旧订单和 EMA9 兼容订单，不自动套用于新模型。

`single_entry` 是新增模型的基础执行配置：一次建仓，无自动金字塔和自动保本，
初始止损取 Pine，之后跟随确认 Pine 止损收紧；确认方向反转时退出。
止损只紧不松、账户风险限制、交易时段调度属于显式执行约束。
若后续模型需要其他下单或退出语义，应新增明确的执行配置和契约测试，不隐式猜测。

OHLC 回测仍使用既有确定性极值路径，不能还原真实根内成交顺序；实盘仅使用每秒最新价。

## 订单与恢复归属

归属键为：`strategy_id + strategy_instance_id + symbol + group_id`。
不同模型或不同执行实例可以管理同一标的，分别保存订单、去重键、bar 游标和恢复状态。
账户风险预算由同一个 OrderManager/RiskGate 汇总。

新订单元数据增加 `strategy_id`、`strategy_instance_id`、`execution_profile`。
旧订单缺少这些字段时仅按默认 `ema9_pyramid/default` 接管。
`pyramid_groups` 状态文件键保留，内容现在也可保存其他模型的执行组。
恢复后仍默认暂停加仓；缺少完整状态时继续显示暂停原因并管理保护与退出。

## 控制台职责

- `console/app.py`：兼容 ASGI 入口，只调用 `create_app()`。
- `console/factory.py`：创建独立应用、挂载路由、启动和清理资源。
- `console/routes/`：HTTP/WebSocket 参数、鉴权依赖和服务调用。
- `console/schemas.py`：既有请求契约及新增可选模型/实例字段。
- `console/services/`：交易、自动执行、设置、身份、跟单、监控、报告、快照等应用功能。
- `console/runtime/bootstrap.py`：显式组装服务及其状态，支持注入数据目录。
- `console/runtime/strategy.py`：统一 Pine 输出调度。
- `console/runtime/tasks.py`：命名后台任务、重复启动保护、取消并等待退出。

导入 `console.app` 不加载生产订单、设置或凭据，也不连接 TV/TWS。
生产资源在生命周期启动时创建；测试通过临时 `RuntimePaths` 创建独立服务。
各功能服务拥有自己的状态，服务通过组装上下文明确访问依赖，不反向导入 `console.app`。
当前应用服务仍使用现有字典和 HTTP 请求模型；完整领域类型与存储迁移属于后续阶段。

## 运行入口

| 入口 | 定位 |
|---|---|
| `python -m uvicorn console.app:app` | 当前生产控制台入口；默认 dry-run |
| `run_console.sh` | 现有控制台启动包装，继续兼容 |
| `auto_runner.py` | 历史回踩 StrategyEngine 运行器，不是 EMA9-ST-ATR 主入口 |
| `main.py` | 历史提案 + PositionManager 入口 |
| `copy_trader.py` | 独立跟单执行器，保留原有跟单语义 |
| `engine/backtest_session.py` | 按模型声明复用公共执行状态机的回测 |

`engine.auto_engine` 继续导出旧 Broker 名称，`engine.pyramid_engine` 保留
`PyramidEngine/PyramidInstance` 兼容路径。新代码直接使用公共 Broker 和 Pine 执行模块。

## 验证与部署边界

测试环境使用 Python 3.11，开发依赖见 `requirements-dev.txt`：

```sh
python3.11 -m venv /tmp/autotrade-test
/tmp/autotrade-test/bin/pip install -r requirements-dev.txt
/tmp/autotrade-test/bin/python -m pytest -q
npm test
```

默认排除 live 测试。状态、日志和配置在加载前即注入临时目录，测试不启动生产监听端口、
不连接 TradingView/TWS。既有 API 路径保存在 `tests/fixtures/phase1_routes.json`，
另有多模型、多执行实例、Broker、恢复、应用隔离、任务退出和写锁归属测试。

本阶段不迁移数据库，不替换生产状态文件，不自动重启实盘。部署或回滚代码时，
保留运行期间生成的真实订单与交易组状态，不用历史文件覆盖券商执行事实。
