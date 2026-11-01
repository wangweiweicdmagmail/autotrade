# autotrade

读取 TradingView Pine 策略模型的输出，执行风控、下单、止损同步和订单恢复。
第一个已接入模型是用户自定义的 **EMA9-ST-ATR**。

当前控制台入口：

```sh
python3 -m uvicorn console.app:app --host 127.0.0.1 --port 8000
```

默认 dry-run。已有 `run_console.sh` 启动方式继续兼容。

- [架构、模型接入与运行入口](ARCHITECTURE.md)
- [第一阶段成果与验证基线（2026-11-02）](docs/milestones/2026-11-02-phase1.md)
- [详细历史设计与交易功能](DESIGN.md)
- 公共模型声明：`service/pine_strategies.json`
- Python 回归：`python -m pytest -q`
- Node/前端检查：`npm test`
- 隔离开发依赖：`requirements-dev.txt`

`main.py`、`auto_runner.py` 是历史流程，策略语义与当前控制台不同，定位见架构文档。
