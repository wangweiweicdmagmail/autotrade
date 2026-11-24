> 2026-11-24本轮最终规则：[开盘小区间中点归一化前十榜](OPENING_POSITION_RANKING.md)。中点为0、半宽为1，默认混合，可查看全池多头／空头前十；09:45起新开仓，开仓方向仍按最新确认close／VWAP。此前VWAP／EMA评分描述仅供历史追溯。

> 最新评分定版：[位置50／VWAP开盘推进30／五分钟EMA5分类20](TREND_SCORE_V6.md)，模型v6。不使用斜率；开仓方向和风险规则保持close／VWAP准入。下文旧纯位置评分供历史追溯。

# 单趋势榜与VWAP方向开仓（2026-11-24）

本页按用户最新要求替代[基本TD准入](BASIC_TD_ENTRY.md)和[双榜方案](TD_RANKING_VIEW.md)。股票仅用regular常规时段，一分钟、五分钟均不加载盘前盘后。TV仅提供K线，本地指标计算架构不变。

## 候选与方向

- 只显示一个趋势榜，按同根已收盘一分钟 `Z=(close−VWAP)/σ` 的 `abs(Z)` 降序取十名，同值按代码；不是EMA3／EMA7，也不是价格直接除以σ。模型 `vwap_close_z_v5`，版本5，`price_source=close`。VWAP和σ的成交量口径不变。
- 收盘价 > 同根VWAP中线只可做多；<中线只可做空；相等不新开仓。无需先突破±1σ。
- 全部新开仓使用当日服务端已接纳完整趋势前十的标的范围，技术方向用所选股票最新独立确认的一分钟close与VWAP。新分钟全池未齐时保留上一完整名单，不等整榜才更新方向。
- 不再要求个股TD、ST、EMA9、大盘TD或双指数同向。指数仍显示参考TD，TD着色与智能退出的TD规则保留。删除TD榜计算、切换和多空显示筛选；旧 `candidate_ranking=td` 请求明确拒绝，不能静默转为趋势单。
- 风险、账户、报价时效、合法保护止损、重复建仓、常规盘、当日完整名单和最终发单前复核继续执行。离榜／方向变化不会因此自动平仓。

## 手动市价与限价

开仓卡片提供市价／限价切换。市价按现有 `MKT父单 + STP保护子单`；限价为 `LMT DAY父单 + STP GTC保护子单`，不是触价后追市价。

| 趋势方向 | 可选限价 |
|---|---|
| 多头 | VWAP中线；上轨VWAP+1σ |
| 空头 | VWAP中线；下轨VWAP−1σ |

价格使用所选股票最新有效确认一分钟的轨道，服务端重新计算，忽略浏览器提交的价格与止损；限价、σ止损和方向证据取同一次定价快照，防止确认数据更新时混用两根轨道。取整沿用股票美分精度。限价提交后固定，不跟随每分钟轨道自动改价、不自动升级市价。可能立即成交，也可能一直未成交；挂单可由用户在订单批次撤销。方向翻转后新请求重新选同向价位，已有受理挂单不因本次修改自动撤改。

初始止损仍由用户选择：五分钟2×ATR且至少位于当前五分钟K线外，或以入场参考价向亏损方向偏移1σ。市价用当前观察价定量；限价用最终限价与止损距离定量。限价不是新的自动策略，不改变Pine自动／组合信号的市价入场、模型止损或既有退出配置。两类手动单共用重复批次防护，挂单占用既有风险额度。保本／候选跟踪按实际成交后计时，未成交不能记为持仓。

## 接口与追溯

开仓读取 `entry_candidates`，契约 `data_contract_version=4`、`entry_policy=stock_vwap_side_v1`、`direction_source=confirmed_vwap_direction_v1`；`td_required=false`、`ema9_required=false`、`beta_filter_required=false`、`vwap_direction_required=true`。原 `confirmed_td` 继续服务于展示和智能退出，新增 `confirmed_vwap` 与各行 `direction_fact` 同源。

方向事实含同根 `close/vwap/sigma`、方向、bar开盘时间、确认时间、来源ID和报价时效。VWAP方向事实不依赖TD计数或TD比较周期；缺VWAP时TD退出仍可工作，缺TD时有效VWAP不被额外拦截。股票方向事实只接受regular；指数default仅作观察。

手动限价仍使用 `POST /api/lots/limit-stop`，例如：

```json
{
  "symbol":"NASDAQ:EXAMPLE", "side":"long",
  "limit_type":"vwap_limit", "entry_mode":"rhythm_atr",
  "candidate_ranking":"trend", "vwap_limit_level":"upper",
  "manual_stop_mode":"atr_2", "limit_px":100, "stop_px":98
}
```

`vwap_limit_level` 为 `vwap|upper|lower`；多头lower、空头upper明确拒绝。示例价格仅是请求占位，服务端用确认轨道和所选止损规则重算。市价仍为 `limit_type=market`。账本保存 `rhythm_entry`（限价档位、参考价、方向事实ID／时间）、`candidate_admission`（名单ID与方向证据）及实际父单类型，成交以券商回报为准。未知受理结果只对账不重发。

## 缓存与生效

旧v3、EMA3位置v4及不同session的已接纳缓存保留磁盘但不作为v5榜单；先校验完整性，再等首份完整v5常规盘批次接替。原审计、研究数据、执行账本和历史缓存不删除。TV席位继续十分钟整体重排，分钟出入榜局部替换，持仓优先。

后端及Node生产代码需重启加载，随后刷新浏览器。隔离测试使用合成数据、临时账本和券商桩，不连接TV/TWS或发送实盘订单。

本次验证：`python -m pytest -q` 为1297项通过、13项跳过、4项live排除；`npm test` 为257项通过。跟单测试已有的FakeProc.wait协程警告未改变。完整页面模板通过Vue编译；独立临时Chrome以合成行情验证多空限价档位自动切换及预览定量，无页面错误。测试未读取生产订单、未连接TV/TWS、未重启服务或发实盘单。
