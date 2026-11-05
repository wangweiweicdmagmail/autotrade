# Pine 自动信号改为市价入场

日期：2026-11-02。用户反馈 GOOGL 已触发信号，但等待 Pine 参考价的 LMT 未成交，明确要求自动策略使用市价入场。

## 最终行为

- 所有通过公共 Pine 执行引擎产生的首仓与加仓，显式使用 **MKT DAY 入场父单 + STP GTC 保护止损子单**。不依赖手动 `MARKET_ORDER_STRICT` 开关，也不会为保证旧行为而静默退回 LMT。
- `Entry_Price` / 兼容的 `EMA_9` 继续表示策略模型参考价。执行引擎不再等待价格回到参考线才成交，数量按触发观察价到实际止损的距离估算。成交、余仓与保护依据券商回报管理，提交成功不等于已成交。
- 首仓初始绝对止损继续来自 Pine；兼容金字塔加仓继续使用本交易组的既有实际止损。策略信号、时段、加仓解锁、保本、去重、账户风控和退出协调规则保留。
- 实际成交可能滑点，风险预算是计划估算，不保证真实成交风险被严格封顶。市价单仍受券商可交易时段、连接和订单受理条件约束，不保证立即或足额成交。
- 手动限价入口保留。`limit_type=auto_<模型ID>` 继续标记来源与归属，不能把它改成 `market` 而丢失模型恢复身份。

## 契约与持久化

`OrderPlan.entry_order_type` 显式记录 `MKT/LMT`，`entry_price` 保留 Pine 参考，`risk_reference_price` 记录用于定量的触发观察价。LiveBroker、SimBroker 和 OrderManager 沿同一声明执行。

兼容的 `place_limit_stop` / `place_limit_stop_lot` 方法名保留，实际类型由声明决定。订单管理仍先持久化受理意图，再调用网关；券商引用、累计成交增量和未知结果不重发的规则保持不变。

`OrderLot.entry_order_type` 随现有账本载荷保存，不改变 SQLite 表结构。旧记录缺失时留空并显示“类型未记录”，不据来源字段猜造历史订单类型。已有 LMT 挂单不会在升级时被自动转换、撤销或另发 MKT。

## 回测、夜盘模拟与界面

SimBroker 的 MKT 使用信号处理时的观察价模拟成交，普通 LMT 继续使用原触价模型。夜盘仍只消费人工启动后的新鲜输入，不回放历史高低点补成交。

历史回测沿用原确定性极值路径：多头先观察最高价、空头先观察最低价。新 MKT 按该观察价模拟入场，而非 Pine 参考线，因此数量、盈亏和风险统计可能明显变化。该路径不是信号实际发生时的市场价格；报告新增明确限制说明，不能把模拟盈利当成策略验证通过。

订单批次、实例订单、自动提交提醒和回测订单均区分市价与限价；CSV 增加订单类型、风险参考价，JSON 的旧 `placed.limit` 保留兼容并增加 `reference/risk_reference/order_type`。

合成数据截图：[订单类型显示](screenshots/split-screen-2026-11-02/market-orders-1020.png)、[回测委托与成交核对](screenshots/split-screen-2026-11-02/market-backtest-1020.png)。不是实际 GOOGL 成交证明。

## 验证与生效

- 新增多空“价格已离开 Pine 参考价”回归，确认调用 MKT、正确数量、保留 Pine 参考、STOP 及恢复归属；覆盖手动 LMT、非法止损方向、模拟不等回踩和旧字段兼容。
- 生产工作区回归的业务断言通过，但文件清单护栏受到运行中 SQLite 临时 journal 变化干扰。没有关闭护栏或改动生产账本。
- 随后在不复制生产配置、凭据、订单及研究库的临时代码副本运行 `python -m pytest -q`：**441 passed，13 skipped，4 deselected，13 项既有警告**。跳过项依赖未复制的历史研究数据，live 默认排除。
- 隔离运行发现原有 ShadowTracker 构造未沿用 RuntimePaths，现改为使用 `data_dir/research.db`；生产默认路径仍是 logs，测试构造不再接触生产研究库。
- `npm test`：**14 项通过**。合成界面截图检查无渲染错误；代码检查及差异格式检查通过。

本次没有重启运行中的交易服务，没有操作真实订单或 TWS。**需要重启控制台后，后续新触发订单才加载此规则**。已有 GOOGL 或其他 LMT 挂单应由用户按真实券商状态核对处理，不能为测试或切换类型而自动补发。

长期规则同步到 [Pine 输出契约](PINE_INDICATOR_CONTRACT.md)、[开发提示词](PINE_INDICATOR_DEVELOPMENT_PROMPT.md)、[架构](../ARCHITECTURE.md)及[夜盘说明](NIGHT_PAPER_TEST.md)。
