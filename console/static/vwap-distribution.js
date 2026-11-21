'use strict';

const VwapDistribution = {
  props: { frame: Object, progress: Object },
  emits: ['select'],
  data: () => ({ selected: null, pages: { positive: 0, negative: 0, above: 0, below: 0 }, plotWidth: 960, plotHeight: 480, plotReady: false }),
  mounted() {
    this._plotObserver = new ResizeObserver(([entry]) => {
      this.resizePlot(entry.contentRect.width, entry.contentRect.height);
    });
    this._plotObserver.observe(this.$refs.plot);
  },
  beforeUnmount() {
    this._plotObserver?.disconnect();
    if (this._resizeFrame) cancelAnimationFrame(this._resizeFrame);
  },
  watch: {
    plotWidth() { if (this.detail) this.reveal(this.detail); },
    plotHeight() { if (this.detail) this.reveal(this.detail); },
    frame() { if (this.detail) this.reveal(this.detail); },
  },
  computed: {
    latest() { return this.frame?.time ?? 0; },
    rows() {
      return (this.frame?.rows ?? [])
        .filter(row => [-1, 1].includes(row.st_direction) && Number.isFinite(row.position))
        .map(row => ({ ...row, ticker: row.symbol.split(':').pop() }));
    },
    vwapSpeedRows() {
      return this.rows.filter(row => Number.isFinite(row.vwap_speed_30m))
        .sort((a, b) => b.vwap_speed_30m - a.vwap_speed_30m || a.symbol.localeCompare(b.symbol));
    },
    vwapSpeedWaiting() {
      if (!this.rows.length) return '等待完整行情';
      if (!this.rows.some(row => Object.prototype.hasOwnProperty.call(row, 'vwap_speed_30m'))) return '等待加载新版行情数据';
      return '等待开盘参考 VWAP 与有效标准差';
    },
    vwapSpeedGroups() {
      const highest = this.vwapSpeedRows.slice(0, 5);
      const selected = new Set(highest.map(row => row.symbol));
      const lowest = [...this.vwapSpeedRows].sort((a, b) => a.vwap_speed_30m - b.vwap_speed_30m
        || a.symbol.localeCompare(b.symbol)).filter(row => !selected.has(row.symbol)).slice(0, 5);
      return [{key: 'highest', title: '最大 5 个', rows: highest},
        {key: 'lowest', title: '最小 5 个', rows: lowest}];
    },
    regions() {
      const top = 42;
      const bottom = this.plotHeight - 12;
      const aboveCount = this.rows.filter(row => row.position > 1).length;
      const belowCount = this.rows.filter(row => row.position < -1).length;
      const trendHeight = bottom - top - 32;
      const minimumHeight = Math.min(36, trendHeight / 2);
      const total = aboveCount + belowCount;
      const aboveHeight = total
        ? Math.max(minimumHeight, Math.min(trendHeight - minimumHeight, trendHeight * aboveCount / total))
        : trendHeight / 2;
      const split = top + aboveHeight;
      const columnWidth = (this.plotWidth - 88) / 3;
      const middleStart = columnWidth + 44;
      const rightStart = (columnWidth + 44) * 2;
      const upper = Math.max(2, Math.ceil(Math.max(0, ...this.rows.map(row => row.position))));
      const lower = Math.min(-2, Math.floor(Math.min(0, ...this.rows.map(row => row.position))));
      return [
        { key: 'positive', title: '震荡上区间', left: 0, right: columnWidth, top, bottom, low: 0, high: 1,
          ticks: [1, 0.5, 0], fill: '#1a2637' },
        { key: 'negative', title: '震荡下区间', left: middleStart, right: middleStart + columnWidth,
          top, bottom, low: -1, high: 0, ticks: [0, -0.5, -1], fill: '#1a2637' },
        { key: 'above', title: '多头趋势', left: rightStart, right: this.plotWidth, top, bottom: split,
          low: 1, high: upper, ticks: [upper, 1], fill: '#112e25' },
        { key: 'below', title: '空头趋势', left: rightStart, right: this.plotWidth, top: split + 32, bottom,
          low: lower, high: -1, ticks: [-1, lower], fill: '#301d2a' },
      ].map(region => {
        const rows = this.rows.filter(row => this.regionKey(row) === region.key)
          .sort((a, b) => b.position - a.position || a.symbol.localeCompare(b.symbol));
        const laneCount = Math.max(1, Math.floor((region.right - region.left) / 240));
        const laneWidth = (region.right - region.left) / laneCount;
        const bilateral = laneWidth >= 240;
        const sides = bilateral ? 2 : 1;
        const perLane = Math.max(1, Math.floor((region.bottom - region.top) / 36));
        const laneCapacity = perLane * sides;
        const capacity = laneCount * laneCapacity;
        const totalPages = Math.max(1, Math.ceil(rows.length / capacity));
        const page = Math.min(this.pages[region.key], totalPages - 1);
        const lanes = Array.from({ length: laneCount }, (_, index) => {
          const left = region.left + index * laneWidth;
          const right = left + laneWidth;
          const outer = region.key === 'above' || region.key === 'below';
          return { left, right, outer,
            axisX: bilateral ? (left + right) / 2 : outer ? left + 4 : right - 4 };
        });
        const anchors = rows.map((row, index) => {
          const lane = lanes[Math.floor(index % capacity / laneCapacity)];
          return { ...row, rank: index + 1, column: region.key, anchorX: lane.axisX,
            anchorY: this.positionY(row.position, region) };
        });
        const pageRows = anchors.slice(page * capacity, (page + 1) * capacity);
        const visible = pageRows.map((row, index) => {
          const laneIndex = Math.floor(index / laneCapacity);
          const lane = lanes[laneIndex];
          const count = Math.min(laneCapacity, pageRows.length - laneIndex * laneCapacity);
          const gap = (region.bottom - region.top) / Math.ceil(count / sides);
          const localIndex = index % laneCapacity;
          const side = bilateral ? (localIndex % 2 === 0 ? 'left' : 'right') : lane.outer ? 'right' : 'left';
          const x = bilateral ? lane.axisX + (side === 'left' ? -36 : 36) : lane.outer ? lane.left + 38 : lane.right - 38;
          return { ...row, ...lane, lane: laneIndex, side, x, labelX: x + (side === 'left' ? -22 : 22),
            y: region.top + (Math.floor(localIndex / sides) + 0.5) * gap };
        });
        return { ...region, rows, lanes, bilateral, capacity, page, totalPages, anchors, visible };
      });
    },
    points() { return this.paintOrder(this.regions.flatMap(region => region.visible)); },
    anchors() { return this.paintOrder(this.regions.flatMap(region => region.anchors)); },
    paginatedRegions() { return this.regions.filter(region => region.totalPages > 1); },
    detail() { return this.rows.find(row => row.symbol === this.selected); },
    timestamp() {
      return this.latest ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'America/New_York', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).format(this.latest * 1000) : '';
    },
    refreshedAt() {
      const updatedAt = this.frame?.display_updated_at || this.frame?.revision;
      return updatedAt ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
      }).format(updatedAt) : '';
    },
    refreshStatus() {
      if (this.progress?.total && this.progress.time > this.latest) {
        return '正在收集下一批 · ' + this.progress.ready + '/' + this.progress.total;
      }
      if (this.frame?.display_only) {
        const delayed = this.frame.display_delayed || [];
        if (delayed.length) return '已更新 ' + this.rows.length + '/' + this.frame.display_expected
          + ' · 暂缓 ' + delayed.map(symbol => symbol.split(':').pop()).join('、');
        const auxiliary = this.frame.display_auxiliary_waiting || [];
        if (auxiliary.length) return '已更新 · EMA7 速度待补 ' + auxiliary.length;
      }
      if (this.progress?.total && (this.progress.time > this.latest || this.progress.ready < this.progress.total)) {
        return '正在收集下一批 · ' + this.progress.ready + '/' + this.progress.total;
      }
      return this.latest ? (this.refreshedAt ? '实时整批 · ' + this.refreshedAt : '整批已更新') : '';
    },
  },
  methods: {
    resizePlot(width, height) {
      // 隐藏 tab 的 0 尺寸不能覆盖有效布局；同一绘制帧只提交一次尺寸变化。
      if (!(width > 0 && height > 0)) return;
      this._nextPlotSize = { width: Math.max(480, Math.round(width)), height: Math.max(160, Math.round(height)) };
      if (this._resizeFrame) return;
      this._resizeFrame = requestAnimationFrame(() => {
        this._resizeFrame = null;
        this.plotWidth = this._nextPlotSize.width;
        this.plotHeight = this._nextPlotSize.height;
        this.plotReady = true;
      });
    },
    regionKey(row) {
      if (row.position > 1) return 'above';
      if (row.position < -1) return 'below';
      return row.position < 0 ? 'negative' : 'positive';
    },
    positionY(position, bounds) {
      const region = bounds || this.regions.find(item => item.key === this.regionKey({ position }));
      return region.top + (region.high - position) / (region.high - region.low) * (region.bottom - region.top);
    },
    signed(value) { return Number.isFinite(value) ? (value > 0 ? '+' : '') + value.toFixed(2) : '—'; },
    vwapSpeedText(value) { return (value > 0 ? '+' : '') + value.toFixed(3); },
    vwapSpeedTitle(row) {
      const price = value => Number.isFinite(value) ? value.toFixed(4) : '—';
      const reference = row.vwap_reference_time ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).format(row.vwap_reference_time * 1000) : '—';
      return `${row.ticker} · 当前 VWAP ${price(row.vwap)} · 参考 ${reference} ${price(row.vwap_reference)}`
        + `${row.vwap_reference_mode === 'opening' ? '（开盘第 1 分钟）' : ''} · 当前 σ ${price(row.sigma)}`;
    },
    isReference(row) { return ['QQQ', 'NVDA', 'SPY'].includes(row.ticker); },
    paintOrder(rows) {
      const priority = row => row.symbol === this.selected ? 2 : this.isReference(row) ? 1 : 0;
      return [...rows].sort((a, b) => priority(a) - priority(b));
    },
    color(row) { return row.symbol === this.selected ? '#86e7ff' : this.isReference(row) ? '#f5cb70' : '#8498b3'; },
    connector(row) {
      const elbow = row.anchorX + (row.x - row.anchorX) * 0.35;
      return `M${row.anchorX} ${row.anchorY}H${elbow}L${row.x} ${row.y}`;
    },
    pathStyle(path) { return { d: `path("${path}")` }; },
    linkStyle(row) { return this.pathStyle(this.connector(row)); },
    changePage(key, offset) {
      const region = this.regions.find(item => item.key === key);
      this.pages[key] = Math.max(0, Math.min(region.totalPages - 1, region.page + offset));
    },
    reveal(row) {
      const key = this.regionKey(row);
      const region = this.regions.find(item => item.key === key);
      const index = region.rows.findIndex(item => item.symbol === row.symbol);
      this.pages[key] = Math.floor(index / region.capacity);
    },
    select(row) {
      this.selected = row.symbol;
      this.reveal(row);
      this.$emit('select', row.symbol);
    },
  },
  template: `
    <section class="vwap-distribution">
      <header>
        <div class="vwap-heading-main"><strong>VWAP 分布图</strong><span>1 分钟 · EMA7(close) · ST（10，4）</span></div>
        <div class="vwap-update-line" role="status">
          <span>{{ latest ? '美东 ' + timestamp + ' · ' + rows.length + ' 个标的' : '等待完整行情' }}</span>
          <span class="vwap-refresh-status" :class="{'has-delayed': frame?.display_only}"
                :title="refreshStatus">{{ refreshStatus }}</span>
        </div>
      </header>
      <div class="vwap-visuals">
      <div class="vwap-plot" ref="plot">
        <svg v-if="plotReady" v-memo="[frame, selected, pages.positive, pages.negative, pages.above, pages.below, plotWidth, plotHeight]"
             :viewBox="'0 0 ' + plotWidth + ' ' + plotHeight" role="group"
             aria-label="左列震荡上区间，中列震荡下区间，右上多头趋势、右下空头趋势，各区按位置排序并标注序号">
          <g v-for="region in regions" :key="region.key" class="vwap-position-region" :data-region="region.key">
            <title>{{ region.title }} · {{ region.rows.length }} 个标的</title>
            <rect class="vwap-region-body" :style="{ y: region.top + 'px', height: (region.bottom - region.top) + 'px' }"
                  :x="region.left" :y="region.top" :width="region.right - region.left"
                  :height="region.bottom - region.top" :fill="region.fill"/>
            <path v-for="lane in region.lanes" :key="lane.axisX" class="vwap-position-axis"
                  :style="pathStyle('M' + lane.axisX + ' ' + region.top + 'V' + region.bottom)"
                  :d="'M' + lane.axisX + ' ' + region.top + 'V' + region.bottom" stroke="#667a95" stroke-opacity="0.65"/>
            <g v-for="tick in region.ticks" :key="tick" class="vwap-axis-tick" :data-value="tick"
               :class="{ 'is-reference': tick === 0 || Math.abs(tick) === 1 }"
               :style="{ transform: 'translate(0px,' + positionY(tick, region) + 'px)' }">
              <path :d="'M' + region.left + ' 0H' + region.right"
                    :stroke="tick > 0 ? '#3d9b73' : tick < 0 ? '#b15f76' : '#71849e'" stroke-dasharray="3 4"/>
              <text :x="region.key === 'positive' || region.key === 'negative' ? region.right + 3 : region.left - 3"
                    y="3" :text-anchor="region.key === 'positive' || region.key === 'negative' ? 'start' : 'end'"
                    fill="#c2d0e1" font-size="10">{{ tick > 0 ? '+' : '' }}{{ tick }}</text>
            </g>
          </g>
          <g v-for="region in regions" :key="'title-' + region.key" class="vwap-region-heading"
             :data-region-title="region.key"
             :style="{ transform: 'translate(' + region.left + 'px,' + (region.key === 'below' ? region.top - 30 : 6) + 'px)' }">
            <rect x="1" y="0" :width="region.right - region.left - 2" height="28" rx="4"/>
            <text x="12" y="20">{{ region.title }}</text>
          </g>
          <text v-if="!rows.length" :x="plotWidth / 2" :y="plotHeight / 2" text-anchor="middle" fill="#bdc9db">
            等待同一分钟的各标的数据齐备
          </text>
          <path v-for="row in points" :key="'link-' + row.symbol" class="vwap-position-link"
                :data-symbol="row.symbol" :data-selected="row.symbol === selected"
                :d="connector(row)" :style="linkStyle(row)" fill="none" :stroke="color(row)"
                :stroke-width="row.symbol === selected ? 2.5 : isReference(row) ? 1.5 : 1"
                :opacity="row.symbol === selected ? 1 : isReference(row) ? 0.8 : 0.3" pointer-events="none"/>
          <g v-for="row in points" :key="row.symbol" class="vwap-ranked-stock"
             :style="{ transform: 'translate(' + row.x + 'px,' + row.y + 'px)' }"
             :data-symbol="row.symbol" :data-column="row.column" :data-lane="row.lane" :data-side="row.side"
             :data-rank="row.rank" :data-selected="row.symbol === selected"
             role="button" tabindex="0" :aria-label="row.ticker + '，第 ' + row.rank + ' 名，位置 ' + signed(row.position)"
             :aria-pressed="row.symbol === selected"
             @click="select(row)" @keydown.enter.prevent="select(row)" @keydown.space.prevent="select(row)">
            <title>{{ row.ticker }} · 第 {{ row.rank }} 名 · 位置 {{ signed(row.position) }}σ · 速度 {{ signed(row.speed) }}</title>
            <rect :x="row.side === 'left' ? row.left + 2 - row.x : 20" y="-17"
                  :width="row.side === 'left' ? row.x - row.left - 22 : row.right - row.x - 22" height="34" rx="4"
                  :fill="row.symbol === selected ? '#1e3e51' : '#101823'"/>
            <circle r="16" :fill="row.st_direction === 1 ? '#176047' : '#8a3e52'"
                    :stroke="color(row)" :stroke-width="isReference(row) || row.symbol === selected ? 2.5 : 1"/>
            <text text-anchor="middle" dominant-baseline="central" fill="white" font-size="9.5" font-weight="700">{{ row.ticker }}</text>
            <g :text-anchor="row.side === 'left' ? 'end' : 'start'"
               :fill="row.symbol === selected ? '#c6f5ff' : '#dce7f5'">
              <text :x="row.labelX - row.x" y="-2" font-size="10.5" font-weight="600">{{ row.rank }} · {{ signed(row.position) }}σ</text>
              <text :x="row.labelX - row.x" y="11" font-size="9" fill="#a8b9cf">速度 {{ signed(row.speed) }}</text>
            </g>
          </g>
          <g v-for="row in anchors" :key="'anchor-' + row.symbol" class="vwap-position-anchor"
             :style="{ transform: 'translate(' + row.anchorX + 'px,' + row.anchorY + 'px)' }"
             :data-symbol="row.symbol" :data-column="row.column" :data-selected="row.symbol === selected"
             role="button" tabindex="0" :aria-label="row.ticker + '，位置 ' + signed(row.position)"
             :aria-pressed="row.symbol === selected"
             @click="select(row)" @keydown.enter.prevent="select(row)" @keydown.space.prevent="select(row)">
            <title>{{ row.ticker }} · {{ signed(row.position) }}σ</title>
            <circle r="7" fill="transparent"/>
            <circle :r="row.symbol === selected ? 5 : isReference(row) ? 4 : 3"
                    :fill="color(row)" :stroke="row.symbol === selected ? '#e5fbff' : '#101823'"/>
          </g>
        </svg>
      </div>
      <aside class="vwap-speed-ranking" :class="{'is-compact': plotHeight < 270}" aria-label="VWAP 三十分钟归一化速度排名">
        <header class="vwap-speed-heading" :title="'(当前 VWAP − 参考 VWAP) ÷ 当前标准差；未满 30 分钟取开盘第 1 分钟，否则取 30 分钟前。已就绪 ' + vwapSpeedRows.length + '/' + rows.length">
          <strong>VWAP 速度排名</strong><small>Δ30m/σ</small>
        </header>
        <p v-if="!vwapSpeedRows.length" class="vwap-speed-empty">{{ vwapSpeedWaiting }}</p>
        <div v-else class="vwap-speed-lists">
          <section v-for="group in vwapSpeedGroups" :key="group.key" :data-speed-group="group.key" :aria-label="group.title">
            <h4>{{ group.title }}</h4>
            <div class="vwap-speed-entries">
              <button v-for="(row, index) in group.rows" :key="row.symbol" type="button" class="vwap-speed-stock"
                :data-symbol="row.symbol" :data-selected="row.symbol === selected"
                :class="{'is-selected': row.symbol === selected, 'is-reference': isReference(row)}"
                :aria-pressed="row.symbol === selected" :aria-label="row.ticker + '，VWAP 三十分钟速度 ' + vwapSpeedText(row.vwap_speed_30m) + 'σ'"
                :title="vwapSpeedTitle(row)"
                @click="select(row)">
                <span class="vwap-speed-rank">{{ index + 1 }}</span><strong>{{ row.ticker }}</strong>
                <span class="vwap-speed-value" :class="row.vwap_speed_30m >= 0 ? 'is-positive' : 'is-negative'">{{ vwapSpeedText(row.vwap_speed_30m) }}σ</span>
              </button>
              <span v-if="!group.rows.length" class="vwap-speed-empty">暂无更多已就绪标的</span>
            </div>
          </section>
        </div>
      </aside>
      </div>
      <nav v-if="paginatedRegions.length" class="vwap-ranking-nav" aria-label="轨道位置分页">
        <div v-for="region in paginatedRegions" :key="region.key">
          <button :disabled="region.page === 0" :aria-label="region.title + '上一页'" @click="changePage(region.key, -1)">‹</button>
          <span>{{ region.title }} {{ region.page + 1 }}/{{ region.totalPages }}</span>
          <button :disabled="region.page + 1 >= region.totalPages" :aria-label="region.title + '下一页'" @click="changePage(region.key, 1)">›</button>
        </div>
      </nav>
      <footer v-if="detail">{{ detail.ticker }} · 下轨 {{ detail.lower.toFixed(2) }} · VWAP {{ detail.vwap.toFixed(2) }} ·
        上轨 {{ detail.upper.toFixed(2) }} · EMA7 {{ detail.ema.toFixed(2) }} · 位置 {{ signed(detail.position) }}σ · EMA7 速度 {{ signed(detail.speed) }}
        <span v-if="Number.isFinite(detail.vwap_speed_30m)"> · VWAP 30m {{ vwapSpeedText(detail.vwap_speed_30m) }}σ</span></footer>
    </section>`,
};
