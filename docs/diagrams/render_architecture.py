"""Generate the editable SVG architecture diagram; Python standard library only."""
from html import escape
from pathlib import Path

OUT = Path(__file__).parent
W, H = 2600, 2470
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', '''
<defs>
 <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#536579"/></marker>
 <marker id="teal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#167b82"/></marker>
</defs>
<style>
text{font-family:"PingFang SC","Heiti SC","STHeiti",sans-serif;fill:#172d43}
.title{font-size:48px;font-weight:600}.sub{font-size:27px;fill:#52667a}
.heading{font-size:31px;font-weight:600}.body{font-size:27px}.code{font-size:23px;fill:#52667a}
.label{font-size:23px;fill:#52667a}.lane{font-size:24px;font-weight:600;fill:#52667a}
</style>
<rect width="2600" height="2470" fill="#f4f7fb"/>
''']

def text(x, y, s, cls='body'):
    parts.append(f'<text x="{x}" y="{y}" class="{cls}">{escape(s)}</text>')

def box(x,y,w,h,title,lines,code='',kind='normal'):
    fill,stroke={'normal':('#ffffff','#cdd8e4'),'core':('#eaf2ff','#91b3df'),'pine':('#e5f5f0','#8bc9b4'),'external':('#edf0f4','#bdc9d5')}[kind]
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    text(x+26,y+45,title,'heading')
    for i,line in enumerate(lines): text(x+26,y+85+i*34,line)
    if code:
        for i,line in enumerate(code.split('\n')): text(x+26,y+h-12-(len(code.split('\n'))-1-i)*30,line,'code')

def edge(points,label='',lx=0,ly=0,dash=False,feedback=False):
    col='#167b82' if feedback else '#536579'
    coords=' '.join(f'{x},{y}' for x,y in points)
    extra=' stroke-dasharray="10 8"' if dash else ''
    marker='teal' if feedback else 'arrow'
    parts.append(f'<polyline points="{coords}" fill="none" stroke="{col}" stroke-width="3" stroke-linejoin="round"{extra} marker-end="url(#{marker})"/>')
    if label:
        width=len(label)*24+24
        parts.append(f'<rect x="{lx-8}" y="{ly-26}" width="{width}" height="34" rx="5" fill="#f4f7fb"/>')
        text(lx,ly,label,'label')

text(60,80,'Pine 驱动的量化交易执行系统','title')
text(60,128,'当前实现架构 · 策略模型生成决策，执行模型管理交易 · 2026-11-22','sub')
text(60,200,'声明与执行配置','lane')
text(690,200,'主链路：策略输出 → 交易执行 → 券商','lane')
text(2000,200,'运维与运行保障','lane')

box(690,230,1220,160,'01  策略模型  /  Strategy Model',[
 '本地 Pine 源码：由 PineTS 计算方向、入场、参考价和止损',
 '首个模型：EMA9-ST-ATR；已接入模型复用相同输出契约'],kind='pine')
box(690,450,1220,160,'02  策略输出契约  /  Strategy Output Contract',[
 '入场信号 · 方向 · 入场参考价 · 多头止损 · 空头止损',
 'ATR 为辅助输出；信号出现不代表已下单或已成交'],code='StrategyOutput (PineOutput) / domain/contracts.py')
box(690,670,1220,170,'03  行情与信号接入层',[
 '只订阅 TradingView K线，PineTS worker 本地计算指标',
 '实时快照 + 确认快照 + 最新行情价格'],code='tv_cloud_daemon.js / pinets_live.js / realtime_collector.py')
box(690,900,1220,170,'04  执行编排服务',[
 '启停执行实例、检查容量，按模型 / 实例 / 标的分发快照',
 '校验时间对齐和输出，分别推进实时与确认处理'],code='AutoService  /  StrategyRuntime')
box(690,1130,1220,230,'05  交易执行引擎  /  执行模型的运行组件',[
 '实时快照 → 入场校验、去重、风险定量、申请建仓',
 '确认快照 → 止损收紧、方向转向提示及已授权退出',
 '依据执行配置管理交易组；不重新计算 Pine 行情信号'],code='engine/pine_execution.py  /  PineExecutionEngine',kind='core')
box(690,1420,1220,150,'06  交易通道适配层  /  实盘路径',[
 '将引擎操作转换为订单管理调用：建仓、撤单、平仓、移止损'],code='LiveBroker  /  engine/broker_protocols.py')
box(690,1630,1220,170,'07  订单管理模块',[
 '维护订单生命周期，管理市价入场与保护止损单',
 '提交前调用账户风险检查；接收成交与订单状态'],code='execution/order_lot.py  /  OrderManager')
box(690,1860,1220,140,'08  券商连接网关',[
 '传递委托、撤改单，接收订单 / 成交 / 账户信息'],code='execution/ibkr.py  /  IBKRGateway')
box(690,2060,1220,140,'09  券商执行环境',[
 'IBKR / TWS：实际订单、成交、持仓与账户事实'],kind='external')

box(60,450,540,300,'策略模型注册表',[
 '模型 ID / 指标 ID',
 'plot 与公共字段映射',
 '实时 / 确认快照键',
 '指定执行配置'],code='service/pine_strategies.json\nPineModelRegistry / pine_models.js')
box(60,1010,540,300,'执行配置',[
 'single_entry：单次建仓',
 'ema9_legacy_pyramid：',
 '保留原金字塔、保本及时段规则',
 '新策略不自动继承旧规则'],code='execution_profile',kind='core')
box(60,1430,540,340,'回测与夜盘模拟验证',[
 'K线缓存 + 本地计算结果',
 '→ 独立的同类执行引擎',
 '→ SimBroker 模拟成交',
 '→ 交易记录与回测结果',
 '夜盘：独立K线 / PineTS / 模拟通道'],code='BacktestSession / NightTestService\nSimBroker / PinePaperFeed')
box(60,1870,540,300,'执行实例与交易归属',[
 'strategy_id：策略模型',
 'strategy_instance_id：执行实例',
 'symbol：标的',
 'group_id：一轮交易组',
 '状态隔离，账户风险统一汇总'])

box(2000,450,540,290,'交易运维控制台',[
 '模型 / 实例 / 执行配置选择',
 '启动、暂停及交易操作',
 '订单、风险、恢复状态展示',
 'HTTP / WebSocket 交互'],code='console/routes + services\nconsole/static')
box(2000,980,540,340,'状态持久化与恢复模块',[
 'SQLite 事务保存订单与交易组',
 '版本校验、迁移与 JSON 导出',
 '持久化受理意图，按引用认回',
 '未知结果暂停新入场并告警',
 '重启对账与保护数量恢复'],code='execution/state_store.py\nOrderManager / writer_lock.py')
box(2000,1480,540,300,'风险控制模块',[
 '计划风险 → 下单数量',
 '账户风险预算与交易约束',
 '按实际止损距离控制风险',
 '多个执行实例统一汇总'],code='RiskGate / order_calc\nOrderManager 中的风险检查')
box(2000,1870,540,300,'应用运行时',[
 '创建应用与组装依赖',
 '管理后台任务和生命周期',
 '启动 / 清理资源',
 '注入隔离状态路径'],code='factory / bootstrap / tasks')

for y in [390,610,840,1070,1360,1570,1800,2000]:
    edge([(1300,y),(1300,y+60)])
edge([(600,550),(690,550)],'字段规范',608,530,dash=True)
edge([(600,710),(645,710),(645,750),(690,750)],dash=True)
edge([(600,1160),(690,1160)],dash=True)
edge([(2000,600),(1950,600),(1950,980),(1910,980)],dash=True)
edge([(1910,1020),(1970,1020),(1970,700),(2000,700)],feedback=True,dash=True)
edge([(1910,1210),(2000,1210)],dash=True)
edge([(1910,1720),(1950,1720),(1950,1270),(2000,1270)],dash=True)
edge([(1910,1690),(2000,1690)],'风控检查',1918,1667,dash=True)
edge([(2000,1530),(1950,1530),(1950,1320),(1910,1320)],dash=True)
edge([(2000,1950),(1970,1950),(1970,790),(1910,790)],dash=True)
edge([(690,1250),(630,1250),(630,1520),(600,1520)],'同类引擎',480,1378,dash=True)
edge([(690,2140),(655,2140),(655,1750),(690,1750)],feedback=True)
text(95,2230,'实线箭头：主数据 / 订单流      青色实线：券商回报      虚线：配置、控制、支撑或复用关系','sub')
text(95,2290,'阅读要点：策略模型决定“是否交易、方向和价格”；执行模型决定“如何安全、可恢复地完成交易”。','body')
text(95,2340,'范围说明：本图为当前 Pine 主执行链路的逻辑架构；职责模块不等于独立进程或微服务。','code')
text(95,2380,'回测限制：OHLC 无法还原真实根内成交顺序；图中未表示第二个真实策略已注册。','code')
text(95,2420,'兼容说明：既有 API、模型标识与持久化字段保留；旧云指标入口已退役，跟单及研究辅助流程未展开。','code')
parts.append('</svg>')
(OUT/'system-architecture.svg').write_text('\n'.join(parts),encoding='utf-8')
print(OUT/'system-architecture.svg')
