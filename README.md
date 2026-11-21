# autotrade

读取 TradingView Pine 策略模型的输出，执行风控、下单、止损同步和订单恢复。
第一个已接入模型是用户自定义的 **EMA9-ST-ATR**。

当前功能与使用规则见 [近期功能整理（2026-11-21）](docs/FEATURES_2026-11-21.md)：候选与订单工作台、TV 八格与六格联动、趋势评分、开仓准入、候选持仓保护、VWAP 策略及验证边界。

开发新指标时，先读取 [项目协作约定](AGENTS.md) 和 [Pine 指标长期规范](docs/PINE_INDICATOR_CONTRACT.md)。
这些规则作为项目上下文沿用，用户只需描述本次策略逻辑，无需再次粘贴通用提示词。

当前控制台入口：

```sh
python3 -m uvicorn console.app:app --host 127.0.0.1 --port 8000
```

默认 dry-run。已有 `run_console.sh` 启动方式继续兼容。

首次没有 TradingView 凭据时，加载页会引导用户在本机专用 Chrome 登录自己的账户；后台自动提取并私密保存登录会话，随后继续加载行情，不需要手动运行脚本。远程客户端只显示进度，服务器登录操作仍在服务器电脑完成。见 [TradingView 首次登录引导](docs/TRADINGVIEW_LOGIN.md)。

控制台顶栏的“系统架构”入口在新标签页打开架构图、模块术语、策略输出规范和开发模板，支持图片缩放及 PNG / SVG 下载。说明内容直接读取项目文档。

“回测”页通过“运行回测”一次完成数据加载和模拟执行。人工选择交易日及美东启动时间，核对启动后的策略输出、执行决策和订单结果；回测参数独立于实盘设置。

选择 `ema7_autotrade_v2` 时，回测只从 TradingView 拉取 5 分钟 K 线，并用 PineTS 本地计算策略模型。首次使用前运行 `npm ci`。K 线和计算结果保存在 `logs/backtest_history.sqlite3`；同一标的和交易日调整启动时间时，页面显示“已复用指标结果”。第一版支持常规盘 78 根 K 线完整的交易日，要求开盘前至少 400 根预热数据。回测使用与已验证 TradingView 3.0 相同的预测偏移参数 0，报告记录实际参数及数据版本。

Pine 自动信号的首仓与加仓使用 **市价 MKT + STOP 保护单**，不等待回到 Pine 参考线；数量按触发观察价与止损距离估算，实际成交以券商回报为准。手动限价和已有挂单保留原类型。见 [市价入场变更与验证记录](docs/PINE_MARKET_ENTRY_2026-11-02.md)。

“夜盘模拟”页可独立订阅 24h 数据，查看 5 分钟 K 线、实时/确认 Pine 属性，并手动启动模拟执行。
默认允许入场窗口为美东 20:00–次日 04:00，报告独立保存在 `logs/paper_tests/`；详见[夜盘模拟使用说明](docs/NIGHT_PAPER_TEST.md)。

控制台按执行监控、回测验证、研究复盘和系统运维组织工作区。参数按作用范围管理，实例行显示生效规则与风险，订单明细集中展示成交、止损和保护回报；回测支持价格/决策联动、人工预期和 JSON / CSV 导出。见 [界面优化交付记录](docs/UI_OPTIMIZATION_DELIVERY_2026-11-02.md)。

导航集中为单排，默认打开“候选与订单”，左侧候选榜和持仓保护控制，右侧订单批次。其他工作区按需切换，研究和运维通过同排菜单进入；控制台内部的执行实例分屏按钮和拖动分隔条已移除，各 Tab 使用完整内容区。早期布局保留在 [历史分屏优化记录](docs/UI_SPLIT_SCREEN_OPTIMIZATION_2026-11-02.md)，当前以 [工作台说明](docs/MARKET_RHYTHM.md) 为准。

启动控制台后默认准备专用 Chrome 的三个独立窗口：`auto_trade` 八格、`auto_trade2` 六格、AutoTrade 控制台。已有确切布局页面会复用，不重新加载图表；三个窗口的位置由用户安排。`CONSOLE_AUTO_OPEN_BROWSER=0` 关闭自动打开，`CONSOLE_AUTO_ARRANGE_WINDOWS=0` 跳过三窗口整理；旧左右分屏不是默认启动路径。远程客户端使用自己的 [Chrome 扩展](extensions/autotrade-tv/README.md) 绑定本机图表。详见 [浏览器工作区说明](docs/BROWSER_WORKSPACE.md)。

- [架构、模型接入与运行入口](ARCHITECTURE.md)
- [当前功能、操作规则与验证汇总（2026-11-21）](docs/FEATURES_2026-11-21.md)
- [候选榜评分、TV 联动与候选持仓保护](docs/MARKET_RHYTHM.md)
- [当前风险预算与开仓限制](docs/RISK_LIMITS.md)
- [TD 分时指标与开仓门槛](docs/TD_PRICE_LINE.md)
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

### TradingView 行情代理

如果控制台通过 Tailscale 远程访问，同时 TradingView 行情需要走本机代理，可在 `service/tv_market_proxy.local.json` 写入 `{"proxy":"http://127.0.0.1:1082"}`，将地址替换为本机 HTTP 代理地址。也可以用环境变量 `TV_MARKET_PROXY` 为部署指定地址。行情进程重连时会读取配置，登录授权和行情 WebSocket 使用同一个代理。删除本地配置并清除环境变量后恢复默认连接方式。
