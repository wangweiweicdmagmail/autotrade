#!/usr/bin/env node
/**
 * tv_control.js — Local TradingView Desktop client control via CDP.
 *
 * Built on top of the open-source project tradesdontlie/tradingview-mcp (MIT),
 * whose connection layer is vendored at controller/vendor/tradingview-mcp/.
 *
 * Provides three functions (CommonJS exports + CLI):
 *   listPanes()              -> panes in visual row-first order (symbol, def index, rect)
 *   setGridSymbols(symbols)  -> set symbols on panes in visual row-first order
 *   centerAll()              -> center the latest bar of every pane
 *
 * Internal API strategy:
 *   Primary : window.TradingViewApi._chartWidgetCollection  (tradingview-mcp path)
 *   Fallback: window._exposed_chartWidgetCollection         (probed on this TV build)
 * Neither collection is ordered visually (def order != row/column order), so
 * panes are always re-ordered geometrically using each chart widget's DOM rect.
 *
 * CLI:
 *   node controller/tv_control.js listPanes [--json]
 *   node controller/tv_control.js setGridSymbols NASDAQ:AAPL,NASDAQ:MSFT,... [--json]
 *   node controller/tv_control.js setGridSymbols '["NASDAQ:AAPL","NASDAQ:MSFT"]' [--json]
 *   node controller/tv_control.js centerAll [--json]
 *
 * Env: TV_CDP_HOST (default 127.0.0.1), TV_CDP_PORT (default 9222).
 */
'use strict';

const conn = require('./vendor/tradingview-mcp/connection.cjs');

/**
 * In-page helper (evaluated in the TradingView chart tab).
 * Collects all chart widgets, reads symbol/resolution, computes each pane's
 * DOM rect, and returns them in visual row-first order.
 * Exposed on window.__tvCtl so follow-up calls can reuse the widget refs.
 */
