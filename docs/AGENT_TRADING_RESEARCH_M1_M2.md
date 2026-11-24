> 2026-11-24本轮最终规则：[开盘小区间中点归一化前十榜](OPENING_POSITION_RANKING.md)。中点为0、半宽为1，默认混合，可查看全池多头／空头前十；09:45起新开仓，开仓方向仍按最新确认close／VWAP。此前VWAP／EMA评分描述仅供历史追溯。

# Agent 研究归档与逐单复盘使用说明

M1与M2已实现。本版增加旁路行情归档、历史补取、榜单版本、订单研究投影、逐单问题、方法记录和离线CLI。M3完整策略实验与M4影子决策仍按[开发方案](AGENT_TRADING_RESEARCH_PLAN.md)后续实施。

## 启用与数据边界

正式控制台下一次正常重启后，现有TV订阅会把预载历史、新收盘K线及修订写入`logs/research/spool/`；后台写入`logs/research/archive.sqlite3`。股票沿用extended，指数沿用default，必要日线沿原会话口径保存。归档不依赖指标成功，也不重新订阅全池指标。

版本记录保留本地Pine源码、模型映射、依赖锁、评分与感知源码，以及准入、执行和保护代码与实际风险配置。订单日志补充唯一事件身份、实际账户归属、精简行情与大盘观察、保护选项及研究判断关联。新增字段只用于研究，不更改准入、发单、风控或退出判断。

现有`live_audit.sqlite3`、执行账本和历史日志保持原存储。新研究库只读投影执行账本；旧审计仅在交易已有精确帧引用时按需读取，不全库扫描、不删除。仅已入库、已校验且标记封存的新增传输副本可清理，未完成或损坏的片段保留。

未收盘K线不作为历史确认K线入库。行情修订保存不同版本，按`as_of`读取时只返回当时已经观察到的数据；事后补取不会被回填为过去已知。交易的实际榜单引用与入场前最近观察到的榜单分开返回，引用缺失会明确列出。

## 数据质量与恢复

归档有内容校验、提交水位、事务入库、重放去重、研究库版本检查与备份。传输写入有内存限额；写入失败、丢弃计数、暂存半行和损坏记录会显式记录。仍在内存中且尚未持久化的记录不能保证抗进程崩溃，K线缺口进入补取，关键订单事实仍保留在原执行账本及事件中。

后台在启动后留出预热时间，之后以低频检查当日缺口，最多每轮两个历史请求；每个日期和行情序列最多三次，失败会记录，不无限重试。手动历史补取也有请求数上限。没有TV授权、源端历史不足或补后仍缺失时保持受限状态。

数据清单区分完整、采集中、未收到、待核实缺口与会话计划未知。空分钟不能直接认定无成交；不会补造价格。现金股票常规盘及扩展盘的日历采用[ICE公布的2026至2028年交易安排](https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/)，处理节假日、提前收市和夏令时。该范围外、指数default会话以及日线参考不会冒充已完成全会话校验，仍可读取已实际保存的数据。

## 离线命令

在项目目录使用项目Python解释器运行。以下命令的`--data-dir`可指向独立数据目录；默认是项目`logs/`。离线研究不启动控制台，不连接TWS。

```sh
python research.py --data-dir logs data-status --day 2026-11-20
python research.py --data-dir logs sync
python research.py --data-dir logs daily
python research.py --data-dir logs daily --day 2026-11-20
python research.py --data-dir logs trade-review --group GROUP_ID --questions
python research.py --data-dir logs questions --status pending --limit 20 --offset 0
python research.py --data-dir logs asked --question QUESTION_ID
python research.py --data-dir logs answer --question QUESTION_ID --text-file answer.txt --source "用户答复来源"
python research.py --data-dir logs methods
python research.py --data-dir logs method --text-file rule.txt --source "用户确认来源" --question QUESTION_ID --status user_confirmed
```

`answer`保留答复原意和来源；用户明确确认后可加`--confirm`。已回答问题不能退回已问状态，重复提交相同答复不重复追加。方法可用`--method-id`增加修订版本；M2只接受假设及用户确认的方法，不允许直接标记为已验证。

查询行情与当时榜单：

```sh
python research.py --data-dir logs bars --symbol NASDAQ:MSFT --interval 1m --start START_EPOCH --end END_EPOCH --limit 1000
python research.py --data-dir logs bars --symbol NASDAQ:MSFT --interval 1m --series-id SERIES_ID --start START_EPOCH --end END_EPOCH --as-of OBSERVED_EPOCH
python research.py --data-dir logs rankings --day 2026-11-20 --at CONFIRMED_EPOCH --as-of OBSERVED_EPOCH
python research.py --data-dir logs backfill --day 2026-11-20 --credentials service/creds.json --max-requests 4
python research.py --data-dir logs backup --to /path/to/new-research-backup.sqlite3
```

同一标的存在多个数据源或时段时，行情查询要求明确`series-id`，不会任意选一个。补取只请求历史K线，凭据通过进程标准输入中的文件路径提供，不写进命令行凭据或结果。备份目标必须不存在，并执行SQLite一致性检查。

## 每日复盘与持久记录

每日任务保持北京时间10:00，改为调用`daily`。首次处理最近有交易的日期；后续优先处理新动作和用户答复，再逐日推进历史积压。无新增材料返回`skipped`，不重复生成交易结论。任务依赖本机可运行及数据可读，失败不会当成无交易。

每笔复盘包含实际批次与交易组、精确成交身份、动作发起者、原始判断证据、当时榜单、入场前可知与事后研究窗口，以及问题与解释。已知自动保护、策略退出和外部守卫与人工行为分开；未知行为保持待问。精确账户与成交ID去重，跨日退出持续归入原组，不能按股票、数量和价格猜配。

