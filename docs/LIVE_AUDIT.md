# 实盘数据审计与 Agent 只读查询

本页说明保留的按秒审计与只读接口。2026-11-23新增的确认K线与榜单归档、逐单问答和方法记录见[M1与M2使用说明](AGENT_TRADING_RESEARCH_M1_M2.md)。完整研究目标见[Agent交易研究数据系统](AGENT_TRADING_RESEARCH.md)，后续实验和影子决策尚待实施。

## 行情与执行记录

行情与信号接入层把交易执行引擎可见的每帧行情快照写入 `logs/live_audit.sqlite3`。每帧保留采集时间、源进程发布时间和当时的标的集合。按标的保存实时 K 线、确认 K 线、七个 PineTS 指标的实时及确认输出和相关历史上下文。重复内容复用已保存的版本，快照编号仍逐帧保留。指标缺失或确认暂未就绪时保留当时的实际数据。

执行事件继续追加到 `logs/events-YYYYMMDD.jsonl`。主端的策略判断和手动入场记录可带 `decision_market_frame`，用它查询实际判断时的行情。其他事件的 `market_frame_at_event` 表示事件产生时最新的行情。券商事件记录提交、改单、撤单、状态和成交回报，包含订单号、父单号、订单引用和成交编号。跟单端的券商事件保存在各自的跟单事件日志。当前订单和历史归档仍以执行账本为准。

审计写入由后台批量完成。Agent 可通过 `market_audit` 查看最近提交编号、队列长度、累计丢失帧数和写入错误。磁盘故障、积压或进程异常退出时，尚未落盘的帧可能缺失。本版保留的是应用接收并可供执行模型读取的快照，时间粒度约为一秒。TradingView 原始逐笔更新和 PineTS 未发布的中间结果没有包含在该快照中。

## 只读接口

| 用途 | 本机查询路径 |
| --- | --- |
| 当前行情和七个指标 | `GET /api/market`，也可用 `/ws` 订阅 |
| 按审计编号还原某标的行情 | `GET /api/market/audit?symbol=CAPITALCOM%3ANAS100&frame_id=<编号>` |
| 某标的最近的行情审计帧 | `GET /api/market/audit?symbol=CAPITALCOM%3ANAS100&limit=20`，下一页传 `before` 为最小编号减一 |
| 当前挂单和持仓批次 | `GET /api/lots?active_only=true`，可附加 `symbol` |
| 当前聚合持仓 | `GET /api/positions` |
| 历史交易归档 | `GET /api/trades/history?symbol=NASDAQ%3AMSFT&limit=100`，可用 `before` 按平仓时间分页 |
| 按批次或券商订单跟踪事件 | `GET /api/events?lot_id=<批次号>` 或 `GET /api/events?order_id=<订单号>` |

行情审计查询仅接受回环连接。Agent 可从当前行情取数，也可先从历史记录找到批次，再按事件中的行情编号还原交易时点的输入。