const COLLECT_SRC = `
(function () {
  function readSymbol(cw) {
    try { var s = cw.symbol(); return (s && s.value) ? s.value() : s; } catch (e) {}
    try { return cw.model().mainSeries().symbol(); } catch (e) { return null; }
  }
  function readResolution(cw) {
    try { return cw.model().mainSeries().interval(); } catch (e) { return null; }
  }
  function rectOf(cw, def) {
    try {
      if (cw._mainDiv && cw._mainDiv.getBoundingClientRect) {
        var b = cw._mainDiv.getBoundingClientRect();
        return { x: b.x, y: b.y, w: b.width, h: b.height };
      }
    } catch (e) {}
    // Fallback: match def width/height against DOM chart containers.
    try {
      var dw = def ? (typeof def.width === 'function' ? def.width() : def.width) : null;
      var dh = def ? (typeof def.height === 'function' ? def.height() : def.height) : null;
      if (dw && dh) {
        var best = null, bestScore = 1e9;
        var els = document.querySelectorAll('[class*="chart-container"]');
        for (var i = 0; i < els.length; i++) {
          var r = els[i].getBoundingClientRect();
          var score = Math.abs(r.width - dw) + Math.abs(r.height - dh);
          if (score < bestScore) { bestScore = score; best = r; }
        }
        if (best && bestScore < 20) return { x: best.x, y: best.y, w: best.width, h: best.height };
      }
    } catch (e) {}
    return null;
  }
  function collect() {
    var widgets = [];
    var exposedDefs = (window._exposed_chartWidgetCollection
      && window._exposed_chartWidgetCollection._chartWidgetsDefs) || null;
    var used = null;
    // Primary: tradingview-mcp path
    try {
      var cwc = window.TradingViewApi && window.TradingViewApi._chartWidgetCollection;
      if (cwc && typeof cwc.getAll === 'function') {
        var all = cwc.getAll();
        if (all && all.length) {
          used = 'TradingViewApi._chartWidgetCollection';
          widgets = all.map(function (cw) {
            var defIndex = -1, def = null;
            if (exposedDefs) {
              for (var i = 0; i < exposedDefs.length; i++) {
                if (exposedDefs[i].chartWidget === cw) { defIndex = i; def = exposedDefs[i]; break; }
              }
            }
            return { cw: cw, defIndex: defIndex, def: def };
          });
        }
      }
    } catch (e) {}
    // Fallback: probed _exposed path
    if (!widgets.length) {
      if (!exposedDefs || !exposedDefs.length) {
        throw new Error('No chart widget collection found (tried window.TradingViewApi._chartWidgetCollection and window._exposed_chartWidgetCollection)');
      }
      used = '_exposed_chartWidgetCollection';
      widgets = exposedDefs.map(function (d, i) { return { cw: d.chartWidget, defIndex: i, def: d }; });
    }
    var panes = widgets.map(function (w, i) {
      if (w.defIndex < 0) w.defIndex = i;
      return {
        cw: w.cw,
        defIndex: w.defIndex,
        symbol: readSymbol(w.cw),
        resolution: readResolution(w.cw),
        rect: rectOf(w.cw, w.def),
      };
    });
    // 过滤不可见 widget：切换布局（如六宫格→四宫格）后，被移除的 pane
    // 仍留在 collection 里但 rect 为 0x0，不过滤会把 symbol 灌到隐藏 pane 上
    var visible = panes.filter(function (p) {
      return p.rect && p.rect.w > 50 && p.rect.h > 50;
    });
    // Visual row-first order: bucket rows by y, sort rows by y then panes by x.
    var sorted = visible.slice().sort(function (a, b) {
      var ay = a.rect ? a.rect.y : 1e9, by = b.rect ? b.rect.y : 1e9;
      var ax = a.rect ? a.rect.x : 1e9, bx = b.rect ? b.rect.x : 1e9;
      return (ay - by) || (ax - bx);
    });
    var rows = [];
    sorted.forEach(function (p) {
      var y = p.rect ? p.rect.y : 1e9;
      var h = p.rect ? p.rect.h : 0;
      var row = rows.find(function (r) { return Math.abs(r.y - y) < Math.max(30, 0.5 * (r.h || h)); });
      if (row) { row.panes.push(p); }
      else { rows.push({ y: y, h: h, panes: [p] }); }
    });
    rows.sort(function (a, b) { return a.y - b.y; });
    var ordered = [];
    rows.forEach(function (r) {
      r.panes.sort(function (a, b) { return (a.rect ? a.rect.x : 0) - (b.rect ? b.rect.x : 0); });
      r.panes.forEach(function (p) { ordered.push(p); });
    });
    window.__tvCtlPanes = ordered.map(function (p) { return p.cw; });
    return {
      source: used,
      count: ordered.length,
      panes: ordered.map(function (p, i) {
        return { visualIndex: i, defIndex: p.defIndex, symbol: p.symbol, resolution: p.resolution, rect: p.rect };
      }),
    };
  }
  return collect();
})()
`;

function tickerOf(symbol) {
  if (!symbol) return symbol;
  const s = String(symbol);
  const i = s.indexOf(':');
  return i >= 0 ? s.slice(i + 1) : s;
}

/** List panes in visual row-first order. */
async function listPanes() {
  const result = await conn.evaluate(COLLECT_SRC);
  return { success: true, source: result.source, count: result.count, panes: result.panes };
}

/**
 * Set symbols on panes in visual row-first order.
 * Tolerates exchange normalization (e.g. NASDAQ:AAPL -> BATS:AAPL):
 * read-back verification compares only the ticker part.
 */
