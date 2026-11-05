# autotrade

读取 TradingView Pine 策略模型的输出，执行风控、下单、止损同步和订单恢复。
第一个已接入模型是用户自定义的 **EMA9-ST-ATR**。

开发新指标时，先读取 [项目协作约定](AGENTS.md) 和 [Pine 指标长期规范](docs/PINE_INDICATOR_CONTRACT.md)。
这些规则作为项目上下文沿用，用户只需描述本次策略逻辑，无需再次粘贴通用提示词。

当前控制台入口：

```sh
python3 -m uvicorn console.app:app --host 127.0.0.1 --port 8000
```

默认 dry-run。已有 `run_console.sh` 启动方式继续兼容。

控制台顶栏的“系统架构”入口在新标签页打开架构图、模块术语、策略输出规范和开发模板，支持图片缩放及 PNG / SVG 下载。说明内容直接读取项目文档。

“回测”页通过“运行回测”一次完成数据加载和模拟执行。人工选择交易日及美东启动时间，核对启动后的策略输出、执行决策和订单结果；回测参数独立于实盘设置。

Pine 自动信号的首仓与加仓使用 **市价 MKT + STOP 保护单**，不等待回到 Pine 参考线；数量按触发观察价与止损距离估算，实际成交以券商回报为准。手动限价和已有挂单保留原类型。见 [市价入场变更与验证记录](docs/PINE_MARKET_ENTRY_2026-11-02.md)。

“夜盘模拟”页可独立订阅 24h 数据，查看 5 分钟 K 线、实时/确认 Pine 属性，并手动启动模拟执行。
默认允许入场窗口为美东 20:00–次日 04:00，报告独立保存在 `logs/paper_tests/`；详见[夜盘模拟使用说明](docs/NIGHT_PAPER_TEST.md)。

控制台按执行监控、回测验证、研究复盘和系统运维组织工作区。参数按作用范围管理，实例行显示生效规则与风险，订单明细集中展示成交、止损和保护回报；回测支持价格/决策联动、人工预期和 JSON / CSV 导出。见 [界面优化交付记录](docs/UI_OPTIMIZATION_DELIVERY_2026-11-02.md)。

导航集中为单排，默认执行监控独占内容区；订单、FVG、选股按需切换，选股独立位于 FVG 监控之后，研究和运维通过同排菜单进入。需要同时看订单与实例时可主动分屏，底部不再保留重复 Tab 栏。所有工作区已适配与 TradingView 左右分屏，见 [分屏优化与逐页截图](docs/UI_SPLIT_SCREEN_OPTIMIZATION_2026-11-02.md)。

macOS 启动控制台后默认自动打开专用 Chrome，并整理为原生左右分屏：左侧 autotrade，右侧 TradingView。复用已有图表及正确排列的分屏；通过 PID 绑定的原生可访问性句柄定位专用浏览器，需要运行控制台的应用具有辅助功能权限。可用 `CONSOLE_AUTO_SPLIT_BROWSER=0` 保留双标签页，用 `CONSOLE_AUTO_OPEN_BROWSER=0` 完全关闭自动打开。详见 [浏览器自动分屏说明](docs/BROWSER_WORKSPACE.md)。

- [架构、模型接入与运行入口](ARCHITECTURE.md)
- [专业术语与模块命名规范](docs/TERMINOLOGY.md)
- [独立系统架构图（PNG / SVG）](docs/diagrams/README.md)
- [Pine 指标长期开发规范](docs/PINE_INDICATOR_CONTRACT.md)
- [新 Pine 指标开发提示词与统一导出属性](docs/PINE_INDICATOR_DEVELOPMENT_PROMPT.md)
- [第一阶段成果与验证基线（2026-11-02）](docs/milestones/2026-11-02-phase1.md)
- [第二阶段：领域契约与订单操作协调（2026-11-02）](docs/milestones/2026-11-02-phase2.md)
- [第三阶段：本地事务账本与故障恢复（2026-11-02）](docs/milestones/2026-11-02-phase3.md)
- [详细历史设计与交易功能](DESIGN.md)
- 公共模型声明：`service/pine_strategies.json`
- Python 回归：`python -m pytest -q`
- Node/前端检查：`npm test`
- 隔离开发依赖：`requirements-dev.txt`

`main.py`、`auto_runner.py` 是历史流程，策略语义与当前控制台不同，定位见架构文档。

小区间突破的完整信号、预约、风险和退出口径见 [突破规则](docs/OPENING_BREAKOUT_RULES.md)，也可从系统架构中的“突破规则”查看。
