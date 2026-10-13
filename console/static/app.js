/* autotrade 交易控制台前端逻辑（Vue3 + Element Plus，四宫格） */
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
      status: { dry_run: true, connected: false, halted: false, equity: null, daily_pnl: null, market_age: null, market_running: true },
      market: {},
      lots: [],
      riskUsd: 300,   // 单笔资金（最大风险，美元，手工输入默认 300）
      maxPositionPct: 100,  // 最大仓位比例（%），固定 100
      maxDelay: 0,    // K线收盘→前端收到 的最大延时（秒）
      now: Date.now() / 1000,   // 前端实时时钟（每秒更新，驱动时钟走秒）
      selectRefs: {},           // el-select 实例引用（读内部 query）
      logs: [],                 // 运行日志（后端环形缓冲区）
      bottomTab: 'screen',      // 底部面板 tab：screen(选股) / log(日志)
      screen: { long: [], short: [], pullbacks: [] },  // 横截面选股 + 回踩事件队列
      performance: null,          // 复盘绩效统计
      stabEvents: [],             // 入场信号（稳定 K线触发，可下单）
      alertEvents: [],            // 预警信号（实时 K线触发，仅提醒）
      lastStabSignal: {},         // 已提醒过的回踩信号（去重）
      lastAlertSignal: {},        // 已提醒过的预警信号（去重）
      events: [],                 // 历史事件（持久日志回溯）
      eventFilter: 'all',         // 事件类型过滤
      slots: [
        { symbol: '', limitType: 'channel' },   // 宫格1：默认多头第一名，ST回踩
        { symbol: '', limitType: 'channel' },   // 宫格2：默认空头第一名，ST回踩
      ],
      placing: false,
      ws: null,
      lastPullback: {},   // 各标的 ST 回踩状态（用于检测变化触发语音提醒）
    };
  },

  computed: {
    equity() { return this.status.equity || 0; },
    watchlist() {
      return Object.keys(this.market).sort((a, b) =>
        a.split(':').pop().localeCompare(b.split(':').pop()));
    },
    slotRows() {
      const rows = this.slots.map((slot) => this.calcSlot(slot));
      return this.allowShort ? rows : rows.slice(0, 1);   // 屏蔽空头时只显示宫格1（多头）
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
        pierce_signal: '回踩信号', order_placed: '下单', order_closed: '平仓',
        order_close_failed: '平仓失败', order_cancelled: '撤单',
        order_cancel_failed: '撤单失败',
      };
      return m[t] || t;
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
          this.screen = msg.screen || { long: [], short: [], pullbacks: [] };
          this.stabEvents = msg.stab_events || [];
          this.alertEvents = msg.alert_events || [];
          this.fillDefaults();
          this.checkPullback();
          this.checkAlertSignal();
          this.checkStabSignal();
          const d = msg.status.bar_update_delay;
          if (d != null && d > this.maxDelay) this.maxDelay = d;
        }
      };
      ws.onclose = () => setTimeout(() => this.connectWS(), 2000);
      ws.onerror = () => ws.close();
    },

    // 给空 slot 填默认前 4 个标的
    fillDefaults() {
      // 宫格1默认放多头第一名，宫格2默认放空头第一名
      if (!this.slots[0].symbol && this.screen.long.length) {
        this.slots[0].symbol = this.screen.long[0].symbol;
      }
      if (!this.slots[1].symbol && this.screen.short.length) {
        this.slots[1].symbol = this.screen.short[0].symbol;
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

    // 检测 ST 回踩变化（false→true 触发闪烁 + 语音）
    checkPullback() {
      this.slotRows.forEach((slot) => {
        const key = slot.symbol;
        if (!key) return;
        const prev = !!this.lastPullback[key];
        if (slot.pullback && !prev) {
          const ticker = key.split(':').pop();
          const spelled = ticker.split('').join(' ');   // S K H Y，逐字母，不硬拼成单词
          const dir = slot.side === 'long' ? '多头' : '空头';
          this.speak(`${spelled} ${dir} 进入 ST 回踩区域`);
        }
        this.lastPullback[key] = !!slot.pullback;
      });
    },

    // 检测新的预警信号（实时 K线触发，仅语音提醒「注意观察」）
    checkAlertSignal() {
      const cur = {};
      for (const ev of this.alertEvents) {
        cur[ev.symbol] = true;
        if (!this.lastAlertSignal[ev.symbol]) {
          const spelled = ev.ticker.split('').join(' ');
          const dir = ev.side === 'long' ? '多头' : '空头';
          this.speak(`${spelled} ${dir} ST 回踩预警，注意观察`);
        }
      }
      this.lastAlertSignal = cur;
    },

    // 检测新的入场信号（稳定 K线触发，最高优先级：语音 + 自动切信号 tab）
    checkStabSignal() {
      const confirmed = this.stabEvents.filter(e => e.stage === 'confirmed');
      const cur = {};
      for (const ev of confirmed) {
        cur[ev.symbol] = true;
        if (!this.lastStabSignal[ev.symbol]) {
          const spelled = ev.ticker.split('').join(' ');
          const dir = ev.side === 'long' ? '多头' : '空头';
          this.speak(`${spelled} ${dir} ST 回踩入场信号，请确认下单`);
          this.bottomTab = 'stab';   // 自动切到信号 tab
          this.$notify({
            title: 'ST 回踩入场信号',
            message: `${ev.ticker} ${dir} 回踩，限价 ${ev.limit_px} 止损 ${ev.stop_px}`,
            type: 'warning', duration: 60000, position: 'bottom-right',
          });
        }
      }
      this.lastStabSignal = cur;
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
      this.$message.success(`已选中 ${r.ticker} → 宫格${idx + 1}（${r.side === 'long' ? '多' : '空'} ${r.move_atr} ATR）`);
    },

    // ST 刺破：点「处理」，标记待收盘判断
    // ST 回踩信号：确认下单
    async confirmStab(ev) {
      try {
        await this.$confirm(
          `确认下单 ${ev.ticker} ${ev.side === 'long' ? '多' : '空'}？限价 ${ev.limit_px}，止损 ${ev.stop_px}`,
          'ST回踩信号下单', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch(`/api/stab/confirm?symbol=${encodeURIComponent(ev.symbol)}&risk_usd=${this.riskUsd}`, { method: 'POST' });
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '下单失败'); }
        this.$message.success(`已下单 ${ev.ticker}（${ev.side === 'long' ? '多' : '空'}）`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    stabStage(ev) {
      const m = { confirmed: '✓ 回踩信号', ordered: '已下单' };
      return m[ev.stage] || ev.stage;
    },

    // 一键清除所有交易信号（入场 + 预警）
    async clearSignals() {
      try {
        await this.$confirm('确认清除所有交易信号？', '清除信号', { type: 'warning' });
      } catch { return; }
      try {
        const r = await fetch('/api/signal/clear', { method: 'POST' });
        if (!r.ok) { const d = await r.json(); throw new Error(d.detail || '清除失败'); }
        const d = await r.json();
        this.stabEvents = [];
        this.alertEvents = [];
        this.lastStabSignal = {};
        this.lastAlertSignal = {};
        this.$message.success(`已清除 ${d.cleared} 条信号`);
      } catch (e) { this.$message.error(e.message || String(e)); }
    },

    calcSlot(slot) {
      const data = this.market[slot.symbol];
      const empty = { side: null, close: null, ema7: null, channelPx: null, stopPx: null, limitPx: null, qty: 0, amount: 0, risk: 0, ready: false, chg: 0 };
      if (!data) return empty;
      // 策略用「确认值」（上一根已收盘 K 线的 ST），稳定不抖
      const st = data.st_c || {};
      const bar = data.bar || {};

      let side = null, stopPx = null, channelPx = null;
      if (st.up_trend != null) { side = 'long'; stopPx = st.up_trend; channelPx = st.up_channel; }
      else if (st.down_trend != null) { side = 'short'; stopPx = st.down_trend; channelPx = st.down_channel; }

      const ema7 = data.ema7 != null ? +data.ema7.toFixed(2) : null;
      const close = bar.close != null ? +bar.close.toFixed(2) : null;
      const limitPx = slot.limitType === 'channel' ? channelPx : ema7;
      const ready = !!(side && stopPx && limitPx && limitPx > 0 && stopPx > 0);
      const pos = ready ? calcPosition(this.equity, this.riskUsd, limitPx, stopPx, this.maxPositionPct, this.status.max_order_qty || 0) : { qty: 0, amount: 0, risk: 0 };
      // 资金占比 = 占用金额 / 净值（%）
      const equityRatio = this.equity > 0 && pos.amount > 0 ? +((pos.amount / this.equity) * 100).toFixed(1) : 0;
      // chg：距止损的距离（多头高于止损为正=绿，空头低于止损为正=绿）
      const chg = (ready && close != null) ? (close - stopPx) * (side === 'long' ? 1 : -1) : 0;

      // 趋势强度（st_strength 指标）
      const sg = data.strength || {};
      const strength = {
        bars: sg.bars != null ? Math.round(sg.bars) : null,
        move_atr: sg.move_atr != null ? +sg.move_atr.toFixed(2) : null,
        avg_atr: sg.avg_atr != null ? +sg.avg_atr : null,
      };
      // 止损距离 + 过近提示（< 0.5×ATR 视为止损过近，仅提示不拦截）
      const stopDist = (limitPx != null && stopPx != null)
        ? +Math.abs(limitPx - stopPx).toFixed(3) : null;
      const tooClose = (stopDist != null && strength.avg_atr && strength.avg_atr > 0)
        ? stopDist < strength.avg_atr * 0.5 : false;

      // ST 回踩判断：价格触及通道边沿（多头跌破上边沿 / 空头涨破下边沿）
      const pullback = (ready && channelPx != null && close != null)
        ? (side === 'long' ? close <= channelPx : close >= channelPx)
        : false;

      return {
        symbol: slot.symbol,       // 下单需要
        limitType: slot.limitType, // 下单需要
        side, close, ema7,
        channelPx: channelPx != null ? +channelPx.toFixed(2) : null,
        stopPx: stopPx != null ? +stopPx.toFixed(2) : null,
        limitPx: limitPx != null ? +limitPx.toFixed(2) : null,
        qty: pos.qty, amount: pos.amount, risk: pos.risk,
        equityRatio, ready, chg, strength, stopDist, tooClose, pullback,
      };
    },

    async placeOrder(slot) {
      if (!slot.ready) return this.$message.warning('数据不完整，无法下单');
      this.placing = true;
      try {
        const body = {
          symbol: slot.symbol, side: slot.side, qty: slot.qty,
          limit_px: slot.limitPx, stop_px: slot.stopPx,
          limit_type: slot.limitType,
          risk_usd: this.riskUsd,
          reason: `${slot.symbol.split(':').pop()} ${slot.side} ${slot.limitType} 限价`,
        };
        const r = await fetch('/api/lots/limit-stop', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || '下单失败');
        this.$message.success(`已下单 ${data.lot.lot_id}（${slot.symbol} ${slot.side} x${slot.qty} @${slot.limitPx}）`);
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
        this.$message.success('已平仓');
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
    stateTag(s) { return { ACTIVE: 'success', PENDING: 'warning', CANCELLED: 'info', FAILED: 'danger', CLOSED: 'info' }[s] || 'info'; },
  },
}).use(ElementPlus).mount('#app');
