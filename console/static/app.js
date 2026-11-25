/* autotrade 交易控制台前端逻辑（Vue3 + Element Plus，两宫格） */
const { createApp } = Vue;
const TV_EXTENSION_PRESENCE = 'chrome-extension://ehppgccaimaoccmhnncamnkpcknfmhge/presence.json';
const TV_EXTENSION_DOWNLOAD = '/api/tv/extension.zip';
const openingVwapClock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

// 一次性提醒去重的持久化（存 localStorage）：刷新页面后保本/自动策略事件/
// 自动平仓/未快速突破等「只播一次」的提醒不再重播
function loadNotified(key) {
  try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { return {}; }
}
function saveNotified(key, map) {
  try { localStorage.setItem(key, JSON.stringify(map)); } catch (e) { /* 忽略 */ }
}

// 以损定量：理论股数 = 单笔资金 ÷ |限价−止损|，再按「最大仓位比例」封顶
function calcPosition(equity, riskUsd, limitPx, stopPx, maxPositionPct, maxOrderQty) {
  if (!riskUsd || riskUsd <= 0 || limitPx <= 0 || stopPx <= 0) {
    return { qty: 0, amount: 0, risk: 0 };
  }
  const dist = Math.abs(limitPx - stopPx);
  if (dist <= 0) return { qty: 0, amount: 0, risk: 0 };
  let qty = Math.floor(riskUsd / dist);   // 理论仓位（按单笔资金）
  const maxQty = (equity > 0 && maxPositionPct > 0)
    ? Math.floor(equity * maxPositionPct / 100 / limitPx)
    : qty;                                  // 仓位封顶（金额 ≤ 净值 × 比例）
  qty = Math.min(qty, maxQty);
  if (maxOrderQty > 0) qty = Math.min(qty, maxOrderQty);
  return {
    qty,
    amount: +(qty * limitPx).toFixed(2),
    risk: +(dist * qty).toFixed(2),        // 实际风险 = 股数 × 止损距离
  };
}

