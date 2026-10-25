/* autotrade 交易控制台前端逻辑（Vue3 + Element Plus，两宫格） */
const { createApp } = Vue;

// 一次性提醒去重的持久化（存 localStorage）：刷新页面后保本/自动平仓/
// 未快速突破等「只播一次」的提醒不再重播
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
  data() {
    return {
      status: { dry_run: true, connected: false, halted: false, equity: null, daily_pnl: null, market_age: null, market_running: true, allow_short: true, total_risk_usd: 0, max_total_risk_usd: 0, total_position_pct: null },
      market: {},
      lots: [],
      riskUsd: (() => {   // 单笔资金（最大风险，美元，localStorage 持久化）
        try { const v = parseFloat(localStorage.getItem('risk_usd')); return isFinite(v) && v > 0 ? v : 300; } catch (e) { return 300; }
      })(),
      maxPositionPct: (() => {   // 最大仓位比例（%，localStorage 持久化）
        try { const v = parseFloat(localStorage.getItem('max_position_pct')); return isFinite(v) && v > 0 ? v : 100; } catch (e) { return 100; }
      })(),
      atrMult: 1.0,        // 全局止损 ATR 倍数（快照 status.stop_atr_mult 为准，全客户端同步）
      atrMultDirty: false, // 止损倍数输入框编辑中标记（编辑时不被快照覆盖）
      screenPmFilter: false,  // 选股盘前区间过滤开关（快照 status.screen_premarket_filter 同步）
      fvgPmFilter: false,     // FVG 监控盘前区间过滤开关（快照 status.fvg_premarket_filter 同步）
      imessageRecipient: '',  // iMessage 告警收件人（快照 status.imessage_recipient 同步，空=跳过该通道）
      imessageDirty: false,   // 收件人输入框编辑中标记（编辑时不被快照覆盖）
      alerts: [],             // 活跃告警（后端 Notifier，快照 msg.alerts）
      seenAlertKeys: {},      // 已通报过的告警 key -> 告警对象（声音/弹窗去重 + 解除播报）
      dismissedAlerts: {},    // 已手动消除的告警 key（本地，服务端解除后自动复位）
      maxDelay: 0,    // K线收盘→前端收到 的最大延时（秒）
      now: Date.now() / 1000,   // 前端实时时钟（每秒更新，驱动时钟走秒）
      selectRefs: {},           // el-select 实例引用（读内部 query）
      logs: [],                 // 运行日志（后端环形缓冲区）
      bottomTab: 'fvg',         // 底部面板 tab：fvg(FVG监控，默认) / screen(选股) / auto / log ...
      screen: { long: [], short: [] },  // 横截面选股（多头/空头两列）
      screenSort: 'divergence',   // divergence=EMA发散度 / duration=ST持续 / push=ST推进
      performance: null,          // 复盘绩效统计
      scanAlerts: [],             // 持仓扫描提醒
      stopAlerts: [],             // 止损单守卫提醒（持仓缺少 STP/TRAIL 保护单）
      externalAlerts: [],         // 外部订单/持仓告警（IBKR 手动单，告警模式）
      autoCloseNotices: [],       // 自动平仓记录（语音/通知）
      lastScanAlert: {},          // 已提醒过的持仓扫描（按 scanned_at 去重）
      lastBreakeven: loadNotified('notified_breakeven'),   // 已播报过的保本（持久化，刷新不重播）
      slowBreakAlerts: [],        // 未快速突破提醒（开仓超时未保本）
      lastSlowBreak: loadNotified('notified_slowbreak'),   // 已播报过的未快速突破（持久化）
      lastStopAlert: {},          // 已提醒过的止损单缺失（按 checked_at 去重）
      lastExternalAlert: {},      // 已提醒过的外部订单/持仓（kind:ticker 去重，60s 节流）
      lastAutoCloseNotice: loadNotified('notified_autoclose'),  // 已播报过的自动平仓（持久化）
      events: [],                 // 历史事件（持久日志回溯）
      eventFilter: 'all',         // 事件类型过滤
      shadow: { stats: {}, events: [] },   // 当日影子回测（统计 + 回踩事件）
      fvgMonitor: { slots: [], retests: [] },   // FVG 监控快照（bar 槽位 + 多空事件 + 首次回踩事件）
      fvgWindow: (() => {          // FVG 面板时间窗口（分钟）：15 / 30 / 60，localStorage 持久化
        try { const v = parseInt(localStorage.getItem('fvg_window'), 10); return [15, 30, 60].includes(v) ? v : 15; } catch (e) { return 15; }
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
      newsBusy: false,             // 手动刷新中（10 分钟冷却由后端管）
      // 悬浮交易台折叠状态（localStorage 持久化，小屏省空间）
      deckCollapsed: (() => { try { return localStorage.getItem('deck_collapsed') === '1'; } catch (e) { return false; } })(),
      fvgFreshIds: {},             // 会话内新到事件 id -> 到达时间（新鲜行高亮用）
      fvgInitDone: false,          // 首屏基线标记（首次快照不补报历史事件）
      lastFvgDataTime: null,       // 上一次数据时钟（bar 收盘滴答用，首次仅建基线）
      breakoutSignals: [],         // 盘前突破信号（后端快照，今日已确认）
      breakoutSeenIds: {},         // 已通报过的突破信号（按 id 去重）
      breakoutInitDone: false,     // 突破信号首屏基线标记
      auto: { enabled: false, strategy: '', instances: [], proposals: [] },
      autoSymbols: '',            // 自动策略启动表单：标的（空=全池）
      autoBusy: false,
      autoProposalBusy: {},       // proposal_id -> 是否正在提交决策
      lastAutoEntryProposals: {}, // 已提醒过的开仓候选（按 proposal_id 去重）
      wbSymbol: '',               // 回测工作台：标的
      wbDay: '',                  // 回测工作台：交易日
      wbLoading: false,
      wbError: '',
      wb: null,                   // 回测会话状态（进度/实例/订单生命周期）
      btSymbol: '',               // 历史回测表单：标的
      btDay: '',                  // 历史回测表单：交易日（默认昨日）
      btLoading: false,
      btResult: null,
      btError: '',
      slots: [
        { symbol: '', limitType: 'market' },   // 唯一宫格：默认选股第一名，市价单（方向按钮按趋势多空都开）
      ],
      placing: false,
      ws: null,
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
    sortedScreenLong() { return this.sortScreenRows(this.screen.long); },
    sortedScreenShort() { return this.sortScreenRows(this.screen.short); },
    slotRows() {
      // 单格交易台（v2026-10-23 起一格，方向按钮按趋势多空都开）
      return this.slots.map((slot) => this.calcSlot(slot));
    },
    // 订单批次生命周期排序：挂单/持仓在前、已完结在后（按平仓时间倒序）；
    // 生效中组内再按「本客户端交易员优先 → 创建时间」排序（自己的批次一眼可见）
    sortedLots() {
      const rank = { PENDING: 0, CLOSING: 0, ACTIVE: 1 };
      const norm = (t) => t || 'master';   // 旧数据无 trader 视为 master
      const own = (l) => (this.traderName && norm(l.trader) === this.traderName) ? 0 : 1;
      return [...this.lots].sort((a, b) => {
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
  },

  // 参数持久化：单笔资金/最大仓位/FVG 时间窗口改动即写 localStorage（各浏览器独立）
  watch: {
    riskUsd(v) { try { localStorage.setItem('risk_usd', String(v)); } catch (e) { /* 忽略 */ } },
    maxPositionPct(v) { try { localStorage.setItem('max_position_pct', String(v)); } catch (e) { /* 忽略 */ } },
    fvgWindow(v) { try { localStorage.setItem('fvg_window', String(v)); } catch (e) { /* 忽略 */ } },
    deckCollapsed(v) { try { localStorage.setItem('deck_collapsed', v ? '1' : '0'); } catch (e) { /* 忽略 */ } },
  },

  mounted() {
    // 先确认本客户端身份（master 以服务端判定为准，不在前端猜 IP），未确认前每 5s 轮询
    this.checkIdentity();
    setInterval(() => this.checkIdentity(), 5000);
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
      this.now = Date.now() / 1000;
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
  },

  beforeUnmount() {
    if (this._outsideDeckHandler) {
      document.removeEventListener('click', this._outsideDeckHandler, true);
    }
  },

  methods: {
    async fetchPerformance() {
      try {
        const r = await fetch('/api/performance');
        if (r.ok) this.performance = await r.json();
      } catch (e) { /* 忽略 */ }
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
        order_placed: '下单', order_closed: '平仓', order_close_requested: '平仓请求',
        order_close_failed: '平仓失败', order_cancelled: '撤单',
        order_cancel_failed: '撤单失败', position_scan_alert: '平仓提醒',
        auto_close_st_flip: 'ST转空自动平仓', missing_stop_alert: '止损单缺失',
        external_position_alert: '外部订单/持仓提醒',
        pullback_event: '回踩事件', shadow_settled: '影子结算',
        breakout_signal: '盘前突破',
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
        auto_breakeven: '移动保本', auto_trail: 'ST跟踪',
        auto_reverse_close: '反向平仓', auto_done: '实例结束',
        auto_adopted: '收养人工仓',
        auto_entry_proposed: '开仓候选', auto_entry_approved: '确认开仓候选',
        auto_entry_rejected: '忽略开仓候选', auto_entry_expired: '开仓候选过期',
      };
      return am[t] || t;   // 未映射的类型原样显示（防御）
    },

    // 策略：启动 / 停止 / 开仓候选决策
    async startAuto() {
      this.autoBusy = true;
      try {
        const symbols = this.autoSymbols.split(',').map(s => s.trim()).filter(Boolean);
        const r = await fetch('/api/auto/start', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ strategy: 'trend_pullback', symbols, params: {} }),
        });
        this.checkWrite403(r);
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '启动失败');
        this.$message.success(`已启动 ${d.started.length} 个实例${d.skipped.length ? '，跳过 ' + d.skipped.length : ''}`);
      } catch (e) {
        this.$message.error(e.message || String(e));
      } finally {
        this.autoBusy = false;
      }
    },
    async stopAuto(closePositions) {
      try {
        if (closePositions) {
          await this.$confirm('确认停止策略并平掉全部存量持仓？', '停止并全平', { type: 'warning' });
        }
        const r = await fetch(`/api/auto/stop?close_positions=${closePositions}`, { method: 'POST', headers: this.tradeHeaders() });
        this.checkWrite403(r);
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '停止失败'); }
        this.$message.success('已停止');
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

    // 回测工作台（驱动与实盘同一个 StrategyEngine，仅数据源/网关换成回放+模拟）
    async wbLoad() {
      this.wbLoading = true;
      this.wbError = '';
      try {
        const r = await fetch('/api/bt/load', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: this.wbSymbol || 'NVDA', day: this.wbDay || '', strategy: 'trend_pullback', params: {} }),
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
    async wbStep(n) {
      try {
        const r = await fetch(`/api/bt/step?n=${n}`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '步进失败');
        this.wb = d;
      } catch (e) { this.$message.error(e.message || String(e)); }
    },
    async wbRunAll() {
      try {
        const r = await fetch('/api/bt/run_all', { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '运行失败');
        this.wb = d;
      } catch (e) { this.$message.error(e.message || String(e)); }
    },
    exitReasonText(r) {
      return { stop: '止损', trend_reverse: '趋势反向', eod: '收盘了结',
               eod_cancel: '收盘撤单', manual: '手动平仓' }[r] || r;
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

    connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      this.ws = ws;
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'snapshot') {
          this.status = msg.status;
          // 止损 ATR 倍数跟快照走（全客户端同步）；编辑中（dirty）不覆盖输入框
          if (!this.atrMultDirty && msg.status && msg.status.stop_atr_mult != null) {
            this.atrMult = msg.status.stop_atr_mult;
          }
          // 盘前区间过滤开关跟快照走（全客户端同步）
          if (msg.status) {
            this.screenPmFilter = !!msg.status.screen_premarket_filter;
            this.fvgPmFilter = !!msg.status.fvg_premarket_filter;
            // 盘前新闻徽标联动：hot ticker → gist（tooltip）
            this.newsHot = (msg.status.news && msg.status.news.hot) || {};
            if (!this.imessageDirty) {
              this.imessageRecipient = msg.status.imessage_recipient || '';
            }
          }
          this.alerts = msg.alerts || [];   // 活跃告警（横幅 + 声音）
          this.checkAlerts();
          this.lots = msg.lots || [];
          this.market = msg.market || {};
          this.logs = msg.logs || [];
          this.screen = msg.screen || { long: [], short: [] };
          this.scanAlerts = msg.scan_alerts || [];
          this.stopAlerts = msg.stop_alerts || [];
          this.externalAlerts = msg.external_alerts || [];
          this.slowBreakAlerts = msg.slow_break_alerts || [];
          this.autoCloseNotices = msg.auto_close_notices || [];
          this.shadow = msg.shadow || { stats: {}, events: [] };
          this.auto = msg.auto || { enabled: false, strategy: '', instances: [], proposals: [] };
          this.fvgMonitor = msg.fvg_monitor || { slots: [], retests: [] };
          this.breakoutSignals = msg.breakout_signals || [];   // 盘前突破信号（今日已确认）
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
          this.checkAutoEntryProposals();
          this.checkFvgEvents();
          this.checkFvgWarnings();
          this.checkFvgTick();
          this.checkBreakoutSignals();
          const d = msg.status.bar_update_delay;
          if (d != null && d > this.maxDelay) this.maxDelay = d;
        }
      };
      ws.onclose = () => setTimeout(() => this.connectWS(), 2000);
      ws.onerror = () => ws.close();
    },

    sortScreenRows(rows) {
      const key = { divergence: 'divergence', duration: 'st_duration_bars', push: 'st_push_count', fvg: 'fvg_cum_count' }[this.screenSort];
      return [...rows].sort((a, b) => {
        const av = a[key] ?? -Infinity;
        const bv = b[key] ?? -Infinity;
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

    // 语音播报（Web Speech API）
    speak(text) {
      try {
        if (!('speechSynthesis' in window)) return;
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'zh-CN';
        u.rate = 1.1;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(u);
      } catch (e) { /* 忽略语音失败 */ }
    },

    // 持仓扫描提醒 → 弹框确认平仓（取消后下次继续提醒）；
    // 实盘默认已自动平仓，只有自动平仓失败或开关关闭时才会出现这里的提醒
    checkScanAlert() {
      for (const a of this.scanAlerts) {
        const ts = a.scanned_at;
        if (this.lastScanAlert[a.ticker] === ts) continue;   // 本次扫描已提醒过
        this.lastScanAlert[a.ticker] = ts;
        const lead = a.auto_close_failed ? '自动平仓失败！' : '';
        this.$confirm(
          `${lead}${a.ticker} 多头 ${a.qty} 股，ST 已转空头（${a.st_short}），现价 ${a.close}，确认平仓？`,
          '持仓扫描：ST 转空头', {
            type: 'warning',
            confirmButtonText: '平仓',
            cancelButtonText: '暂不',
            closeOnClickModal: false,
          }
        ).then(() => this.closePosition(a.symbol))
          .catch(() => { /* 取消：下次扫描继续提醒 */ });
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
          message: `${n.ticker} 多头 ${n.qty} 股，ST 已转空头（${n.st_short}），平仓价 ${n.close}${n.pending ? '，等待实际成交' : ''}`,
          type: n.pending ? 'warning' : 'success', duration: 30000, position: 'bottom-right',
        });
      }
      if (dirty) saveNotified('notified_autoclose', this.lastAutoCloseNotice);
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

    // FVG 事件通报：未见过的 id → 常驻通知；仅 ratio ≥ 1×ATR 的进语音，小缺口只上通知列表
    checkFvgEvents() {
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
      // ATR 倍数过滤：低于阈值的事件不进信号栏/语音（ratio 缺失无法判断大小，仍进信号栏但不进语音）
      const minRatio = this.fvgNotifyMinRatio;
      const notifiable = fresh.filter(ev => ev.ratio == null || ev.ratio >= minRatio);
      // 进右侧信号栏（替代 ElNotification 平铺弹窗：开盘洪峰时可滚动、
      // ATR 降序、逐条删除）
      this.pushFvgFeed(notifiable, false);
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

    // FVG 信号栏（右侧常驻）：确认事件与预警统一推入，ATR 倍数降序展示，
    // 用户从上往下处理，叉号逐条删除（已删除的由 fvgSeenIds 保证不再回来）
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
      const bar = this.market[ev.symbol] && this.market[ev.symbol].bar;
      const close = bar && bar.close;
      if (!close || close <= 0) return this.$message.warning('无实时行情，无法追价');
      const ticker = ev.ticker || this.tickerOf(ev.symbol);
      const isLong = ev.side === 'long';
      const limit = +(close * (isLong ? 1.005 : 0.995)).toFixed(2);
      // 止损 = 智能止损口径（smart_stop 指标优先）：现价 ∓ 距离（多−/空+）
      const symData = this.market[ev.symbol] || {};
      const dist = this.smartStopDist(symData, ev.side, close, ev.atr);
      const stop = +(isLong ? close - dist : close + dist).toFixed(2);
      // 以损定量用现价（预期成交价）为参考价，溢价限价不进风险口径（与服务端一致）
      const pos = calcPosition(this.equity, this.riskUsd, close, stop, this.maxPositionPct, this.status.max_order_qty || 0);
      if (!pos.qty) return this.$message.warning('以损定量结果为 0 股，无法下单');
      if (!force) {
        try {
          await this.$confirm(
            `市价追${isLong ? '多' : '空'} ${ticker} ×${pos.qty} 股 @≈${limit}，止损 ${stop}（风险 $${pos.risk}），确认下单？`,
            'FVG 追价确认', { type: 'warning' });
        } catch { return; }   // 取消：不下单
      }
      try {
        const body = {
          symbol: ev.symbol, side: ev.side, qty: pos.qty,
          limit_px: limit, stop_px: stop,
          limit_type: 'chase',
          reason: force && forceReason ? `FVG追价（执意开仓：${forceReason}）` : 'FVG追价',
          risk_usd: this.riskUsd, force,
          max_position_pct: this.maxPositionPct,
        };
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
        this.$message.success(`盘前区间过滤已${v ? '开启' : '关闭'}（全局生效）`);
      } catch (e) {
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
      for (const l of this.lots) {
        if (l.breakeven_reached && !this.lastBreakeven[l.lot_id]) {
          dirty = true;
          const ticker = (l.symbol || '').split(':').pop();
          const spelled = ticker.split('').join(' ');
          this.speak(`${spelled} 已保本，止损移到成本`);
          this.$notify({
            title: '保本',
            message: `${ticker} 已保本（止损移到入场成本），剩余风险为 0`,
            type: 'success', duration: 10000, position: 'bottom-right',
          });
        }
        this.lastBreakeven[l.lot_id] = !!l.breakeven_reached;
      }
      if (dirty) saveNotified('notified_breakeven', this.lastBreakeven);
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
      const matches = this.watchlist.filter((sym) => {
        const t = sym.split(':').pop().toUpperCase();
        return t.startsWith(q) || t.includes(q);
      });
      if (matches.length) {
        this.slots[i].symbol = matches[0];
      }
    },

    // 点选横截面选股结果：单格交易台直接填入选中标的（方向由按钮按趋势自适应）；
    // 同时 CDP 联动本机 TV 桌面端第一个 pane 切到该标的（失败静默降级）
    pickScreen(r) {
      this.slots[0].symbol = r.symbol;
      this.$message.success(`已选中 ${r.ticker}（${r.side === 'long' ? '多' : '空'} 发散 ${r.divergence}）`);
      this.tvShow(r.symbol);
    },

    // CDP 联动：POST /api/tv/show 切 TV 第一个 pane（幂等快速；TV 未开/失败静默）
    async tvShow(symbol) {
      // 冷启动（浏览器未开）要等 Chrome 启动 + TV 页加载，最长约 1 分钟：
      // 超过 1s 未返回就弹进度提示（不自动消失），拿到结果后关闭
      let waiting = null;
      const timer = setTimeout(() => {
        if (this.$notify) {
          waiting = this.$notify({
            title: 'TV 图表联动',
            message: '浏览器未打开，正在启动并加载 TradingView，请稍候…',
            type: 'info', duration: 0,
          });
        }
      }, 1000);
      try {
        const r = await fetch('/api/tv/show', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify({ symbol }),
        });
        const d = await r.json().catch(() => ({}));
        if (waiting) waiting.close();
        // 失败时把原因报出来（比如浏览器未以可控模式运行），否则用户以为没反应
        if (!d.ok && d.reason && this.$notify) {
          this.$notify({ title: 'TV 图表未切换', message: d.reason,
                         type: 'warning', duration: 5000 });
        }
      } catch (e) {
        if (waiting) waiting.close();
        /* 静默降级：不影响选股/下单 */
      } finally {
        clearTimeout(timer);
      }
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

    calcSlot(slot) {
      const data = this.market[slot.symbol];
      const empty = {
        symbol: slot.symbol, limitType: slot.limitType,
        side: null, close: null, limitPx: null, stopPx: null,
        qty: 0, amount: 0, risk: 0, equityRatio: 0, ready: false, chg: 0,
        trendState: null, divergence: null, stopDist: null, atr: null,
        fvgDir: null, fvgText: '—',
      };
      if (!data) return empty;
      // EMA/通道用实时值显示；中点单/市价单 ATR 使用已收盘确认值（与服务端一致）；
      // 回踩单限价/止损取上上根已收盘 bar（bar_c2）极值
      const ts = data.ts || {};
      const tsC = data.ts_c || ts;
      const bar = data.bar || {};
      const barC = data.bar_c || {};
      const barC2 = data.bar_c2 || {};

      const close = bar.close != null ? +bar.close.toFixed(2) : null;
      const atrSource = (slot.limitType === 'midpoint' || slot.limitType === 'market') ? tsC : ts;
      const atr = atrSource.atr > 0 ? atrSource.atr : null;
      const trendState = ts.trend_state != null ? ts.trend_state : null;
      const side = ts.direction === 1 ? 'long' : (ts.direction === -1 ? 'short' : null);

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
      let stopPx;
      if (slot.limitType === 'retest') {
        const v = side === 'long' ? barC2.low : (side === 'short' ? barC2.high : null);
        stopPx = v != null ? +v.toFixed(2) : null;
      } else {
        stopPx = (limitPx != null && atr != null && side)
          ? +(limitPx + (side === 'long' ? -1 : 1)
              * this.smartStopDist(data, side, limitPx, atr)).toFixed(2)
          : null;
      }

      // 震荡（trendState=0）也放行：与服务端闸门一致，方向由 ST direction 决定
      const ready = !!(side && limitPx && limitPx > 0 && stopPx && stopPx > 0);
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
        limitType: slot.limitType, // 下单需要
        side, close, limitPx, stopPx, atr,
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
      return { ema: 'EMA快线', channel: '通道边沿', midpoint: '中点限价单', retest: '回踩限价单', chase: 'FVG追价', market: '市价单', twap: 'TWAP分批' }[t] || t;
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

    async placeOrder(slot, force = false, forceReason = '') {
      if (!slot.ready) return this.$message.warning('数据不完整，无法下单');
      this.placing = true;
      try {
        const ticker = slot.symbol.split(':').pop();
        const reason = (force && forceReason) ? `执意开仓：${forceReason}`
          : `${ticker} ${slot.side} ${slot.limitType} 限价`;
        const body = {
          symbol: slot.symbol, side: slot.side, qty: slot.qty,
          limit_px: slot.limitPx, stop_px: slot.stopPx,
          limit_type: slot.limitType, reason,
          risk_usd: this.riskUsd, force,
          max_position_pct: this.maxPositionPct,
        };
        const r = await fetch('/api/lots/limit-stop', {
          method: 'POST', headers: this.tradeHeaders(true),
          body: JSON.stringify(body),
        });
        this.checkWrite403(r);
        const data = await r.json();
        if (!r.ok) {
          // 未通过多头/空头过滤 → 二次确认：必须填写开仓原因才能执意开仓
          if (this.isNotLongCandidate(data.detail)) {
            const reasonText = await this.askForceReason(ticker, slot.side);
            if (reasonText == null) return;   // 取消：不开仓
            return await this.placeOrder(slot, true, reasonText);
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

    async cancelLot(row) {
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
    exitReasonLabel(r) {
      return { trail_stop: '跟踪止损', stop: '止损', manual: '手动平仓', eod: '尾盘强平',
               external_close: '外部平仓', phantom_cleanup: '人工核销' }[r] || r;
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