async function setGridSymbols(symbols) {
  if (!Array.isArray(symbols) || symbols.length === 0) {
    throw new Error('setGridSymbols requires a non-empty array of symbols');
  }
  // Establish visual order and stash widget refs on window.__tvCtlPanes.
  const before = await conn.evaluate(COLLECT_SRC);
  const n = Math.min(symbols.length, before.count);

  // 1) 同步灌入（页内 JS 不能用 setTimeout —— TV 窗口后台时
  //    Chromium 会把定时器节流到分钟级，导致 evaluateAsync 挂起）
  const setSrc = `
(function () {
  var symbols = ${JSON.stringify(symbols.slice(0, n).map(String))};
  var panes = window.__tvCtlPanes || [];
  var results = [];
  for (var i = 0; i < symbols.length && i < panes.length; i++) {
    try {
      var cw = panes[i];
      if (typeof cw.setSymbol !== 'function') throw new Error('chartWidget.setSymbol is not a function');
      try { cw.setSymbol(symbols[i]); } catch (e1) { cw.setSymbol(symbols[i], {}); }
      results.push({ visualIndex: i, requested: symbols[i], ok: true });
    } catch (e) {
      results.push({ visualIndex: i, requested: symbols[i], ok: false, error: e.message });
    }
  }
  return results;
})()
`;
  const results = await conn.evaluate(setSrc);

  // 2) Node 侧等待（不占用页内定时器），再回读校验
  await new Promise((r) => setTimeout(r, 2000));
  const readbackSrc = `
(function () {
  var panes = window.__tvCtlPanes || [];
  return panes.slice(0, ${n}).map(function (cw) {
    var sym = null;
    try { var s = cw.symbol(); sym = (s && s.value) ? s.value() : s; } catch (e) {}
    if (!sym) { try { sym = cw.model().mainSeries().symbol(); } catch (e) {} }
    return sym;
  });
})()
`;
  const readback = await conn.evaluate(readbackSrc);

  const panes = results.map((r, i) => {
    const actual = readback[i] || null;
    const verified = r.ok && tickerOf(actual) === tickerOf(r.requested);
    return { ...r, actual, verified };
  });
  return {
    success: panes.every((p) => p.verified),
    source: before.source,
    paneCount: before.count,
    applied: n,
    panes,
  };
}

/** Center the latest bar of every pane (setRightOffset = width/barSpacing/2). */
async function centerAll() {
  const src = `
(function () {
  var panes = window.__tvCtlPanes;
  if (!panes || !panes.length) {
    void ${COLLECT_SRC.trim()};
  }
  panes = window.__tvCtlPanes;
  var out = [];
  for (var i = 0; i < panes.length; i++) {
    try {
      var ts = panes[i].model().m_model.timeScale();
      ts.setRightOffset(ts.width() / ts.barSpacing() / 2);
      out.push({ visualIndex: i, ok: true });
    } catch (e) {
      out.push({ visualIndex: i, ok: false, error: e.message });
    }
  }
  return out;
})()
`;
  const results = await conn.evaluate(src);
  return { success: results.every((r) => r.ok), count: results.length, panes: results };
}

// --- 图表内嵌交易提案卡（人机确认交互，PoC） ---