研究进度和问答保存在：

- `logs/agent_research/progress.json`：已复盘版本和历史推进水位。
- `logs/agent_research/questions.jsonl`：问题与每次状态、答复和来源。
- `logs/agent_research/methods.jsonl`：方法版本、确认状态、支持问题与反例。
- `logs/agent_research/reviews/`：按交易日期生成的JSON与Markdown报告。

问答和方法使用进程锁、追加写入及持久化；进度使用原子替换。在新会话中继续读取同一研究记录即可。报告完成后才推进复盘水位，旧答案和方法修订保留历史。

完整交易组的计数排除分笔成交的重复计算，未结束组不算完成。初始实际风险按真实累计入场数量、成交均价及初始止损估算；缺少可靠数量时保留未知，不用委托数量替代。费用不明时不输出净R。MFE／MAE来自原记录，不能从分钟高低点伪造入场或退出前后的根内路径。超过100组仅提示应进行首次评估，不代表可自动实盘。

## 控制台复盘研究页

导航新增“复盘研究”，也可从“研究复盘”菜单进入。左侧按美东日期列出交易组，包含方向、状态、开平仓时间、组累计已实现盈亏和未解问题；右侧展示开仓、平仓、减仓、保本及止损调整的时间线。券商与执行回报默认折叠，可勾选展开。

首次进入或点击“刷新订单”会增量同步研究投影；选择日期时整理该日的未解问题，不启动实盘服务、不改执行账本或复盘进度。跨日交易组在相关活动日可见，盈亏按完整组累计展示，不能将这些日期简单相加当作逐日盈亏。

每条问题可直接填写、保存、确认或修订交易思考，仍追加写入同一个`questions.jsonl`，来源为`console:master`；保留原始问题、每次答复和版本，不自动升级成已验证方法。草稿在当前页面会话内保留，刷新离开前提示未保存内容；跨会话更新发生冲突时拒绝覆盖，先展示最新解释，再由用户处理草稿。

本页沿用本机研究访问范围；远程客户端不因此取得全部交易思考的读取或编辑权限。保存接口核对本机来源和版本，长度受限，没有任何订单操作入口。新增后端须重启控制台加载，之后刷新页面即可进入。

隔离验收：Python完整998项、Node／前端239项通过；Python另有13项条件跳过和4项live排除，保留既有FakeProc.wait警告。浏览器用合成订单验证填写、保存和重载，解释仍可读回；测试未连接TV／TWS或写生产数据。[界面预览](screenshots/research-review-preview-2026-11-23.jpg)仅含合成订单。

## 本机研究接口

控制台加载新版本后提供：

- `GET /api/research/data-status?day=YYYY-MM-DD`
- `GET /api/research/bars?series_id=...&start=...&end=...`
- `GET /api/research/rankings?day=YYYY-MM-DD&at=...`
- `GET /api/research/trades/{交易组或批次身份}`
- `GET /api/research/questions?status=pending`

仅允许回环连接。原有查询保持只读，新增工作台接口：

- `POST /api/research/workbench/refresh`：增量同步订单研究投影，返回可用日期。
- `GET /api/research/workbench/days`、`GET /api/research/workbench/day?day=YYYY-MM-DD`：日期与逐笔摘要。
- `POST /api/research/workbench/day?day=YYYY-MM-DD`：整理当日未解问题并返回摘要。
- `POST /api/research/workbench/trades/{identity}`：查看时间线及问题。
- `POST /api/research/workbench/trades/{identity}/questions/{question_id}`：提交`text/revision/confirmed`，仅追加解释；版本冲突返回409。

用户也可继续通过离线CLI保存答复。接口没有开平仓或修改生产配置的能力。现有`/api/market`额外报告`research_archive`健康状态，原行情与交易接口保留。交易影子Agent的提示词与待开发方案见[影子研究员](AGENT_SHADOW_TRADER.md)；本页上线不代表已启用影子交易。

## 验证范围

隔离测试覆盖K线版本、预载与根内过滤、历史修订、观察时间、半行、校验失败、断电式恢复、单调提交水位、封存传输清理、补取限流、日历、账本只读、精确成交去重、跨日组、未知动作提问、问答并发、问题状态、答复重开、历史队列、离线CLI及本机接口隔离。测试与性能样本不连接真实TV／TWS、不读写生产数据。

2026-11-23在不含生产日志、凭据和历史导出数据的临时项目副本中完成完整回归：Python 994项通过、13项跳过、4项live测试排除；Node 233项通过。Python保留一条既有跟单测试中FakeProc.wait未等待的RuntimeWarning。代码及文档通过`git diff --check`。本次未重启正式控制台，新增采集的正式运行验收留待下次正常启动。

性能样本使用合成的54个标的订阅矩阵，共245534根已确认K线。本机单次测量中，Node预载归档回调合计约63ms，含异步持久日志约282ms；已保存4000根的重复更新回调平均约0.39ms，后台SQLite导入约3.54秒，零丢弃、零归档问题。该样本验证新增归档开销，不代表真实开盘的端到端行情或交易延迟；正式采集需在用户正常重启后另行观察。


2026-11-24：新增数据库优先的`cache-ranking-day`与`rank-at`，目标日完整1m／5m直接入现有archive.sqlite3，指定时刻严格截断并使用同一生产评分；不写真实历史榜单。股票当前默认regular，指数default；具体版本与命令见[评分及历史重建](TREND_SCORE_V6.md)。
