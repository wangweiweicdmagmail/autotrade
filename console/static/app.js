/* autotrade 交易控制台前端逻辑（Vue3 + Element Plus，两宫格） */
const { createApp } = Vue;

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
      status: { dry_run: true, connected: false, halted: false, equity: null, daily_pnl: null, market_age: null, market_running: true, allow_short: true },
      market: {},
      lots: [],
      riskUsd: 300,   // 单笔资金（最大风险，美元，手工输入默认 300）
      maxPositionPct: 100,  // 最大仓位比例（%），固定 100
      maxDelay: 0,    // K线收盘→前端收到 的最大延时（秒）
      now: Date.now() / 1000,   // 前端实时时钟（每秒更新，驱动时钟走秒）
      selectRefs: {},           // el-select 实例引用（读内部 query）
      logs: [],                 // 运行日志（后端环形缓冲区）
      bottomTab: 'screen',      // 底部面板 tab：screen(选股) / log(日志)
      screen: { long: [], short: [] },  // 横截面选股（多头/空头两列）
      screenSort: 'divergence',   // divergence=EMA发散度 / duration=ST持续 / push=ST推进
      performance: null,          // 复盘绩效统计
      scanAlerts: [],             // 持仓扫描提醒
      stopAlerts: [],             // 止损单守卫提醒（持仓缺少 STP/TRAIL 保护单）
      autoCloseNotices: [],       // 自动平仓记录（语音/通知）
      lastScanAlert: {},          // 已提醒过的持仓扫描（按 scanned_at 去重）
      lastStopAlert: {},          // 已提醒过的止损单缺失（按 checked_at 去重）
      lastAutoCloseNotice: {},    // 已播报过的自动平仓（按 closed_at 去重）
      events: [],                 // 历史事件（持久日志回溯）
      eventFilter: 'all',         // 事件类型过滤
      shadow: { stats: {}, events: [] },   // 当日影子回测（统计 + 回踩事件）
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
        { symbol: '', limitType: 'ema' },   // 宫格1：默认多头第一名，EMA快线限价单
        { symbol: '', limitType: 'ema' },   // 宫格2：默认空头第一名，EMA快线限价单
      ],
      placing: false,
      ws: null,
    };
  },

  computed: {
    equity() { return this.status.equity || 0; },
    watchlist() {
      return Object.keys(this.market).sort((a, b) =>
        a.split(':').pop().localeCompare(b.split(':').pop()));
    },
    sortedScreenLong() { return this.sortScreenRows(this.screen.long); },
    sortedScreenShort() { return this.sortScreenRows(this.screen.short); },
    slotRows() {
      const rows = this.slots.map((slot) => this.calcSlot(slot));
      return this.allowShort ? rows : rows.slice(0, 1);   // 屏蔽空头时只显示宫格1（多头）
    },
    // 订单批次生命周期排序：挂单/持仓在前（按创建时间），已完结在后（按平仓时间倒序）
    sortedLots() {
      const rank = { PENDING: 0, CLOSING: 0, ACTIVE: 1 };
      return [...this.lots].sort((a, b) => {
        const ra = rank[a.state] ?? 2, rb = rank[b.state] ?? 2;
        if (ra !== rb) return ra - rb;
        if (ra < 2) return a.created_at - b.created_at;
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
    allowShort() { return this.status.allow_short !== false; },
  },

  mounted() {
    this.connectWS();
    // 每秒刷新前端时钟，让 TV/IBKR 时钟走秒
    setInterval(() => { this.now = Date.now() / 1000; }, 1000);
    // 定时拉取绩效统计（每 10s）
    this.fetchPerformance();
    setInterval(() => this.fetchPerformance(), 10000);
    // 定时拉取历史事件（每 5s）
    this.fetchEvents();
    setInterval(() => this.fetchEvents(), 5000);
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

    eventTypeText(t) {
      const m = {
        order_placed: '下单', order_closed: '平仓', order_close_requested: '平仓请求',
        order_close_failed: '平仓失败', order_cancelled: '撤单',
        order_cancel_failed: '撤单失败', position_scan_alert: '平仓提醒',
        auto_close_st_flip: 'ST转空自动平仓', missing_stop_alert: '止损单缺失',
        pullback_event: '回踩事件', shadow_settled: '影子结算',
        midpoint_limit_repriced: '中点单改价', midpoint_limit_reprice_failed: '中点单改价失败',
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
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ strategy: 'trend_pullback', symbols, params: {} }),
        });
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
        const r = await fetch(`/api/auto/stop?close_positions=${closePositions}`, { method: 'POST' });
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
          ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: '' }) }
          : { method: 'POST' };
        const r = await fetch(`/api/auto/proposals/${encodeURIComponent(id)}/${action}`, options);
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
          this.lots = msg.lots || [];
          this.market = msg.market || {};
          this.logs = msg.logs || [];
          this.screen = msg.screen || { long: [], short: [] };
          this.scanAlerts = msg.scan_alerts || [];
          this.stopAlerts = msg.stop_alerts || [];
          this.autoCloseNotices = msg.auto_close_notices || [];
          this.shadow = msg.shadow || { stats: {}, events: [] };
          this.auto = msg.auto || { enabled: false, strategy: '', instances: [], proposals: [] };
          this.fillDefaults();
          this.checkScanAlert();
          this.checkStopAlert();
          this.checkAutoCloseNotice();
          this.checkAutoEntryProposals();
          const d = msg.status.bar_update_delay;
          if (d != null && d > this.maxDelay) this.maxDelay = d;
        }
      };
      ws.onclose = () => setTimeout(() => this.connectWS(), 2000);
      ws.onerror = () => ws.close();
    },

    sortScreenRows(rows) {
      const key = { divergence: 'divergence', duration: 'st_duration_bars', push: 'st_push_count' }[this.screenSort];
      return [...rows].sort((a, b) => {
        const av = a[key] ?? -Infinity;
        const bv = b[key] ?? -Infinity;
        if (av !== bv) return bv - av;
        return (b.divergence ?? -1) - (a.divergence ?? -1);
      });
    },

    // 给空 slot 填默认标的
    fillDefaults() {
      // 宫格默认标的跟随当前选股排序
      if (!this.slots[0].symbol && this.sortedScreenLong.length) {
        this.slots[0].symbol = this.sortedScreenLong[0].symbol;
      }
      if (!this.slots[1].symbol && this.sortedScreenShort.length) {
        this.slots[1].symbol = this.sortedScreenShort[0].symbol;
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

    // 自动平仓播报（提交与成交分开表达，避免把 CLOSING 当成已成交）
    checkAutoCloseNotice() {
      for (const n of this.autoCloseNotices) {
        const ts = n.closed_at;
        if (this.lastAutoCloseNotice[n.ticker] === ts) continue;
        this.lastAutoCloseNotice[n.ticker] = ts;
        const spelled = n.ticker.split('').join(' ');
        this.speak(n.pending ? `${spelled} 已提交自动平仓，等待成交` : `${spelled} 已自动平仓`);
        this.$notify({
          title: n.pending ? '已提交自动平仓' : '已自动平仓',
          message: `${n.ticker} 多头 ${n.qty} 股，ST 已转空头（${n.st_short}），平仓价 ${n.close}${n.pending ? '，等待实际成交' : ''}`,
          type: n.pending ? 'warning' : 'success', duration: 30000, position: 'bottom-right',
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

    async closePosition(symbol) {
      try {
        const r = await fetch(`/api/positions/${encodeURIComponent(symbol)}/close`, { method: 'POST' });
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '平仓失败'); }
        const d = await r.json();
        this.$message.success(`已提交处理 ${d.symbol}（${d.side} x${d.qty}），等待实际成交`);
      } catch (e) { this.$message.error(e.message || String(e)); }
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

    // 点选横截面选股结果：多头 → 宫格1，空头 → 宫格2
    pickScreen(r) {
      const idx = r.side === 'long' ? 0 : 1;
      this.slots[idx].symbol = r.symbol;
      this.$message.success(`已选中 ${r.ticker} → 宫格${idx + 1}（${r.side === 'long' ? '多' : '空'} 发散 ${r.divergence}）`);
    },

    calcSlot(slot) {
      const data = this.market[slot.symbol];
      const empty = {
        symbol: slot.symbol, limitType: slot.limitType,
        side: null, close: null, limitPx: null, stopPx: null,
        qty: 0, amount: 0, risk: 0, equityRatio: 0, ready: false, chg: 0,
        trendState: null, divergence: null, stopDist: null, atr: null,
      };
      if (!data) return empty;
      // EMA/通道用实时值显示；中点单限价与 ATR 使用已收盘确认值
      const ts = data.ts || {};
      const tsC = data.ts_c || ts;
      const bar = data.bar || {};
      const barC = data.bar_c || {};

      const close = bar.close != null ? +bar.close.toFixed(2) : null;
      const atrSource = slot.limitType === 'midpoint' ? tsC : ts;
      const atr = atrSource.atr > 0 ? atrSource.atr : null;
      const trendState = ts.trend_state != null ? ts.trend_state : null;
      const side = ts.direction === 1 ? 'long' : (ts.direction === -1 ? 'short' : null);

      let limitPx;
      if (slot.limitType === 'midpoint') {
        limitPx = (barC.high != null && barC.low != null)
          ? +(((barC.high + barC.low) / 2).toFixed(2)) : null;
      } else if (slot.limitType === 'channel') {
        limitPx = ts.channel_edge != null ? +ts.channel_edge.toFixed(2) : null;
      } else {
        limitPx = ts.ema_fast != null ? +ts.ema_fast.toFixed(2) : null;
      }
      // 止损 = 限价 ∓ 1×ATR（多头限价−ATR，空头限价+ATR）
      const stopPx = (limitPx != null && atr != null && side)
        ? +(limitPx + (side === 'long' ? -1 : 1) * atr).toFixed(2)
        : null;

      const ready = !!(side && trendState !== 0 && limitPx && limitPx > 0 && stopPx && stopPx > 0);
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

      return {
        symbol: slot.symbol,       // 下单需要
        limitType: slot.limitType, // 下单需要
        side, close, limitPx, stopPx, atr,
        qty: pos.qty, amount: pos.amount, risk: pos.risk,
        equityRatio, ready, chg, trendState, divergence, stopDist,
      };
    },

    trendText(s) {
      return { 1: '单边多头', '-1': '单边空头', 0: '震荡' }[s] || '—';
    },
    trendClass(s) {
      return s === 1 ? 'pnl-pos' : (s === -1 ? 'pnl-neg' : 'muted');
    },

    typeLabel(t) {
      return { ema: 'EMA快线', channel: '通道边沿', midpoint: '中点限价单' }[t] || t;
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
        };
        const r = await fetch('/api/lots/limit-stop', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
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
        const r = await fetch(`/api/lots/${row.lot_id}/cancel`, { method: 'POST' });
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
        const r = await fetch(`/api/lots/${row.lot_id}/close`, { method: 'POST' });
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '平仓失败'); }
        this.$message.success('已提交平仓，等待实际成交');
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
    // 盈亏口径：CLOSED 显示已实现盈亏，ACTIVE 显示浮动盈亏
    lotPnl(row) { return row.state === 'CLOSED' ? row.realized_pnl : row.pnl; },
    exitReasonLabel(r) {
      return { trail_stop: '跟踪止损', stop: '止损', manual: '手动平仓', eod: '尾盘强平',
               external_close: '外部平仓', phantom_cleanup: '人工核销' }[r] || r;
    },
    stateTag(s) { return { ACTIVE: 'success', PENDING: 'warning', CLOSING: 'warning', CANCELLED: 'info', FAILED: 'danger', CLOSED: 'info' }[s] || 'info'; },
  },
}).use(ElementPlus).mount('#app');