function escHtml(v) {
  return String(v == null ? '' : v).replace(/[<>&"']/g, '');
}

/**
 * 在 TV 窗口右侧停靠栏注入一张交易提案卡（纵向堆叠，遮住自选表区域，
 * 不遮挡宫格 K 线）。卡片不再依赖标的在宫格内。
 * proposal: { id?, symbol, side: 'long'|'short', entry, stop, qty, risk, reason? }
 * 按钮点击结果写入 window.__tvDecisions，由 pollDecisions() 取走。
 * 页内不做任何 setTimeout 等待（TV 窗口后台时定时器被节流），
 * 卡片/停靠栏消失（TV React 重渲染抹掉注入 DOM）由调用方重新 showProposal。
 * 停靠栏容器 pointer-events:none、卡片 auto：不拦截底层 UI 的鼠标操作。
 */
async function showProposal(proposal) {
  if (!proposal || !proposal.symbol) {
    throw new Error('showProposal requires { symbol, side, entry, stop, qty, risk }');
  }
  const p = {
    id: proposal.id || ('p' + Date.now().toString(36)),
    symbol: String(proposal.symbol),
    side: proposal.side === 'short' ? 'short' : 'long',
    entry: proposal.entry, stop: proposal.stop,
    qty: proposal.qty, risk: proposal.risk,
    reason: proposal.reason || '',
    badge: proposal.badge || '',
  };
  const injectSrc = `
(function () {
  var p = ${JSON.stringify(p)};
  window.__tvDecisions = window.__tvDecisions || [];
  var old = document.getElementById('tv-prop-' + p.id);
  if (old) old.remove();
  // 右侧停靠面板：单例容器，不透明，精确覆盖右侧自选表区域，卡片纵向堆叠
  var dock = document.getElementById('tv-prop-dock');
  if (!dock) {
    dock = document.createElement('div');
    dock.id = 'tv-prop-dock';
    dock.style.cssText = 'position:fixed;z-index:999999;background:#131722;'
      + 'border-left:1px solid #2a2e39;display:flex;flex-direction:column;box-sizing:border-box';
    var head = document.createElement('div');
    head.style.cssText = 'padding:10px 12px;border-bottom:1px solid #2a2e39;color:#d1d4dc;'
      + 'font:600 13px/1.4 -apple-system,sans-serif;display:flex;justify-content:space-between;'
      + 'align-items:baseline;flex:none';
    head.innerHTML = '<span>信号提案</span>'
      + '<span id="tv-prop-dock-count" style="color:#787b86;font-weight:400"></span>';
    var bodyEl = document.createElement('div');
    bodyEl.id = 'tv-prop-dock-body';
    bodyEl.style.cssText = 'flex:1;overflow-y:auto;padding:8px;display:flex;'
      + 'flex-direction:column;gap:8px;pointer-events:none';
    dock.appendChild(head);
    dock.appendChild(bodyEl);
    document.body.appendChild(dock);
    // 窗口尺寸变化时重定位（事件驱动而非定时器，不受后台节流影响）
    window.addEventListener('resize', function () {
      if (window.__tvPlaceDock) window.__tvPlaceDock();
    });
  }
  // 定位：实时测量右侧面板（自选表）rect 并精确覆盖；面板不存在时退化为右缘固定宽度
  window.__tvPlaceDock = function () {
    var d = document.getElementById('tv-prop-dock');
    if (!d) return;
    var panel = null;
    var sels = ['.layout__area--right', '[class*="layout__area--right"]'];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) {
        var r = el.getBoundingClientRect();
        if (r.width > 100 && r.height > 200) { panel = r; break; }
      }
    }
    if (panel) {
      d.style.left = panel.x + 'px'; d.style.top = panel.y + 'px';
      d.style.width = panel.width + 'px'; d.style.height = panel.height + 'px';
      d.style.right = 'auto'; d.style.bottom = 'auto';
    } else {
      d.style.left = 'auto'; d.style.top = '48px'; d.style.right = '0';
      d.style.bottom = '0'; d.style.width = '300px'; d.style.height = 'auto';
    }
  };
  window.__tvPlaceDock();
  var dockBody = document.getElementById('tv-prop-dock-body');
  var card = document.createElement('div');
  card.id = 'tv-prop-' + p.id;
  var dirColor = p.side === 'long' ? '#26a69a' : '#ef5350';
  var dirLabel = p.side === 'long' ? '做多' : '做空';
  card.style.cssText = 'position:relative;width:100%;box-sizing:border-box;pointer-events:auto;'
    + 'background:#1e222d;border:1px solid #2a2e39;border-left:3px solid ' + dirColor + ';'
    + 'border-radius:8px;color:#d1d4dc;font:13px/1.6 -apple-system,sans-serif;padding:10px 12px;'
    + 'box-shadow:0 4px 16px rgba(0,0,0,.5)';
  var inputStyle = 'width:72px;background:#131722;border:1px solid #434651;border-radius:4px;'
    + 'color:#d1d4dc;padding:1px 4px;text-align:right';
  card.innerHTML = ''
    + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">'
    +   '<b style="color:' + dirColor + ';font-size:14px">${escHtml(tickerOf(p.symbol))} ' + dirLabel + '</b>'
    +   '<span style="color:#787b86;font-size:11px">信号提案' + (p.badge ? ' · 格${escHtml(p.badge)}' : '') + '</span>'
    + '</div>'
    + '<div>入场 <span style="float:right">${escHtml(p.entry)}</span></div>'
    + '<div>止损 <input id="tv-prop-stop-' + p.id + '" value="${escHtml(p.stop)}" style="' + inputStyle + '"></div>'
    + '<div>仓位 <input id="tv-prop-qty-' + p.id + '" value="${escHtml(p.qty)}" style="' + inputStyle + '"> 股</div>'
    + '<div>风险 <span style="float:right;color:#ef5350">$${escHtml(p.risk)}</span></div>'
    + (p.reason ? '<div style="color:#787b86;font-size:11px;margin-top:2px">${escHtml(p.reason)}</div>' : '')
    + '<div style="display:flex;gap:8px;margin-top:8px">'
    +   '<button id="tv-prop-ok-' + p.id + '" style="flex:1;background:' + dirColor + ';color:#fff;border:0;border-radius:4px;padding:5px 0;cursor:pointer">批准</button>'
    +   '<button id="tv-prop-no-' + p.id + '" style="flex:1;background:#2a2e39;color:#d1d4dc;border:1px solid #434651;border-radius:4px;padding:5px 0;cursor:pointer">拒绝</button>'
    + '</div>';
  dockBody.appendChild(card);
  var cntEl = document.getElementById('tv-prop-dock-count');
  if (cntEl) cntEl.textContent = dockBody.children.length + ' 张待决策';
  function decide(action) {
    var qtyEl = document.getElementById('tv-prop-qty-' + p.id);
    var stopEl = document.getElementById('tv-prop-stop-' + p.id);
    window.__tvDecisions.push({
      id: p.id, action: action, symbol: p.symbol,
      qty: qtyEl && qtyEl.value !== '' ? Number(qtyEl.value) : p.qty,
      stop: stopEl && stopEl.value !== '' ? Number(stopEl.value) : p.stop,
      ts: Date.now(),
    });
    var els = card.querySelectorAll('button,input');
    for (var i = 0; i < els.length; i++) els[i].disabled = true;
    var tag = document.createElement('div');
    tag.style.cssText = 'text-align:center;margin-top:6px;color:#787b86';
    tag.textContent = action === 'approve' ? '已批准，等待系统下单' : '已拒绝';
    card.appendChild(tag);
  }
  // 用 addEventListener 绑定：页内 CSP 可能拦截内联 onclick
  document.getElementById('tv-prop-ok-' + p.id).addEventListener('click', function () { decide('approve'); });
  document.getElementById('tv-prop-no-' + p.id).addEventListener('click', function () { decide('reject'); });
  return { id: p.id, docked: true };
})()
`;
  const placed = await conn.evaluate(injectSrc);
  return { success: true, ...placed };
}

/** 取走全部待处理决策（读即清空），并自动移除已决策的卡片。 */
async function pollDecisions() {
  const src = `
(function () {
  var d = window.__tvDecisions || [];
  window.__tvDecisions = [];
  // 已决策卡片自动移除，面板自清洁
  for (var i = 0; i < d.length; i++) {
    var el = document.getElementById('tv-prop-' + d[i].id);
    if (el) el.remove();
  }
  var body = document.getElementById('tv-prop-dock-body');
  var cntEl = document.getElementById('tv-prop-dock-count');
  if (body && cntEl) {
    cntEl.textContent = body.children.length > 0
      ? body.children.length + ' 张待决策' : '';
  }
  return d;
})()
`;
  const decisions = await conn.evaluate(src);
  return { success: true, count: decisions.length, decisions };
}

/** 移除指定提案卡。 */
async function hideProposal(id) {
  if (!id) throw new Error('hideProposal requires an id');
  const src = `
(function () {
  var el = document.getElementById('tv-prop-' + ${JSON.stringify(String(id))});
  if (el) { el.remove(); return true; }
  return false;
})()
`;
  return { success: true, removed: await conn.evaluate(src) };
}

/** 清空停靠栏（移除全部提案卡）。 */
async function clearProposals() {
  const src = `
(function () {
  var dock = document.getElementById('tv-prop-dock');
  if (!dock) return 0;
  var body = document.getElementById('tv-prop-dock-body');
  var n = body ? body.children.length : 0;
  dock.remove();
  return n;
})()
`;
  return { success: true, removed: await conn.evaluate(src) };
}

/**
 * 批量呈现提案：先把提案标的按顺序灌入宫格（卡 1 → 第一格，视觉行优先），
 * 再按同序把卡片堆进右侧停靠栏，卡片角标「格 N」与宫格位置一一对应。
 * proposals: [{ symbol, side, entry, stop, qty, risk, reason? }, ...]
 */
async function showProposals(proposals) {
  if (!Array.isArray(proposals) || proposals.length === 0) {
    throw new Error('showProposals requires a non-empty JSON array');
  }
  // 1) 宫格标的与卡片顺序对齐（只改前 N 格，其余保持）
  const grid = await setGridSymbols(proposals.map((p) => String(p.symbol)));
  // 2) 清掉上一批卡片
  await clearProposals();
  // 3) 依序注入，停靠栏纵向顺序 = 宫格视觉行优先顺序
  const cards = [];
  for (let i = 0; i < proposals.length; i++) {
    cards.push(await showProposal({ ...proposals[i], badge: i + 1 }));
  }
  return {
    success: grid.success && cards.every((c) => c.success),
    gridApplied: grid.applied,
    gridVerified: grid.success,
    cards,
  };
}

// --- CLI ---

function parseSymbols(arg) {
  if (!arg) throw new Error('Usage: tv_control.js setGridSymbols SYM1,SYM2,...  (or a JSON array)');
  const t = arg.trim();
  if (t.startsWith('[')) return JSON.parse(t);
  return t.split(',').map((s) => s.trim()).filter(Boolean);
}

async function dispatch(cmd, args) {
  switch (cmd) {
    case 'listPanes':
      return listPanes();
    case 'setGridSymbols':
      return setGridSymbols(Array.isArray(args) ? args : parseSymbols(args));
    case 'centerAll':
      return centerAll();
    case 'showProposal':
      // CLI 路径 args 是字符串，daemon 路径已是解析好的对象
      return showProposal(typeof args === 'string' ? JSON.parse(args) : args);
    case 'pollDecisions':
      return pollDecisions();
    case 'hideProposal':
      return hideProposal(args);
    case 'clearProposals':
      return clearProposals();
    case 'showProposals':
      return showProposals(typeof args === 'string' ? JSON.parse(args) : args);
    default:
      throw new Error(`Unknown command "${cmd || ''}". Commands: listPanes, setGridSymbols, centerAll, showProposal, showProposals, pollDecisions, hideProposal, clearProposals`);
  }
}

/**
 * Daemon 模式：常驻进程，stdin/stdout 按行 JSON-RPC。
 * 请求:  {"id": 1, "cmd": "setGridSymbols", "args": ["NASDAQ:QQQ"]}
 * 响应:  {"id": 1, "result": {...}} 或 {"id": 1, "error": "..."}
 * 避免每次调用重建 CDP 连接（重连退避实测可达 40s+）。
 */
function runDaemon() {
  const readline = require('readline');
  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: null, error: 'bad json' }) + '\n');
      return;
    }
    try {
      // CDP evaluate 偶发挂起（chrome-remote-interface 无内置超时），
      // 25s 兜底返回错误，避免 daemon 永久卡死
      const result = await Promise.race([
        dispatch(req.cmd, req.args),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('dispatch timeout (25s)')), 25000)),
      ]);
      process.stdout.write(JSON.stringify({ id: req.id, result }) + '\n');
    } catch (err) {
      process.stdout.write(JSON.stringify({ id: req.id, error: err.message }) + '\n');
    }
  });
  process.stderr.write('tv_control daemon ready\n');
}

async function main(argv) {
  const json = argv.includes('--json');
  const args = argv.filter((a) => a !== '--json');
  const cmd = args[0];
  const out = await dispatch(cmd, args.slice(1).join(' ') || undefined);
  process.stdout.write((json ? JSON.stringify(out) : JSON.stringify(out, null, 2)) + '\n');
}

if (require.main === module) {
  if (process.argv[2] === 'daemon') {
    runDaemon();   // 常驻模式，不退出
  } else {
    main(process.argv.slice(2))
      .then(async () => {
        // conn.disconnect() 偶尔挂起导致进程不退，加 2s 强制退出
        await Promise.race([
          conn.disconnect(),
          new Promise((r) => setTimeout(r, 2000)),
        ]);
        process.exit(0);
      })
      .catch((err) => {
        process.stderr.write(`error: ${err.message}\n`);
        Promise.race([
          conn.disconnect(),
          new Promise((r) => setTimeout(r, 2000)),
        ]).finally(() => process.exit(1));
      });
  }
}

module.exports = { listPanes, setGridSymbols, centerAll, showProposal, showProposals, pollDecisions, hideProposal, clearProposals, tickerOf };