createApp({
  components: { ...(typeof VwapDistribution === "undefined" ? {} : { VwapDistribution }),
    ...(typeof MarketRhythm === "undefined" ? {} : { MarketRhythm }),
    ...(typeof ResearchReviewPanel === "undefined" ? {} : { ResearchReviewPanel }) },
  directives: {
    rangeWidth: {
      mounted(element, binding) {
        const observer = new ResizeObserver(([entry]) => binding.value(entry.contentRect.width, entry.contentRect.height));
        observer.observe(element);
        element.rangeWidthObserver = observer;
      },
      unmounted(element) {
        element.rangeWidthObserver.disconnect();
      },
    },
  },
  data() {
    return {
      status: { dry_run: true, connected: false, halted: false, equity: null, daily_pnl: null, market_age: null, market_transport_age: null, market_bar_age: null, market_stale: false, market_stale_reason: '', market_running: true, allow_short: true, total_risk_usd: 0, max_total_risk_usd: 0, total_position_pct: null },
      market: {},
      vwapDistributionFrame: null,
      vwapDistributionProgress: null,
      vwapDistributionSourceFrame: null,
      vwapDisplayWait: null,
      openingVwapSignals: [],
      openingVwapBusy: {},
      openingVwapSelected: '',
      lots: [],
      lotsReady: false,
      orderSymbolFilter: '',
      orderKindFilter: 'all',
      maxPositionPct: 140, // 单笔定量及单只股票合计上限；快照同步服务端配置
      atrMult: 1.0,        // 全局止损 ATR 倍数（快照 status.stop_atr_mult 为准，全客户端同步）
      atrMultDirty: false, // 止损倍数输入框编辑中标记（编辑时不被快照覆盖）
      pyramidAutoBreakeven: true,
      pyramidBreakevenR: 3.0,
      pyramidAddUnlockR: 2.0,
      pyramidParamsDirty: false,
      screenOpeningRangeFilter: false,
      screenPmFilter: false,  // 选股盘前区间过滤开关（快照 status.screen_premarket_filter 同步）
      fvgPmFilter: true,     // FVG 监控盘前区间过滤开关（快照 status.fvg_premarket_filter 同步）
      imessageRecipient: '',  // iMessage 告警收件人（快照 status.imessage_recipient 同步，空=跳过该通道）
      imessageDirty: false,   // 收件人输入框编辑中标记（编辑时不被快照覆盖）
      alerts: [],             // 活跃告警（后端 Notifier，快照 msg.alerts）
      seenAlertKeys: {},      // 已通报过的告警 key -> 告警对象（声音/弹窗去重 + 解除播报）
      dismissedAlerts: {},    // 已手动消除的告警 key（本地，服务端解除后自动复位）
      maxDelay: 0,    // K线收盘→前端收到 的最大延时（秒）
      now: Date.now() / 1000,   // 前端实时时钟（每秒更新，驱动时钟走秒）
      selectRefs: {},           // el-select 实例引用（读内部 query）
      logs: [],                 // 运行日志（后端环形缓冲区）
      bottomTab: 'rhythm',
      rhythmFrame: null,
      holdingRankState: null,
      holdingRankNotice: null,
      entryCandidates: {ready:false,reason:'等待候选榜单',rows:[]},
      entryCandidateViews: {},
      confirmedVwap: {version:0,rows:{}},
      confirmedTd: {version:0,rows:{}},
      tvGridStatus: '等待TV双窗口席位齐备',
      tvGridLastKey: '',
      tvGridLayoutKeys: {},
      tvGridFailedKey: '',
      tvGridRetryAt: 0,
      tvGridRetries: 0,
      tvGridInFlight: false,
      tvGridPending: null,
      tvEntryMarkers: [],
      tvMarkersAttemptKey: '',
      tvMarkersRetryAt: 0,
      tvMarkersRetries: 0,
      tvMarkersBusy: false,
      tvMarkerStatus: '等待实盘成交',
      ipoOpenings: {watches:[],events:[],error:''},
      notifiedIpoOpenings: Object.assign({},loadNotified('notified_ipo_openings')),
      workspaceGroup: 'execution',
      workspaceExpanded: true,
      workspaceLayouts: {},
      settingsVisible: false,
      globalControlsVisible: false,
      executionParamsOpen: false,
      workspaceTabs: {
        execution: ['rhythm', 'vwap1m', 'openingBreakouts', 'instances', 'fvg', 'screen', 'momentum'], validation: ['backtest', 'night'],
        research: ['reports', 'review', 'news', 'shadow', 'returns', 'perf'],
        operations: ['log', 'events', 'copy'],
      },
      workspaceNames: { execution: '执行监控', validation: '回测验证', research: '研究复盘', operations: '系统运维' },
      workspaceTabNames: { instances: '执行监控', orders: '订单批次', fvg: 'FVG 监控', backtest: '回测验证', night: '夜盘模拟',
        screen: '选股', momentum: '推进力', review: '复盘研究', reports: '盘前报告', news: '盘前新闻', shadow: '影子', returns: '收益对比', perf: '绩效',
        openingBreakouts: '区间分布图', vwap1m: 'VWAP 分布图', rhythm: '候选与订单', log: '运行日志', events: '事件', copy: '跟单管理' },
      bottomPanelHeight: (() => {
        try { const v = parseInt(localStorage.getItem('bottom_panel_height'), 10); return v >= 180 && v <= 800 ? v : 420; } catch (e) { return 420; }
      })(),
      panelResizing: false,
      momentum: { long: [], short: [], ready_count: 0 },
      momentumBreakoutsOnly: false,
      screen: { long: [], short: [] },  // 横截面选股（多头/空头两列）
      screenSort: 'divergence',   // divergence=EMA发散度 / duration=ST持续 / push=ST推进
      performance: null,          // 复盘绩效统计
      returnsRange: (() => {
        const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        const end = new Date(), start = new Date();
        start.setDate(end.getDate() - 29);
        return [fmt(start), fmt(end)];
      })(),
      returnsData: null,
      returnsLoading: false,
      returnsError: '',
      returnsRequestId: 0,
      scanAlerts: [],             // 持仓扫描提醒
      stopAlerts: [],             // 止损单守卫提醒（持仓缺少 STP/TRAIL 保护单）
      externalAlerts: [],         // 外部订单/持仓告警（IBKR 手动单，告警模式）
      autoCloseNotices: [],       // 自动平仓记录（语音/通知）
      autoNotices: [],            // 金字塔建仓/加仓/平仓事件（语音/通知）
      prioritySpeechCount: 0,
      autoModel: 'ema9_pyramid',
      autoInstanceId: 'default',
      autoModels: [{ id: 'ema9_pyramid', name: 'EMA9-ST-ATR', execution_profile: 'ema9_legacy_pyramid' }],
      fvgReverseAlerts: [],       // 反向 FVG 守卫提醒（锁利/平仓失败）
      fvgReverseNotices: [],      // 反向 FVG 自动平仓记录（语音/通知）
      lastScanAlert: {},          // 已提醒过的持仓扫描（按 scanned_at 去重）
      lastBreakeven: loadNotified('notified_breakeven'),   // 已播报过的保本（持久化，刷新不重播）
      lastTimedExit: loadNotified('notified_candidate_timed_exit'),
      slowBreakAlerts: [],        // 未快速突破提醒（开仓超时未保本）
      lastSlowBreak: loadNotified('notified_slowbreak'),   // 已播报过的未快速突破（持久化）
      lastStopAlert: {},          // 已提醒过的止损单缺失（按 checked_at 去重）
      lastExternalAlert: {},      // 已提醒过的外部订单/持仓（kind:ticker 去重，60s 节流）
      lastExternalEntryResult: loadNotified('notified_external_entry_results'),
      lastAutoCloseNotice: loadNotified('notified_autoclose'),  // 已播报过的自动平仓（持久化）
      lastAutoNotice: loadNotified('notified_pyramid'),     // 已播报过的金字塔事件（持久化）
      autoNoticesInitDone: false, // 首屏只建基线，不补播服务端历史事件
      lastFvgReverseAlert: {},          // 已提醒过的反向 FVG（lot_id+alerted_at 去重）
      lastFvgReverseNotice: loadNotified('notified_fvg_reverse'),  // 已播报过的反向FVG平仓（持久化）
      events: [],                 // 历史事件（持久日志回溯）
      eventFilter: 'all',         // 事件类型过滤
      shadow: { stats: {}, events: [] },   // 当日影子回测（统计 + 回踩事件）
      strategyShadowReport: null,
      strategyShadowDay: '',
      strategyShadowLoading: false,
      strategyShadowError: '',
      shadowTypeFilter: 'all',             // 影子 tab 信号类型筛选（all/pullback/proposal/fvg_event/fvg_retest/fvg_warn/breakout）
      reportDay: '',                       // 日报日期（缺省=当天）
      report: null,                        // 每日复盘报告（/api/shadow/report）
      reportLoading: false,
      precheck: null,                      // 盘前检查单（/api/precheck 结果或最近一次落盘）
      precheckVisible: false,              // 盘前检查结果面板
      precheckLoading: false,
      fvgMonitor: { slots: [], retests: [] },   // FVG 监控快照（bar 槽位 + 多空事件 + 首次回踩事件）
      fvgWindow: (() => {          // FVG 面板时间窗口（分钟）：10 / 15 / 30，localStorage 持久化
        try { const v = parseInt(localStorage.getItem('fvg_window'), 10); return [10, 15, 30].includes(v) ? v : 15; } catch (e) { return 15; }
      })(),
      // 五分钟滴答提醒音开关（默认开，localStorage 持久化）
      fvgTickOn: (() => { try { return localStorage.getItem('fvg_tick_on') !== '0'; } catch (e) { return true; } })(),
      // FVG 通报 ATR 倍数过滤：低于该倍数的事件不弹窗不语音（默认 1.0，localStorage 持久化）
      fvgNotifyMinRatio: (() => {
        try { const v = parseFloat(localStorage.getItem('fvg_notify_min_ratio')); return isFinite(v) && v >= 0 ? v : 1.0; } catch (e) { return 1.0; }
      })(),
      fvgSeenIds: {},              // 已通报过的 FVG 事件（按 id 去重）
      fvgWarnSeenIds: {},          // 已通报过的 FVG 预警（形成中，按 id 去重）
      fvgWarnInitDone: false,      // 预警首屏基线标记
      fvgFeed: [],                 // FVG 信号栏（右侧常驻，ATR 倍数降序，逐条删除；含突破卡片）
      fvgFeedCollapsed: false,     // 信号栏收起状态
      news: null,                  // 盘前新闻摘要（/api/news，30s 轮询 + 手动刷新）
      newsHot: {},                 // 快照 status.news.hot：ticker → gist（「新闻」徽标联动）
      liveNewsTickers: [],          // 快照 status.news.live_tickers：近 15 分钟有新浪快讯的标的
      liveNewsDialog: { visible: false, ticker: '', items: [], loading: false,
                        error: '', updatedAt: null, requestId: 0 },
      newsBusy: false,             // 手动刷新中（10 分钟冷却由后端管）
      reportList: [],              // 盘前报告历史列表（/api/reports，日期倒序）
      reportDate: '',              // 当前查看的报告日期（YYYYMMDD）
      reportMd: '',                // 当前报告 markdown 全文
      reportStatus: { running: false, started_at: 0, last_result: null, last_error: null },
      reportWasRunning: false,     // 生成中→完成 跳变检测（完成即刷新列表+提示）
      srcInput: '',                // 视频源输入框（YouTube 链接）
      srcList: [],                 // 视频源列表（/api/report/sources）
      tvSwitchRequest: 0,
      tvExtensionState: 'checking',
      tvExtensionReason: '',
      tvExtensionChecking: false,
      tvSetupVisible: false,
      tvHostBindingVisible: false,
      tvHostCharts: [],
      tvHostTarget: '',
      tvHostBindingBusy: false,
      tvSetupPrompted: false,
      tvChromeAddressCopied: false,
      tvExtensionDownload: TV_EXTENSION_DOWNLOAD,
      // 悬浮交易台折叠状态（localStorage 持久化，小屏省空间）
      deckCollapsed: (() => { try { return localStorage.getItem('deck_collapsed') === '1'; } catch (e) { return false; } })(),
      fvgFreshIds: {},             // 会话内新到事件 id -> 到达时间（新鲜行高亮用）
      fvgInitDone: false,          // 首屏基线标记（首次快照不补报历史事件）
      lastFvgDataTime: null,       // 上一次数据时钟（bar 收盘滴答用，首次仅建基线）
      breakoutSignals: [],         // 盘前突破信号（后端快照，今日已确认）
      openingBreakouts: [],
      goldenSubmitted: {},
      goldenFeedHistory: {},
      rangeSelectedSymbol: '',
      rangeFocus: null,
      rangeActionBusy: false,
      rangePlotWidth: 0,
      rangePlotHeight: 560,
      openingBreakoutCoverage: null,
      openingBreakoutsInitialized: false,
      openingFeedDrag: null,
      openingFeedCollapsed: (() => { try { return localStorage.getItem('opening_feed_collapsed') === '1'; } catch (_) { return false; } })(),
      openingFeedPreferenceSet: (() => { try { return localStorage.getItem('opening_feed_collapsed') != null; } catch (_) { return false; } })(),
      breakoutSeenIds: {},         // 已通报过的突破信号（按 id 去重）
      breakoutInitDone: false,     // 突破信号首屏基线标记
      auto: { enabled: false, strategy: '', instances: [], proposals: [] },
      expandedAutoOrders: {},
      autoBreakevenBusy: {},
      demoMode: (() => { try { return localStorage.getItem('demo_mode') === '1'; } catch (e) { return false; } })(),
      demoAutoInstances: [
        { inst_id: 'demo-nvda', enabled: true, last_order_budget: 200, last_order_risk: 198.9, last_order_qty: 90, last_order_stop: 208.22, symbol: 'NASDAQ:NVDA', strategy: 'ema9_pyramid', state: 'ACTIVE', side: 'short', source: 'manual_seed', entries: 2, avg_cost: 207.0931, entry_px: 207.0931, qty: 442, stop_px: 207.09, initial_risk_usd: 300, initial_r: 0.85, unlocked: true, breakeven_done: true, unrealized_r: 1.662, mfe_r: 3.2, second_mfe_r: 0.5, max_notional: 137820.69, max_position_pct: 63.28, lot_ids: ['demo-nvda-1', 'demo-nvda-2'] },
        { inst_id: 'demo-qcom', symbol: 'NASDAQ:QCOM', strategy: 'ema9_pyramid', state: 'ACTIVE', side: 'short', source: 'auto', entries: 3, avg_cost: 166.61, entry_px: 166.61, qty: 494, stop_px: 166.61, initial_risk_usd: 300, initial_r: 1.37, unlocked: true, breakeven_done: true, unrealized_r: 2.35, mfe_r: 3.55, second_mfe_r: 2.10, max_notional: 81346.98, max_position_pct: 37.35, lot_ids: ['demo-qcom-1', 'demo-qcom-2', 'demo-qcom-3'] },
        { inst_id: 'demo-amd', enabled: false, restored: true, recovery_block_reason: '缺少完整整组快照，自动加仓已暂停', symbol: 'NASDAQ:AMD', strategy: 'ema9_pyramid', state: 'ACTIVE', side: 'long', source: 'manual_seed', entries: 1, avg_cost: 513.07, entry_px: 513.07, qty: 72, stop_px: 508.92, initial_risk_usd: 300, initial_r: 4.15, unlocked: false, breakeven_done: false, unrealized_r: -0.22, mfe_r: 1.55, second_mfe_r: 0, max_notional: 37244.52, max_position_pct: 17.10, lot_ids: ['demo-amd-1'] },
      ],
      demoOrderLots: [
        { lot_id: 'demo-nvda-1', symbol: 'NASDAQ:NVDA', side: 'short', qty: 352, filled_qty: 352, state: 'ACTIVE', limit_type: 'market', trader: 'master', entry_px: 207.37, avg_fill: 207.37, stop_px: 207.09, last_px: 205.68, realized_pnl: 0, created_at: 1793368200, updated_at: 1793375400, reason: '手动首仓', signal_snapshot: { pyramid_seed: true } },
        { lot_id: 'demo-nvda-2', symbol: 'NASDAQ:NVDA', side: 'short', qty: 90, filled_qty: 90, state: 'ACTIVE', limit_type: 'auto_ema9_pyramid', trader: 'auto', entry_px: 206.01, avg_fill: 206.01, stop_px: 207.09, last_px: 205.68, realized_pnl: 0, created_at: 1793375400, updated_at: 1793376600, reason: 'EMA9金字塔 第2次', signal_snapshot: { entry_index: 2, risk_budget: 200, planned_risk_usd: 198.9 } },
        { lot_id: 'demo-qcom-1', symbol: 'NASDAQ:QCOM', side: 'short', qty: 218, filled_qty: 218, state: 'ACTIVE', limit_type: 'auto_ema9_pyramid', trader: 'auto', entry_px: 168.52, avg_fill: 168.52, stop_px: 166.61, last_px: 163.86, realized_pnl: 0, created_at: 1793367000, updated_at: 1793370000, reason: 'EMA9金字塔 第1次', signal_snapshot: { entry_index: 1 } },
        { lot_id: 'demo-qcom-2', symbol: 'NASDAQ:QCOM', side: 'short', qty: 172, filled_qty: 172, state: 'ACTIVE', limit_type: 'auto_ema9_pyramid', trader: 'auto', entry_px: 165.36, avg_fill: 165.36, stop_px: 166.61, last_px: 163.86, realized_pnl: 0, created_at: 1793370000, updated_at: 1793372000, reason: 'EMA9金字塔 第2次', signal_snapshot: { entry_index: 2, risk_budget: 200, planned_risk_usd: 198.9 } },
        { lot_id: 'demo-qcom-3', symbol: 'NASDAQ:QCOM', side: 'short', qty: 104, filled_qty: 104, state: 'ACTIVE', limit_type: 'auto_ema9_pyramid', trader: 'auto', entry_px: 164.67, avg_fill: 164.67, stop_px: 166.61, last_px: 163.86, realized_pnl: 0, created_at: 1793372000, updated_at: 1793373000, reason: 'EMA9金字塔 第3次', signal_snapshot: { entry_index: 3 } },
        { lot_id: 'demo-amd-1', symbol: 'NASDAQ:AMD', side: 'long', qty: 72, filled_qty: 72, state: 'ACTIVE', limit_type: 'market', trader: 'master', entry_px: 513.07, avg_fill: 513.07, stop_px: 508.92, last_px: 512.15, realized_pnl: 0, created_at: 1793367000, updated_at: 1793368000, reason: '手动首仓', signal_snapshot: { pyramid_seed: true } },
      ],
      autoSymbols: '',            // 自动策略启动表单：标的（空=全池）
      autoBusy: false,
      autoProposalBusy: {},       // proposal_id -> 是否正在提交决策
      lastAutoEntryProposals: {}, // 已提醒过的开仓候选（按 proposal_id 去重）
      wbSymbol: '',               // 回测工作台：标的
      wbDay: '',                  // 回测工作台：交易日
      wbStartTime: '09:45',       // 人工观察后启动执行实例的时间（ET）
      wbModel: 'ema9_pyramid',
      wbRiskUsd: 300,
      wbEquity: 100000,
      wbAutoBreakeven: true,
      wbBreakevenR: 3,
      wbAddUnlockR: 2,
      wbLoading: false,
      wbError: '',
      wb: null,                   // 回测会话状态（进度/实例/订单生命周期）
      wbExpected: '',
      wbReview: '',
      wbExpectationSnapshot: '',
      night: {}, nightBusy: false,
      nightForm: { symbol: 'NASDAQ:NVDA', strategy: 'ema9_pyramid', session: '24h', risk_budget: 100, window_start: '20:00', window_end: '04:00' },
      wbChartSelection: 0,
      wbPrevious: null,
      decisionCollapsed: {},       // 连续同原因忽略组：默认折叠，点击展开
      btSymbol: '',               // 历史回测表单：标的
      btDay: '',                  // 历史回测表单：交易日（默认昨日）
      btLoading: false,
      btResult: null,
      btError: '',
      slots: [
        { symbol: '', limitType: 'rhythm_market', orderType:'MKT', limitLevel:'vwap', candidateRanking:'trend',candidateView:'mixed', stopMode:'atr_2', manualDirection: null, directionConfirming: false,
          vwapOutsideOnly:true },
      ],
      candidateProtection:{version:1,timed_breakeven_enabled:true,breakeven_minutes:5,trailing_enabled:false,trailing_source:'ema9_5m'},
      candidateProtectionSaving:false,
      intelligentExit:{version:1,enabled:false,breakeven_minutes:5},
      intelligentExitSaving:false,
      placing: false,
      ws: null,
      systemClosing: false,
      systemClosed: false,
      systemCloseError: '',
      // ---- 局域网多客户端身份 ----
      identity: 'init',       // init / register（需登记）/ pending（待确认）/ ok（master 或已审批 trader）
      identityNotice: '',     // 登记页提示（如 token 过期原因）
      traderName: '',         // 已确认身份名：master 或交易员名（表格高亮、排序用）
      traderToken: (() => { try { return localStorage.getItem('trader_token') || ''; } catch (e) { return ''; } })(),
      traderInputName: (() => { try { return localStorage.getItem('trader_name') || ''; } catch (e) { return ''; } })(),  // 登记输入框（默认回填已存名称）
      registerBusy: false,    // 登记提交中
      clients: { pending: [], approved: [] },   // WS 快照里的客户端列表（master 审批条用）
      // ---- 跟单管理 ----
      copy: null,               // 跟单快照（msg.copy；无该段时保持 null → tab 显示「后端未就绪」）
      copyDraft: {},            // 服务器配置编辑草稿（name -> 可编辑字段副本，独立于每秒快照避免被覆盖）
      copyDialog: false,        // 添加服务器弹窗
      copyBusy: false,
      copyForm: { name: '', host: '127.0.0.1', port: 7497, fa_group: '', size_mode: 'risk', risk_usd: 300, size_multiplier: 1.0 },
    };
  },

  computed: {
    showOpeningVwapCandidates() {
      const et = Object.fromEntries(openingVwapClock.formatToParts(this.now * 1000)
        .map(({ type, value }) => [type, value]));
      const minute = Number(et.hour) * 60 + Number(et.minute);
      return et.weekday !== 'Sat' && et.weekday !== 'Sun' && minute >= 570 && minute < 585;
    },
    vwapPullbackCandidates() {
      const active = new Set((this.auto?.instances || [])
        .filter(row => row.strategy === 'vwap_outer_pullback' && row.state !== 'DONE')
        .map(row => row.symbol));
      return (this.vwapDistributionFrame?.rows || [])
        .filter(row => Number.isFinite(row.position) && Math.abs(row.position) > 1 && row.st_direction === Math.sign(row.position) &&
          Number.isFinite(row.speed))
        .map(row => {
          const direction = Math.sign(row.position);
          const edge = direction === 1 ? row.upper : row.lower;
          const stop = row.st_price;
          const ready = Number.isFinite(edge) && Number.isFinite(stop) && stop > 0 &&
            (edge - stop) * direction > 0;
          return { ...row, direction, edge, stop, ready, active: active.has(row.symbol) };
        })
        .sort((a, b) => b.position - a.position || a.symbol.localeCompare(b.symbol));
    },
    openingVwapChart() {
      const row = this.openingVwapSignals.find(signal => signal.id === this.openingVwapSelected);
      if (!row || row.candles?.length !== 3) return null;
      const upper = Math.max(row.vwap_band, ...row.candles.map(bar => bar.high));
      const lower = Math.min(row.vwap_band, ...row.candles.map(bar => bar.low));
      const y = price => 10 + (upper - price) / Math.max(upper - lower, 0.01) * 74;
      return {
        row, bandY: y(row.vwap_band), stopY: y(row.stop_px),
        bars: row.candles.map((bar, index) => ({
          time: bar.time, x: 45 + index * 73,
          high: y(bar.high), low: y(bar.low), top: y(Math.max(bar.open, bar.close)),
          height: Math.max(2, Math.abs(y(bar.open) - y(bar.close))),
          color: bar.close > bar.open ? '#45be8a' : '#f57384',
        })),
      };
    },
    rangeDistribution() {
      const symbols = new Set([...this.watchlist, ...Object.keys(this.status.market_session?.symbols || {})]);
      return RangeDistribution.build(this.market, symbols, this.rangePlotWidth, this.rangePlotHeight, this.rangeFocus);
    },
    rangeSelectedRow() {
      return this.rangeDistribution.rows.find(row => row.symbol === this.rangeSelectedSymbol);
    },
    rangeReferenceLines() {
      return RangeDistribution.referenceLines(this.rangeSelectedRow, this.rangeDistribution);
    },
    rangeReadyTargets() {
      return this.rangeDistribution.rows.filter(row => this.rangeTargetReady(row))
        .sort((a, b) => b.distance - a.distance || Math.abs(b.speed) - Math.abs(a.speed));
    },
    rangeGoldenSignal() {
      return this.goldenSignalRows.find(row => row.symbol === this.rangeSelectedSymbol);
    },
    rangeTargetBlock() {
      if (this.demoMode) return '演示模式';
      if (!this.rangeSelectedRow) return '点击闪烁目标锁定股票';
      if (!this.rangeTargetReady(this.rangeSelectedRow)) return '目标未就绪或行情已过期';
      if (this.status.halted) return '账户风控已暂停';
      if (!this.status.dry_run && !this.status.connected) return 'IBKR 未连接';
      return '';
    },
    rangeGoldenBlock() {
      if (!this.rangeSelectedSymbol) return '点击图中的股票选择目标';
      return this.goldenEntryBlock(this.rangeGoldenSignal);
    },
    rangeAutoBlock() {
      if (this.rangeTargetBlock) return this.rangeTargetBlock;
      if (!this.autoModels.some(model => model.id === 'ema7_autotrade_v2')) return 'EMA7 v2 模型未加载';
      const row = this.rangeSelectedRow;
      const model = this.market[row.symbol]?.e7v2;
      if (model?.time !== row.time || !(model.entry_price > 0)) return 'EMA7 v2 实时指标未就绪';
      if (model.direction !== (row.minor === 0 ? 1 : -1)) return 'EMA7 v2 方向与突破方向不一致';
      return '';
    },
    goldenSignalRows() {
      const rows = [];
      for (const [symbol, data] of Object.entries(this.market)) {
        const time = data.bar?.time;
        if (!(time <= this.now && this.now < time + 300)) continue;
        const row = this.goldenFeedRow(symbol, data.golden, time);
        if (row) rows.push(row);
      }
      return rows.sort((a, b) => Number(b.touched) - Number(a.touched) || a.ticker.localeCompare(b.ticker));
    },
    goldenFeedRows() {
      const day = this.goldenFeedDay(this.now);
      const active = this.goldenSignalRows;
      const activeIds = new Set(active.map(row => row.id));
      const expired = Object.values(this.goldenFeedHistory).filter(row =>
        row.day === day && row.time + 300 <= this.now && !activeIds.has(row.id));
      return [...active, ...expired];
    },
    openingBreakoutGroups() {
      const groups = new Map();
      for (const row of this.openingBreakouts) {
        const barTime = this.openingBreakoutBarTime(row);
        if (!groups.has(barTime)) groups.set(barTime, []);
        groups.get(barTime).push(row);
      }
      return Array.from(groups, ([time, rows]) => ({
        time, rows: rows.sort((a, b) => (b.body_atr ?? -1) - (a.body_atr ?? -1)),
      }))
        .sort((a, b) => b.time - a.time);
    },
    entrySignalGroups() {
      const groups = new Map();
      for (const row of this.goldenFeedRows) {
        if (!groups.has(row.time)) groups.set(row.time, []);
        groups.get(row.time).push({ kind: 'golden', row });
      }
      for (const group of this.openingBreakoutGroups) {
        if (!groups.has(group.time)) groups.set(group.time, []);
        groups.get(group.time).push(...group.rows.map(row => ({ kind: 'opening', row })));
      }
      return Array.from(groups, ([time, rows]) => ({ time, rows })).sort((a, b) => b.time - a.time);
    },
    latestEntrySignalTime() {
      return this.entrySignalGroups[0]?.time ?? 0;
    },
    visibleMomentumRows() {
      if (!this.momentumBreakoutsOnly) return this.momentum;
      return {
        long: this.momentum.long.filter(row => row.breakout_distance > 0),
        short: this.momentum.short.filter(row => row.breakout_distance > 0),
      };
    },
    wbStartTimes() {
      return Array.from({ length: 24 * 12 }, (_, index) => {
        const hour = String(Math.floor(index / 12)).padStart(2, '0');
        const minute = String((index % 12) * 5).padStart(2, '0');
        return hour + ':' + minute;
      });
    },
    riskUsd() { return this.status.risk_per_trade_usd || 0; },
    manualSymbols() { const snapshot=this.candidateSnapshotFor('trend',this.slots[0]?.candidateView||'mixed'); return this.status.candidate_entry_required ? (snapshot?.rows||[]).map(r=>r.symbol) : this.watchlist; },
    nightChart() {
      const rows = (this.night.bars || []).slice(-90);
      const fields = new Map((this.night.history_outputs || []).map(row => [row.time, row]));
      const valid = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
      const prices = rows.flatMap(row => [row.high, row.low, fields.get(row.time)?.entry_price,
        fields.get(row.time)?.long_stop, fields.get(row.time)?.short_stop]).filter(valid);
      if (!rows.length || !prices.length) return { candles: [], paths: [], labels: [] };
      const low = Math.min(...prices), high = Math.max(...prices), span = Math.max(high - low, 0.1);
      const x = index => 30 + index * 920 / Math.max(rows.length - 1, 1);
      const y = price => 230 - (price - low) / span * 205;
      return {
        candles: rows.filter(row => [row.open, row.high, row.low, row.close].every(valid)).map(row => {
          const index = rows.indexOf(row), out = fields.get(row.time) || {};
          return { x: x(index), top: y(Math.max(row.open, row.close)), bottom: y(Math.min(row.open, row.close)),
            high: y(row.high), low: y(row.low), up: row.close >= row.open,
            title: `${this.nightTime(row.time)} ET  O ${row.open} H ${row.high} L ${row.low} C ${row.close} | 方向 ${out.direction ?? '—'} 信号 ${out.entry_signal == null ? '—' : Number(out.entry_signal)} 参考 ${out.entry_price ?? '—'} 多止损 ${out.long_stop ?? '—'} 空止损 ${out.short_stop ?? '—'}` };
        }),
        paths: [['entry_price', '#eab85c', '入场参考价'], ['long_stop', '#54b892', '多头止损'], ['short_stop', '#ea7474', '空头止损']].map(([field, color, label]) => {
          let gap = true;
          const d = rows.map((row, i) => { const value = fields.get(row.time)?.[field];
            if (!valid(value)) { gap = true; return ''; } const command = gap ? 'M' : 'L'; gap = false; return `${command}${x(i)},${y(value)}`; }).join(' ');
          return { d, color, label };
        }),
        labels: [0, Math.floor(rows.length / 2), rows.length - 1].map(i => ({ x: x(i), text: this.nightTime(rows[i].time) })),
      };
    },
    equity() { return this.status.equity || 0; },
    // 总风险敞口占预算比例：>100% 红色、>80% 琥珀色、预算 0（不限制）不着色
    totalRiskClass() {
      const budget = Number(this.status.max_total_risk_usd || 0);
      if (budget <= 0) return '';
      const ratio = Number(this.status.total_risk_usd || 0) / budget;
      return ratio > 1 ? 'risk-over' : (ratio > 0.8 ? 'risk-warn' : '');
    },
    watchlist() {
      return Object.keys(this.market).sort((a, b) =>
        a.split(':').pop().localeCompare(b.split(':').pop()));
    },
    logPanelStyle() {
      if (this.bottomTab === 'instances') return { display: 'none' };
      return { flex: '1 1 auto', minHeight: '240px' };
    },
    selectedAutoModel() { return this.autoModels.find(m => m.id === this.autoModel) || {}; },
    executionOverview() {
      const rows = (this.auto && this.auto.instances) || [];
      return {
        active: rows.filter(i => i.enabled && i.state !== 'DONE').length,
        blocked: rows.filter(i => i.recovery_block_reason || i.exit_requested_reason).length,
        paused: rows.filter(i => !i.enabled && i.state !== 'DONE').length,
      };
    },
    wbPriceChart() {
      const rows = (this.wb && this.wb.replay_bars) || [];
      if (!rows.length) return null;
      const prices = rows.map(r => r.close).filter(Number.isFinite);
      for (const row of rows) if (row.execution_stop > 0) prices.push(row.execution_stop);
      const orders = this.wb.orders || [];
      for (const order of orders) for (const key of ['filled', 'closed']) {
        if (order[key] && Number.isFinite(order[key].px)) prices.push(order[key].px);
      }
      if (!prices.length) return null;
      let low = Math.min(...prices), high = Math.max(...prices);
      const margin = Math.max((high - low) * 0.12, 0.01); low -= margin; high += margin;
      const x = index => 62 + index * 900 / Math.max(1, rows.length - 1);
      const y = price => 215 - (price - low) * 185 / (high - low);
      const point = (stamp, price) => {
        const index = rows.findIndex(r => r.time === stamp);
        return index < 0 ? null : { x: x(index), y: y(price), index };
      };
      const fills = [], exits = [];
      for (const order of orders) {
        const filled = order.filled && point(order.filled.t, order.filled.px);
        const closed = order.closed && point(order.closed.t, order.closed.px);
        if (filled) fills.push({ ...filled, lot_id: order.lot_id });
        if (closed) exits.push({ ...closed, lot_id: order.lot_id });
      }
      const selected = Math.min(this.wbChartSelection, rows.length - 1);
      let stopPath = '', penDown = false;
      rows.forEach((row, index) => {
        if (row.execution_stop > 0) { stopPath += `${penDown ? 'L' : 'M'}${x(index)},${y(row.execution_stop)} `; penDown = true; }
        else penDown = false;
      });
      return { path: rows.map((r, i) => `${i ? 'L' : 'M'}${x(i)},${y(r.close)}`).join(' '),
        stopPath,
        signals: rows.flatMap((r, i) => r.entry_signal ? [{ x: x(i), y: y(r.close), index: i }] : []),
        fills, exits, selected: { ...rows[selected], x: x(selected), y: y(rows[selected].close) },
        ticks: [low, (low + high) / 2, high].map(price => ({ price, y: y(price) })),
        first: rows[0].hm, last: rows[rows.length - 1].hm };
    },
    sortedScreenLong() { return this.sortScreenRows(this.screen.long); },
    sortedScreenShort() { return this.sortScreenRows(this.screen.short); },
    slotRows() {
      // 单格交易台（v2026-10-23 起一格，方向按钮按趋势多空都开）
      return this.slots.map((slot) => this.calcSlot(slot));
    },
    candidateLimitSides() {
      return this.slots.map(slot=>(this.entryCandidates.rows||[]).find(row=>row.symbol===slot.symbol)?.side||null);
    },
    manualTradeContexts() {
      return this.slots.map(slot => `${slot.symbol}|${slot.limitType}`);
    },
    tvPaperOrderNotices() {
      const notices = this.status?.tv_paper_sync?.notices ?? [];
      return notices.filter(notice => notice.message !== '上次 TV 操作结果待核对，暂停自动重发');
    },
    // 订单批次生命周期排序：挂单/持仓在前、已完结在后（按平仓时间倒序）；
    // 生效中组内再按「本客户端交易员优先 → 创建时间」排序（自己的批次一眼可见）
    sortedLots() {
      const rank = { PENDING: 0, CLOSING: 0, ACTIVE: 1 };
      const norm = (t) => t || 'master';   // 旧数据无 trader 视为 master
      const own = (l) => (this.traderName && norm(l.trader) === this.traderName) ? 0 : 1;
      const sourceLots = this.demoMode ? this.demoOrderLots : this.lots;
      const rows = sourceLots.filter(l => {
        if (this.orderSymbolFilter && !this.sameOrderSymbol(l.symbol,this.orderSymbolFilter)) return false;
        const pyramid = l.limit_type === 'auto_ema9_pyramid'
          || (l.signal_snapshot && l.signal_snapshot.pyramid_seed);
        if (this.orderKindFilter === 'pyramid' && !pyramid) return false;
        if (this.orderKindFilter === 'manual' && pyramid) return false;
        return true;
      });
      return rows.sort((a, b) => {
        const ra = rank[a.state] ?? 2, rb = rank[b.state] ?? 2;
        if (ra !== rb) return ra - rb;
        if (ra < 2) {
          const oa = own(a), ob = own(b);
          if (oa !== ob) return oa - ob;
          // 同侧（自己/他人）内按交易员聚块，块内按创建时间——配合表格的
          // 交易员间隔带，每个交易员的订单各自连续成块
          const ta = norm(a.trader), tb = norm(b.trader);
          if (ta !== tb) return ta < tb ? -1 : 1;
          return a.created_at - b.created_at;
        }
        return (b.closed_at || b.updated_at) - (a.closed_at || a.updated_at);
      });
    },
    // TV 时钟（秒级，TV 数据源在本地 daemon，服务器时间 ≈ 本地）
    tvTime() { return this.now; },
    // IBKR 时钟（秒级，用本地流逝补齐心跳 2s 更新间隔）
    ibkrTime() {
      const base = this.status.ibkr_time || 0;
      const server = this.status.server_time || 0;
      return base > 0 ? base + (this.now - server) : 0;
    },
    filteredEvents() {
      if (this.eventFilter === 'all') return this.events;
      if (this.eventFilter === 'auto_prefix') return this.events.filter(e => (e.type || '').startsWith('auto'));
      return this.events.filter(e => e.type === this.eventFilter);
    },
    // FVG 槽位按时间窗口过滤（后端固定给 60 分钟 12 槽，前端截取前 N 槽）
    fvgSlotsFiltered() {
      const n = Math.max(1, Math.round(this.fvgWindow / 5));
      return ((this.fvgMonitor && this.fvgMonitor.slots) || []).slice(0, n);
    },
    // FVG 首次回踩事件：与槽位窗口同口径（retest_time 落在窗口内，窗口分钟数 = fvgWindow），
    // 按回踩时间倒序（后端已排序，这里再稳一手）
    fvgRetestsFiltered() {
      const cutoff = this.now - this.fvgWindow * 60;
      return ((this.fvgMonitor && this.fvgMonitor.retests) || [])
        .filter(rt => rt.retest_time != null && rt.retest_time >= cutoff)
        .sort((a, b) => b.retest_time - a.retest_time);
    },
    // 盘前新闻：自选股热点卡片列表（ticker 字母序）
    hotList() {
      const s = this.news && this.news.summary;
      const hot = (s && s.hot_symbols) || {};
      return Object.keys(hot).sort().map(t => ({ ticker: t, ...hot[t] }));
    },
    // FVG 信号栏：突破卡片固定排最上（按到达时间新→旧），FVG 卡片 ATR 倍数降序
    // （最大在最上面优先处理），缺失排最后，并列按到达时间
    sortedFvgFeed() {
      return [...this.fvgFeed].sort((a, b) => {
        const ab = a.kind === 'breakout' ? 1 : 0, bb = b.kind === 'breakout' ? 1 : 0;
        if (ab !== bb) return bb - ab;
        if (ab) return b.ts - a.ts;
        return ((b.ratio ?? -1) - (a.ratio ?? -1)) || (b.ts - a.ts);
      });
    },
    // FVG 窗口内统计 chips：多头 / 空头 / ≥1×ATR 个数 / 回踩个数
    fvgStats() {
      let long = 0, short = 0, ge1 = 0;
      for (const s of this.fvgSlotsFiltered) {
        for (const ev of [...(s.long || []), ...(s.short || [])]) {
          if (ev.side === 'long') long++; else short++;
          if (ev.ratio != null && ev.ratio >= 1) ge1++;
        }
      }
      return { long, short, ge1, retest: this.fvgRetestsFiltered.length };
    },
    // 告警横幅：活跃且未手动消除的告警（critical 红 / warning 琥珀）
    visibleAlerts() {
      return this.alerts.filter(a => !this.dismissedAlerts[a.key]);
    },
    // 影子 tab：信号类型筛选 + 类型中文名 + 结局中文名
    filteredShadowEvents() {
      const evs = (this.shadow && this.shadow.events) || [];
      if (this.shadowTypeFilter === 'all') return evs;
      return evs.filter(t => t.signal_type === this.shadowTypeFilter);
    },
    decisionGroups() {
      const groups = [];
      for (const item of ((this.wb && this.wb.management) || [])) {
        const prev = groups[groups.length - 1];
        if (item.title === '忽略信号' && prev && prev.title === '忽略信号'
            && prev.reason === item.reason) {
          prev.items.push(item);
          prev.end = item.hm;
        } else {
          groups.push({ ...item, items: [item], start: item.hm, end: item.hm });
        }
      }
      return groups;
    },
    wbSelectedModel() {
      return this.autoModels.find(model => model.id === this.wbModel) || {};
    },
    autoView() {
      return this.demoMode
        ? { enabled: true, strategy: 'ema9_pyramid', instances: this.demoAutoInstances.map(i => ({
          ...i, execution_profile: 'ema9_legacy_pyramid', risk_budgets: [300, 200, 100],
          auto_breakeven: true, breakeven_r: 3, add_unlock_r: 2,
          start_direction: i.side === 'long' ? 1 : -1,
        })), proposals: [] }
        : this.auto;
    },
    orderedAutoInstances() {
      // Sort a copy: snapshot order and relative order within each tier stay intact.
      const priority = i => this.instanceHasPosition(i) ? 0 : this.instanceInactive(i) ? 2 : 1;
      return [...(this.autoView.instances || [])].sort((a, b) => priority(a) - priority(b));
    },
    inactiveAutoCount() { return this.orderedAutoInstances.filter(i => this.instanceInactive(i)).length; },
    allowShort() { return this.status.allow_short !== false; },
    // 各服务器跟踪订单聚合（附加服务器名；follower 侧无映射的 master 单后端不会下发，无需补）
    copyOrders() {
      const out = [];
      for (const s of ((this.copy && this.copy.servers) || [])) {
        for (const o of (s.orders || [])) out.push({ ...o, server: s.name });
      }
      return out;
    },
    // 各服务器失败记录聚合（过滤已忽略，按时间倒序）
    copyFailures() {
      const out = [];
      for (const s of ((this.copy && this.copy.servers) || [])) {
        for (const f of (s.failures || [])) {
          if (!f.dismissed) out.push({ ...f, server: s.name });
        }
      }
      return out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    },
    returnsChart() {
      const empty = { ticks: [], labels: [], accountPaths: [], qqqPaths: [], accountEnd: null, qqqEnd: null };
      const rows = (this.returnsData && this.returnsData.rows) || [];
      if (!rows.length) return empty;
      const values = rows.flatMap(r => [r.account_cumulative, r.qqq_cumulative])
        .filter(v => typeof v === 'number' && isFinite(v));
      if (!values.length) return empty;
      let min = Math.min(0, ...values), max = Math.max(0, ...values);
      const pad = Math.max((max - min) * 0.12, 0.005);
      min -= pad;
      max += pad;
      const x = i => rows.length === 1 ? 521 : 62 + i * 918 / (rows.length - 1);
      const y = v => 18 + (max - v) * 244 / (max - min);
      const paths = key => {
        const out = [];
        let d = '';
        rows.forEach((r, i) => {
          const v = r[key];
          if (typeof v !== 'number' || !isFinite(v)) {
            if (d) out.push(d);
            d = '';
            return;
          }
          d += `${d ? ' L' : 'M'} ${x(i).toFixed(2)} ${y(v).toFixed(2)}`;
        });
        if (d) out.push(d);
        return out;
      };
      const endPoint = key => {
        for (let i = rows.length - 1; i >= 0; i--) {
          const v = rows[i][key];
          if (typeof v === 'number' && isFinite(v)) return { x: x(i), y: y(v) };
        }
        return null;
      };
      const labelIndexes = new Set();
      const labelCount = Math.min(6, rows.length);
      for (let i = 0; i < labelCount; i++) {
        labelIndexes.add(Math.round(i * (rows.length - 1) / Math.max(1, labelCount - 1)));
      }
      return {
        ticks: Array.from({ length: 5 }, (_, i) => {
          const value = min + (max - min) * i / 4;
          return { value, y: y(value) };
        }),
        labels: [...labelIndexes].sort((a, b) => a - b).map(i => ({
          index: i, x: x(i), text: rows[i].date.slice(5),
        })),
        accountPaths: paths('account_cumulative'),
        qqqPaths: paths('qqq_cumulative'),
        accountEnd: endPoint('account_cumulative'),
        qqqEnd: endPoint('qqq_cumulative'),
      };
    },
  },

  // 参数持久化：单笔资金/最大仓位/FVG 时间窗口改动即写 localStorage（各浏览器独立）
  watch: {
    candidateLimitSides: {
      flush:'sync',
      handler(sides) { sides.forEach((side,i)=>{
        if(['long','short'].includes(side)&&['upper','lower'].includes(this.slots[i].limitLevel))
          this.slots[i].limitLevel=side==='long'?'upper':'lower';
      }); },
    },
    manualTradeContexts: {
      flush: 'sync',
      handler(contexts, previous = []) {
        contexts.forEach((context, i) => {
          if (context !== previous[i]) {
            this.slots[i].manualDirection = null;
            this.slots[i].limitLevel='vwap';
            this.slots[i].directionRevision = (this.slots[i].directionRevision || 0) + 1;
          }
        });
      },
    },
    latestEntrySignalTime(time, previousTime) {
      if (time <= previousTime) return;
      this.openingFeedDrag = null;
      this.$nextTick(this.scrollOpeningFeedToLatest);
    },
    goldenSignalRows(rows, previous) {
      if (this.demoMode || this.status.market_stale) return;
      this.rememberGoldenFeedRows(rows);
      const seen = new Map(previous.map(row => [row.id, row.touched]));
      const fresh = rows.filter(row => !seen.has(row.id) || (row.touched && !seen.get(row.id)));
      if (!fresh.length) return;
      if (fresh.some(row => row.time === this.latestEntrySignalTime && !seen.has(row.id))) {
        this.openingFeedDrag = null;
        this.$nextTick(this.scrollOpeningFeedToLatest);
      }
      const summary = fresh.slice(0, 3).map(row =>
        row.ticker + (row.side === 'long' ? ' 多' : ' 空') + (row.touched ? '已触线' : '待回撤'));
      this.$message.info('gold signal · ' + summary.join('、') + (fresh.length > 3 ? ' 等 ' + fresh.length + ' 条' : ''));
    },
    fvgWindow(v) { try { localStorage.setItem('fvg_window', String(v)); } catch (e) { /* 忽略 */ } },
    deckCollapsed(v) { try { localStorage.setItem('deck_collapsed', v ? '1' : '0'); } catch (e) { /* 忽略 */ } },
    demoMode(v) { try { localStorage.setItem('demo_mode', v ? '1' : '0'); } catch (e) { /* 忽略 */ } },
  },

  mounted() {
    try {
      const saved = JSON.parse(localStorage.getItem('workspace_layouts') || '{}');
      this.workspaceLayouts = saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
    } catch (_) { this.workspaceLayouts = {}; }
    this.fetchAutoModels();
    this.fetchNightStatus();
    setInterval(() => { if (this.bottomTab === 'night') this.fetchNightStatus(); }, 2000);
    // 先确认本客户端身份（master 以服务端判定为准，不在前端猜 IP），未确认前每 5s 轮询
    this.checkIdentity();
    setInterval(() => {
      this.checkIdentity();
      this.checkTvExtension();
    }, 5000);
    window.addEventListener('autotrade:tv-bridge-ready', () => this.checkTvExtension());
    window.addEventListener('focus', () => this.checkTvExtension());
    this.connectWS();
    // 点击悬浮交易台以外任意位置 → 自动收起（Element Plus 下拉/弹窗渲染在 body，
    // 不属于 .deck，必须排除，否则选股点选项/确认框按钮会误收起）
    this._outsideDeckHandler = (e) => {
      if (this.deckCollapsed) return;
      const t = e.target;
      if (t.closest('.deck') || t.closest('.deck-toggle-btn')) return;
      if (t.closest('.el-select-dropdown') || t.closest('.el-popper')
          || t.closest('.el-message-box') || t.closest('.el-message')
          || t.closest('.el-notification')) return;
      this.deckCollapsed = true;
    };
    document.addEventListener('click', this._outsideDeckHandler, true);
    // 每秒刷新前端时钟，让 TV/IBKR 时钟走秒；同时扫掉信号栏里超过
    // 4 分钟的 FVG 卡片（自动到期，与手动删除并存）
    setInterval(() => {
      const lastMinute = Math.floor(this.now / 60);
      this.now = Date.now() / 1000;
      if (Math.floor(this.now / 60) !== lastMinute) this.checkHoldingRankChanges();
      const cutoff = this.now - 240;
      if (this.fvgFeed.length && this.fvgFeed.some(c => c.ts < cutoff)) {
        this.fvgFeed = this.fvgFeed.filter(c => c.ts >= cutoff);
      }
    }, 1000);
    // 定时拉取绩效统计（每 10s）
    this.fetchPerformance();
    setInterval(() => this.fetchPerformance(), 10000);
    // 定时拉取历史事件（每 5s）
    this.fetchEvents();
    setInterval(() => this.fetchEvents(), 5000);
    // 盘前新闻（每 30s 轮询，手动刷新后立即拉）
    this.fetchNews();
    setInterval(() => this.fetchNews(), 30000);
    // 盘前报告：历史列表 + 生成进度轮询（2s；完成跳变时刷新列表并提示）
    this.fetchReports();
    this.fetchReportStatus();
    this.fetchSources();
    setInterval(() => this.fetchReportStatus(), 2000);
  },

  beforeUnmount() {
    if (this._outsideDeckHandler) {
      document.removeEventListener('click', this._outsideDeckHandler, true);
    }
    document.removeEventListener('pointermove', this._panelResizeMove);
    document.removeEventListener('pointerup', this._panelResizeEnd);
  },

  methods: {
    nightTime(ts) { return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—'; },
    async fetchNightStatus() {
      try { const response = await fetch('/api/night-test/status'); if (response.ok) this.night = await response.json(); } catch (_) { /* show current sample until connection returns */ }
    },
    async nightAction(action) {
      if (this.nightBusy) return;
      this.nightBusy = true;
      try {
        const body = action === 'connect' ? { symbol: this.nightForm.symbol.toUpperCase(), strategy: this.nightForm.strategy, session: this.nightForm.session }
          : action === 'start' ? { risk_budget: this.nightForm.risk_budget, window_start: this.nightForm.window_start, window_end: this.nightForm.window_end } : {};
        const response = await fetch(`/api/night-test/${action}`, { method: 'POST', headers: this.tradeHeaders(true), body: JSON.stringify(body) });
        this.checkWrite403(response);
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '测试请求失败');
        this.night = result;
      } catch (error) { this.$message.error(error.message || '测试请求失败'); }
      finally { this.nightBusy = false; }
    },
    selectWorkspace(group, tab) {
      if(group==='execution'&&tab==='orders'&&this.workspaceTabs.execution.includes('rhythm'))tab='rhythm';
      if (this.workspaceTabs && !this.workspaceTabs[group]) return;
      if (tab && !this.workspaceTabs[group].includes(tab)) return;
      this.saveWorkspaceLayout();
      this.workspaceGroup = group;
      const saved = {...(this.workspaceLayouts[group] || {})};
      if(group==='execution'&&saved.tab==='orders'&&this.workspaceTabs.execution.includes('rhythm'))saved.tab='rhythm';
      this.bottomTab = tab || (this.workspaceTabs[group].includes(saved.tab) && saved.tab) || this.workspaceTabs[group][0];
      this.workspaceExpanded = this.bottomTab !== 'instances';
      if (saved.height >= 180) this.bottomPanelHeight = saved.height;
      this.onBottomTabChange(this.bottomTab);
    },
    saveWorkspaceLayout() {
      this.workspaceLayouts[this.workspaceGroup] = { tab: this.bottomTab, height: this.bottomPanelHeight };
      try { localStorage.setItem('workspace_layouts', JSON.stringify(this.workspaceLayouts)); } catch (_) { /* presentation only */ }
    },
    profileName(profile) { return { single_entry: '单次建仓', ema9_legacy_pyramid: 'EMA9-ST-ATR 金字塔兼容' }[profile] || profile || '未提供'; },
    instanceTitle(i) { return `${this.autoModelName(i.strategy)} / ${i.strategy_instance_id || 'default'} / ${i.symbol}`; },
    instanceHasPosition(i) {
      if (Number(i.qty) > 0) return true;
      return this.autoLotRows(i).some(order => Number(order.filled_qty) > 0
        && !['CLOSED', 'DONE', 'CANCELLED', 'FAILED'].includes(order.state));
    },
    instanceInactive(i) {
      return i.state === 'DONE' && i.qty != null && Number(i.qty) === 0 && !this.instanceHasPosition(i)
        && !(i.lot_ids || []).length && !i.pending_lot_id
        && !i.exit_requested_reason && !i.recovery_block_reason;
    },
    instanceState(i) {
      if (i.exit_requested_reason || i.state === 'CLOSING') return '退出中';
      if (i.recovery_block_reason) return '恢复待核验';
      if (i.state === 'DONE') return '已结束';
      return { WATCHING: '等待信号', PENDING: '委托中', ACTIVE: '持仓中', AWAITING_CONFIRM: '等待确认' }[i.state] || i.state || '未知';
    },
    orderState(state) { return { ACTIVE: '持仓中', PENDING: '委托中', CLOSING: '退出中', CLOSED: '已平仓', DONE: '已结算', CANCELLED: '已撤单', FAILED: '失败' }[state] || state; },
    entryOrderTypeText(order) {
      return { MKT: '市价 MKT', LMT: '限价 LMT' }[order.entry_order_type] || '类型未记录';
    },
    instanceMaxEntries(i) { return Array.isArray(i.risk_budgets) ? i.risk_budgets.length : null; },
    instanceRuleText(i) {
      if (i.breakeven_done) return '已执行整组保本';
      if (i.auto_breakeven == null) return '保本配置未提供';
      if (!i.auto_breakeven) return '自动保本关闭';
      return i.breakeven_r > 0 ? `自动保本门槛 ${i.breakeven_r}R` : '保本门槛未提供';
    },
    protectionText(order) {
      if (order.state !== 'ACTIVE' || !order.filled_qty) return '未持仓 / 不适用';
      if (this.demoMode && (order.lot_id || '').startsWith('demo-')) return '演示：非券商订单';
      if (this.status.dry_run) return '模拟执行：无券商确认';
      if (order.protection_restore_needed) return '保护单待恢复';
      const status = order.protection_order_status;
      if (['Submitted', 'PreSubmitted'].includes(status)) {
        if (!(order.protection_order_qty >= order.filled_qty)) return `保护数量待核验（${order.protection_order_qty ?? '未知'}/${order.filled_qty}）`;
        return `券商回报：已受理 ${status}${this.status.connected ? '' : ' · 当前连接已断开'}`;
      }
      return status ? `券商回报：${status}` : '券商确认未提供';
    },
    guardDemoOrder(row) {
      if (this.demoMode || (row.lot_id || '').startsWith('demo-')) {
        this.$message.info('实例与订单批次演示不执行交易操作');
        return true;
      }
      return false;
    },
    async fetchPerformance() {
      try {
        const r = await fetch('/api/performance');
        if (r.ok) this.performance = await r.json();
      } catch (e) { /* 忽略 */ }
    },

    startPanelResize(event) {
      event.preventDefault();
      const startY = event.clientY;
      const startHeight = this.bottomPanelHeight;
      this.panelResizing = true;
      this._panelResizeMove = e => {
        const max = Math.max(240, window.innerHeight - 180);
        this.bottomPanelHeight = Math.round(Math.min(max, Math.max(180, startHeight + startY - e.clientY)));
      };
      this._panelResizeEnd = () => {
        this.panelResizing = false;
        document.removeEventListener('pointermove', this._panelResizeMove);
        document.removeEventListener('pointerup', this._panelResizeEnd);
        try { localStorage.setItem('bottom_panel_height', String(this.bottomPanelHeight)); } catch (e) { /* 忽略 */ }
        this.saveWorkspaceLayout();
      };
      document.addEventListener('pointermove', this._panelResizeMove);
      document.addEventListener('pointerup', this._panelResizeEnd);
    },

    onBottomTabChange(name) {
      if (!this.openingFeedPreferenceSet) this.openingFeedCollapsed = name === 'rhythm';
      if (this.workspaceLayouts) this.saveWorkspaceLayout();
      if (name === 'shadow') this.loadStrategyShadow();
      if (name === 'returns' && !this.returnsData && !this.returnsLoading) {
        this.fetchReturnsComparison();
      }
    },

    async loadStrategyShadow() {
      if (this.strategyShadowLoading) return;
      this.strategyShadowLoading = true;
      this.strategyShadowError = '';
      try {
        const response = await fetch('/api/shadow/strategy-report?day=' + encodeURIComponent(this.strategyShadowDay || ''));
        const report = await response.json();
        if (!response.ok) throw new Error(typeof report.detail === 'string' ? report.detail : '影子复盘读取失败');
        this.strategyShadowReport = report;
      } catch (error) {
        this.strategyShadowReport = null;
        this.strategyShadowError = error.message;
      } finally {
        this.strategyShadowLoading = false;
      }
    },

    fmtPct(value, digits = 2) {
      if (typeof value !== 'number' || !isFinite(value)) return '—';
      const pct = value * 100;
      return `${pct > 0 ? '+' : ''}${pct.toFixed(digits)}%`;
    },

    async fetchReturnsComparison() {
      if (!Array.isArray(this.returnsRange) || this.returnsRange.length !== 2) {
        this.returnsError = '请选择起始和结束日期';
        return;
      }
      const requestId = ++this.returnsRequestId;
      this.returnsLoading = true;
      this.returnsError = '';
      try {
        const [start, end] = this.returnsRange;
        const r = await fetch(`/api/returns/compare?start=${start}&end=${end}`);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '收益数据加载失败');
        if (requestId === this.returnsRequestId) this.returnsData = data;
      } catch (e) {
        if (requestId === this.returnsRequestId) this.returnsError = e.message || String(e);
      } finally {
        if (requestId === this.returnsRequestId) this.returnsLoading = false;
      }
    },

    async fetchEvents() {
      try {
        const r = await fetch('/api/events?limit=200');
        if (r.ok) this.events = await r.json();
      } catch (e) { /* 忽略 */ }
    },

    // 盘前新闻：拉取今日摘要（空态不报错）
    async fetchNews() {
      try {
        const r = await fetch('/api/news');
        if (r.ok) this.news = await r.json();
      } catch (e) { /* 忽略 */ }
    },

    // 手动刷新（后端 10 分钟冷却）；成功后立即重拉
    async refreshNews() {
      this.newsBusy = true;
      try {
        const r = await fetch('/api/news/refresh', { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '刷新失败');
        this.$message.success('盘前新闻已重新生成');
        await this.fetchNews();
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.newsBusy = false;
      }
    },

    hasLiveNews(ticker) {
      return this.liveNewsTickers.includes((ticker || '').toUpperCase());
    },
    async openLiveNews(ticker) {
      const d = this.liveNewsDialog;
      const requestId = d.requestId + 1;
      Object.assign(d, { visible: true, ticker: (ticker || '').toUpperCase(),
                         loading: true, error: '', requestId });
      try {
        const r = await fetch(`/api/news/live?ticker=${encodeURIComponent(ticker)}&minutes=15`);
        const payload = await r.json();
        if (!r.ok) throw new Error(payload.detail || '实时新闻加载失败');
        if (d.requestId !== requestId) return;
        d.items = payload.items || [];
        d.updatedAt = payload.updated_at;
        d.error = payload.error || '';
      } catch (e) {
        if (d.requestId !== requestId) return;
        d.items = [];
        d.error = e.message || String(e);
      } finally {
        if (d.requestId === requestId) d.loading = false;
      }
    },

    // ---- 盘前报告工作流 ----
    // 历史列表（日期倒序）；首次载入自动选最新一份
    async fetchReports() {
      try {
        const r = await fetch('/api/reports');
        if (!r.ok) return;
        const d = await r.json();
        this.reportList = d.reports || [];
        if (!this.reportDate && this.reportList.length) {
          this.loadReport(this.reportList[0].date);
        }
      } catch (e) { /* 忽略 */ }
    },
    async loadReport(date) {
      try {
        const r = await fetch(`/api/reports/${date}`);
        if (!r.ok) return;
        const d = await r.json();
        this.reportDate = date;
        this.reportMd = d.markdown || '';
      } catch (e) { /* 忽略 */ }
    },
    // 生成今日报告：异步长任务，按下即禁用（running 由 status 轮询驱动），
    // 409=在跑 / 429=冷却 由后端把关
    async generateReport() {
      try {
        const r = await fetch('/api/reports/generate', { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '生成失败');
        this.$message.info('报告生成中（采集约 2 分钟），进度见按钮倒计时');
        this.fetchReportStatus();
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },
    // 生成状态轮询：running 跳变 true→false = 完成/失败 → 刷新列表+通知
    async fetchReportStatus() {
      try {
        const r = await fetch('/api/reports/status');
        if (!r.ok) return;
        const st = await r.json();
        if (this.reportWasRunning && !st.running) {
          if (st.last_error) {
            this.$notify({ title: '盘前报告生成失败', message: st.last_error, type: 'error', duration: 0, position: 'bottom-right' });
          } else if (st.last_result) {
            this.$notify({ title: '盘前报告已生成', message: `${st.last_result.date} · ${st.last_result.degraded ? 'LLM 降级（纯标题汇编）' : st.last_result.model}`, type: 'success', duration: 10000, position: 'bottom-right' });
            this.reportList = [];
            await this.fetchReports();
            if (st.last_result.date) this.loadReport(st.last_result.date);
          }
        }
        this.reportWasRunning = !!st.running;
        this.reportStatus = st;
      } catch (e) { /* 忽略 */ }
    },
    // 轻量 markdown 渲染（局域网离线环境，不引外部 CDN）：
    // 先 HTML 转义，再处理 标题/加粗/无序有序列表/链接/分隔线；
    // watchlist ticker 包成可点 tv-link（data-tv 委托 reportClick 切 TV）
    renderMd(text) {
      if (!text) return '';
      const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const tickers = new Set(Object.keys(this.market).map(s => s.split(':').pop()));
      const inline = s => esc(s)
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/(https?:\/\/[^\s)]+)/g, '<a href="$1" target="_blank" rel="noopener">链接</a>')
        .replace(/\b([A-Z]{2,5})\b/g, (m) =>
          tickers.has(m) ? `<span class="tv-link md-ticker" data-tv="${m}">${m}</span>` : m);
      const out = [];
      let inUl = false, inOl = false;
      const closeLists = () => {
        if (inUl) { out.push('</ul>'); inUl = false; }
        if (inOl) { out.push('</ol>'); inOl = false; }
      };
      for (const line of text.split('\n')) {
        const t = line.trim();
        let m;
        if ((m = t.match(/^(#{1,3})\s+(.*)/))) {
          closeLists();
          const lv = m[1].length;
          out.push(`<h${lv} class="md-h">${inline(m[2])}</h${lv}>`);
        } else if (/^---+$/.test(t)) {
          closeLists(); out.push('<hr class="md-hr">');
        } else if ((m = t.match(/^[-*]\s+(.*)/))) {
          if (!inUl) { closeLists(); out.push('<ul class="md-ul">'); inUl = true; }
          out.push(`<li>${inline(m[1])}</li>`);
        } else if ((m = t.match(/^\d+\.\s+(.*)/))) {
          if (!inOl) { closeLists(); out.push('<ol class="md-ol">'); inOl = true; }
          out.push(`<li>${inline(m[1])}</li>`);
        } else if (t === '') {
          closeLists();
        } else {
          closeLists();
          out.push(`<p class="md-p">${inline(t)}</p>`);
        }
      }
      closeLists();
      return out.join('');
    },
    // 报告正文里的 ticker 点击（事件委托）→ CDP 切 TV
    reportClick(e) {
      const t = e.target.closest('[data-tv]');
      if (t) this.tvShow(t.dataset.tv);
    },

    // ---- 报告视频源管理 ----
    async fetchSources() {
      try {
        const r = await fetch('/api/report/sources');
        if (r.ok) this.srcList = (await r.json()).sources || [];
      } catch (e) { /* 忽略 */ }
    },
    async addSource() {
      const url = (this.srcInput || '').trim();
      if (!url) return;
      try {
        const r = await fetch('/api/report/sources', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ url }),
        });
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '添加失败');
        this.srcInput = '';
        this.$message.success(d.duplicated ? '该视频已在源列表' : '已添加，正在抓取元数据…');
        await this.fetchSources();
        setTimeout(() => this.fetchSources(), 4000);   // 元数据补完后刷新标题
      } catch (e) { this.$message.error(e.message || String(e)); }
    },
    async toggleSource(s) {
      try {
        await fetch(`/api/report/sources/${encodeURIComponent(s.video_id)}/toggle`,
                    { method: 'POST', headers: this.tradeHeaders() });
        await this.fetchSources();
      } catch (e) { /* 忽略 */ }
    },
    async deleteSource(s) {
      try {
        await fetch(`/api/report/sources/${encodeURIComponent(s.video_id)}/delete`,
                    { method: 'POST', headers: this.tradeHeaders() });
        await this.fetchSources();
      } catch (e) { /* 忽略 */ }
    },

    // ---- 局域网多客户端：身份登记与审批 ----
    // 启动时 + 每 5s 查询本客户端身份；未确认（guest/trader 待审批）时遮罩挡住交互
    async checkIdentity() {
      if (this.identity === 'ok') return;   // 已确认后不再轮询
      try {
        const headers = {};
        if (this.traderToken) headers['X-Trader-Token'] = this.traderToken;
        const r = await fetch('/api/clients/self', { headers });
        if (!r.ok) return;   // 静默：guest 无 token 属正常情况，不弹错
        const d = await r.json();
        if (d.role === 'master') {
          this.traderName = 'master';
          this.identity = 'ok';
        } else if (d.role === 'trader' && d.approved) {
          this.traderName = d.name;
          this.identity = 'ok';
          this.checkTvExtension();
        } else if (d.role === 'trader' && d.expired) {
          // token 超过有效期：清除本地凭证，回到登记流程（提示过期原因）
          this.identityNotice = '登录已过期（超过 ' + (d.ttl_hours || 10) + ' 小时有效期），请重新登记';
          this.logoutTrader();
          this.identity = 'register';
        } else if (d.role === 'trader') {
          this.identity = 'pending';    // 已登记，等待 master 确认
        } else {
          this.identity = 'register';   // guest：需登记交易员名称
        }
      } catch (e) { /* 服务不可达：保持现状，下轮重试 */ }
    },

    // 清除本机交易员凭证（token 过期/主动退出时调用）
    logoutTrader() {
      this.traderToken = '';
      this.traderName = '';
      this.holdingRankState = null;
      this.holdingRankNotice?.close?.();
      this.holdingRankNotice = null;
      try {
        localStorage.removeItem('trader_token');
        localStorage.removeItem('trader_name');
      } catch (e) { /* 忽略 */ }
    },

    // guest 提交登记：存 token/name 到 localStorage → 进入等待确认状态
    async registerTrader() {
      const name = (this.traderInputName || '').trim();
      if (!name) return this.$message.warning('请输入交易员名称');
      this.registerBusy = true;
      try {
        const r = await fetch('/api/clients/register', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '登记失败');
        this.traderToken = d.token;
        try {
          localStorage.setItem('trader_token', d.token);
          localStorage.setItem('trader_name', name);
        } catch (e) { /* 忽略 */ }
        this.identity = 'pending';
        this.$message.success('已提交登记，等待 master 确认');
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.registerBusy = false;
      }
    },

    // 写操作统一请求头：带交易员 token（本机 master 无 token 也由后端放行）
    tradeHeaders(json = false) {
      const h = {};
      if (json) h['Content-Type'] = 'application/json';
      if (this.traderToken) h['X-Trader-Token'] = this.traderToken;
      return h;
    },
    // 写响应 403 统一提示（客户端未确认）
    checkWrite403(r) {
      if (r.status === 403) throw new Error('客户端未确认，等待 master 审批');
    },

    // master 审批：确认 / 拒绝待审批交易员（快照只带 token_prefix，后端按前缀匹配）
    async approveClient(c) {
      try {
        const r = await fetch(`/api/clients/${encodeURIComponent(c.token_prefix)}/approve`,
          { method: 'POST', headers: this.tradeHeaders() });
        if (!r.ok) { const d = await r.json(); throw new Error(typeof d.detail === 'string' ? d.detail : '确认失败'); }
        this.$message.success(`已确认交易员 ${c.name}`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },
    async rejectClient(c) {
      try {
        await this.$confirm(`确认拒绝交易员 ${c.name}（${c.token_prefix}）？其需重新登记`, '拒绝确认', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/clients/${encodeURIComponent(c.token_prefix)}/reject`,
          { method: 'POST', headers: this.tradeHeaders() });
        if (!r.ok) { const d = await r.json(); throw new Error(typeof d.detail === 'string' ? d.detail : '拒绝失败'); }
        this.$message.success(`已拒绝 ${c.name}`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    eventTypeText(t) {
      const m = {
        execution_fill: '成交增量 / 均价核对',
        pine_confirmation_unverified: '确认输出时间待核验',
        order_placed: '下单', order_closed: '平仓', order_close_requested: '平仓请求',
        order_close_failed: '平仓失败', order_cancelled: '撤单',
        order_cancel_failed: '撤单失败', position_scan_alert: '平仓提醒',
        auto_close_st_flip: 'ST转向自动平仓（历史）', missing_stop_alert: '止损单缺失',
        external_position_alert: '外部订单/持仓提醒',
        pullback_event: '回踩事件', shadow_settled: '影子结算',
        breakout_signal: '盘前突破',
        opening_range_breakout: '开盘小区间突破',
        midpoint_limit_repriced: '中点单改价', midpoint_limit_reprice_failed: '中点单改价失败',
        retest_limit_repriced: '回踩单改价', retest_limit_reprice_failed: '回踩单改价失败',
        lot_revived: '对账复活',
        external_order_cancelled: '外部挂单已撤', external_order_cancel_failed: '外部挂单撤单失败',
        external_position_closed: '外部持仓已平', external_position_close_failed: '外部持仓平仓失败',
        external_position_close_pending: '外部持仓平仓中', position_close_requested: '工具仓位平仓请求',
      };
      if (m[t]) return m[t];
      // 全自动策略事件（auto_* 前缀映射）
      const am = {
        auto_start: '自动策略启动', auto_stop: '自动策略停止',
        auto_started: '实例启动', auto_stopped: '实例停止',
        auto_order_placed: '策略建仓', auto_filled: '策略成交',
        auto_order_failed: '策略下单失败', auto_expired: '埋单过期',
        auto_breakeven: '移动保本', breakout_first_bar_protection: '突破首根K线保护', breakout_close_protection: '突破五分钟保护', auto_trail: 'ST跟踪',
        candidate_order_protection:'候选订单保护',candidate_order_protection_failed:'候选订单保护待重试',
        intelligent_exit_action:'智能退出仓位',intelligent_exit_failed:'智能退出未完成',
        auto_reverse_close: '反向平仓', auto_done: '实例结束',
        auto_adopted: '收养人工仓',
        auto_entry_proposed: '开仓候选', auto_entry_approved: '确认开仓候选',
        auto_entry_rejected: '忽略开仓候选', auto_entry_expired: '开仓候选过期',
      };
      return am[t] || t;   // 未映射的类型原样显示（防御）
    },

    autoNext(i) {
      if (i.state === 'DONE') return '本轮已结束，不自动重开';
      if (i.state === 'CLOSING' || i.exit_requested_reason) return '退出中：撤单 / 平仓确认';
      if (i.recovery_block_reason) return i.recovery_block_reason;
      if (i.pending_lot_id || i.state === 'PENDING') return '等待挂单实际成交';
      if (i.enabled === false) return '新入场已暂停，已有委托及保护/退出继续管理';
      if (!i.entries) return `等待${this.autoModelName ? this.autoModelName(i.strategy) : '策略模型'}入场信号`;
      if (!Array.isArray(i.risk_budgets) || !i.risk_budgets.length) return '执行上限未提供，仅显示已知状态';
      if (i.entries >= i.risk_budgets.length) return '仅管理，等整组退出';
      const unlockR = i.add_unlock_r;
      if (!(unlockR > 0)) return '加仓门槛未提供';
      if (i.entries === 1 && !i.unlocked) return `等首仓达到${unlockR}R`;
      if (i.entries === 2 && !i.unlocked) return `等整组均价达到${unlockR}R`;
      if (i.entries === 2 && (i.second_mfe_r || 0) < 1) return '等第二仓达到1R';
      return `等${this.autoModelName ? this.autoModelName(i.strategy) : 'EMA9-ST-ATR'}第${i.entries + 1}次入场信号`;
    },
    async fetchAutoModels() {
      try {
        const response = await fetch('/api/auto/models');
        if (response.ok) this.autoModels = (await response.json()).models;
      } catch (_) { /* 断线期间保留最近模型列表 */ }
    },
    autoModelName(id) {
      return this.autoModels.find(model => model.id === id)?.name || id || 'EMA9-ST-ATR';
    },
    autoRiskText(i) {
      if (!i.last_order_budget) return i.entries ? '历史订单风险记录未提供' : '成交后开始统计MFE';
      return `最近一笔：预算 $${this.fmt2(i.last_order_budget)} / 计划风险 $${this.fmt2(i.last_order_risk)} · ${i.last_order_qty}股 · 止损 ${this.fmt2(i.last_order_stop)}`;
    },
    orderRiskText(o) {
      const meta = o.signal_snapshot || {};
      const planned = meta.planned_risk_usd ?? o.init_risk_usd;
      if (planned == null) return '风险记录缺失';
      return `预算 ${meta.risk_budget == null ? '—' : '$' + this.fmt2(meta.risk_budget)} / 计划 $${this.fmt2(planned)}`;
    },
    autoPnl(i) {
      if (!i.entries || !i.qty || !i.avg_cost) return null;
      let px = 0;
      if (this.demoMode) {
        const rows = this.demoOrderLots.filter(l => (i.lot_ids || []).includes(l.lot_id));
        px = rows.length ? (rows[0].last_px || rows[0].avg_fill) : 0;
      } else {
        px = (((this.market || {})[i.symbol] || {}).bar || {}).close || 0;
      }
      if (!px) return null;
      const sign = i.side === 'long' ? 1 : -1;
      return +((px - i.avg_cost) * sign * i.qty).toFixed(2);
    },
    viewAutoOrders(i) {
      const key = i.inst_id || i.symbol;
      this.expandedAutoOrders[key] = !this.expandedAutoOrders[key];
    },
    autoLotRows(inst) {
      const source = this.demoMode ? this.demoOrderLots : this.lots;
      const ids = inst.lot_ids || [];
      return source.filter(l => ids.includes(l.lot_id));
    },
    autoBreakevenKey(i) { return `${i.strategy || 'ema9_pyramid'}:${i.strategy_instance_id || 'default'}:${i.symbol}:${i.group_id || ''}`; },
    autoBreakevenHint(i) {
      if (this.demoMode) return '演示模式不执行真实保本';
      if (this.autoBreakevenBusy[this.autoBreakevenKey(i)]) return '正在处理此交易组的保本请求';
      if (i.state === 'DONE' || i.state === 'CLOSING' || i.exit_requested_reason) return '交易组已结束或正在退出';
      if (i.recovery_block_reason) return '交易组恢复状态待核验，请先核对订单明细';
      if (i.pending_lot_id || i.state === 'PENDING') return '请等待挂单成交或撤单确认后再保本';
      if (!(i.qty > 0) || !(i.avg_cost > 0) || !i.group_id) return '等待实际持仓、成交均价和交易组信息';
      if (i.breakeven_done) return '当前记录止损已至交易组成本或更有利，保护回报见订单明细';
      return '';
    },
    async breakevenAutoInstance(i) {
      const hint = this.autoBreakevenHint(i);
      if (hint) { this.$message.info(hint); return; }
      const key = this.autoBreakevenKey(i);
      const body = { group_id: i.group_id, qty: i.qty, avg_cost: i.avg_cost };
      this.autoBreakevenBusy[key] = true;
      try {
        await this.$confirm(`对象：${this.instanceTitle(i)}。本交易组 ${i.qty} 股，实际成交均价 ${i.avg_cost}。将该组已成交订单的保护止损收紧到交易组均价（按价格精度取整），保留更有利止损，不处理挂单。实际保护以券商回报为准。`, '整组保本确认', { type: 'warning', confirmButtonText: '确认保本', cancelButtonText: '取消' });
        const response = await fetch(`/api/auto/instances/${encodeURIComponent(i.symbol)}/breakeven?strategy=${encodeURIComponent(i.strategy || 'ema9_pyramid')}&instance_id=${encodeURIComponent(i.strategy_instance_id || 'default')}`,
          { method: 'POST', headers: this.tradeHeaders(true), body: JSON.stringify(body) });
        this.checkWrite403(response);
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '保本请求失败');
        if (!result.ok) {
          this.$message.warning(`部分保本修改未完成：${(result.failed || []).map(row => `${row.lot_id} ${row.reason}`).join('；')}。请核对订单明细，已成功的修改会保留。`);
        } else {
          this.$message.success(`保本价格 ${result.target_stop}：已提交 ${(result.updated || []).length} 笔，已在成本或更有利 ${(result.unchanged || []).length} 笔。实际保护回报见订单明细。`);
        }
      } catch (error) {
        if (error !== 'cancel' && error !== 'close') this.$message.error(error.message || String(error));
      } finally { delete this.autoBreakevenBusy[key]; }
    },
    async stopAutoInstance(i, closePositions) {
      if (this.demoMode) {
        this.$message.info('演示模式不执行真实策略控制');
        return;
      }
      try {
        if (closePositions) {
          await this.$confirm(`对象：${this.instanceTitle(i)}。关联订单 ${(i.lot_ids || []).length} 笔，持仓 ${i.qty || 0} 股。将暂停新入场、撤销关联挂单并请求平仓；等待券商确认后才算完成。`, '退出此交易组', { type: 'warning' });
        }
        const r = await fetch(`/api/auto/instances/${encodeURIComponent(i.symbol)}/stop?close_positions=${closePositions}&strategy=${encodeURIComponent(i.strategy || 'ema9_pyramid')}&instance_id=${encodeURIComponent(i.strategy_instance_id || 'default')}`,
                              { method: 'POST', headers: this.tradeHeaders() });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '操作失败');
        this.$message.success(closePositions ? '此交易组退出请求已提交，等待撤单与成交确认' : '此实例新入场已暂停；已有委托及保护/退出继续管理');
      } catch (e) {
        if (e !== 'cancel') this.$message.error(e.message || String(e));
      }
    },
    async adoptManualLot(row) {
      if (this.demoMode) {
        this.$message.info('演示模式不执行真实接管');
        return;
      }
      try {
        await this.$confirm(`将 ${row.symbol.split(':').pop()} 手动仓作为金字塔首仓接管？`,
                            '手动仓转全自动', { type: 'warning' });
        const r = await fetch('/api/auto/adopt', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ symbol: row.symbol, lot_id: row.lot_id, strategy: this.autoModel, instance_id: this.autoInstanceId }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '接管失败');
        this.$message.success('已转全自动管理');
        this.selectWorkspace('execution');
      } catch (e) {
        if (e !== 'cancel') this.$message.error(e.message || String(e));
      }
    },

    // 策略：启动 / 停止 / 开仓候选决策
    async startAuto(target = null) {
      if (this.autoBusy) return;
      if (this.demoMode) {
        this.$message.info('演示模式不会启动真实策略');
        return;
      }
      this.autoBusy = true;
      let symbols = [];
      try {
        if (target?.validate && !target.validate()) return;
        symbols = Array.isArray(target?.symbols) ? target.symbols
          : this.autoSymbols.split(',').map(s => s.trim()).filter(Boolean);
        const request = {
          strategy: target?.strategy || this.autoModel,
          instance_id: target?.instance_id || this.autoInstanceId, symbols,
        };
        const preview = await fetch('/api/auto/start/preview', {
          method: 'POST', headers: this.tradeHeaders(true), body: JSON.stringify(request),
        });
        this.checkWrite403(preview);
        const check = await preview.json();
        if (!preview.ok) throw new Error(typeof check.detail === 'string' ? check.detail : '方向检查失败');
        if (target?.direction && symbols.some(symbol => check.directions[symbol] !== target.direction)) {
          throw new Error('策略方向与锁定目标不一致，请重新核对');
        }
        const directionText = value => ({ 1: '多', [-1]: '空' }[value] || '未就绪');
        const targets = check.symbols.map(s => `${s} ${check.follow_signal_direction ? '按信号方向' : directionText(check.directions[s])}`).join('、');
        try {
          const candidate = check.candidate;
          const levels = candidate ? `当前外沿 ${candidate.entry_price.toFixed(2)}，ST 止损 ${candidate.stop_px.toFixed(2)}。触线时按最新值重新核对。` : '';
          await this.$confirm(
            `策略模型 ${check.model}，实例 ${request.instance_id}，首仓预算 $${check.risk}。QQQ 当前方向：${directionText(check.directions['NASDAQ:QQQ'])}（仅供参考）。标的方向：${targets}。${levels}确认启动？`,
            '启动执行实例 · 二次确认', {
              type: 'warning', confirmButtonText: '确认启动', cancelButtonText: '取消',
              closeOnClickModal: false, autofocus: false,
            });
        } catch { return; }
        if (target?.validate && !target.validate()) return;
        const r = await fetch('/api/auto/start', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ ...request, symbols: check.symbols, confirmed_directions: check.directions }),
        });
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '启动失败');
        const reasons = Object.entries(d.skipped_reasons || {}).map(([s, reason]) => `${s.split(':').pop()}：${reason}`);
        if (d.skipped.length) this.speakStrategyFailure('策略启动失败', d.skipped);
        if (reasons.length) this.$message.warning(reasons.join('；'));
        else if (d.skipped.length) this.$message.warning(`已启动 ${d.started.length} 个实例，跳过 ${d.skipped.length} 个标的`);
        else this.$message.success(`已启动 ${d.started.length} 个实例`);
      } catch (e) {
        this.speakStrategyFailure('策略启动失败', symbols);
        this.$message.error(e.message || String(e));
      } finally {
        this.autoBusy = false;
      }
    },
    async stopAuto(closePositions) {
      if (this.demoMode) {
        this.$message.info('演示模式不执行真实策略控制');
        return;
      }
      try {
        if (closePositions) {
          const rows = (this.auto && this.auto.instances || []).filter(i => i.state !== 'DONE');
          const details = rows.map(i => `${this.instanceTitle(i)}：持仓${i.qty || 0}股，关联订单${(i.lot_ids || []).length}笔`).join('；');
          await this.$confirm(`作用于全部自动执行实例。将停止新入场、撤销关联挂单并请求退出关联持仓，不包含未接管的手动单。${details}。提交后仍需等待券商确认。`, '退出全部自动执行交易组', { type: 'warning' });
        }
        const r = await fetch(`/api/auto/stop?close_positions=${closePositions}`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '停止失败'); }
        this.$message.success(closePositions ? '全部自动执行实例的退出请求已提交，请核对撤单与成交结果' : '全部实例已暂停新入场；已有委托及保护/退出继续管理');
      } catch (e) {
        if (e !== 'cancel') this.$message.error(e.message || String(e));
      }
    },
    async decideAutoProposal(p, action) {
      const id = p.proposal_id;
      if (this.autoProposalBusy[id]) return;
      this.autoProposalBusy = { ...this.autoProposalBusy, [id]: true };
      try {
        const options = action === 'reject'
          ? { method: 'POST', headers: this.tradeHeaders(true), body: JSON.stringify({ reason: '' }) }
          : { method: 'POST', headers: this.tradeHeaders() };
        const r = await fetch(`/api/auto/proposals/${encodeURIComponent(id)}/${action}`, options);
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '候选处理失败');
        this.$message.success(action === 'approve'
          ? `已确认 ${d.symbol} 开仓，x${d.qty}`
          : `已忽略 ${d.symbol} 开仓候选`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        const busy = { ...this.autoProposalBusy };
        delete busy[id];
        this.autoProposalBusy = busy;
      }
    },
    autoStateTag(s) {
      return { WATCHING: 'info', AWAITING_CONFIRM: 'warning', PENDING: 'warning', ACTIVE: 'success', DONE: 'info' }[s] || 'info';
    },

    // ---- 跟单管理 ----
    // 为每个服务器维护一份可编辑草稿：快照每秒覆盖 copy 本体，
    // 草稿按 name 独立保存，未点「保存」前的编辑不会被快照冲掉
    syncCopyDrafts() {
      const servers = (this.copy && this.copy.servers) || [];
      const draft = {};
      for (const s of servers) {
        draft[s.name] = this.copyDraft[s.name] || {
          size_mode: s.size_mode || 'risk',
          risk_usd: s.risk_usd ?? 300,
          size_multiplier: s.size_multiplier ?? 1.0,
          max_drawdown_pct: s.max_drawdown_pct ?? 3.0,
          direction: s.direction || 'both',
        };
      }
      this.copyDraft = draft;
    },

    // 跟单 REST 写操作统一入口：带交易员 token（403 → 未审批提示），非 2xx 抛 detail
    async copyPost(url, body) {
      const opt = { method: 'POST', headers: this.tradeHeaders(body != null) };
      if (body != null) opt.body = JSON.stringify(body);
      const r = await fetch(url, opt);
      this.checkWrite403(r);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '操作失败');
      return d;
    },

    // 启用开关即时生效（不走进草稿）
    async copyToggleEnable(s, v) {
      try {
        await this.copyPost(`/api/copy/servers/${encodeURIComponent(s.name)}/${v ? 'enable' : 'disable'}`);
        this.$message.success(`${s.name} 已${v ? '启用' : '停用'}`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    // 保存：提交草稿中的可编辑字段（name/enabled 不可改，由后端忽略）
    async copySaveServer(s) {
      try {
        const d = this.copyDraft[s.name] || {};
        await this.copyPost(`/api/copy/servers/${encodeURIComponent(s.name)}/update`, {
          size_mode: d.size_mode, risk_usd: d.risk_usd,
          size_multiplier: d.size_multiplier, max_drawdown_pct: d.max_drawdown_pct,
          direction: d.direction,
        });
        this.$message.success(`${s.name} 配置已保存`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    async copyDeleteServer(s) {
      try {
        await this.$confirm(`确认删除跟单服务器 ${s.name}（${s.host}:${s.port}）？`, '删除确认', { type: 'warning' });
      } catch { return; }
      try {
        await this.copyPost(`/api/copy/servers/${encodeURIComponent(s.name)}/delete`);
        this.$message.success(`已删除 ${s.name}`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    // 添加服务器：clientId 服务端自动分配，新增默认 enabled=false
    async copyAddServer() {
      const f = this.copyForm;
      if (!f.name.trim()) return this.$message.warning('请输入名称');
      this.copyBusy = true;
      try {
        await this.copyPost('/api/copy/servers', {
          name: f.name.trim(), host: f.host.trim(), port: f.port,
          fa_group: f.fa_group.trim(), account_id: '',
          size_mode: f.size_mode, risk_usd: f.risk_usd,
          size_multiplier: f.size_multiplier,
          max_drawdown_pct: 3.0, direction: 'both',
        });
        this.$message.success(`已添加 ${f.name}（默认停用，启用后开始跟单）`);
        this.copyDialog = false;
      } catch (e) { this.$message.error(e.message || String(e)); }
      finally { this.copyBusy = false; }
    },

    // 失败订单 / 熔断操作 → cmd 接口（retry / dismiss / unhalt）
    async copyCmd(server, body, okText) {
      try {
        await this.copyPost(`/api/copy/servers/${encodeURIComponent(server)}/cmd`, body);
        this.$message.success(okText);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },
    copyRetry(f) { this.copyCmd(f.server, { retry: [f.master_lot_id], dismiss: [], unhalt: false }, '已提交重放'); },
    copyDismiss(f) { this.copyCmd(f.server, { retry: [], dismiss: [f.master_lot_id], unhalt: false }, '已忽略'); },
    copyUnhalt(s) { this.copyCmd(s.name, { retry: [], dismiss: [], unhalt: true }, '已解除熔断'); },

    // 连接状态灯：enabled=false 灰 / connected+process_alive 绿 / 其余红
    copyDotClass(s) {
      if (!s.enabled) return 'off';
      return (s.connected && s.process_alive) ? 'on' : 'bad';
    },
    // 回撤配色：已熔断红 / 达到熔断线 80% 琥珀 / 其余默认
    copyDdClass(s) {
      if (s.halted) return 'pnl-neg';
      const max = Number(s.max_drawdown_pct || 0);
      const dd = Number(s.drawdown_pct || 0);
      if (max > 0 && dd >= max * 0.8) return 'risk-warn';
      return '';
    },
    // follower 报错行整行淡红
    copyOrderRowClass({ row }) { return row.error ? 'copy-row-err' : ''; },

    isDecisionCollapsed(group, index) {
      return group.items.length > 1 && this.decisionCollapsed[index] !== false;
    },
    toggleDecisionGroup(index) {
      const current = this.decisionCollapsed[index] !== false;
      this.decisionCollapsed[index] = !current;
    },

    // 一次完成数据加载和独立模拟，不操作实盘或共享步进会话。
    wbCopySettings() {
      if (this.wbLoading) return;
      this.wbModel = this.autoModel;
      if (this.riskUsd > 0) this.wbRiskUsd = this.riskUsd;
      if (this.equity > 0) this.wbEquity = this.equity;
      this.wbAutoBreakeven = this.pyramidAutoBreakeven;
      this.wbBreakevenR = this.pyramidBreakevenR;
      this.wbAddUnlockR = this.pyramidAddUnlockR;
    },
    wbDirectionText(direction) {
      return direction === 1 ? '多头' : direction === -1 ? '空头' : '中性 / 未知';
    },
    wbRiskPlan(params) {
      return (params.risk_ratios || []).map(ratio => this.fmt2(ratio * params.initial_risk_usd)).join(' / $');
    },
    wbFocusTime(stamp) {
      const index = ((this.wb && this.wb.replay_bars) || []).findIndex(row => row.time === stamp);
      if (index >= 0) this.wbChartSelection = index;
    },
    wbReport() {
      return { format_version: 1, result: this.wb, manual_review: {
        expected: this.wbExpectationSnapshot || '', conclusion: this.wbReview || '未核对',
      } };
    },
    downloadText(filename, content, type) {
      const url = URL.createObjectURL(new Blob([content], { type }));
      const link = document.createElement('a'); link.href = url; link.download = filename;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    wbExportJson() {
      if (this.wb) this.downloadText(`backtest-${this.wb.strategy}-${this.wb.day}.json`, JSON.stringify(this.wbReport(), null, 2), 'application/json;charset=utf-8');
    },
    wbExportPrevious() {
      if (this.wbPrevious?.report) this.downloadText(`backtest-previous-${this.wbPrevious.day}.json`, JSON.stringify(this.wbPrevious.report, null, 2), 'application/json;charset=utf-8');
    },
    wbCsvCell(value) {
      let text = value == null ? '' : String(value);
      if (typeof value === 'string' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    },
    wbExportCsv() {
      if (!this.wb) return;
      const rows = [['策略模型', '执行配置', '标的', '交易日', '人工启动ET', '订单', '方向', '数量', '委托时间ET', '入场参考价', '初始止损', '成交时间ET', '成交价', '退出时间ET', '退出价', '退出原因', '盈亏美元', '实际R', '订单类型', '风险参考价']];
      for (const o of this.wb.orders) rows.push([this.wb.model_name, this.wb.execution_profile, this.wb.symbol, this.wb.day, this.wb.run_context.start_time,
        o.lot_id, o.side, o.qty, o.placed?.hm, o.placed?.limit, o.placed?.stop,
        o.filled?.hm, o.filled?.px, o.closed?.hm, o.closed?.px, o.closed?.reason, o.pnl, o.rr,
        o.entry_order_type || o.placed?.order_type || '', o.placed?.risk_reference]);
      this.downloadText(`backtest-orders-${this.wb.day}.csv`, '\uFEFF' + rows.map(row => row.map(this.wbCsvCell).join(',')).join('\r\n'), 'text/csv;charset=utf-8');
    },
    async wbRun() {
      if (this.wbLoading) return;
      this.wbError = '';
      if (!(this.wbSymbol || '').trim()) { this.wbError = '请输入回测标的'; return; }
      if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(this.wbStartTime || '')) {
        this.wbError = '请选择执行实例启动时间（美东时间）'; return;
      }
      if (!(this.wbRiskUsd > 0) || !(this.wbEquity > 0)) {
        this.wbError = '模拟初始资金和首仓风险预算必须大于 0'; return;
      }
      this.wbLoading = true;
      if (this.wb) this.wbPrevious = { model_name: this.wb.model_name, symbol: this.wb.symbol,
        day: this.wb.day, start_time: this.wb.run_context?.start_time, summary: { ...this.wb.summary }, report: this.wbReport() };
      this.wb = null;
      this.wbExpectationSnapshot = this.wbExpected;
      this.wbReview = '';
      this.wbChartSelection = 0;
      this.decisionCollapsed = {};
      try {
        const params = { initial_risk_usd: this.wbRiskUsd, equity: this.wbEquity };
        if (this.wbSelectedModel.execution_profile === 'ema9_legacy_pyramid') {
          Object.assign(params, { pyramid_auto_breakeven: this.wbAutoBreakeven,
            pyramid_breakeven_r: this.wbBreakevenR, pyramid_add_unlock_r: this.wbAddUnlockR });
        }
        const r = await fetch('/api/bt/run', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: this.wbSymbol.trim(), day: this.wbDay || '',
            start_time: this.wbStartTime, strategy: this.wbModel, params }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '加载失败');
        this.wb = d;
      } catch (e) {
        this.wbError = e.message || String(e);
      } finally {
        this.wbLoading = false;
      }
    },
    exitReasonText(r) {
      return { stop: '止损', trend_reverse: '趋势反向', eod: '收盘了结',
               eod_cancel: '收盘撤单', manual: '手动平仓' }[r] || r;
    },
    shadowTypeText(t) {
      return { pullback: '回踩', proposal: '策略候选', fvg_event: 'FVG事件',
               fvg_retest: 'FVG回踩', fvg_warn: 'FVG预警', breakout: '突破' }[t] || t || '回踩';
    },
    shadowOutcomeText(o) {
      return { hit_stop: '触止损', reached_2R: '到2R', eod_flat: '收盘了结',
               unfilled: '未成交' }[o] || o;
    },

    // 每日复盘报告（/api/shadow/report，缺省=当天，任意历史日可重算）
    async loadReport() {
      this.reportLoading = true;
      try {
        const d = this.reportDay || '';
        const r = await fetch(`/api/shadow/report?day=${encodeURIComponent(d)}`);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '加载失败');
        this.report = data;
      } catch (e) {
        this.report = null;
        this.$message.error(e.message || String(e));
      } finally {
        this.reportLoading = false;
      }
    },

    // 打开盘前检查时读取当前状态，避免展示已恢复的历史红项。
    async openPrecheck() {
      this.precheckVisible = true;
      await this.runPrecheck();
    },
    async runPrecheck() {
      this.precheckLoading = true;
      try {
        const r = await fetch('/api/precheck');
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '检查失败');
        this.precheck = data;
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.precheckLoading = false;
      }
    },
    precheckIcon(s) {
      return { ok: '🟢', bad: '🔴', neutral: '🟡', skip: '⚪' }[s] || '⚪';
    },
    fmt2(v) { return (v === null || v === undefined) ? '—' : Number(v).toFixed(2); },

    // 历史回测：指定标的 + 交易日，逐 bar 喂给回测引擎
    async runBacktest() {
      this.btLoading = true;
      this.btError = '';
      try {
        const sym = (this.btSymbol || 'NVDA').trim();
        const day = this.btDay || '';
        const r = await fetch(`/api/backtest?symbol=${encodeURIComponent(sym)}&day=${encodeURIComponent(day)}`);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '回测失败');
        this.btResult = d;
      } catch (e) {
        this.btResult = null;
        this.btError = e.message || String(e);
      } finally {
        this.btLoading = false;
      }
    },

    // epoch 秒 → 美东 HH:MM（bar 时间）
    hmET(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toLocaleTimeString('en-US',
        { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
    },

    vwapDisplayFallback(update, market, observedAt) {
      const progress = update?.progress;
      const time = progress?.time, total = progress?.total;
      if (!Number.isSafeInteger(time) || time <= 0 || !Number.isSafeInteger(total) || total <= 0
          || !Number.isFinite(observedAt) || progress.ready >= total) {
        this.vwapDisplayWait = null;
        return null;
      }
      if (!this.vwapDisplayWait || this.vwapDisplayWait.time !== time || observedAt < this.vwapDisplayWait.since) {
        this.vwapDisplayWait = {time, since: observedAt};
      }
      if (observedAt - this.vwapDisplayWait.since < 5 || time < (this.vwapDistributionFrame?.time ?? 0)) return null;
      const source = this.vwapDistributionSourceFrame;
      const sessions = this.status.market_session?.symbols || {};
      const expected = source?.rows?.length === total
        ? source.rows.map(row => row.symbol)
        : Object.keys(sessions).filter(symbol => typeof sessions[symbol]?.requested === 'string'
          && sessions[symbol].requested !== 'default');
      if (expected.length !== total || new Set(expected).size !== total) return null;
      expected.sort();
      const rows = [], delayed = [], auxiliary = [];
      for (const symbol of expected) {
        const row = market[symbol]?.vwap_1m;
        if (row?.time !== time || ![-1, 1].includes(row.st_direction)
            || !['position', 'vwap', 'upper', 'lower', 'ema'].every(key => Number.isFinite(row[key]))) {
          delayed.push(symbol);
          continue;
        }
        rows.push({...row, symbol});
        if (!Number.isFinite(row.speed)) auxiliary.push(symbol);
      }
      if (rows.length < Math.ceil(total * 0.9)) return null;
      // 这是显示层的容错画面；原始完整批次和交易行情保持各自的时间与版本。
      const key = JSON.stringify([time, rows, delayed, auxiliary]);
      if (this.vwapDistributionFrame?.display_key === key) return this.vwapDistributionFrame;
      return {time, revision: 0, rows, display_only: true, display_expected: total,
        display_delayed: delayed, display_auxiliary_waiting: auxiliary,
        display_updated_at: observedAt * 1000, display_key: key};
    },

    acceptVwapDistribution(update, market = {}, observedAt = Date.now() / 1000) {
      const frame = update?.frame;
      const previous = this.vwapDistributionSourceFrame
        || (this.vwapDistributionFrame?.display_only ? null : this.vwapDistributionFrame);
      const revision = frame?.revision === undefined ? 0 : frame.revision;
      const rows = frame?.rows;
      const total = update?.progress?.total;
      const complete = Array.isArray(rows) && rows.length > 0
        && rows.every(row => typeof row?.symbol === 'string' && row.symbol && row.time === frame.time)
        && new Set(rows.map(row => row.symbol)).size === rows.length
        && (!(total > 0) || rows.length === total);
      const valid = Number.isSafeInteger(frame?.time) && frame.time > 0
        && Number.isSafeInteger(revision) && revision >= 0 && complete;
      const newer = valid && (!previous || frame.time > previous.time
        || (frame.time === previous.time && revision > (previous.revision ?? 0)));
      if (newer) this.vwapDistributionSourceFrame = frame;
      const current = this.vwapDistributionFrame;
      const source = this.vwapDistributionSourceFrame;
      const candidate = this.vwapDisplayFallback(update, market, observedAt)
        || (newer || !current?.display_only || source?.time > current.time ? source : null);
      const changed = candidate && candidate !== current && (!current || candidate.time >= current.time);
      if (changed) this.vwapDistributionFrame = candidate;
      this.vwapDistributionProgress = update?.progress ?? null;
      return !!changed;
    },

    connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      this.ws = ws;
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'snapshot') {
          this.status = msg.status;
          if(Number.isFinite(msg.status?.max_symbol_position_pct)&&msg.status.max_symbol_position_pct>0)
            this.maxPositionPct=msg.status.max_symbol_position_pct;
          this.confirmedVwap = msg.confirmed_vwap || {version:0,rows:{}};
          this.entryCandidateViews = msg.entry_candidate_views || {};
          this.entryCandidates = msg.entry_candidates || {ready:false,reason:'等待候选榜单',rows:[]};
          this.confirmedTd = msg.confirmed_td || {version:0,rows:{}};
          if(!this.candidateProtectionSaving&&msg.status?.candidate_order_protection)
            this.candidateProtection={...msg.status.candidate_order_protection};
          if(!this.intelligentExitSaving&&msg.status?.intelligent_exit)
            this.intelligentExit={...msg.status.intelligent_exit};
          // 止损 ATR 倍数跟快照走（全客户端同步）；编辑中（dirty）不覆盖输入框
          if (!this.atrMultDirty && msg.status && msg.status.stop_atr_mult != null) {
            this.atrMult = msg.status.stop_atr_mult;
          }
          if (!this.pyramidParamsDirty && msg.status) {
            this.pyramidAutoBreakeven = msg.status.pyramid_auto_breakeven !== false;
            this.pyramidBreakevenR = msg.status.pyramid_breakeven_r ?? 3.0;
            this.pyramidAddUnlockR = msg.status.pyramid_add_unlock_r ?? 2.0;
          }
          // 盘前区间过滤开关跟快照走（全客户端同步）
          if (msg.status) {
            this.screenOpeningRangeFilter = !!msg.status.screen_opening_range_filter;
            this.screenPmFilter = !!msg.status.screen_premarket_filter;
            this.fvgPmFilter = !!msg.status.fvg_premarket_filter;
            // 盘前新闻徽标联动：hot ticker → gist（tooltip）
            this.newsHot = (msg.status.news && msg.status.news.hot) || {};
            this.liveNewsTickers = (msg.status.news && msg.status.news.live_tickers) || [];
            if (!this.imessageDirty) {
              this.imessageRecipient = msg.status.imessage_recipient || '';
            }
          }
          this.alerts = msg.alerts || [];   // 活跃告警（横幅 + 声音）
          this.checkAlerts();
          this.lots = msg.lots || [];
          this.lotsReady = true;
          this.checkHoldingRankChanges(msg.holding_rank_monitor);
          this.tvEntryMarkers = msg.tv_entry_markers || [];
          this.syncTvEntryMarkers();
          this.market = msg.market || {};
          this.checkIpoOpenings(msg.ipo_openings, msg.ts);
          this.acceptVwapDistribution(msg.vwap_distribution, this.market, msg.ts);
          this.openingVwapSignals = msg.vwap_opening_signals || [];
          if (!this.demoMode && !this.status.market_stale) this.rememberConfirmedGoldenFeedRows();
          this.logs = msg.logs || [];
          this.momentum = msg.momentum || { long: [], short: [], ready_count: 0 };
          this.screen = msg.screen || { long: [], short: [] };
          this.scanAlerts = msg.scan_alerts || [];
          this.stopAlerts = msg.stop_alerts || [];
          this.externalAlerts = msg.external_alerts || [];
          this.slowBreakAlerts = msg.slow_break_alerts || [];
          this.autoCloseNotices = msg.auto_close_notices || [];
          this.autoNotices = msg.auto_notices || [];
          this.fvgReverseAlerts = msg.fvg_reverse_alerts || [];
          this.fvgReverseNotices = msg.fvg_reverse_notices || [];
          this.shadow = msg.shadow || { stats: {}, events: [] };
          this.auto = msg.auto || { enabled: false, strategy: '', instances: [], proposals: [] };
          this.fvgMonitor = msg.fvg_monitor || { slots: [], retests: [] };
          this.breakoutSignals = msg.breakout_signals || [];   // 盘前突破信号（今日已确认）
          this.openingBreakoutCoverage = msg.opening_breakout_coverage || null;
          this.updateOpeningBreakouts(msg.opening_breakouts || []);
          this.clients = msg.clients || { pending: [], approved: [] };   // 客户端列表（master 审批条）
          this.copy = msg.copy || null;   // 跟单快照（缺段 = 后端未就绪，tab 容错提示）
          this.syncCopyDrafts();
          this.fillDefaults();
          this.checkScanAlert();
          this.checkBreakeven();
          this.checkSlowBreak();
          this.checkStopAlert();
          this.checkExternalAlert();
          this.checkAutoCloseNotice();
          this.checkAutoNotices();
          this.checkFvgReverseAlert();
          this.checkFvgReverseNotice();
          this.checkAutoEntryProposals();
          this.checkFvgEvents();
          this.checkFvgWarnings();
          this.checkFvgTick();
          this.checkBreakoutSignals();
          this.checkExternalEntryResults(msg.external_entry_results || [], msg.ts);
          const d = msg.status.bar_update_delay;
          if (d != null && d > this.maxDelay) this.maxDelay = d;
        }
      };
      ws.onclose = () => {
        if (!this.systemClosing && !this.systemClosed) {
          setTimeout(() => this.connectWS(), 2000);
        }
      };
      ws.onerror = () => ws.close();
    },

    sortScreenRows(rows) {
      const key = { divergence: 'divergence', duration: 'st_duration_bars', push: 'st_push_count', fvg: 'fvg_cum_count', slope: 'ema_slope_atr' }[this.screenSort];
      return [...rows].sort((a, b) => {
        const score = row => {
          const value = row[key];
          if (!Number.isFinite(value)) return -Infinity;
          return this.screenSort === 'slope' && row.side === 'short' ? -value : value;
        };
        const av = score(a);
        const bv = score(b);
        if (av !== bv) return bv - av;
        return (b.divergence ?? -1) - (a.divergence ?? -1);
      });
    },

    // 给空 slot 填默认标的
    fillDefaults() {
      // 单格默认标的跟随当前选股排序第一名（多空按当前排序方向）
      if (!this.slots[0].symbol && this.sortedScreenLong.length) {
        this.slots[0].symbol = this.sortedScreenLong[0].symbol;
      }
    },

    checkIpoOpenings(update, now=Date.now()/1000) {
      if(!update||!Array.isArray(update.watches)||!Array.isArray(update.events))return;
      this.ipoOpenings=update;
      if(this.demoMode||!Number.isFinite(now))return;
      const day=new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(now*1000));
      let changed=false;
      for(const event of update.events) {
        if(typeof event?.id!=='string'||!event.id||event.listing_date!==day
          ||!Number.isFinite(event.price)||event.price<=0||!Number.isFinite(event.detected_at)
          ||now<event.detected_at||now-event.detected_at>120||!Number.isFinite(event.announce_until)||now>event.announce_until
          ||!update.watches.some(w=>w.symbol===event.symbol&&w.listing_date===event.listing_date)
          ||this.notifiedIpoOpenings[event.id])continue;
        // Another console tab may already have announced while this tab was open.
        const saved=loadNotified('notified_ipo_openings')||{};
        if(saved[event.id]){this.notifiedIpoOpenings[event.id]=saved[event.id];continue;}
        this.notifiedIpoOpenings[event.id]=event.detected_at;changed=true;
        const ticker=this.tickerOf(event.symbol),price=event.price.toFixed(event.price<1?4:2);
        this.speak(`新股 ${ticker.split('').join(' ')} 已开始交易。首次收到成交价格 ${price} 美元。`,{queue:true,priority:true});
        this.$notify({title:'IPO 开交易',message:`${ticker} 已收到首个有效成交价 $${price}，美东 ${this.hmET(event.observed_at)}；这是数据源首次观察价。`,type:'success',duration:0});
      }
      if(changed){
        this.notifiedIpoOpenings=Object.fromEntries(Object.entries(this.notifiedIpoOpenings).sort((a,b)=>b[1]-a[1]).slice(0,100));
        saveNotified('notified_ipo_openings',this.notifiedIpoOpenings);
      }
    },
    testIpoVoice() {
      this.speak('新股开交易语音提醒已就绪，等待真实成交价格。',{queue:true,priority:true});
      this.$message.success('已加入语音队列，请确认浏览器与系统音量');
    },

    // 语音播报（Web Speech API）
    speak(text, { queue = false, priority = false } = {}) {
      let release;
      try {
        if (!('speechSynthesis' in window)) return;
        const speech = window.speechSynthesis;
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'zh-CN';
        u.rate = 1.1;
        if (!queue && !this.prioritySpeechCount) speech.cancel();
        if (priority) {
          this.prioritySpeechCount += 1;
          release = () => {
            if (!release) return;
            release = undefined;
            this.prioritySpeechCount -= 1;
          };
          u.onend = u.onerror = release;
        }
        speech.speak(u);
      } catch (e) { release?.(); }
    },

    checkHoldingRankChanges(monitor, now=this.now) {
      if (this.demoMode) return;
      const {state,outside,remind,stale}=rhythmHoldingRankChanges(this.holdingRankState,monitor,this.lots,now);
      this.holdingRankState=state;
      if(!outside.length) {
        this.holdingRankNotice?.close?.();
        this.holdingRankNotice=null;
        return;
      }
      if(!remind)return;
      const summary=({symbol,rank},spoken=false)=>{
        const ticker=this.tickerOf(symbol);
        const name=spoken?ticker.split('').join(' '):ticker;
        return rank?name+' 第 '+rank+' 名':name+' 无确认名次';
      };
      const age=stale?'沿用上次确认榜单，':'';
      const time=this.hmET(state.board.snapshot.as_of)+' ET 确认';
      this.speak('持仓仍在混合榜前十之外，'+age+outside.map(row=>summary(row,true)).join('，'),{queue:true,priority:true});
      this.holdingRankNotice?.close?.();
      this.holdingRankNotice=this.$notify({title:'持仓仍在前十之外',
        message:(stale?'沿用 ': '')+time+' · '+outside.map(row=>summary(row)).join('；'),
        type:'warning',duration:0,position:'bottom-right'});
    },

    speakStrategyFailure(action, symbols) {
      const tickers = symbols.map(symbol => this.tickerOf(symbol).split('').join(' ')).filter(Boolean);
      const target = tickers.length === 1 ? tickers[0] : tickers.length > 1 ? `${tickers.length} 个标的` : '';
      this.speak([target, `${action}，请检查`].filter(Boolean).join(' '), { priority: true });
    },

    // ST 确认转向只播报和通知，平仓仍由用户点击已有仓位操作。
    checkScanAlert() {
      for (const a of this.scanAlerts) {
        if (this.lastScanAlert[a.ticker] === a.scanned_at) continue;
        this.lastScanAlert[a.ticker] = a.scanned_at;
        const side = a.side === 'short' ? '空头' : '多头';
        const confirmed = a.confirmed_direction === 1 ? '多头' : a.confirmed_direction === -1 ? '空头' : (a.side === 'short' ? '多头' : '空头');
        const realtime = a.realtime_direction === 1 ? '多头' : a.realtime_direction === -1 ? '空头' : '未就绪';
        const time = a.confirmed_bar_time ? this.hmET(a.confirmed_bar_time) + '开盘K线' : '时间未记录';
        this.speak(`${a.ticker.split('').join(' ')} 持仓${side}，收盘确认ST${confirmed}，实时ST${realtime}，请检查`);
        this.$notify({
          title: 'ST方向提醒（收盘确认）', type: 'warning', duration: 0, position: 'bottom-right',
          message: `${a.ticker} 持仓${side} ${a.qty} 股；实时ST ${realtime}；收盘确认ST ${confirmed}（美东${time}），确认ST线 ${a.st_line ?? '—'}。仅提醒，由你决定是否处理。`,
        });
      }
    },

    // 止损单守卫：持仓缺少 STP/TRAIL 保护单 → 语音 + 弹窗（每分钟重复，直到补齐）
    checkStopAlert() {
      for (const a of this.stopAlerts) {
        const ts = a.checked_at;
        if (this.lastStopAlert[a.ticker] === ts) continue;   // 本次检查已提醒过
        this.lastStopAlert[a.ticker] = ts;
        const spelled = a.ticker.split('').join(' ');
        this.speak(`警告 ${spelled} 持仓没有止损单，请立即检查`);
        this.$notify({
          title: '⚠️ 止损单缺失（裸仓）',
          message: `${a.ticker} ${a.side === 'long' ? '多头' : '空头'} ${a.qty} 股，保护单仅 ${a.covered_qty} 股，请立即补挂止损！`,
          type: 'error', duration: 0, position: 'bottom-right',
        });
      }
    },

    // 外部订单/持仓告警：IBKR 手动下单/持仓被检测到 → 语音 + 常驻弹窗提醒人工处理
    // （告警模式：系统绝不自动市价平仓外部仓位；重复出现每 60s 重播一次）
    checkExternalAlert() {
      for (const a of this.externalAlerts) {
        const key = a.kind + ':' + a.ticker;
        if (this.now - (this.lastExternalAlert[key] || 0) < 60) continue;
        this.lastExternalAlert[key] = this.now;
        const spelled = (a.ticker || '').split('').join(' ');
        const what = a.kind === 'position'
          ? `${a.side === 'long' ? '多头' : '空头'}持仓 ${a.qty} 股`
          : `${a.side} ${a.qty} 股 ${a.order_type} 挂单`;
        this.speak(`提醒 ${spelled} 检测到手动${a.kind === 'position' ? '持仓' : '挂单'}，工具未接管，请人工确认`);
        this.$notify({
          title: '⚠️ 检测到 IBKR 手动订单/持仓',
          message: `${a.ticker} ${what}（非本工具订单，系统不会自动处理，请人工确认）`,
          type: 'warning', duration: 0, position: 'bottom-right',
        });
      }
    },

    checkExternalEntryResults(results, now) {
      if (!results.length) return;
      const storageKey = 'notified_external_entry_results';
      const seen = { ...this.lastExternalEntryResult, ...loadNotified(storageKey) };
      const spoken = [];
      for (const result of results) {
        if (!result.id || !result.symbol || seen[result.id]
            || !(result.ts > now - 60 && result.ts <= now)) continue;
        const ticker = result.symbol.split('').join(' ');
        if (result.kind === 'cancelled') spoken.push(`${ticker} 外部挂单已自动撤销`);
        else if (result.kind === 'closed') spoken.push(`${ticker} 外部持仓已自动平仓，成交 ${result.qty} 股`);
        else continue;
        seen[result.id] = result.ts;
      }
      if (!spoken.length) return;
      this.lastExternalEntryResult = Object.fromEntries(
        Object.entries(seen).filter(([, ts]) => ts > now - 60),
      );
      saveNotified(storageKey, this.lastExternalEntryResult);
      this.speak(spoken.join('。'), { queue: true });
    },

    // 自动平仓播报（提交与成交分开表达，避免把 CLOSING 当成已成交；持久化刷新不重播）
    checkAutoCloseNotice() {
      let dirty = false;
      for (const n of this.autoCloseNotices) {
        const ts = n.closed_at;
        if (this.lastAutoCloseNotice[n.ticker] === ts) continue;
        this.lastAutoCloseNotice[n.ticker] = ts;
        dirty = true;
        const spelled = n.ticker.split('').join(' ');
        this.speak(n.pending ? `${spelled} 已提交自动平仓，等待成交` : `${spelled} 已自动平仓`);
        this.$notify({
          title: n.pending ? '已提交自动平仓' : '已自动平仓',
          message: `${n.ticker} ${n.side === 'short' ? '空头' : '多头'} ${n.qty} 股，历史ST转向退出，ST线 ${n.st_line ?? '—'}，平仓价 ${n.close}${n.pending ? '，等待实际成交' : ''}`,
          type: n.pending ? 'warning' : 'success', duration: 30000, position: 'bottom-right',
        });
      }
      if (dirty) saveNotified('notified_autoclose', this.lastAutoCloseNotice);
    },

    // 策略执行通知：提交与成交分开播报，首屏不补播历史事件。
    checkAutoNotices() {
      let dirty = false;
      const fresh = [];
      for (const n of this.autoNotices) {
        if (!n.id || this.lastAutoNotice[n.id]) continue;
        this.lastAutoNotice[n.id] = n.ts || true;
        dirty = true;
        if (this.autoNoticesInitDone) fresh.push(n);
      }
      this.autoNoticesInitDone = true;
      fresh.sort((a, b) => (a.ts || 0) - (b.ts || 0));
      const spoken = [];
      const failures = [];
      for (const n of fresh) {
        const ticker = (n.symbol || '').split(':').pop();
        const spelled = ticker.split('').join(' ');
        const side = n.side === 'long' ? '多头' : n.side === 'short' ? '空头' : '';
        const action = (n.entries || 1) > 1 ? `第 ${n.entries} 次加仓` : '首次建仓';
        let title = '自动策略提醒';
        let text = `${spelled} 自动策略状态更新`;
        let message = ticker;
        let type = 'info';
        let duration = 10000;
        if (n.event === 'pyramid_order_placed') {
          title = `${action}已提交`;
          text = `${spelled} ${side}${action}已提交，等待成交`;
          message = `${ticker} ${side} ${n.qty || 0} 股，${this.entryOrderTypeText(n)}，Pine 参考价 ${n.entry_px}，止损 ${n.stop_px}，等待成交`;
        } else if (n.event === 'pyramid_entry_filled') {
          title = `${action}已成交`;
          text = `${spelled} ${side}${action}已成交`;
          message = `${ticker} ${side} ${n.qty || 0} 股，成交均价 ${n.entry_px}`;
          type = 'success';
        } else if (n.event === 'pyramid_order_failed') {
          title = '自动策略下单失败';
          text = `${spelled} 自动策略下单失败，请检查`;
          message = `${ticker}：${n.error || '下单失败，请检查日志'}`;
          type = 'error';
          duration = 0;
        } else if (n.event === 'pyramid_close_requested') {
          const ok = n.ok !== false;
          title = ok ? '整组平仓已提交' : '整组平仓提交失败';
          text = ok ? `${spelled} 整组平仓已提交，等待成交` : `${spelled} 整组平仓提交失败，请检查`;
          message = `${ticker} ${side} ${n.qty || 0} 股，原因 ${n.reason || '策略退出'}${ok ? '，等待成交' : ''}`;
          type = ok ? 'warning' : 'error';
          duration = ok ? 10000 : 0;
        } else if (['pyramid_trend_reverse_alert', 'auto_trend_reverse_alert'].includes(n.event)) {
          title = 'ST / 策略方向转向提醒';
          text = `${spelled} ${side}持仓方向已反向，请检查`;
          message = `${ticker} ${side} ${n.qty || 0} 股，确认方向已反向，现价 ${n.price ?? '—'}，请自行决定是否平仓`;
          type = 'warning';
          duration = 0;
        } else if (n.event === 'pyramid_flat') {
          title = '整组平仓已完成';
          text = `${spelled} 自动策略已完成平仓`;
          message = `${ticker} ${side}仓位已全部结束`;
          type = 'success';
        } else {
          continue;
        }
        (type === 'error' ? failures : spoken).push(text);
        this.$notify({ title, message, type, duration, position: 'bottom-right' });
      }
      if (failures.length) this.speak(failures.join('。'), { priority: true });
      if (spoken.length) this.speak(spoken.join('，'));
      if (dirty) saveNotified('notified_pyramid', this.lastAutoNotice);
    },

    // 反向 FVG 自动平仓播报（镜像自动平仓通知，持久化刷新不重播）
    checkFvgReverseNotice() {
      let dirty = false;
      for (const n of this.fvgReverseNotices) {
        const ts = n.closed_at;
        if (this.lastFvgReverseNotice[n.ticker] === ts) continue;
        this.lastFvgReverseNotice[n.ticker] = ts;
        dirty = true;
        const spelled = n.ticker.split('').join(' ');
        this.speak(`${spelled} 反向 F V G，浮亏 ${Math.abs(n.pnl || 0).toFixed(0)} 美元，已提交自动平仓`);
        this.$notify({
          title: '反向 FVG 自动平仓',
          message: `${n.ticker} ${n.side === 'long' ? '多头' : '空头'} ${n.qty} 股浮亏（${n.pnl}），反向缺口确认，平仓价 ${n.price}，等待实际成交`,
          type: 'warning', duration: 30000, position: 'bottom-right',
        });
      }
      if (dirty) saveNotified('notified_fvg_reverse', this.lastFvgReverseNotice);
    },

    // FVG 转向仅通知；任何保本/平仓均由用户在对应批次手动确认。
    checkFvgReverseAlert() {
      for (const a of this.fvgReverseAlerts) {
        const key = `${a.lot_id}:${a.alerted_at}`;
        if (this.lastFvgReverseAlert[key]) continue;
        this.lastFvgReverseAlert[key] = true;
        const side = a.side === 'long' ? '多头' : '空头';
        this.speak(`${(a.ticker || '').split('').join(' ')} 出现反向 F V G，请确认是否处理持仓`);
        this.$notify({
          title: '反向 FVG：等待人工决定', type: 'warning', duration: 0, position: 'bottom-right',
          message: `${a.ticker} ${side} ${a.qty} 股，当前浮动盈亏 $${a.pnl}。系统仅提醒；如需处理，请在对应订单批次明确确认保本或平仓。`,
        });
      }
    },

    // 开仓候选：按 proposal_id 去重，同批候选只播报一次汇总提醒
    checkAutoEntryProposals() {
      const proposals = this.auto.proposals || [];
      const fresh = proposals.filter(p => !this.lastAutoEntryProposals[p.proposal_id]);
      if (!fresh.length) return;
      for (const p of fresh) this.lastAutoEntryProposals[p.proposal_id] = p.created_at;
      const tickers = fresh.map(p => p.symbol.split(':').pop()).join('、');
      this.speak(`发现 ${fresh.length} 个开仓候选，请确认`);
      this.$notify({
        title: `发现 ${fresh.length} 个开仓候选`,
        message: `${tickers}。请到「自动策略」比较趋势与仓位后确认或忽略。`,
        type: 'warning', duration: 0, position: 'bottom-right',
      });
    },

    // FVG 确认事件保留在监控 Tab 和语音通报，不重复进入右侧信号栏
    checkFvgEvents() {
      if (this.fvgFeed.some(c => !c.warn && c.kind !== 'breakout')) {
        this.fvgFeed = this.fvgFeed.filter(c => c.warn || c.kind === 'breakout');
      }
      const slots = (this.fvgMonitor && this.fvgMonitor.slots) || [];
      const all = [];
      for (const s of slots) all.push(...(s.long || []), ...(s.short || []));
      const fresh = all.filter(ev => !this.fvgSeenIds[ev.id]);
      if (!fresh.length) return;
      for (const ev of fresh) {
        this.fvgSeenIds[ev.id] = true;
        this.fvgFreshIds[ev.id] = this.now;
      }
      if (!this.fvgInitDone) {   // 首屏只建基线，不补报缓冲内历史事件
        this.fvgInitDone = true;
        return;
      }
      fresh.sort((a, b) => (b.ratio || 0) - (a.ratio || 0));
      const minRatio = this.fvgNotifyMinRatio;
      const notifiable = fresh.filter(ev => ev.ratio == null || ev.ratio >= minRatio);
      const voiced = notifiable.filter(ev => ev.ratio != null && ev.ratio >= minRatio);
      if (voiced.length) {
        const parts = voiced.map(ev =>
          `${ev.ticker.split('').join(' ')} ${ev.side === 'long' ? '多' : '空'}头 F V G，${ev.ratio.toFixed(1)} 倍 A T R`);
        this.speak(`新 F V G 缺口，${parts.join('，')}`);
      }
    },

    // FVG 预警通报：实时值（periods[0]）在第三根 K 线形成过程中即提醒，
    // 每 bar 每方向仅一次；同样受 ATR 阈值过滤，首屏仅建基线
    checkFvgWarnings() {
      const warnings = (this.fvgMonitor && this.fvgMonitor.warnings) || [];
      const fresh = warnings.filter(w => !this.fvgWarnSeenIds[w.id]);
      if (!fresh.length) return;
      for (const w of fresh) this.fvgWarnSeenIds[w.id] = true;
      if (!this.fvgWarnInitDone) {   // 首屏只建基线
        this.fvgWarnInitDone = true;
        return;
      }
      const minRatio = this.fvgNotifyMinRatio;
      const notifiable = fresh.filter(w => w.ratio == null || w.ratio >= minRatio);
      this.pushFvgFeed(notifiable, true);   // 预警也进右侧信号栏（带「形成中」徽标）
      const voiced = notifiable.filter(w => w.ratio != null && w.ratio >= minRatio);
      if (voiced.length) {
        const parts = voiced.map(w =>
          `${w.ticker.split('').join(' ')} ${w.side === 'long' ? '多' : '空'}头`);
        this.speak(`F V G 预警，${parts.join('，')}缺口形成中`);
      }
    },

    // FVG 信号栏（右侧常驻）：仅推入预警，ATR 倍数降序展示；确认事件在监控 Tab 查看
    pushFvgFeed(evs, warn) {
      for (const ev of evs) {
        this.fvgFeed.push({
          id: ev.id, side: ev.side, ticker: ev.ticker, symbol: ev.symbol,
          bottom: ev.bottom, top: ev.top, ratio: ev.ratio,
          warn, bar_time: ev.bar_time || 0, ts: this.now,
        });
      }
    },

    // 盘前突破信号通报（多空双出）：未见过的 id → 进右侧信号栏（「突破」/
    // 「跌破」卡片，固定排最上）+ 语音播报；首屏只建基线不补报（与 FVG 同口径）
    checkBreakoutSignals() {
      const fresh = this.breakoutSignals.filter(s => !this.breakoutSeenIds[s.id]);
      if (!fresh.length) return;
      for (const s of fresh) this.breakoutSeenIds[s.id] = true;
      if (!this.breakoutInitDone) {
        this.breakoutInitDone = true;
        return;
      }
      for (const s of fresh) {
        this.fvgFeed.push({
          id: s.id, kind: 'breakout',
          side: s.side === '跌破低点' ? 'short' : 'long', ticker: s.ticker,
          symbol: s.symbol,
          confirm_time: s.confirm_time, confirm_epoch: s.confirm_epoch || 0,
          break_time: s.break_time, close: s.close,
          pmh: s.premarket_high, pml: s.premarket_low, zone_width: s.zone_width,
          crowd: s.crowd, triple: s.triple, ts: this.now,
        });
      }
      const parts = fresh.map(s =>
        `${s.ticker.split('').join(' ')} ${s.side === '跌破低点' ? '跌破盘前低点' : '站上盘前高点'}`);
      this.speak(`盘前突破，${parts.join('，')}`);
    },
    updateOpeningBreakouts(rows) {
      const previous = new Map(this.openingBreakouts.map(row => [row.id, row.status]));
      this.openingBreakouts = rows;
      if (!this.openingBreakoutsInitialized) {
        this.openingBreakoutsInitialized = true;
        return;
      }
      const fresh = rows.filter(row => {
        const before = previous.get(row.id);
        if (row.status === 'preview') return before === undefined;
        if (row.status === 'confirmed') return before !== 'confirmed';
        return ['submitted', 'submission_failed'].includes(row.status) && before !== row.status;
      });
      if (!fresh.length) return;
      const failed = fresh.filter(row => row.status === 'submission_failed');
      if (failed.length) this.speakStrategyFailure('开盘小区间突破下单失败', failed.map(row => row.ticker));
      const summaries = fresh.map(row => `${row.ticker} ${row.side === 'long' ? '上破' : '下破'} ${this.openingBreakoutStatus(row)}`);
      this.$notify({
        title: '开盘小区间突破',
        message: summaries.slice(0, 5).join(' · ') + (fresh.length > 5 ? ` · 共 ${fresh.length} 个，查看小区间突破页` : ''),
        type: 'info', duration: 8000, position: 'bottom-right',
      });
    },
    toggleOpeningFeed() {
      this.openingFeedCollapsed = !this.openingFeedCollapsed;
      this.openingFeedPreferenceSet = true;
      try { localStorage.setItem('opening_feed_collapsed', this.openingFeedCollapsed ? '1' : '0'); } catch (_) { /* presentation only */ }
      if (!this.openingFeedCollapsed) this.$nextTick(() => this.scrollOpeningFeedToLatest());
    },
    scrollOpeningFeedToLatest() {
      const feed = this.$refs.openingFeed;
      if (feed) feed.scrollLeft = 0;
    },
    startOpeningFeedDrag(event) {
      const feed = event.currentTarget;
      if (event.pointerType !== 'mouse' || event.button !== 0
          || event.target.closest('button, a') || feed.scrollWidth <= feed.clientWidth
          || event.clientY >= feed.getBoundingClientRect().top + feed.clientHeight) return;
      feed.setPointerCapture(event.pointerId);
      this.openingFeedDrag = {
        pointerId: event.pointerId, startX: event.clientX, scrollLeft: feed.scrollLeft,
      };
    },
    moveOpeningFeedDrag(event) {
      const drag = this.openingFeedDrag;
      if (drag?.pointerId !== event.pointerId) return;
      event.currentTarget.scrollLeft = drag.scrollLeft + drag.startX - event.clientX;
    },
    endOpeningFeedDrag(event) {
      if (this.openingFeedDrag?.pointerId === event.pointerId) this.openingFeedDrag = null;
    },
    dismissFvgFeed(id) {
      this.fvgFeed = this.fvgFeed.filter(c => c.id !== id);
    },

    // 五分钟滴答提醒：数据时钟（status.data_time，bar 收盘时前进）每前进一次
    // 播一声短促滴答（WebAudio 合成，不依赖音频文件）；首次加载仅建基线不播
    checkFvgTick() {
      const dt = this.status.data_time;
      if (!dt) return;
      if (this.lastFvgDataTime == null) {
        this.lastFvgDataTime = dt;
        return;
      }
      if (dt !== this.lastFvgDataTime) {
        this.lastFvgDataTime = dt;
        if (this.fvgTickOn) this.playTick();
      }
    },
    playTick() {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this._audioCtx = this._audioCtx || new AC();
        const ctx = this._audioCtx;
        if (ctx.state === 'suspended') ctx.resume();
        // 3 秒钟声：基频 + 两个泛音，指数衰减 3 秒，柔和但持续可闻
        const partials = [[660, 0.20], [990, 0.10], [1320, 0.05]];
        for (const [freq, vol] of partials) {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = 'sine';
          osc.frequency.value = freq;
          gain.gain.setValueAtTime(vol, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 3.0);
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.start();
          osc.stop(ctx.currentTime + 3.0);
        }
      } catch (e) { /* 忽略音频失败 */ }
    },
    toggleFvgTick() {
      this.fvgTickOn = !this.fvgTickOn;
      try { localStorage.setItem('fvg_tick_on', this.fvgTickOn ? '1' : '0'); } catch (e) { /* 忽略 */ }
      if (this.fvgTickOn) this.playTick();   // 打开时试听一声
    },
    saveFvgNotifyMinRatio() {   // ATR 通报阈值持久化
      try { localStorage.setItem('fvg_notify_min_ratio', String(this.fvgNotifyMinRatio)); } catch (e) { /* 忽略 */ }
    },

    // FVG 展示辅助
    fvgAgo(ts) {
      if (!ts) return '';
      const m = Math.max(0, Math.round((this.now - ts) / 60));
      return m <= 0 ? '刚刚' : m + ' 分钟前';
    },
    fvgIsFresh(ev) {   // 会话内新到的事件高亮 5 分钟
      const t = this.fvgFreshIds[ev.id];
      return t != null && (this.now - t) < 300;
    },
    fvgRatioClass(r) {   // 4 档配色：<0.5 灰 / 0.5~1 蓝 / 1~2 琥珀 / >2 红渐变
      if (r == null || r < 0.5) return 'r-lo';
      if (r < 1) return 'r-mid';
      if (r <= 2) return 'r-hi';
      return 'r-xhi';
    },
    fvgRatioWidth(r) {   // 条形宽度：3×ATR 满格
      if (r == null || r <= 0) return 0;
      return Math.min(100, Math.round(r / 3 * 100));
    },
    fvgStateText(s) {
      return { open: '未回踩', retest: '回踩中', filled: '已回补' }[s] || '未回踩';
    },

    // 追价按钮可见条件：现价与缺口边缘（多头看上沿 top / 空头看下沿 bottom）的距离
    // ≤ 追价阈值（status.fvg_chase_max_ratio，默认 0.5）× 缺口 ATR；不满足直接不渲染
    fvgCanChase(ev) {
      if (!ev || ev.atr == null || ev.atr <= 0) return false;
      const bar = this.market[ev.symbol] && this.market[ev.symbol].bar;
      const close = bar && bar.close;
      if (close == null || close <= 0) return false;
      const maxRatio = this.status.fvg_chase_max_ratio != null ? this.status.fvg_chase_max_ratio : 0.5;
      const edge = ev.side === 'long' ? ev.top : ev.bottom;
      if (edge == null) return false;
      return Math.abs(close - edge) <= maxRatio * ev.atr;
    },

    // FVG 追价下单：限价 = 现价 ±0.5%（与服务端 ENTRY_LIMIT_PREMIUM_PCT 一致，保证快速成交），
    // 止损 = 现价 ∓ atrMult×ATR（锚定预期成交价而非含溢价限价，与服务端一致；atrMult = 全局止损倍数），
    // 股数按以损定量；服务端会按实时价重新定价，浏览器价仅供参考
    async chaseFvg(ev, force = false, forceReason = '') {
      if (this.placing && !force) return;
      const bar = this.market[ev.symbol] && this.market[ev.symbol].bar;
      const close = bar && bar.close;
      if (!close || close <= 0) return this.$message.warning('无实时行情，无法追价');
      const ticker = ev.ticker || this.tickerOf(ev.symbol);
      const isLong = ev.side === 'long';
      const limit = +(close * (isLong ? 1.005 : 0.995)).toFixed(2);
      // 止损 = 智能止损口径（smart_stop 指标优先）：现价 ∓ 距离（多−/空+）
      const symData = this.market[ev.symbol] || {};
      const dist = this.smartStopDist(symData, ev.side, close, ev.atr);
      const opening = this.openingManualStop(bar, ev.side);
      const stop = opening ? opening.price : +(isLong ? close - dist : close + dist).toFixed(2);
      if (!(stop > 0 && (isLong ? stop < close : stop > close))) {
        return this.$message.warning('当前行情无法形成有效止损，暂不能追价');
      }
      // 以损定量用现价（预期成交价）为参考价，溢价限价不进风险口径（与服务端一致）
      const pos = calcPosition(this.equity, this.riskUsd, close, stop, this.maxPositionPct, this.status.max_order_qty || 0);
      if (!pos.qty) return this.$message.warning('以损定量结果为 0 股，无法下单');
      this.placing = true;
      try {
        if (!force) {
          try {
            await this.$confirm(
              `市价追${isLong ? '多' : '空'} ${ticker} ×${pos.qty} 股 @≈${limit}，止损 ${stop}（风险 $${pos.risk}），确认下单？`,
              'FVG 追价确认', { type: 'warning' });
          } catch { return; }
        }
        const body = {
          symbol: ev.symbol, side: ev.side, qty: pos.qty,
          limit_px: limit, stop_px: stop,
          limit_type: 'chase',
          reason: force && forceReason ? `FVG追价（执意开仓：${forceReason}）` : 'FVG追价',
          force,
          max_position_pct: this.maxPositionPct,
        };
        if (!await this.confirmSlowMarketEntry(body)) return;
        const r = await fetch('/api/lots/limit-stop', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify(body),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) {
          // 未通过多头/空头过滤 → 二次确认：必须填写开仓原因才能执意开仓（沿用 placeOrder 的 force 模式）
          if (this.isNotLongCandidate(data.detail)) {
            const reasonText = await this.askForceReason(ticker, ev.side);
            if (reasonText == null) return;   // 取消：不开仓
            return await this.chaseFvg(ev, true, reasonText);
          }
          throw new Error(typeof data.detail === 'string' ? data.detail : (data.detail && data.detail.msg) || '下单失败');
        }
        const lot = data.lot;
        this.$message.success(`已追价下单 ${lot.lot_id}（${lot.symbol} ${lot.side} x${lot.qty} @${lot.entry_px}，止损 ${lot.stop_px}）`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.placing = false;
      }
    },

    // 全局止损 ATR 倍数：改动即 POST 服务端（settings.json 持久化），快照再同步回所有客户端
    async saveAtrMult(v) {
      if (v == null) return;   // 清空输入触发的 null 不提交
      try {
        const r = await fetch('/api/settings', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ stop_atr_mult: v }),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '保存失败');
        this.atrMult = data.stop_atr_mult;
        this.$message.success(`止损 ATR 倍数已更新为 ${data.stop_atr_mult}×（全局生效）`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    // 金字塔加仓解锁与自动保本参数：服务端持久化并立即应用到实时策略
    async savePyramidParam(key, value) {
      if (value == null) return;
      try {
        const body = {}; body[key] = value;
        const r = await fetch('/api/settings', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify(body),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '保存失败');
        this.pyramidAutoBreakeven = data.pyramid_auto_breakeven;
        this.pyramidBreakevenR = data.pyramid_breakeven_r;
        this.pyramidAddUnlockR = data.pyramid_add_unlock_r;
        this.$message.success('自动策略参数已更新（实时策略与后续回测生效）');
      } catch (e) {
        if (key === 'pyramid_auto_breakeven') this.pyramidAutoBreakeven = !value;
        this.$message.error(e.message || String(e));
      }
    },

    // 盘前区间过滤开关（选股 / FVG 监控）：改动即 POST 服务端（settings.json
    // 持久化，全客户端同步），下一帧快照生效；失败回滚本地勾选
    async savePmFilter(key, v) {
      try {
        const body = {}; body[key] = v;
        const r = await fetch('/api/settings', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify(body),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '保存失败');
        const label = key === "screen_opening_range_filter" ? "小区间过滤" : "盘前区间过滤";
        this.$message.success(`${label}已${v ? '开启' : '关闭'}（全局生效）`);
      } catch (e) {
        if (key === 'screen_opening_range_filter') this.screenOpeningRangeFilter = !v;
        if (key === 'screen_premarket_filter') this.screenPmFilter = !v;
        if (key === 'fvg_premarket_filter') this.fvgPmFilter = !v;
        this.$message.error(e.message || String(e));
      }
    },

    // iMessage 告警收件人：改动即 POST 服务端（空串=清除，跳过该通道）
    async saveImessage(v) {
      if (v == null) return;
      try {
        const r = await fetch('/api/settings', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ imessage_recipient: v }),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '保存失败');
        this.imessageRecipient = data.imessage_recipient || '';
        this.$message.success(data.imessage_recipient
          ? `iMessage 告警收件人已更新（全局生效）` : 'iMessage 告警通道已关闭');
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    // ---- 告警横幅 + 声音通报（后端 Notifier 快照驱动） ----
    // 新告警：语音 + 常驻弹窗；服务端解除：语音播报「已解除」（手动消除过的不播）；
    // 告警消失后清空本地标记，再次触发会重新显示/播报
    checkAlerts() {
      const cur = {};
      for (const a of this.alerts) {
        cur[a.key] = a;
        if (this.seenAlertKeys[a.key]) continue;
        this.seenAlertKeys[a.key] = a;
        this.speak(`${a.title}，${a.msg}`);
        this.$notify({
          title: a.title, message: a.msg,
          type: a.level === 'critical' ? 'error' : (a.level === 'warning' ? 'warning' : 'info'),
          duration: 0, position: 'bottom-right',
        });
      }
      for (const k of Object.keys(this.seenAlertKeys)) {
        if (cur[k]) continue;
        const a = this.seenAlertKeys[k];
        delete this.seenAlertKeys[k];
        if (this.dismissedAlerts[k]) {
          delete this.dismissedAlerts[k];   // 已解除：复位消除标记
        } else {
          this.speak(`${a.title}，已解除`);
        }
      }
    },
    dismissAlert(key) {
      this.dismissedAlerts = { ...this.dismissedAlerts, [key]: true };
    },

    async closePosition(symbol) {
      try {
        const r = await fetch(`/api/positions/${encodeURIComponent(symbol)}/close`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '平仓失败'); }
        const d = await r.json();
        this.$message.success(`已提交处理 ${d.symbol}（${d.side} x${d.qty}），等待实际成交`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    // 保本检测：止损移到成本时语音播报（false→true 去重，持久化刷新不重播）
    checkBreakeven() {
      let dirty = false;
      let exitDirty = false;
      for (const l of this.lots) {
        if(l.state==='CLOSED'&&['candidate_timed_breakeven_stop','intelligent_timed_breakeven'].includes(l.exit_reason)&&!this.lastTimedExit[l.lot_id]){
          const ticker=(l.symbol||'').split(':').pop();
          const minutes=l.signal_snapshot?.intelligent_exit?.last_decision?.breakeven_minutes??l.signal_snapshot?.candidate_order_protection?.breakeven_minutes_applied;
          this.speak(`${ticker.split('').join(' ')} 持仓${minutes}分钟，已按保本规则平仓`);
          this.$notify({title:'定时保本退出',message:`${ticker} 到期时价格已越过成本，已按规则平仓，实际盈亏见订单批次`,type:'warning',duration:10000,position:'bottom-right'});
          this.lastTimedExit[l.lot_id]=true;exitDirty=true;
        }
        if (l.breakeven_reached && !this.lastBreakeven[l.lot_id]) {
          dirty = true;
          const ticker = (l.symbol || '').split(':').pop();
          const spelled = ticker.split('').join(' ');
          const smart=l.signal_snapshot?.intelligent_exit;
          const protection=l.signal_snapshot?.candidate_order_protection;
          const timed=smart?.timed_breakeven_done||protection?.breakeven_trigger==='timed_breakeven'&&protection.timed_breakeven_done;
          const minutes=smart?.breakeven_minutes??protection?.breakeven_minutes_applied;
          this.speak(timed?`${spelled} 持仓${minutes}分钟，已自动保本`:`${spelled} 已保本，止损已至成本或更有利`);
          this.$notify({
            title: timed?'定时自动保本':'保本',
            message: `${ticker} ${timed?'持仓'+minutes+'分钟，已自动保本':'已保本'}（止损已至成本或更有利），剩余风险为 0`,
            type: 'success', duration: 10000, position: 'bottom-right',
          });
        }
        this.lastBreakeven[l.lot_id] = !!l.breakeven_reached;
      }
      if (dirty) saveNotified('notified_breakeven', this.lastBreakeven);
      if (exitDirty) saveNotified('notified_candidate_timed_exit',this.lastTimedExit);
    },

    // 未快速突破提醒：开仓超时未保本 → 语音播报「是否减仓」
    checkSlowBreak() {
      let dirty = false;
      for (const a of this.slowBreakAlerts) {
        if (this.lastSlowBreak[a.lot_id]) continue;
        this.lastSlowBreak[a.lot_id] = true;
        dirty = true;
        const ticker = a.ticker || (a.symbol || '').split(':').pop();
        const spelled = ticker.split('').join(' ');
        const mins = Math.round((a.elapsed_seconds || 0) / 60);
        this.speak(`${spelled} 开仓 ${mins} 分钟未保本，未快速突破，是否减仓`);
        this.$notify({
          title: '未快速突破',
          message: `${ticker} 开仓 ${mins} 分钟未保本，请评估是否减仓`,
          type: 'warning', duration: 30000, position: 'bottom-right',
        });
      }
      if (dirty) saveNotified('notified_slowbreak', this.lastSlowBreak);
    },

    // 记录 el-select 实例引用
    setSelectRef(el, i) {
      if (el) this.selectRefs[i] = el;
    },

    // 回车：未在列表中选择时，自动切换为匹配的第一个标的
    confirmSelect(i) {
      const sel = this.selectRefs[i];
      const q = (sel && sel.query ? String(sel.query) : '').trim().toUpperCase();
      if (!q) return;
      const matches = this.manualSymbols.filter((sym) => {
        const t = sym.split(':').pop().toUpperCase();
        return t.startsWith(q) || t.includes(q);
      });
      if (matches.length) {
        this.slots[i].symbol = matches[0];
      }
    },

    // 点选横截面选股结果：单格交易台直接填入选中标的（方向由按钮按趋势自适应）；
    // 所有单标的切图保留同窗口 TV 的固定第六格，失败时显示原因。
    pickScreen(r) {
      this.slots[0].candidateRanking='trend';this.slots[0].candidateView='mixed';
      this.slots[0].symbol = r.symbol;
      this.$message.success(`已选中 ${r.ticker}`);
      this.tvShow(r.symbol);
    },

    selectVwapStock(symbol) {
      if (!symbol) return;
      this.pickScreen({ symbol, ticker: this.tickerOf(symbol) });
    },

    selectRhythmStock(symbol,options={}) {
      this.slots[0].candidateView=options.view||'mixed';
      if (symbol) {this.slots[0].candidateRanking='trend';this.slots[0].symbol=symbol;}
    },

    manageRhythmPosition(symbol) {
      if(!symbol)return;
      this.slots[0].symbol=symbol;this.orderSymbolFilter=symbol;this.orderKindFilter='all';this.deckCollapsed=true;
    },

    sameOrderSymbol(a,b) {
      if(a===b)return true;
      const stock=s=>typeof s==='string'&&(!s.includes(':')||['NASDAQ','NYSE','AMEX','BATS'].includes(s.split(':')[0]));
      return stock(a)&&stock(b)&&a.split(':').pop()===b.split(':').pop();
    },

    async syncTvEntryMarkers(force=false) {
      if(this.identity!=='ok'||document.visibilityState==='hidden'||this.tvGridInFlight||this.tvMarkersBusy)return;
      const entries=this.tvEntryMarkers||[],key=JSON.stringify(entries);
      if(!force&&key===this.tvMarkersAttemptKey&&(this.tvMarkersRetries>=4||Date.now()<this.tvMarkersRetryAt))return;
      if(force||key!==this.tvMarkersAttemptKey){this.tvMarkersRetries=0;this.tvMarkersRetryAt=0;}
      this.tvMarkersAttemptKey=key;this.tvMarkersBusy=true;
      try {
        let result;
        if(this.traderName==='master') {
          if(!this.status.tv_entry_markers)throw Error('成交标记需重启控制台加载');
          const token=Array.from(window.crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
          const dataset=document.documentElement.dataset;dataset.autotradeTvViewToken=token;
          try{
            const response=await fetch('/api/tv/entry-markers',{method:'POST',headers:this.tradeHeaders(true),body:JSON.stringify({source_token:token})});
            result=await response.json();if(!response.ok)throw Error('成交标记请求失败');
          }finally{if(dataset.autotradeTvViewToken===token)delete dataset.autotradeTvViewToken;}
        }else {
          const results=[];
          for(const layout of ['candidates','focus'])results.push(await this.requestTvBridge({id:'marks-'+layout+'-'+Date.now(),type:'mark-entries',entries,layout},10000));
          result={ok:results.every(r=>r?.ok),marked:results.reduce((n,r)=>n+(r?.marked||0),0),reason:results.find(r=>!r?.ok)?.reason};
        }
        if(!result?.ok)throw Error(result?.reason||result?.failures?.[0]?.reason||'原生成交标记未确认');
        this.tvMarkerStatus=entries.length?'已显示'+result.marked+'组成本/止损线':'当前空仓';
        this.tvMarkersRetryAt=Infinity;
      }catch(error){
        this.tvMarkerStatus=error.message||'仓位线段暂不可用';
        this.tvMarkersRetries++;this.tvMarkersRetryAt=Date.now()+Math.min(15000,2**this.tvMarkersRetries*1000);
      }
      finally{
        this.tvMarkersBusy=false;
        const pending=this.tvGridPending;this.tvGridPending=null;
        if(pending)this.syncRhythmGrid(pending);
        else if(JSON.stringify(this.tvEntryMarkers||[])!==key)this.syncTvEntryMarkers();
      }
    },

    async syncRhythmGrid({symbols=[],focusSymbols=[],force=false}) {
      if(this.identity!=='ok'||this.bottomTab!=='rhythm'||document.visibilityState==='hidden')return;
      const jobs=[{layout:'candidates',symbols,cells:8,label:'八格'},
        {layout:'focus',symbols:focusSymbols,cells:6,label:'六格'}]
        .filter(job=>Array.isArray(job.symbols)&&job.symbols.length===job.cells&&job.symbols.every(Boolean));
      if(!jobs.length)return;
      const key=jobs.map(j=>j.layout+':'+j.symbols.join(',')).join('|');
      if(this.tvGridInFlight||this.tvMarkersBusy){this.tvGridPending={symbols:[...symbols],focusSymbols:[...focusSymbols],force};return;}
      if(!force&&key===this.tvGridLastKey)return;
      if(!force&&key===this.tvGridFailedKey&&(this.tvGridRetries>=6||Date.now()<this.tvGridRetryAt))return;
      if(force||key!==this.tvGridFailedKey){this.tvGridRetries=0;this.tvGridRetryAt=0;}
      this.tvGridInFlight=true;this.tvGridStatus='正在逐格核对TV双窗口';
      this.tvGridLastKey=''; // A partial switch also invalidates the previous arrangement.
      const messages=[];let failed=false,token='';
      const dataset=document.documentElement.dataset;
      try {
        if(this.traderName==='master') {
          token=Array.from(window.crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
          dataset.autotradeTvViewToken=token;
        }
        for(const job of jobs) {
          const layoutKey=job.symbols.join(',');
          if(!force&&this.tvGridLayoutKeys[job.layout]===layoutKey){messages.push(job.label+'已确认');continue;}
          // A failed or unknown result invalidates only the layout actually touched.
          delete this.tvGridLayoutKeys[job.layout];
          try {
            let result;
            if(this.traderName==='master') {
              if(!this.status.tv_grid_same_window||this.status.tv_grid_layouts?.[job.layout]!==job.cells)
                throw Error('需重启控制台加载TV双窗口联动');
              const response=await fetch('/api/tv/grid',{method:'POST',headers:this.tradeHeaders(true),
                body:JSON.stringify({symbols:job.symbols,layout:job.layout,source_token:token})});
              const data=await response.json();result=response.ok?data:{ok:false,reason:typeof data.detail==='string'?data.detail:'联动失败'};
            }else result=await this.requestTvBridge({id:'grid-'+job.layout+'-'+Date.now(),type:'switch-grid',symbols:job.symbols,layout:job.layout},9000);
            if(!result?.ok||result.panes?.length!==job.cells||!result.panes.every(p=>p.ok)) {
              if(result?.upgrade){this.tvExtensionState='update';this.tvSetupVisible=true;}
              throw Error(result?.reason||job.label+'尚未全部确认');
            }
            this.tvGridLayoutKeys[job.layout]=layoutKey;
            messages.push(job.label+'已确认');
          }catch(error){failed=true;messages.push(job.label+'：'+(error.message||'联动失败，点同步重试'));}
        }
        this.tvGridStatus=messages.join(' · ');
        if(failed){this.tvGridFailedKey=key;this.tvGridRetries++;this.tvGridRetryAt=Date.now()+Math.min(30000,2**this.tvGridRetries*1000);}
        else{this.tvGridLastKey=key;this.tvGridFailedKey='';this.tvGridRetries=0;}
      }finally{
        if(token&&dataset.autotradeTvViewToken===token)delete dataset.autotradeTvViewToken;
        this.tvGridInFlight=false;const pending=this.tvGridPending;this.tvGridPending=null;
        if(pending&&this.bottomTab==='rhythm')this.syncRhythmGrid(pending);else this.syncTvEntryMarkers(true);
      }
    },

    openRhythmTrade({symbol,frame,ranking='trend',view='mixed'}) {
      if (!symbol || ranking!=='trend') return;
      this.rhythmFrame=frame;
      this.slots[0].candidateView=view;
      this.slots[0].candidateRanking='trend';
      this.slots[0].symbol=symbol;
      this.slots[0].limitType='rhythm_market';
      this.slots[0].manualDirection=null;
      this.deckCollapsed=false;
    },

    selectRangeStock(row) {
      this.rangeSelectedSymbol = row.symbol;
      if (this.rangeFocus !== null) this.rangeFocus = row.major;
      this.pickScreen(row);
    },
    rangeTargetReady(row) {
      if (!row?.ready || !this.status.market_running || this.status.market_stale) return false;
      const observedAt = this.market[row.symbol]?.price_observed_at;
      return Number.isFinite(observedAt) && observedAt > 0 && observedAt <= this.now
        && this.now - observedAt <= 45 && row.time <= this.now && this.now < row.time + 300;
    },
    rangeTargetMatches(symbol, direction) {
      const row = this.rangeSelectedRow;
      if (row?.symbol === symbol && this.rangeTargetReady(row)
          && (row.minor === 0 ? 1 : -1) === direction) return true;
      this.$message.warning('锁定目标或就绪状态已变化，请重新确认');
      return false;
    },
    async rangeGoldenOrder() {
      if (this.rangeActionBusy || this.placing || this.autoBusy) return;
      if (this.rangeGoldenBlock) return this.$message.warning(this.rangeGoldenBlock);
      const row = this.rangeGoldenSignal;
      this.rangeActionBusy = true;
      try {
        await this.placeGoldenEntry(row, {
          validateSelection: () => this.rangeSelectedSymbol === row.symbol,
        });
      } finally { this.rangeActionBusy = false; }
    },
    async rangeStartAuto() {
      if (this.rangeActionBusy || this.placing || this.autoBusy) return;
      if (this.rangeAutoBlock) return this.$message.warning(this.rangeAutoBlock);
      const row = this.rangeSelectedRow;
      const direction = row.minor === 0 ? 1 : -1;
      this.rangeActionBusy = true;
      try {
        await this.startAuto({
          strategy: 'ema7_autotrade_v2', instance_id: 'default', symbols: [row.symbol], direction,
          validate: () => this.rangeTargetMatches(row.symbol, direction),
        });
      } finally { this.rangeActionBusy = false; }
    },

    openingVwapStatus(row) {
      return {
        preview: '预选', armed: '已预约', confirmed: '已收盘', submitting: '提交中',
        submitted: '已提交', invalid: '收盘不符合', expired: '已超时',
        rejected: '未下单', unknown: '提交待对账',
      }[row.status] || row.status;
    },
    selectOpeningVwap(row) {
      this.openingVwapSelected = row.id;
      this.tvShow(row.symbol);
    },
    async prepareOpeningVwap(row, cancel = false) {
      if (this.demoMode || this.openingVwapBusy[row.id]) return;
      this.openingVwapBusy[row.id] = true;
      try {
        const response = await fetch('/api/lots/opening-vwap/prepare', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ signal_id: row.id, side: row.side, cancel }),
        });
        this.checkWrite403(response);
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '预约操作失败');
        this.$message.success(cancel ? '已撤销预约' : '已预约，等待收盘确认');
      } catch (error) {
        this.$message.error(error.message || String(error));
      } finally {
        delete this.openingVwapBusy[row.id];
      }
    },
    async tvShow(symbol) {
      if (!symbol || this.identity !== 'ok') return;
      const request = ++this.tvSwitchRequest;
      let result;
      try {
        if (this.traderName === 'master') {
          if (!this.status?.tv_view_same_window || this.status.tv_single_chart_cell !== 6) {
            this.$message.warning('第六格图表联动需重启控制台加载');
            return;
          }
          const sourceToken = Array.from(window.crypto.getRandomValues(new Uint8Array(16)),
            byte => byte.toString(16).padStart(2, '0')).join('');
          const dataset = document.documentElement.dataset;
          dataset.autotradeTvViewToken = sourceToken;
          try {
            const response = await fetch('/api/tv/view', {
              method: 'POST', headers: this.tradeHeaders(true),
              body: JSON.stringify({ symbol, source_token: sourceToken }),
            });
            const data = await response.json();
            result = response.ok ? data : { reason: typeof data.detail === 'string' ? data.detail : '图表联动请求失败' };
          } finally {
            if (dataset.autotradeTvViewToken === sourceToken) delete dataset.autotradeTvViewToken;
          }
        } else {
          result = await this.switchLocalTvChart(symbol, String(request));
        }
      } catch {
        result = { reason: '图表联动暂不可用，请重试。' };
      }
      if (request !== this.tvSwitchRequest) return;
      if (!result?.ok) {
        if (result?.upgrade) {
          this.tvExtensionState = 'update';
          this.tvExtensionReason = result.reason;
          this.tvSetupVisible = true;
        } else if (this.traderName !== 'master' && this.tvExtensionState !== 'ready') {
          this.tvSetupVisible = true;
          this.checkTvExtension();
        } else {
          this.$message.warning(result?.reason || 'TradingView 图表未确认切换。');
        }
      } else if (this.traderName !== 'master') {
        this.$message.success(`本机 TradingView 第六格已切换到 ${result.shown}`);
      }
      if(result?.ok)this.syncTvEntryMarkers(true);
    },
    async openTvHostBinding() {
      this.tvHostBindingVisible=true;this.tvHostBindingBusy=true;
      try {
        const response=await fetch('/api/tv/charts',{headers:this.tradeHeaders()});
        const data=await response.json();if(!response.ok)throw Error(data.detail||'图表读取失败');
        this.tvHostCharts=data.charts||[];
        this.tvHostTarget=this.tvHostCharts.length===1?this.tvHostCharts[0].id:'';
      }catch(error){this.$message.error(error.message||'请重启控制台加载图表绑定功能');}
      finally{this.tvHostBindingBusy=false;}
    },
    async bindTvHostChart() {
      if(!this.tvHostTarget||this.tvHostBindingBusy)return;
      this.tvHostBindingBusy=true;
      const token=Array.from(window.crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
      const dataset=document.documentElement.dataset;dataset.autotradeTvViewToken=token;
      try {
        const response=await fetch('/api/tv/bind',{method:'POST',headers:this.tradeHeaders(true),
          body:JSON.stringify({target_id:this.tvHostTarget,source_token:token})});
        const result=await response.json();if(!response.ok||!result.ok)throw Error(result.reason||result.detail||'绑定失败');
        this.tvHostBindingVisible=false;this.tvGridFailedKey='';this.tvGridLastKey='';this.tvGridLayoutKeys={};
        this.$message.success('已绑定当前控制台与TV图表，可保留两个窗口');
        this.$refs.rhythmPanel?.publishGrid(true);
      }catch(error){this.$message.error(error.message||'图表绑定失败');}
      finally{if(dataset.autotradeTvViewToken===token)delete dataset.autotradeTvViewToken;this.tvHostBindingBusy=false;}
    },
    switchLocalTvChart(symbol, id) {
      return this.requestTvBridge({ id, type: 'switch-symbol', symbol }, 9000);
    },
    requestTvBridge(detail, waitMs) {
      const bridge = document.documentElement.dataset.autotradeTvBridge;
      if (bridge && bridge !== '8') return { ok:false,installed:true,upgrade:true,reason:'请更新本机 Chrome 联动扩展到 0.8.0 并刷新 AutoTrade，启用TV八格与六格联动。' };
      if (bridge !== '8') {
        return { reason: '请在本机 Chrome 扩展中绑定 TradingView 图表，然后刷新此页面。' };
      }
      return new Promise(resolve => {
        const finish = result => {
          clearTimeout(timeout);
          window.removeEventListener('autotrade:local-tv-result', receive);
          resolve(result);
        };
        const receive = event => {
          if (event.detail?.id === detail.id) finish(event.detail);
        };
        const timeout = setTimeout(() => finish({ reason: '本机 TradingView 图表响应超时。' }), waitMs);
        window.addEventListener('autotrade:local-tv-result', receive);
        window.dispatchEvent(new CustomEvent('autotrade:local-tv-request', { detail }));
      });
    },
    async checkTvExtension() {
      if (this.identity !== 'ok' || this.traderName === 'master' || this.tvExtensionChecking) return;
      this.tvExtensionChecking = true;
      try {
        const previous = this.tvExtensionState;
        const status = await this.requestTvBridge({ id: 'status', type: 'chart-status' }, 1200);
        this.tvExtensionReason = status.reason || '';
        let installed = status.installed === true;
        if (!installed && window.chrome) {
          try {
            const response = await fetch(TV_EXTENSION_PRESENCE, { cache: 'no-store', credentials: 'omit' });
            installed = response.ok && (await response.json()).product === 'autotrade-tv';
          } catch { /* Chrome blocks the probe if the extension is unavailable. */ }
        }
        this.tvExtensionState = status.ready ? 'ready' : status.upgrade ? 'update'
          : installed ? 'bind'
            : window.chrome ? 'missing' : 'unsupported';
        if (this.tvExtensionState === 'ready') {
          if (previous !== 'ready') this.tvSetupVisible = false;
          return;
        }
        if (this.tvSetupPrompted) return;
        this.tvSetupPrompted = true;
        this.tvSetupVisible = true;
        if (this.tvExtensionState === 'missing') this.downloadTvExtensionOnce();
      } finally {
        this.tvExtensionChecking = false;
      }
    },
    downloadTvExtensionOnce() {
      const key = 'autotrade_tv_extension_download_requested';
      try {
        if (localStorage.getItem(key)) return;
        localStorage.setItem(key, '1');
      } catch { /* Try once per page if browser storage is unavailable. */ }
      const link = document.createElement('a');
      link.href = TV_EXTENSION_DOWNLOAD;
      link.download = 'autotrade-tv.zip';
      document.body.append(link);
      link.click();
      link.remove();
    },
    async copyTvChromeAddress(event) {
      const address = 'chrome://extensions';
      const input = event.currentTarget;
      try {
        await navigator.clipboard.writeText(address);
      } catch {
        input.select();
        this.tvChromeAddressCopied = document.execCommand?.('copy') === true;
        return;
      }
      this.tvChromeAddressCopied = true;
    },

    // 智能止损距离（与服务端 _smart_stop_dist 同口径）：优先读 smart_stop 指标
    // plot（所见即所得），缺失回退本地计算（最近三根 K 线极值逻辑止损点，
    // 钳制在 1×ATR ~ atrMult×ATR）；入场价已在极值外时按下限
    smartStopDist(data, side, refPx, atr) {
      const tsC = data.ts_c || {};
      const ss = side === 'long' ? tsC.smart_stop_long : tsC.smart_stop_short;
      const closeC = (data.bar_c || {}).close;
      if (ss != null && closeC != null) {
        const d = side === 'long' ? closeC - ss : ss - closeC;
        if (d > 0) return d;   // 指标值（所见即所得）
      }
      const lows = [], highs = [];
      for (const b of [data.bar || {}, data.bar_c || {}, data.bar_c2 || {}]) {
        if (b.low != null && b.high != null) { lows.push(b.low); highs.push(b.high); }
      }
      const logical = side === 'long'
        ? (lows.length ? refPx - Math.min(...lows) : 0)
        : (highs.length ? Math.max(...highs) - refPx : 0);
      const lo = atr, hi = Math.max(this.atrMult, 1.0) * atr;
      return Math.min(Math.max(logical, lo), hi);
    },

    openingManualStop(bar, side, now = new Date()) {
      const et = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).format(now);
      if (et < '09:30' || et >= '09:40') return null;
      const age = now.getTime() / 1000 - bar.time;
      const value = side === 'long' ? bar.low : side === 'short' ? bar.high : null;
      if (!(age >= 0 && age < 300 && bar.low > 0 && bar.high >= bar.low && Number.isFinite(value))) {
        return { price: null };
      }
      return { price: +value.toFixed(2) };
    },

    marketStopOutsideBar(bar, side, atr, allowMissingAtr, now, reference = bar.close) {
      const age = now.getTime() / 1000 - bar.time;
      if (!side || !(age >= 0 && age < 300) ||
          ![bar.low, bar.close, bar.high].every(Number.isFinite) ||
          !(0 < bar.low && bar.low <= bar.close && bar.close <= bar.high)) return null;
      const long = side === 'long';
      const extreme = long ? bar.low : bar.high;
      let outside = +extreme.toFixed(2);
      if (long ? outside >= extreme : outside <= extreme) {
        outside = +(outside + (long ? -0.01 : 0.01)).toFixed(2);
      }
      if (!Number.isFinite(atr) || atr <= 0) return allowMissingAtr ? outside : null;
      const candidate = +(reference + (long ? -2 : 2) * atr).toFixed(2);
      return long ? Math.min(candidate, outside) : Math.max(candidate, outside);
    },

    manualStopPrice(data,side,reference,mode,now,sigma) {
      if(mode==='vwap_sigma') {
        if(!Number.isFinite(reference)||reference<=0||!Number.isFinite(sigma)||sigma<=0||!side
          ||this.marketStopOutsideBar(data?.bar||{},side,null,true,now)===null)return null;
        const stop=+(reference+(side==='long'?-1:1)*sigma).toFixed(2);
        return stop>0&&(side==='long'?stop<reference:stop>reference)?stop:null;
      }
      const bar=data?.bar||{},atr=(data?.ts_c||data?.ts||{}).atr;
      return this.marketStopOutsideBar(bar,side,atr,this.openingManualStop(bar,side,now)!==null,now,reference);
    },

    async toggleMarketDirection(i, enabled) {
      const slot = this.slots[i];
      if (!slot || this.placing || slot.directionConfirming) return;
      if (!enabled) { slot.manualDirection = null; return; }
      if (!slot.symbol || slot.limitType !== 'market') return;
      const symbol = slot.symbol;
      const revision = slot.directionRevision || 0;
      slot.directionConfirming = true;
      try {
        await this.$confirm(
          `${this.tickerOf(symbol)}：确认按震荡行情在区间边缘入场，不要追高追低。开启后可手动选择做多或做空，方向可以与 1 分钟 ST 不一致。`,
          '震荡行情 · 手动方向确认',
          { type: 'warning', confirmButtonText: '确认做震荡行情', cancelButtonText: '取消' });
        if (slot.symbol !== symbol || slot.limitType !== 'market'
            || (slot.directionRevision || 0) !== revision) return;
        slot.manualDirection = { symbol, side: this.calcSlot(slot).side || 'long' };
      } catch { /* 取消时保持 ST 自动方向。 */ }
      finally { slot.directionConfirming = false; }
    },

    setManualMarketSide(i, side) {
      const slot = this.slots[i];
      if (this.placing || !slot || slot.limitType !== 'market'
          || slot.manualDirection?.symbol !== slot.symbol || !['long', 'short'].includes(side)) return;
      slot.manualDirection.side = side;
    },

    ema9EntryState(symbol,side,now=Date.now()/1000) {
      const data=this.market[symbol]||{},bar=data.bar||{},ts=data.ts||{};
      const price=bar.close,ema=ts.ema_9,stamp=bar.time,observed=data.price_observed_at;
      const ready=Number.isFinite(stamp)&&stamp%300===0&&stamp<=now&&now<stamp+300
        &&Number.isFinite(observed)&&stamp<=observed&&observed<=now&&now-observed<=45
        &&Number.isFinite(price)&&price>0&&Number.isFinite(ema)&&ema>0&&(ts.time==null||ts.time===stamp);
      const ok=!!(ready&&(side==='long'?price>ema:side==='short'?price<ema:false));
      const format=v=>Number.isFinite(v)?String(+v.toFixed(5)):'—';
      return {ready,ok,price,ema,reason:ok?'':!ready?'等待新鲜且同根的五分钟EMA9与价格':
        '当前价 '+format(price)+'，五分钟EMA9 '+format(ema)+'，'+(side==='long'?'做多要求价格 > EMA9':'做空要求价格 < EMA9')};
    },

    candidateSnapshotFor(ranking='trend',view='mixed') {
      if(ranking==='trend'&&view!=='mixed')return this.entryCandidateViews?.[view]||{ready:false,reason:'等待方向榜准入数据',rows:[]};
      return ranking==='trend'?this.entryCandidates:{ready:false,reason:'TD榜已移除，仅支持趋势榜开仓',rows:[]};
    },

    tdFactState(fact,now=Date.now()/1000){return rhythmTdFact(fact,now);},

    quoteEntryState(symbol,now=Date.now()/1000) {
      const data=this.market[symbol]||{},bar=data.bar||{},price=bar.close,stamp=bar.time,observed=data.price_observed_at;
      const ready=Number.isFinite(price)&&price>0&&Number.isFinite(stamp)&&stamp%300===0&&0<=now-stamp&&now-stamp<300
        &&Number.isFinite(observed)&&stamp<=observed&&observed<=now&&now-observed<=45;
      return {ready,price,reason:ready?'':'等待新鲜且有效的当前价格'};
    },

    vwapDirectionState(symbol,side,now=Date.now()/1000,ranking='trend') {
      const snapshot=this.candidateSnapshotFor(ranking),candidate=(snapshot.rows||[]).find(r=>r.symbol===symbol);
      const ema=this.ema9EntryState(symbol,side,now),vwap=candidate?.vwap;
      const ready=!!(snapshot.vwap_direction_required===true&&snapshot.ready&&snapshot.data_contract_version===2
        &&this.tdFactState(candidate?.td_fact,now).ready&&ema.ready&&Number.isFinite(vwap)&&vwap>0);
      const ok=!!(ready&&(side==='long'?ema.price>vwap:side==='short'?ema.price<vwap:false));
      const format=v=>Number.isFinite(v)?String(+v.toFixed(5)):'—';
      return {ready,ok,price:ema.price,vwap,reason:ok?'':!ready?'等待VWAP中线方向过滤数据':
        '当前价 '+format(ema.price)+'，VWAP中线 '+format(vwap)+'，'+(side==='long'?'做多要求价格 > VWAP中线':'做空要求价格 < VWAP中线')};
    },

    basicVwapEntry(snapshot) {
      return snapshot?.data_contract_version===4&&snapshot.entry_policy==='stock_vwap_side_v1'
        &&snapshot.direction_source==='confirmed_vwap_direction_v1'&&snapshot.td_required===false
        &&snapshot.ema9_required===false&&snapshot.vwap_direction_required===true&&snapshot.beta_filter_required===false;
    },

    vwapFactState(fact,now=Date.now()/1000){return rhythmVwapFact(fact,now);},

    candidateAdmissionReason(symbol,side=null,ranking='trend',view='mixed') {
      if(!this.status.candidate_entry_required)return '';
      if(ranking!=='trend')return 'TD榜已移除，仅支持趋势榜开仓';
      const snapshot=this.candidateSnapshotFor(ranking,view),row=(snapshot.rows||[]).find(r=>r.symbol===symbol);
      if(!this.basicVwapEntry(snapshot))return '收盘价／VWAP开仓规则版本不匹配，请刷新页面并加载新版服务';
      if(!snapshot.ready)return snapshot.reason||'等待趋势榜';
      if(!row)return '不在当前有效前十候选中';
      const now=Date.now()/1000,fact=this.vwapFactState(row.direction_fact,now);
      if(!fact.ready)return fact.reason;
      if(row.bar_time!==fact.bar_time||row.confirmed_at!==fact.confirmed_at||row.close!==fact.close||row.vwap!==fact.vwap||row.direction!==fact.direction)
        return '收盘价与VWAP准入证据未对齐';
      const wanted=side||row.side,sign=wanted==='long'?1:wanted==='short'?-1:0;
      if(!fact.direction||sign!==fact.direction||row.side!==wanted)return '1分钟收盘价 '+fact.close+' / VWAP '+fact.vwap+'，'+(fact.direction===1?'仅可做多':fact.direction===-1?'仅可做空':'相等不开仓');
      return this.quoteEntryState(symbol,now).reason;
    },

    vwapLimitChoice(slot,candidate) {
      const side=candidate?.side,level=slot.limitLevel||'vwap',mid=candidate?.vwap,sigma=candidate?.sigma;
      if(!['long','short'].includes(side)||!['vwap',side==='long'?'upper':'lower'].includes(level))
        return {ready:false,price:null,reason:'做多仅可选VWAP或上轨，做空仅可选VWAP或下轨'};
      if(!Number.isFinite(mid)||mid<=0||(level!=='vwap'&&(!Number.isFinite(sigma)||sigma<=0)))
        return {ready:false,price:null,reason:'等待确认VWAP轨道'};
      const price=+Number(mid+(level==='upper'?sigma:level==='lower'?-sigma:0)).toFixed(2);
      return {ready:price>0,price,reason:price>0?'':'VWAP限价取整后无效'};
    },

    setCandidateOrderType(i,type) {
      if(!['MKT','LMT'].includes(type))return;
      this.slots[i].orderType=type;this.slots[i].limitLevel='vwap';
    },

    candidateOrderOptions(slot) {
      return {...this.candidateProtection,vwap_outside_only:false};
    },

    async saveIntelligentExit(key,value) {
      if(this.intelligentExitSaving||this.demoMode)return;
      if(this.status.intelligent_exit_version!==1)return this.$message.warning('请重启控制台加载智能退出模块');
      const defaults={version:1,enabled:false,breakeven_minutes:5};
      const next={...(this.intelligentExit.version===1?this.intelligentExit:defaults),[key]:value};
      if(!Number.isInteger(next.breakeven_minutes)||next.breakeven_minutes<1||next.breakeven_minutes>120)
        return this.$message.warning('保本时间须为1–120分钟的整数');
      this.intelligentExitSaving=true;
      try{
        const response=await fetch('/api/settings',{method:'POST',headers:this.tradeHeaders(true),body:JSON.stringify({intelligent_exit:next})});
        this.checkWrite403(response);const data=await response.json();
        if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'智能退出设置保存失败');
        if(data.intelligent_exit?.version!==1)throw Error('后端未确认智能退出设置，请重启控制台');
        this.intelligentExit={...data.intelligent_exit};
        this.$message.success(next.enabled?'智能退出已开启，作用于全部控制台持仓':'智能退出已关闭');
      }catch(error){this.$message.error(error.message||String(error));}
      finally{this.intelligentExitSaving=false;}
    },

    async saveCandidateProtection(key,value) {
      if(this.candidateProtectionSaving||this.demoMode)return;
      if(this.status.candidate_protection_live_version!==1)return this.$message.warning('请重启控制台加载实时保护设置');
      const defaults={version:1,timed_breakeven_enabled:true,breakeven_minutes:5,trailing_enabled:false,trailing_source:'ema9_5m'};
      const next={...(this.candidateProtection.version===1?this.candidateProtection:defaults),[key]:value};
      if(!Number.isInteger(next.breakeven_minutes)||next.breakeven_minutes<1||next.breakeven_minutes>120)
        return this.$message.warning('保本时间须为1–120分钟的整数');
      this.candidateProtectionSaving=true;
      try{
        const r=await fetch('/api/settings',{method:'POST',headers:this.tradeHeaders(true),body:JSON.stringify({candidate_order_protection:next})});
        this.checkWrite403(r);const data=await r.json();
        if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'保护设置保存失败');
        if(data.candidate_order_protection?.version!==1)throw Error('后端未确认实时保护设置，请重启控制台');
        this.candidateProtection={...data.candidate_order_protection};
        this.$message.success('已应用于当前候选持仓和后续候选订单');
      }catch(e){this.$message.error(e.message||String(e));}
      finally{this.candidateProtectionSaving=false;}
    },

    candidateVwapState(slot) {
      const now=Date.now()/1000,candidate=(this.candidateSnapshotFor(slot.candidateRanking||'trend',slot.candidateView||'mixed').rows||[]).find(r=>r.symbol===slot.symbol);
      const point=this.rhythmFrame?.tracks?.find(t=>t.symbol===slot.symbol)?.points?.at(-1);
      const mid=candidate?.vwap??point?.[2],sigma=candidate?.sigma??point?.[3];
      const confirmed=candidate?.confirmed_at??(point?.[0]+60);
      const data=this.market[slot.symbol]||{},price=data.bar?.close,observed=data.price_observed_at;
      const ready=[mid,sigma,confirmed,price,observed].every(Number.isFinite)&&mid>sigma&&sigma>0&&price>0
        &&0<=now-confirmed&&now-confirmed<=120&&0<=now-observed&&now-observed<=45;
      const allowed=!!(ready&&(slot.side==='long'?price>mid+sigma:slot.side==='short'?price<mid-sigma:false));
      return {ok:allowed,detail:!ready?'等待确认VWAP轨道和新鲜报价':
        '现价 '+(+price.toFixed(4))+' · 下轨 '+(+(mid-sigma).toFixed(4))+' / 上轨 '+(+(mid+sigma).toFixed(4))+
        ' · '+(slot.side==='long'?'做多须高于上轨':'做空须低于下轨')};
    },

    candidateStState(slot) {
      const data=this.market[slot.symbol]||{},st=data.candidate_st_1m_c,now=Date.now()/1000;
      const ready=st?.version===1&&st.atr_period===10&&st.multiplier===3&&st.interval_seconds===60
        &&['regular','extended','24h'].includes(st.session)&&[-1,1].includes(st.direction)&&Number.isFinite(st.price)&&st.price>0
        &&Number.isInteger(st.time)&&st.time%60===0&&0<=now-(st.time+60)&&now-(st.time+60)<105
        &&data.market_1m?.bar_c?.time===st.time;
      const ok=!!(ready&&st.direction===(slot.side==='long'?1:-1));
      return {ok,detail:!ready?'等待一分钟ST（10，3）收盘确认':
        'ST'+(st.direction===1?'多头':'空头')+' · '+(slot.side==='long'?'做多':'做空')+(ok?'同向':'，不允许反向开仓')};
    },

    manualEntryChecks(slot) {
      if(!this.status.candidate_entry_required)return [];
      const ranking=slot.candidateRanking||'trend',view=slot.candidateView||'mixed',snapshot=this.candidateSnapshotFor(ranking,view),candidate=(snapshot.rows||[]).find(r=>r.symbol===slot.symbol);
      const side=slot.side||candidate?.side,sign=side==='long'?1:side==='short'?-1:0,now=Date.now()/1000;
      const data=this.market[slot.symbol]||{},quote=this.quoteEntryState(slot.symbol,now);
      const format=v=>Number.isFinite(v)?String(+v.toFixed(5)):'—';
      const fact=this.vwapFactState(candidate?.direction_fact,now);
      const aligned=!!(fact.known&&candidate?.bar_time===fact.bar_time&&candidate?.confirmed_at===fact.confirmed_at
        &&candidate?.close===fact.close&&candidate?.vwap===fact.vwap&&candidate?.direction===fact.direction);
      const confirmed=!!(snapshot.ready&&this.basicVwapEntry(snapshot)&&aligned&&fact.ready);
      const price=slot.limitPx??data.bar?.close,stop=slot.stopPx;
      const stopOK=Number.isFinite(price)&&price>0&&Number.isFinite(stop)&&stop>0&&!!sign&&(side==='long'?stop<price:stop>price);
      const pos=stopOK&&this.equity>0&&this.riskUsd>0?calcPosition(this.equity,this.riskUsd,price,stop,this.maxPositionPct,this.status.max_order_qty||0):{qty:0,risk:0};
      const total=this.status.total_risk_usd,direction=side==='long'?this.status.long_risk_usd:this.status.short_risk_usd;
      const totalLimit=this.status.max_total_risk_usd,directionLimit=this.status.max_direction_risk_usd;
      const riskKnown=[total,direction,totalLimit,directionLimit].every(Number.isFinite)&&!!sign&&pos.qty>0;
      const riskOK=riskKnown&&pos.risk<=this.riskUsd+1e-8&&(totalLimit===0||total+pos.risk<=totalLimit+1e-8)
        &&(directionLimit===0||direction+pos.risk<=directionLimit+1e-8);
      const directionMax=this.status.max_direction_positions||0,count=this.status.direction_position_counts?.[side];
      const countKnown=!!sign&&Number.isInteger(count)&&count>=0;
      const opposite=this.status.direction_position_counts?.[side==='long'?'short':'long'];
      const oppositeKnown=!!sign&&Number.isInteger(opposite)&&opposite>=0;
      const options=slot.candidateOrder;
      const policyReady=!options||(this.status.candidate_order_policy_version===1&&this.status.candidate_protection_live_version===1
        &&Number.isInteger(options.breakeven_minutes)&&options.breakeven_minutes>=1&&options.breakeven_minutes<=120
        &&['ema9_5m','st_1m_10_3'].includes(options.trailing_source));
      return [
        {key:'candidate',label:'趋势前十候选',ok:!!candidate&&ranking==='trend',detail:candidate?'当前第 '+candidate.rank+' 名':'不在有效趋势榜'},
        {key:'vwap_direction',label:'1分钟收盘价／VWAP',ok:aligned&&sign!==0&&fact.direction===sign&&candidate?.side===side,
          detail:fact.known?'收盘 '+format(fact.close)+' / VWAP '+format(fact.vwap)+' · '+(fact.direction===1?'只做多':fact.direction===-1?'只做空':'相等不开仓'):'等待有效收盘价与VWAP'},
        {key:'policy',label:'候选订单设置',ok:policyReady,detail:policyReady?'使用页面顶部的实时保护设置':'请重启控制台或检查保本分钟数（1–120）'},
        {key:'data',label:'个股行情时效',ok:confirmed&&quote.ready&&this.status.market_running!==false,
          detail:this.status.market_running===false?'行情采集已停止':confirmed&&quote.ready?'个股确认有效 · 报价≤45秒':fact.reason||quote.reason||snapshot.reason||'等待个股有效确认数据'},
        {key:'stop',label:'保护止损',ok:stopOK,detail:stopOK?(slot.manualStopMode==='vwap_sigma'?'1σ':'2×ATR / K线外')+' · '+format(stop):'等待方向正确的有效止损'},
        {key:'risk',label:'预估风险预算',ok:!!riskOK,detail:riskKnown?'本笔$'+pos.risk+' · 总$'+format(total+pos.risk)+'/'+(totalLimit||'不限')+' · 同向$'+format(direction+pos.risk)+'/'+(directionLimit||'不限'):'等待数量、账户和风险数据'},
        ...(directionMax>0?[{key:'direction_position',label:'单方向持仓名额',ok:countKnown&&count<directionMax,
          detail:countKnown?(side==='long'?'多头':'空头')+'已占用 '+count+'/'+directionMax+' 笔 · 挂单及平仓中占位，保本不释放':'等待服务端方向持仓笔数'}]:[]),
        ...(this.status.single_direction_only?[{key:'direction_consistency',label:'禁止多空并存',ok:oppositeKnown&&opposite===0,
          detail:!oppositeKnown?'等待当前未结束订单方向':opposite>0?'当前有'+(side==='long'?'空头':'多头')+opposite+'笔未结束订单，不能反向开仓':'无反向未结束订单；同向可多笔、同股可加仓'}]:[]),
        {key:'position',label:'数量与仓位上限',ok:pos.qty>0,detail:pos.qty>0?pos.qty+'股 · 同股合计上限'+this.maxPositionPct+'%，发单前复核':'等待有效风险定量'},
        {key:'account',label:'交易通道',ok:!!(this.status.connected||this.status.dry_run)&&!this.status.halted,
          detail:this.status.halted?'账户风控已暂停':this.status.dry_run?'模拟执行':this.status.connected?'TWS已连接':'等待TWS连接'},
      ];
    },

    calcSlot(slot, sideOverride = null) {
      const data = this.market[slot.symbol];
      const restricted=!!this.status.candidate_entry_required;
      const ranking=slot.candidateRanking||'trend',view=slot.candidateView||'mixed',snapshot=this.candidateSnapshotFor(ranking,view);
      const candidate=(snapshot.rows||[]).find(r=>r.symbol===slot.symbol);
      const stopMode=slot.stopMode||'atr_2';
      const minuteMarket = !restricted && slot.limitType === 'market' && this.status.manual_market_st_interval_seconds === 60;
      const manualSide = !restricted && slot.limitType === 'market' && slot.manualDirection?.symbol === slot.symbol
        && ['long', 'short'].includes(slot.manualDirection.side) ? slot.manualDirection.side : null;
      const marketDirectionMode = restricted?'candidate':manualSide ? 'manual' : 'st';
      const empty = {
        symbol: slot.symbol, limitType: slot.limitType,manualStopMode:stopMode,candidateRanking:ranking,candidateView:view,orderType:slot.orderType||'MKT',limitLevel:slot.limitLevel||'vwap',
        candidateOrder:slot.limitType==='rhythm_market'?this.candidateOrderOptions(slot):null,
        tdCount:Number.isSafeInteger(candidate?.td_count)?candidate.td_count:null,
        minuteMarket, marketDirectionMode, stSide: null,
        side: null, close: null, limitPx: null, stopPx: null,
        qty: 0, amount: 0, risk: 0, equityRatio: 0, ready: false, chg: 0,
        trendState: null, divergence: null, stopDist: null, atr: null,
        fvgDir: null, fvgText: '—',
      };
      if(stopMode==='vwap_sigma'&&!this.status.manual_stop_modes?.includes('vwap_sigma'))
        return {...empty,blockReason:'请重启控制台加载1σ手动止损'};
      if (slot.limitType === 'rhythm_market') {
        const now=new Date();
        const track=this.rhythmFrame?.tracks?.find(t=>t.symbol===slot.symbol);
        const sigma=candidate?.sigma??track?.points?.at(-1)?.[3];
        const limitChoice=this.vwapLimitChoice(slot,candidate);
        const reference=empty.orderType==='LMT'?limitChoice.price:data?.bar?.close;
        const plan=rhythmTradePreview(restricted?snapshot:this.rhythmFrame,slot.symbol,data,now.getTime()/1000,(side,atr)=>
          this.manualStopPrice(data,side,reference,stopMode,now,sigma),reference);
        if(empty.orderType==='LMT'&&!limitChoice.ready){plan.ready=false;plan.blockReason=limitChoice.reason;}
        if(restricted&&(!snapshot.ready||!candidate||candidate.side!==plan.side)) {
          plan.ready=false;plan.blockReason=snapshot.reason||'不在当前候选榜单或VWAP方向已变化';
        }
        const admissionReason=this.candidateAdmissionReason(slot.symbol,plan.side,ranking,view);
        if(admissionReason){plan.ready=false;plan.blockReason=admissionReason;}
        if(this.status.candidate_order_policy_version!==1||this.status.candidate_protection_live_version!==1){plan.ready=false;plan.blockReason='请重启控制台加载候选订单设置';}
        if(!Number.isInteger(empty.candidateOrder.breakeven_minutes)||empty.candidateOrder.breakeven_minutes<1||empty.candidateOrder.breakeven_minutes>120){plan.ready=false;plan.blockReason='保本时间须为1–120分钟的整数';}
        if(empty.candidateOrder.vwap_outside_only){
          const outside=this.candidateVwapState({...empty,...plan});
          if(!outside.ok){plan.ready=false;plan.blockReason='VWAP区间过滤：'+outside.detail;}
        }
        if(!this.basicVwapEntry(snapshot)&&empty.candidateOrder.trailing_enabled&&empty.candidateOrder.trailing_source==='st_1m_10_3'){
          const st=this.candidateStState({...empty,...plan});if(!st.ok){plan.ready=false;plan.blockReason='ST跟踪入场：'+st.detail;}
        }
        const pos=plan.ready?calcPosition(this.equity,this.riskUsd,plan.limitPx,plan.stopPx,this.maxPositionPct,this.status.max_order_qty||0):{qty:0,amount:0,risk:0};
        return {...empty,...plan,...pos,marketDirectionMode:restricted?'candidate':'rhythm',
          equityRatio:this.equity>0?+(pos.amount/this.equity*100).toFixed(1):0,
          stopDistPct:plan.limitPx&&plan.stopPx?+(Math.abs(plan.limitPx-plan.stopPx)/plan.limitPx*100).toFixed(2):null};
      }
      if (!data) return empty;
      // EMA/通道用实时值显示；中点单/市价单 ATR 使用已收盘确认值（与服务端一致）；
      // 回踩单限价/止损取上上根已收盘 bar（bar_c2）极值
      const ts = data.ts || {};
      const tsC = data.ts_c || ts;
      const bar = data.bar || {};
      const barC = data.bar_c || {};
      const barC2 = data.bar_c2 || {};

      const close = bar.close != null ? +bar.close.toFixed(2) : null;
      const atrSource = (restricted || slot.limitType === 'midpoint' || slot.limitType === 'market') ? tsC : ts;
      const atr = atrSource.atr > 0 ? atrSource.atr : null;
      const trendState = ts.trend_state != null ? ts.trend_state : null;
      const now = new Date();
      const minute = data.market_1m?.bar?.time;
      const minuteAge = now.getTime() / 1000 - minute;
      const confirmedMinute = data.market_1m?.bar_c?.time;
      const confirmedSt = data.vwap_pullback_c;
      const minuteSt = confirmedMinute === minute - 60 && confirmedSt?.time === confirmedMinute
        && [1, -1].includes(confirmedSt.direction) ? confirmedSt
          : (data.vwap_pullback?.time === minute ? data.vwap_pullback : null);
      const direction = minuteMarket
        ? (minuteAge >= 0 && minuteAge < 60 ? minuteSt?.direction : null)
        : ts.direction;
      const stSide = direction === 1 ? 'long' : (direction === -1 ? 'short' : null);
      const side = restricted ? candidate?.side : sideOverride || manualSide || stSide;

      let limitPx;
      if (slot.limitType === 'retest') {
        // 回踩限价：多 = bar_c2.high，空 = bar_c2.low（与后端 _retest_prices 同公式）
        const v = side === 'long' ? barC2.high : (side === 'short' ? barC2.low : null);
        limitPx = v != null ? +v.toFixed(2) : null;
      } else if (slot.limitType === 'market') {
        // 市价单：预览限价 = 实时价（服务端按 close ×(1±0.5%) 重新定价，marketable）
        limitPx = close;
      } else if (slot.limitType === 'midpoint') {
        limitPx = (barC.high != null && barC.low != null)
          ? +(((barC.high + barC.low) / 2).toFixed(2)) : null;
      } else if (slot.limitType === 'channel') {
        limitPx = ts.channel_edge != null ? +ts.channel_edge.toFixed(2) : null;
      } else {
        limitPx = ts.ema_fast != null ? +ts.ema_fast.toFixed(2) : null;
      }
      // 止损：回踩单 = bar_c2 另一端极值（多 low / 空 high，不依赖 ATR）；
      // 其余类型 = 智能止损（与服务端 _smart_stop_dist 同公式）：逻辑止损点
      // （最近三根 K 线极值，多=最低点/空=最高点，含当前 bar），钳制在
      // 1×ATR ~ atrMult×ATR（atrMult = 全局上限倍数，快照同步）
      const opening = this.openingManualStop(bar, side, now);
      let stopPx;
      if (slot.limitType === 'market') {
        stopPx = this.marketStopOutsideBar(bar, side, atr, opening !== null, now);
      } else if (opening) {
        stopPx = opening.price;
      } else if (slot.limitType === 'retest') {
        const v = side === 'long' ? barC2.low : (side === 'short' ? barC2.high : null);
        stopPx = v != null ? +v.toFixed(2) : null;
      } else {
        stopPx = (limitPx != null && atr != null && side)
          ? +(limitPx + (side === 'long' ? -1 : 1)
              * this.smartStopDist(data, side, limitPx, atr)).toFixed(2)
          : null;
      }

      if(restricted||stopMode==='vwap_sigma')stopPx=this.manualStopPrice(data,side,limitPx,stopMode,now,candidate?.sigma);
      // Production manual entries use the confirmed candidate direction.
      const ready = !!(side && limitPx && limitPx > 0 && stopPx && stopPx > 0
        &&(!restricted||!this.candidateAdmissionReason(slot.symbol,side,ranking,view))
        && (!minuteMarket || stSide)
        && (!opening || (side === 'long' ? stopPx < limitPx : stopPx > limitPx)));
      const pos = ready ? calcPosition(this.equity, this.riskUsd, limitPx, stopPx, this.maxPositionPct, this.status.max_order_qty || 0) : { qty: 0, amount: 0, risk: 0 };
      // 资金占比 = 占用金额 / 净值（%）
      const equityRatio = this.equity > 0 && pos.amount > 0 ? +((pos.amount / this.equity) * 100).toFixed(1) : 0;
      // chg：距止损的距离（多头高于止损为正=绿，空头低于止损为正=绿）
      const chg = (ready && close != null) ? (close - stopPx) * (side === 'long' ? 1 : -1) : 0;

      // EMA 发散度：仅展示/排序用（优先读指标 Ema_Divergence plot，
      // 缺省由指标 plot 值 ema_fast/ema_slow/atr 换算）；趋势判断只有 trend_state
      let divergence = ts.divergence != null ? Math.abs(ts.divergence) : null;
      if (divergence == null && ts.ema_fast != null && ts.ema_slow != null && atr != null) {
        divergence = Math.abs(ts.ema_fast - ts.ema_slow) / atr;
      }
      divergence = divergence != null ? +divergence.toFixed(2) : null;
      const stopDist = (limitPx != null && stopPx != null)
        ? +Math.abs(limitPx - stopPx).toFixed(3) : null;
      // 止损幅度百分比 = 止损距离 ÷ 限价（止损行合并显示用）
      const stopDistPct = (limitPx != null && limitPx > 0 && stopPx != null)
        ? +((Math.abs(limitPx - stopPx) / limitPx) * 100).toFixed(2) : null;

      // FVG 摘要（确认值优先，回退实时值）：方向 ▲/▼ + 同向累积计数 + 上下沿区间，全缺显示 —
      const fvg = data.fvg_c || data.fvg || {};
      const fvgDir = fvg.fvg_last_dir != null ? fvg.fvg_last_dir : null;
      let fvgText = '—';
      if (fvg.fvg_cum_count != null || fvgDir != null) {
        fvgText = (fvgDir === 1 ? '▲' : (fvgDir === -1 ? '▼' : '')) + (fvg.fvg_cum_count ?? '');
        if (fvg.fvg_last_bottom != null && fvg.fvg_last_top != null) {
          fvgText += ` ${fvg.fvg_last_bottom.toFixed(2)}~${fvg.fvg_last_top.toFixed(2)}`;
        }
      }

      return {
        symbol: slot.symbol,       // 下单需要
        candidateRanking:ranking,candidateView:view,
        limitType: slot.limitType, // 下单需要
        minuteMarket, marketDirectionMode, stSide,
        side, close, limitPx, stopPx, atr,manualStopMode:stopMode,
        tdCount:Number.isSafeInteger(candidate?.td_count)?candidate.td_count:null,
        blockReason:restricted?this.candidateAdmissionReason(slot.symbol,side,ranking,view):'',
        qty: pos.qty, amount: pos.amount, risk: pos.risk,
        equityRatio, ready, chg, trendState, divergence, stopDist, stopDistPct,
        fvgDir, fvgText,
      };
    },

    trendText(s) {
      return { 1: '单边多头', '-1': '单边空头', 0: '震荡' }[s] || '—';
    },
    trendClass(s) {
      return s === 1 ? 'pnl-pos' : (s === -1 ? 'pnl-neg' : 'muted');
    },

    typeLabel(t) {
      return { ema: 'EMA快线', channel: '通道边沿', midpoint: '中点限价单', retest: '回踩限价单', chase: 'FVG追价', market: '市价单', rhythm_market: '趋势开仓', twap: 'TWAP分批' }[t] || t;
    },

    // 判断是否为「不在多头/空头列表」拦截（结构化 code，或纯文本报错兜底）
    isNotLongCandidate(detail) {
      if (detail && detail.code === 'not_long_candidate') return true;
      return typeof detail === 'string'
        && (detail.includes('不在多头列表') || detail.includes('不在空头列表'));
    },

    // 二次确认：执意开仓必须填写开仓原因；返回原因字符串或 null（取消）
    async askForceReason(ticker, side) {
      const dirText = side === 'short' ? '空头' : '多头';
      try {
        const pr = await this.$prompt(
          `${ticker} 未通过${dirText}过滤（不在${dirText}列表）。执意开仓必须填写开仓原因：`,
          '二次确认（执意开仓）', {
            type: 'error',
            confirmButtonText: '执意开仓',
            cancelButtonText: '取消',
            inputPlaceholder: '必填：开仓原因',
            inputValidator: v => (v && v.trim() ? true : '必须填写开仓原因'),
          });
        return pr.value.trim();
      } catch { return null; }
    },

    async confirmSlowMarketEntry(order, now = new Date()) {
      const et = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).format(now);
      const slowMarket = et >= '09:40';
      if (!slowMarket && !order.manualGoldenWindow) return true;
      const ticker = this.tickerOf(order.symbol);
      const side = order.side === 'short' ? '做空' : '做多';
      const terms = order.openingBreakout
        ? `，市价入场，止损 ${order.stop_px.toFixed(2)}，股数按提交时现价和风险预算计算`
        : ` × ${order.qty} 股`;
      try {
        await this.$confirm(
          (slowMarket ? `美东 ${et}，已过开盘十分钟。当前属于慢速行情，手动交易亏钱概率极大。` : '')
            + `确认${side} ${ticker}${terms}？`,
          slowMarket ? '慢速行情 · 手动交易二次确认' : '确认黄金信号市价开仓', {
            type: 'warning', confirmButtonText: '确认继续下单', cancelButtonText: '取消下单',
            closeOnClickModal: false, autofocus: false,
          });
        return true;
      } catch { return false; }
    },

    openingBreakoutBarTime(row) {
      return row.bar_time ?? row.time - 300;
    },

    openingBreakoutStatus(row) {
      return ({
        preview: '盘中预选', preview_wait: '暂不达标', confirming: '等待收盘数据',
        armed: '已预约 · 收盘达标才开仓', confirmed: '收盘确认',
        invalid: '已失效', submitting: '正在提交', submitted: '已提交开仓',
        submission_failed: '提交未成功',
      })[row.status] || '收盘确认';
    },

    openingBreakoutActionLabel(row) {
      if (row.status === 'armed') return '撤销预约';
      if (row.status === 'preview') return '预约收盘开仓';
      return row.side === 'long' ? '市价做多' : '市价做空';
    },

    openingBreakoutEntryDisabled(row) {
      if (this.demoMode || this.placing) return true;
      if (row.status === 'armed') return false;
      if (row.status && !['preview', 'confirmed'].includes(row.status)) return true;
      const confirmedAt = row.confirmed_at || row.time;
      if (row.status !== 'preview' && this.now >= confirmedAt + 20) return true;
      return !row.stop_px
        || this.lots.some(lot => lot.signal_snapshot?.opening_breakout_id === row.id);
    },

    goldenFeedDay(time) {
      return new Date(time * 1000).toLocaleDateString('en-US', { timeZone: 'America/New_York' });
    },

    goldenFeedRow(symbol, signal, time) {
      if (!signal || !Number.isFinite(time) || signal.time !== time) return null;
      const direction = signal.direction;
      const stop = direction === 1 ? signal.long_stop : signal.short_stop;
      if (![1, -1].includes(direction) || !(signal.entry_price > 0 && stop > 0)
          || (signal.entry_price - stop) * direction <= 0) return null;
      return { id: symbol + ':' + time + ':' + direction, symbol, time, direction,
        ticker: this.tickerOf(symbol), side: direction === 1 ? 'long' : 'short',
        entry: signal.entry_price, stop_px: stop, touched: signal.entry_signal === 1 };
    },

    rememberGoldenFeedRows(rows) {
      const day = this.goldenFeedDay(this.now);
      for (const [id, row] of Object.entries(this.goldenFeedHistory)) {
        if (row.day !== day) delete this.goldenFeedHistory[id];
      }
      for (const row of rows) {
        if (this.goldenFeedDay(row.time) !== day) continue;
        const previous = this.goldenFeedHistory[row.id];
        if (previous && (previous.touched || !row.touched)) continue;
        this.goldenFeedHistory[row.id] = { ...row, day, touched: row.touched || !!previous?.touched };
      }
    },

    rememberConfirmedGoldenFeedRows() {
      const rows = [];
      for (const [symbol, data] of Object.entries(this.market)) {
        const row = this.goldenFeedRow(symbol, data.golden_c, data.bar_c?.time);
        if (row) rows.push(row);
      }
      if (rows.length) this.rememberGoldenFeedRows(rows);
    },

    goldenEntryBlock(row) {
      if (this.demoMode) return '演示模式';
      if (!row || this.now >= row.time + 300) return '当前没有有效的黄金信号';
      if (this.goldenSubmitted[row.id]) return '该黄金信号已提交';
      if (this.status.halted) return '账户风控已暂停';
      if (!this.status.dry_run && !this.status.connected) return 'IBKR 未连接';
      const observation = this.market[row.symbol]?.price_observed_at;
      if (this.status.market_stale || !(observation <= this.now && this.now - observation <= 15)) return '行情已过期';
      return '';
    },

    goldenEntryDisabled(row) {
      return this.placing || !!this.goldenEntryBlock(row);
    },

    async placeGoldenEntry(row, { validateSelection } = {}) {
      if (this.goldenEntryDisabled(row)) return;
      this.placing = true;
      try {
        if (!await this.confirmSlowMarketEntry({ ...row, openingBreakout: true, manualGoldenWindow: true })) return;
        if (validateSelection && !validateSelection()) throw new Error('所选股票已变化，请重新确认');
        const current = this.goldenSignalRows.find(item => item.id === row.id);
        if (!current) throw new Error('黄金信号已变化或过期');
        const block = this.goldenEntryBlock(current);
        if (block) throw new Error(block);
        const response = await fetch('/api/auto/golden-entry', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ symbol: row.symbol, bar_time: row.time, direction: row.direction,
            manual_window: true }),
        });
        this.checkWrite403(response);
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '开仓未确认');
        this.goldenSubmitted[row.id] = true;
        this.$message.success(`黄金策略已提交 ${row.ticker} · ${data.lot_id}`);
      } catch (error) {
        this.$message.error(error.message || String(error));
      } finally {
        this.placing = false;
      }
    },

    async prepareOpeningBreakout(row) {
      if (this.openingBreakoutEntryDisabled(row)) return;
      const cancel = row.status === 'armed';
      this.placing = true;
      try {
        if (!cancel) {
          try {
            await this.$confirm(
              `预约 ${row.ticker} ${row.side === 'long' ? '做多' : '做空'}。开盘已过十分钟，请确认当前行情适合交易。仅当美东 ${this.hmET(this.openingBreakoutBarTime(row))} 这根 5 分钟 K 线收盘仍符合原方向突破标准时，才提交市价单。初始止损取信号实体中点；入场所在的 5 分钟 K 线收盘后移至信号收盘价，不做1R保本，第三根收盘退出。10:30起停止下单，最多同时5笔突破订单。`,
              '确认预约收盘开仓', {
                type: 'warning', confirmButtonText: '确认预约', cancelButtonText: '取消',
                closeOnClickModal: false, autofocus: false,
              });
          } catch { return; }
        }
        const response = await fetch('/api/lots/opening-breakout/prepare', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({
            signal_id: row.id, side: row.side, cancel, max_position_pct: this.maxPositionPct,
          }),
        });
        this.checkWrite403(response);
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '预约操作失败');
        this.$message.success(cancel ? '已撤销预约' : '已预约，等待这根 K 线收盘');
      } catch (error) {
        this.$message.error(error.message || String(error));
      } finally {
        this.placing = false;
      }
    },

    async placeOpeningBreakout(row) {
      if (['preview', 'armed'].includes(row.status)) return this.prepareOpeningBreakout(row);
      if (this.openingBreakoutEntryDisabled(row)) return;
      this.placing = true;
      try {
        if (!await this.confirmSlowMarketEntry({ ...row, openingBreakout: true })) return;
        const response = await fetch('/api/lots/opening-breakout', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ signal_id: row.id, max_position_pct: this.maxPositionPct }),
        });
        this.checkWrite403(response);
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.msg || '下单失败');
        this.$message.success(`已提交市价开仓 ${row.ticker} × ${data.lot.qty} 股，止损 ${data.lot.stop_px}`);
      } catch (error) {
        this.$message.error(error.message || String(error));
      } finally {
        this.placing = false;
      }
    },

    async placeOrder(slot, force = false, forceReason = '', validate = null) {
      if (this.placing && !force) return;
      if (!slot.ready || !(slot.qty > 0)) return this.$message.warning('行情或风险定量未就绪，无法下单');
      const entryChecksPass=()=>{const failed=this.manualEntryChecks(slot).find(c=>!c.ok);if(failed)this.$message.warning(failed.label+'：'+failed.detail);return !failed;};
      if(!entryChecksPass())return;
      const optionsStillSelected=()=>slot.limitType!=='rhythm_market'||this.slots.some(source=>
        source.symbol===slot.symbol&&(source.candidateRanking||'trend')===(slot.candidateRanking||'trend')&&(source.candidateView||'mixed')===(slot.candidateView||'mixed')&&(source.orderType||'MKT')===(slot.orderType||'MKT')&&(source.limitLevel||'vwap')===(slot.limitLevel||'vwap')&&JSON.stringify(this.candidateOrderOptions(source))===JSON.stringify(slot.candidateOrder));
      if(!optionsStillSelected())return this.$message.warning('候选订单设置已变更，请重新确认');
      const manualChoice = slot.marketDirectionMode === 'manual'
        ? this.slots.find(source => source.symbol === slot.symbol)?.manualDirection : null;
      const directionStillSelected = () => this.status.candidate_entry_required
        ? !this.candidateAdmissionReason(slot.symbol,slot.side,slot.candidateRanking||'trend',slot.candidateView||'mixed')
          &&this.slots.some(s=>s.symbol===slot.symbol&&(s.candidateRanking||'trend')===(slot.candidateRanking||'trend')&&(s.candidateView||'mixed')===(slot.candidateView||'mixed')&&(s.stopMode||'atr_2')===(slot.manualStopMode||'atr_2'))
        : slot.marketDirectionMode !== 'manual'
        || this.slots.some(source => source.symbol === slot.symbol && source.limitType === 'market'
          && source.manualDirection === manualChoice && manualChoice?.side === slot.side);
      if (!directionStillSelected()) return this.$message.warning('手动方向已变更，请重新确认下单');
      this.placing = true;
      try {
        if (validate && !validate()) return;
        const ticker = slot.symbol.split(':').pop();
        const reason = slot.limitType === 'rhythm_market' ? `${ticker} 候选推荐${slot.side === 'long' ? '做多' : '做空'} · ${slot.orderType==='LMT'?'限价 '+slot.limitPx:'市价'} · ${slot.manualStopMode==='vwap_sigma'?'1σ止损':'5分钟2×ATR/当前K线外止损'}`
          : (force && forceReason) ? `执意开仓：${forceReason}`
          : `${ticker} ${slot.side} ${slot.limitType} ${['market','rhythm_market'].includes(slot.limitType) ? '市价' : '限价'}`
            + (slot.marketDirectionMode === 'manual' ? ' · 已确认震荡行情，手动方向' : '');
        const body = {
          symbol: slot.symbol, side: slot.side, qty: slot.qty,
          limit_px: slot.limitPx, stop_px: slot.stopPx,
          limit_type: slot.limitType === 'rhythm_market' ? (slot.orderType==='LMT'?'vwap_limit':'market') : slot.limitType, reason,
          vwap_limit_level:slot.limitLevel||'vwap',
          entry_mode: slot.limitType === 'rhythm_market' ? 'rhythm_atr' : 'default',
          manual_stop_mode: slot.manualStopMode || 'atr_2',
          candidate_ranking: slot.candidateRanking || 'trend',
          candidate_view: slot.candidateView || 'mixed',
          force,
          max_position_pct: this.maxPositionPct,
        };
        if(slot.candidateOrder)body.candidate_order={...slot.candidateOrder};
        if (slot.limitType === 'market') {
          body.market_direction_mode = slot.marketDirectionMode || 'st';
          body.range_direction_confirmed = body.market_direction_mode === 'manual';
        }
        if (!await this.confirmSlowMarketEntry(body)) return;
        if (validate && !validate()) return;
        if(!entryChecksPass())return;
        if(!optionsStillSelected())return this.$message.warning('候选订单设置已变更，请重新确认');
        if (!directionStillSelected()) return this.$message.warning('手动方向已变更，请重新确认下单');
        const r = await fetch('/api/lots/limit-stop', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify(body),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) {
          // 未通过多头/空头过滤 → 二次确认：必须填写开仓原因才能执意开仓
          if (slot.marketDirectionMode !== 'manual' && slot.limitType !== 'rhythm_market' && this.isNotLongCandidate(data.detail)) {
            const reasonText = await this.askForceReason(ticker, slot.side);
            if (reasonText == null) return;   // 取消：不开仓
            return await this.placeOrder(slot, true, reasonText, validate);
          }
          throw new Error(typeof data.detail === 'string' ? data.detail : (data.detail && data.detail.msg) || '下单失败');
        }
        const lot = data.lot;
        this.$message.success(`已下单 ${lot.lot_id}（${lot.symbol} ${lot.side} x${lot.qty} @${lot.entry_px}，止损 ${lot.stop_px}）`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.placing = false;
      }
    },

    async confirmShutdown() {
      if (this.systemClosing || this.systemClosed) return;
      try {
        await this.$confirm(
          '将停止控制台和全部跟单进程，并关闭 TWS。关闭后不会再控制 IBKR 交易；券商已有订单与止损不会自动撤销。',
          '确认关闭系统',
          { type: 'warning', confirmButtonText: '关闭系统', cancelButtonText: '取消',
            confirmButtonClass: 'el-button--danger' });
      } catch { return; }
      this.systemClosing = true;
      try {
        const r = await fetch('/api/system/shutdown', { method: 'POST', headers: this.tradeHeaders() });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || '关闭系统失败');
        this.systemClosed = true;
        this.systemCloseError = (d.errors || []).join('；');
        if (d.tws_closed === false && !this.systemCloseError) {
          this.systemCloseError = 'TWS 未能自动关闭，请检查进程。';
        }
        if (this.ws) this.ws.close();
      } catch (e) {
        this.systemClosing = false;
        this.$message.error(e.message || String(e));
      }
    },

    async cancelLot(row) {
      if (this.guardDemoOrder(row)) return;
      try {
        await this.$confirm(`确认撤销批次 ${row.lot_id}（${row.symbol}）？`, '撤单确认', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/lots/${row.lot_id}/cancel`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '撤单失败'); }
        this.$message.success('已撤单');
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    async closeLot(row) {
      if (this.guardDemoOrder(row)) return;
      try {
        await this.$confirm(
          `确认平仓批次 ${row.lot_id}（${row.symbol} ${row.side} x${row.filled_qty || row.qty}）？`,
          '平仓确认', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/lots/${row.lot_id}/close`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '平仓失败'); }
        this.$message.success('已提交平仓，等待实际成交');
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    async breakevenLot(row) {
      if (this.guardDemoOrder(row)) return;
      try {
        await this.$confirm(
          `确认手动保本 ${row.lot_id}（${row.symbol} ${row.side} x${row.filled_qty || row.qty}）？止损将移到实际成本价 ${row.avg_fill || row.entry_px}`,
          '手动保本确认', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/lots/${row.lot_id}/breakeven`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '保本失败'); }
        this.$message.success('已保本：止损移到成本价');
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    async closeHalf(row) {
      if (this.guardDemoOrder(row)) return;
      const half = Math.floor((row.filled_qty || row.qty) / 2);
      const remain = (row.filled_qty || row.qty) - half;
      try {
        await this.$confirm(
          `确认平仓一半 ${row.lot_id}（${row.symbol}）？平 ${half} 股，剩 ${remain} 股（止损单同步收缩到 ${remain} 股）`,
          '平仓一半确认', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/lots/${row.lot_id}/close-half`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '平仓一半失败'); }
        this.$message.success(`已平一半（${half} 股），止损单已收缩到 ${remain} 股`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      }
    },

    // 工具
    fmtMoney(v) {
      if (v === null || v === undefined) return '—';
      const n = Number(v);
      return n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    },
    clock(ts) {
      if (!ts) return '--:--:--';
      return new Date(ts * 1000).toTimeString().slice(0, 8);
    },
    logTime(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toTimeString().slice(0, 8);
    },
    pnlClass(v) { const n = Number(v); return n > 0 ? 'pnl-pos' : (n < 0 ? 'pnl-neg' : ''); },
    // 'NASDAQ:SKHY' → 'SKHY'（订单表格标的列用，去交易所前缀）
    tickerOf(sym) { return (sym || '').split(':').pop(); },
    // 仓位占比 = 持仓市值 ÷ 净值（挂单用计划值，已完结不再占仓显示 —）
    posPct(row) {
      const eq = this.status && this.status.equity;
      if (!eq || eq <= 0) return '—';
      if (!['PENDING', 'ACTIVE', 'CLOSING'].includes(row.state)) return '—';
      const qty = row.state === 'PENDING' ? row.qty : row.filled_qty;
      const px = row.last_px > 0 ? row.last_px : (row.avg_fill > 0 ? row.avg_fill : row.entry_px);
      if (!qty || !px || px <= 0) return '—';
      return ((qty * px / eq) * 100).toFixed(1) + '%';
    },
    // 订单表格分组：生效中订单加左缘色条（ACTIVE 绿 / PENDING、CLOSING 琥珀），
    // 已完结区首行加间隔带——一眼区分在手订单与历史订单
    lotRowClass({ row }) {
      const open = ['PENDING', 'CLOSING', 'ACTIVE'];
      const lots = this.sortedLots;
      const firstClosed = lots.findIndex(l => !open.includes(l.state));
      const cls = [];
      if (open.includes(row.state)) {
        cls.push(row.state === 'ACTIVE' ? 'lot-open-active' : 'lot-open-pending');
      }
      // 交易员分块边界：相邻两行分属不同交易员时加空白间隔带
      // （旧数据无 trader 视为 master 的订单），区分自己/他人订单
      const idx = lots.indexOf(row);
      const norm = (t) => t || 'master';
      if (idx > 0 && open.includes(row.state) && open.includes(lots[idx - 1].state)
          && norm(lots[idx - 1].trader) !== norm(row.trader)) {
        cls.push('lot-trader-start');
      }
      if (firstClosed > 0 && lots[firstClosed] === row) cls.push('lot-closed-start');
      return cls.join(' ');
    },
    // 盈亏口径：CLOSED 显示已实现盈亏，ACTIVE 显示浮动盈亏
    lotPnl(row) { return row.state === 'CLOSED' ? row.realized_pnl : row.pnl; },
    // mark 价新鲜度：TV 价格时间戳超过 60s 未更新 → 显示「价@HH:MM」提示
    //（该股无新成交，TV 不推新 bar，显示盈亏基于旧价；新鲜则不显示）
    markStaleText(row) {
      const d = this.market[row.symbol];
      const bt = d && d.bar && d.bar.time;
      if (!bt) return '';
      const age = this.now - bt;
      return age > 60 ? '价@' + this.hmET(bt) : '';
    },
    lotSideLabel(row) {
      return row.side === 'long' ? '做多' : row.side === 'short' ? '做空' : '方向未记录';
    },
    lotEntryAction(row) {
      return row.side === 'long' ? 'BUY 买入开仓' : row.side === 'short' ? 'SELL 卖出开仓' : '—';
    },
    orderTime(ts) {
      if (!Number.isFinite(ts) || ts <= 0) return '—';
      return new Date(ts * 1000).toLocaleString('sv-SE', {
        timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      });
    },
    lotOpenTime(row) { return this.orderTime(row.entry_broker_time || row.entry_time); },
    lotExitReason(row) {
      if (row.state === 'CLOSED') return row.exit_reason ? this.exitReasonLabel(row.exit_reason) : '原因未记录';
      if (row.state === 'CLOSING') return '待成交确认：' + this.exitReasonLabel(row.close_reason);
      return '—';
    },
    exitReasonLabel(r) {
      return { trail_stop: '跟踪保护止损成交', stop: '保护止损触发', manual: '用户手动平仓',
        candidate_timed_breakeven_stop:'定时保本退出（到期现价已越过成本）',
        intelligent_timed_breakeven:'智能退出：定时保本到期，现价已越过成本',
        intelligent_td_reverse_3:'智能退出：一分钟TD反向达到3',
        intelligent_td_reverse_4:'智能退出：一分钟TD反向达到4',
        candidate_st_trailing_flip:'ST跟踪止盈反向退出（已启用ST模式）',
        manual_stop: '用户退出策略组', manual_stop_close: '用户退出策略组',
        st_flip: 'ST转向自动平仓（历史规则）', trend_reverse: '策略方向反向退出（历史规则）',
        fvg_reverse: '反向FVG自动退出（历史规则）', eod: '尾盘强平', eod_cancel: '尾盘撤单',
        naked_guard: '保护单恢复失败强平', breakout_three_bar_timeout: '突破入场第三根收盘退出',
        breakout_signal_close_stop: '突破信号收盘价保护退出', superseded_group: '旧交易组退出',
        external_close: '外部平仓', phantom_cleanup: '人工核销' }[r] || r || '原因未记录';
    },
    stateTag(s) { return { ACTIVE: 'success', PENDING: 'warning', CLOSING: 'warning', CANCELLED: 'info', FAILED: 'danger', CLOSED: 'info' }[s] || 'info'; },
    // 交易员列配色：本客户端绿色高亮，master/auto 普通色，他人弱化
    traderClass(t) {
      if (!t) return 'muted';
      if (t === this.traderName) return 'trader-self';
      if (t === 'master' || t === 'auto') return 'trader-normal';
      return 'muted';
    },
  },
}).use(ElementPlus).mount('#app');
