const test = require('node:test');
const assert = require('node:assert/strict');
const { calculateVwap1m, candidateProtectionSnapshot, createVwapMinuteBatch, openingVwapSnapshot } = require('../service/vwap_1m');
const { createCalculator, getDefinition } = require('../service/pinets_runtime');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const first = Date.parse('2026-11-13T14:30:00Z') / 1000;
const bar = (minute, price, volume = 100) =>
  ({ time: first + minute * 60, open: price, high: price, low: price, close: price, volume });

test('candidate ST(10,3) uses only closed regular minutes, preserving the existing ST(10,4)', async () => {
  const source = '//@version=6\nindicator("ST test")\n[line, direction] = ta.supertrend(3.0, 10)\nplot(line, "Line")\nplot(-direction, "Side")';
  const calc=createCalculator({key:'candidate_st_test',model:{interval_seconds:60},source,plots:{price:'Line',direction:'Side'}},
    {version:require('../package.json').dependencies.pinets});
  const bars=Array.from({length:95},(_,i)=>({...bar(i,100+Math.sin(i/10)*4),
    high:101+Math.sin(i/10)*4,low:99+Math.sin(i/10)*4}));
  const rows=await calc({symbol:'NASDAQ:QQQ',bars,metadata:{timezone:'America/New_York',minmove:1,pricescale:100,session:'regular'}});
  for(let i=20;i<90;i++){
    const now=bars[i].time+5;
    const actual=candidateProtectionSnapshot(bars.slice(0,i+1),now),expected=rows[i-1];
    assert(actual);assert.equal(actual.time,bars[i-1].time);
    assert.equal(actual.direction,expected.direction);
    assert.ok(Math.abs(actual.price-expected.price)<1e-7);
    const changed=[...bars.slice(0,i),{...bars[i],close:500,high:500}];
    assert.deepEqual(candidateProtectionSnapshot(changed,now),actual);
    assert.deepEqual(calculateVwap1m(bars.slice(0,i)),calculateVwap1m(bars.slice(0,i),{stMultiplier:4}));
  }
  assert.equal(candidateProtectionSnapshot(bars.slice(0,9),bars[9].time),null);
  const gap=bars.slice(0,30).filter((_,i)=>i!==28);
  assert.equal(candidateProtectionSnapshot(gap,bars[30].time),null);
  assert.equal(candidateProtectionSnapshot(bars, bars.at(-1).time+200),null);
});

test('candidate ST preserves previous regular-session warm-up across a scheduled overnight break',()=>{
  const prior=Array.from({length:30},(_,i)=>({...bar(i,99+i/10),time:first-86400+(360+i)*60,high:104,low:98}));
  const opening={...bar(0,101),high:103,low:100};
  const data=[...prior,opening,{...bar(1,500),high:500}];
  const read=candidateProtectionSnapshot(data,first+65);
  const expected=calculateVwap1m([...prior,opening],{stMultiplier:3});
  assert(read);assert.equal(read.time,opening.time);assert.equal(read.price,expected.st_price);
  assert.equal(read.direction,expected.st_direction);
  assert.equal(candidateProtectionSnapshot([...prior,bar(2,101)],first+185),null);
});

test('premarket and after-hours bars do not affect any one-minute value', () => {
  const regular = Array.from({ length: 12 }, (_, i) => bar(i, 100 + i));
  const mixed = [bar(-2, 400), bar(-1, 300), ...regular, bar(390, 500)];
  assert.deepEqual(calculateVwap1m(mixed), calculateVwap1m(regular));
  const opening = calculateVwap1m(mixed.slice(0, 3));
  assert.equal(opening.vwap, 100);
  assert.equal(opening.ema, 100);
  assert.equal(opening.st_direction, null);
  assert.equal(opening.speed, null);
});

test('new session uses prior regular EMA but requires seven minutes of current session data for speed', () => {
  const monday = 3 * 24 * 60;
  const read = calculateVwap1m([bar(389, 100), bar(390, 200), bar(monday - 1, 300), bar(monday, 104)]);
  assert.equal(read.vwap, 104);
  assert.equal(read.ema, 101);
  assert.equal(read.speed, null);
  assert.equal(calculateVwap1m([bar(-1, 100), bar(390, 200)]), null);
});

test('speed uses the exact seven-minute EMA when intermediate bars are missing', () => {
  const result = calculateVwap1m([bar(0, 100), bar(4, 108), bar(7, 116)]);
  assert.equal(result.ema, 105.5);
  assert.equal(result.speed, 5.5);
  assert.equal(calculateVwap1m([bar(0, 100), bar(4, 108), bar(8, 116)]).speed, null);
});

test('VWAP speed uses volume-weighted VWAP drift and current sigma, with the opening reference before 30 minutes', () => {
  const bars = Array.from({length: 61}, (_, i) => ({...bar(i, 100+i/5, 100+i*7),
    high: 101+i/5, low: 99+i/5, close: 100.1+i/5}));
  const weighted = rows => {
    const volume = rows.reduce((sum, row) => sum+row.volume, 0);
    const mean = rows.reduce((sum, row) => sum+(row.high+row.low+row.close)/3*row.volume, 0)/volume;
    const sigma = Math.sqrt(rows.reduce((sum, row) => sum+((row.high+row.low+row.close)/3-mean)**2*row.volume, 0)/volume);
    return {mean, sigma};
  };
  for (const minute of [1, 7, 29, 30, 31, 60]) {
    const actual = calculateVwap1m(bars.slice(0, minute+1));
    const current = weighted(bars.slice(0, minute+1));
    const referenceIndex = Math.max(0, minute-30);
    const reference = weighted(bars.slice(0, referenceIndex+1)).mean;
    assert.ok(Math.abs(actual.vwap-current.mean) < 1e-10);
    assert.ok(Math.abs(actual.sigma-current.sigma) < 1e-9);
    assert.ok(Math.abs(actual.vwap_reference-reference) < 1e-10);
    assert.ok(Math.abs(actual.vwap_speed_30m-(current.mean-reference)/current.sigma) < 1e-9);
    assert.equal(actual.vwap_reference_time, bars[referenceIndex].time);
    assert.equal(actual.vwap_reference_mode, minute < 30 ? 'opening' : 'rolling');
  }
  const baseline = calculateVwap1m(bars);
  const shiftedClose = calculateVwap1m(bars.map(row => ({...row, close: row.close+0.2, high: row.high-0.2})));
  assert.notEqual(shiftedClose.ema, baseline.ema);
  assert.ok(Math.abs(shiftedClose.vwap_speed_30m-baseline.vwap_speed_30m) < 1e-10);
  const falling = calculateVwap1m(bars.map(row => ({...row, open: 300-row.open, close: 300-row.close,
    high: 300-row.low, low: 300-row.high})));
  assert.ok(Math.abs(falling.vwap_speed_30m+baseline.vwap_speed_30m) < 1e-9);
  const updated = calculateVwap1m([...bars.slice(0, -1), {...bars.at(-1), close: 113, high: 114, volume: 3000}]);
  assert.equal(updated.time, baseline.time);
  assert.equal(updated.vwap_reference, baseline.vwap_reference);
  assert.notEqual(updated.vwap_speed_30m, baseline.vwap_speed_30m);
});

test('VWAP speed never substitutes an adjacent minute, a prior session, or a zero sigma', () => {
  const missing = [bar(0, 100), bar(7, 103), bar(20, 108), bar(31, 110)];
  assert.equal(calculateVwap1m(missing).vwap_speed_30m, null);
  assert.equal(calculateVwap1m(missing).vwap_reference_time, null);
  assert.ok(Number.isFinite(calculateVwap1m([bar(0, 100), bar(7, 103), bar(30, 110)]).vwap_speed_30m));
  const flat = calculateVwap1m(Array.from({length: 31}, (_, i) => bar(i, 100)));
  assert.equal(flat.sigma, 0);
  assert.equal(flat.vwap_speed_30m, null);
  const monday = 3*24*60;
  const nextDay = calculateVwap1m([bar(0, 90), bar(30, 95), bar(monday, 104), bar(monday+7, 108)]);
  assert.equal(nextDay.vwap_reference, 104);
  assert.equal(nextDay.vwap_reference_time, bar(monday, 104).time);
  assert.equal(nextDay.vwap_reference_mode, 'opening');
  assert.equal(calculateVwap1m([bar(7, 100), bar(31, 110)]).vwap_speed_30m, null);
});

test('VWAP speed ranking shows the five highest and lowest signed values and selects through the shared stock action', () => {
  const options = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/vwap-distribution.js'), 'utf8')+'\nVwapDistribution;');
  const events=[];
  const values=[0.3, -0.4, 0, 1.1, -2.2, 0.8, -0.9, 0.7, -1.2, 0.1, 0.5, -0.6];
  const ui={...options.data(), ...options.methods, frame:{rows: values.map((value,i)=>({
    symbol:'NASDAQ:T'+i, time:first+2400, position:i%2 ? 1.2 : -0.5,
    st_direction:1, speed:100-i, vwap_speed_30m:value,
  }))}, $emit:(...args)=>events.push(args)};
  ui.frame.rows.push({...ui.frame.rows[0],symbol:'NASDAQ:MISSING',vwap_speed_30m:null});
  for(const [key,get] of Object.entries(options.computed)) Object.defineProperty(ui,key,{get:()=>get.call(ui)});
  assert.deepEqual(Array.from(ui.vwapSpeedGroups[0].rows,row=>row.vwap_speed_30m),[1.1,0.8,0.7,0.5,0.3]);
  assert.deepEqual(Array.from(ui.vwapSpeedGroups[1].rows,row=>row.vwap_speed_30m),[-2.2,-1.2,-0.9,-0.6,-0.4]);
  const chosen=ui.vwapSpeedGroups[1].rows[0];
  ui.select(chosen);
  assert.equal(events[0].join(','),'select,'+chosen.symbol);
  assert.equal(ui.detail.symbol,chosen.symbol);
  assert.equal(ui.anchors.at(-1).symbol,chosen.symbol);
  assert.ok(ui.points.some(row=>row.symbol===chosen.symbol));
  ui.frame={rows:ui.frame.rows.slice(0,7).map(row=>({...row,vwap_speed_30m:0.25}))};
  const both=ui.vwapSpeedGroups.flatMap(group=>group.rows);
  assert.equal(both.length,7);
  assert.equal(new Set(both.map(row=>row.symbol)).size,7);
  assert.equal(ui.vwapSpeedGroups[0].rows.length,5);
  assert.equal(ui.vwapSpeedGroups[1].rows.length,2);
  assert.deepEqual(Array.from(ui.vwapSpeedGroups[0].rows,row=>row.symbol),['NASDAQ:T0','NASDAQ:T1','NASDAQ:T2','NASDAQ:T3','NASDAQ:T4']);
});

test('an unavailable VWAP speed does not delay the existing complete distribution frame', () => {
  const batch=createVwapMinuteBatch(['QQQ','NVDA']);
  for(const symbol of ['QQQ','NVDA']) batch.observe(symbol,{
    time:first+7*60,st_direction:1,position:0.5,speed:1,vwap:100,upper:102,lower:98,ema:101,
    vwap_speed_30m:null,vwap_reference:null,vwap_reference_time:null,sigma:2,
  });
  assert.equal(batch.snapshot().frame.rows.length,2);
  assert.equal(batch.snapshot().progress.ready,2);
});

test('VWAP display coverage identifies a delayed symbol and an unavailable auxiliary speed', () => {
  const options=vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../console/static/vwap-distribution.js'),'utf8')+'\nVwapDistribution;');
  const ui={frame:{time:first,display_only:true,display_expected:51,display_delayed:['NASDAQ:ASML']},
    rows:Array(50).fill({}),latest:first,progress:{time:first,ready:50,total:51}};
  assert.equal(options.computed.refreshStatus.call(ui),'已更新 50/51 · 暂缓 ASML');
  ui.progress.time+=60;
  assert.equal(options.computed.refreshStatus.call(ui),'正在收集下一批 · 50/51');
  ui.progress.time=first;
  ui.frame.display_delayed=[];
  ui.frame.display_auxiliary_waiting=['NASDAQ:ASML'];
  assert.equal(options.computed.refreshStatus.call(ui),'已更新 · EMA7 速度待补 1');
  assert.equal(options.methods.signed(null),'—');
  assert.equal(options.methods.signed(0),'0.00');
});

test('close-based EMA, sigma position and seven-minute speed agree with Pine after regular-session warm-up', async () => {
  const definition = {
    key: 'vwap_display', model: { interval_seconds: 60 },
    source: fs.readFileSync(path.join(__dirname, '../indicators/vwap-ema7-position-1m.pine'), 'utf8'),
    plots: { ema: 'EMA7_Close', position: 'VWAP_Position', vwap: 'VWAP',
      upper: 'VWAP_Upper', lower: 'VWAP_Lower', speed: 'EMA7_Speed_7m' },
  };
  const calculate = createCalculator(definition, { version: require('../package.json').dependencies.pinets });
  const bars = Array.from({ length: 45 }, (_, i) => ({
    ...bar(i, 100 + i * 0.1, 100 + i * 5), high: 102 + i * 0.15,
    low: 99 + i * 0.1, close: 101 + i * 0.12,
  }));
  const warmup = Array.from({ length: 20 }, (_, i) => ({ ...bar(i, 100), time: first - 86400 + i * 60 }));
  const outputs = (await calculate({ symbol: 'NASDAQ:QQQ', bars: [...warmup, ...bars],
    metadata: { timezone: 'America/New_York', minmove: 1, pricescale: 100, session: 'regular' } })).slice(warmup.length);
  let ema = 100;
  for (let i = 0; i < bars.length; i++) {
    const history = bars.slice(0, i + 1);
    const total = history.reduce((sum, row) => sum + row.volume, 0);
    const mean = history.reduce((sum, row) => sum + (row.high + row.low + row.close) / 3 * row.volume, 0) / total;
    const sigma = Math.sqrt(history.reduce((sum, row) =>
      sum + ((row.high + row.low + row.close) / 3 - mean) ** 2 * row.volume, 0) / total);
    ema = ema == null ? bars[i].close : ema * 0.75 + bars[i].close * 0.25;
    const graph = calculateVwap1m([...warmup, ...history]);
    assert.ok(Math.abs(graph.ema - ema) < 1e-9);
    if (sigma > 1e-9) assert.ok(Math.abs(graph.position - (ema - mean) / sigma) < 1e-8);
    for (const field of Object.keys(definition.plots)) {
      assert.equal(outputs[i][field] == null, graph[field] == null, `${i} ${field} readiness`);
      // PineTS rounds persistent Pine floats, which is magnified by the narrow opening-session sigma.
      if (graph[field] != null) assert.ok(Math.abs(outputs[i][field] - graph[field]) < 1e-6, `${i} ${field}`);
    }
    if (i >= 7) assert.ok(Math.abs(graph.speed - (outputs[i].ema - outputs[i - 7].ema)) < 1e-9);
  }
  assert.equal(calculateVwap1m(Array.from({ length: 12 }, (_, i) => bar(i, 100))).position, null);
});

test('three VWAP columns alternate ranked circles around independent position axes without crossing links', () => {
  const options = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/vwap-distribution.js'), 'utf8') + '\nVwapDistribution;');
  const events = [];
  const ui = { ...options.data(), ...options.methods, plotWidth: 600, plotHeight: 330,
    frame: { rows: Array.from({ length: 54 }, (_, i) => ({
      symbol: i === 1 ? 'NASDAQ:QQQ' : i === 2 ? 'NASDAQ:NVDA' : i === 3 ? 'AMEX:SPY' : 'NASDAQ:T' + i,
      st_direction: i < 30 ? 1 : -1, position: (i - 28) / 5, speed: 54 - i,
    })) }, $emit: (...args) => events.push(args) };
  for (const [key, get] of Object.entries(options.computed)) Object.defineProperty(ui, key, { get: () => get.call(ui) });
  assert.equal(ui.regions[0].rows[0].position, 1);
  assert.equal(ui.regions[1].rows[0].position, -0.2);
  assert.equal(ui.regions[2].rows[0].position, 5);
  assert.equal(ui.regions[3].rows[0].position, -1.2);
  for (const region of ui.regions) {
    assert.ok(region.rows.every(row => ui.regionKey(row) === region.key));
    for (let page = 0; page < region.totalPages; page++) {
      ui.pages[region.key] = page;
      const visible = ui.regions.find(item => item.key === region.key).visible;
      assert.ok(visible.every(row => row.y - 16 >= region.top && row.y + 16 <= region.bottom));
      assert.ok(visible.every(row => row.rank === region.rows.findIndex(item => item.symbol === row.symbol) + 1));
      assert.ok(visible.every(row => row.side === 'left' ? row.x < row.anchorX : row.x > row.anchorX));
      for (let lane = 0; lane < region.lanes.length; lane++) {
        for (const side of ['left', 'right']) {
          const rows = visible.filter(row => row.lane === lane && row.side === side);
          assert.ok(new Set(rows.map(row => row.x)).size <= 1);
          for (let index = 1; index < rows.length; index++) {
            assert.ok(rows[index - 1].position >= rows[index].position);
            assert.ok(rows[index - 1].anchorY <= rows[index].anchorY);
            assert.ok(rows[index - 1].y < rows[index].y);
          }
        }
      }
    }
    ui.pages[region.key] = 0;
  }
  assert.equal(ui.positionY(1), ui.regions[0].top);
  assert.equal(ui.positionY(-1), ui.regions[1].bottom);
  assert.equal(ui.positionY(0), ui.regions[0].bottom);
  assert.equal(ui.positionY(0, ui.regions[1]), ui.regions[1].top);
  assert.equal(ui.positionY(0.5), (ui.regions[0].top + ui.regions[0].bottom) / 2);
  assert.equal(ui.positionY(-0.5), (ui.regions[1].top + ui.regions[1].bottom) / 2);
  assert.equal(ui.regions[3].top - ui.regions[2].bottom, 32);
  assert.ok(ui.regions[0].bottom - ui.regions[0].top > 2 * (ui.regions[2].bottom - ui.regions[2].top));
  assert.equal(ui.anchors.length, 54);
  const qqq = ui.rows.find(row => row.ticker === 'QQQ');
  const nvda = ui.rows.find(row => row.ticker === 'NVDA');
  const spy = ui.rows.find(row => row.ticker === 'SPY');
  assert.equal(ui.color(qqq), '#f5cb70');
  assert.equal(ui.color(nvda), ui.color(qqq));
  assert.equal(ui.color(spy), ui.color(qqq));
  assert.deepEqual(new Set(ui.anchors.slice(-3).map(row => row.ticker)), new Set(['QQQ', 'NVDA', 'SPY']));
  ui.selected = nvda.symbol;
  assert.equal(ui.color(nvda), '#86e7ff');
  assert.equal(ui.anchors.at(-1).symbol, nvda.symbol);
  ui.selected = null;
  const target = ui.regions[3].rows.at(-1);
  ui.select(target);
  assert.equal(events.at(-1).join(','), 'select,' + target.symbol);
  assert.equal(ui.detail.symbol, target.symbol);
  assert.ok(ui.points.some(row => row.symbol === target.symbol));
  assert.equal(ui.anchors.at(-1).symbol, target.symbol);
  assert.equal(ui.points.at(-1).symbol, target.symbol);
  ui.plotHeight = 220;
  options.watch.plotHeight.call(ui);
  assert.ok(ui.points.some(row => row.symbol === target.symbol));
  assert.equal(events.length, 1);
  ui.changePage('below', 999);
  assert.equal(ui.regions[3].page, ui.regions[3].totalPages - 1);
  ui.frame = { rows: [{ ...target, position: 0 }] };
  options.watch.frame.call(ui);
  assert.equal(ui.regions[0].page, 0);
  assert.equal(ui.regions[0].visible[0].symbol, target.symbol);
  assert.equal(ui.regions[0].visible[0].column, 'positive');
  ui.frame = { rows: [-1, 1, -1.01, 1.01, 0].map((position, index) => ({
    symbol: 'NASDAQ:B' + index, st_direction: index % 2 ? 1 : -1, position, speed: 100 - index,
  })) };
  assert.deepEqual(Array.from(ui.regions[0].rows, row => row.position), [1, 0]);
  assert.deepEqual(Array.from(ui.regions[1].rows, row => row.position), [-1]);
  assert.deepEqual(Array.from(ui.regions[2].rows, row => row.position), [1.01]);
  assert.deepEqual(Array.from(ui.regions[3].rows, row => row.position), [-1.01]);
  ui.plotWidth = 1058;
  ui.plotHeight = 450;
  ui.frame = { rows: [...Array.from({length: 40}, (_, i) => 0.975 - i * 0.05),
    1.2, 1.4, 1.6, 1.8, -1.05, -1.2, -1.35, -1.5, -1.65, -1.8, -1.95]
    .map((position, i) => ({symbol: 'NASDAQ:F' + i, position, st_direction: 1, speed: i})) };
  assert.equal(ui.anchors.length, 51);
  assert.equal(ui.points.length, 51);
  assert.equal(ui.regions[0].lanes.length, 1);
  assert.equal(ui.regions[0].bilateral, true);
  assert.deepEqual(Array.from(ui.regions[0].visible.slice(0, 4), row => [row.rank, row.side]),
    [[1, 'left'], [2, 'right'], [3, 'left'], [4, 'right']]);
  assert.equal(ui.paginatedRegions.length, 0);
});

test('trend regions share height by population with a readable minimum and recover space from sparse sides', () => {
  const options = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/vwap-distribution.js'), 'utf8') + '\nVwapDistribution;');
  const ui = {...options.data(), ...options.methods, plotWidth: 1058, plotHeight: 450};
  for (const [key, get] of Object.entries(options.computed)) Object.defineProperty(ui, key, {get: () => get.call(ui)});
  const frame = (above, below) => ({rows: [
    ...Array.from({length: above}, (_, i) => ({symbol: 'NASDAQ:L'+i, position: 1.1+i/20, st_direction: 1})),
    ...Array.from({length: below}, (_, i) => ({symbol: 'NASDAQ:S'+i, position: -1.1-i/20, st_direction: -1})),
  ]});
  for (const [aboveCount, belowCount] of [[18, 1], [1, 18], [10, 2], [2, 10], [5, 5], [51, 0], [0, 51], [0, 0]]) {
    ui.frame = frame(aboveCount, belowCount);
    const [positive, , above, below] = ui.regions;
    const upperHeight = above.bottom - above.top, lowerHeight = below.bottom - below.top;
    assert.equal(below.top - above.bottom, 32);
    assert.equal(upperHeight + lowerHeight + 32, positive.bottom - positive.top);
    assert.ok(upperHeight >= 36 && lowerHeight >= 36);
    if (aboveCount > belowCount) assert.ok(upperHeight > lowerHeight);
    if (aboveCount < belowCount) assert.ok(upperHeight < lowerHeight);
    if (aboveCount === belowCount) assert.equal(upperHeight, lowerHeight);
    if (aboveCount && belowCount && upperHeight > 36 && lowerHeight > 36) {
      assert.ok(Math.abs(upperHeight / lowerHeight - aboveCount / belowCount) < 1e-10);
    }
    for (const region of [above, below]) {
      assert.ok(region.visible.every(row => row.y - 16 >= region.top && row.y + 16 <= region.bottom));
    }
    if (aboveCount + belowCount === 19) {
      assert.equal(above.totalPages, 1);
      assert.equal(below.totalPages, 1);
      assert.equal(ui.points.length, 19);
    }
  }
  ui.plotHeight = 160;
  ui.frame = frame(51, 1);
  assert.ok(ui.regions.slice(2).every(region => region.bottom - region.top >= 36));
  assert.ok(ui.regions.slice(2).every(region => region.visible.every(row => row.y - 16 >= region.top && row.y + 16 <= region.bottom)));
});

const reading = (minute, speed = 1) => ({
  time: bar(minute, 100).time, st_direction: 1, position: 0, speed,
  vwap: 100, lower: 98, upper: 102, ema: 100,
});

test('VWAP plot coalesces resize notifications and retains layout when its tab is hidden', () => {
  const callbacks = new Map();
  let sequence = 0, observer, disconnected = false;
  const options = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../console/static/vwap-distribution.js'), 'utf8') + '\nVwapDistribution;', {
    requestAnimationFrame(fn) { callbacks.set(++sequence, fn); return sequence; },
    cancelAnimationFrame(id) { callbacks.delete(id); },
    ResizeObserver: class {
      constructor(fn) { observer = fn; }
      observe() {}
      disconnect() { disconnected = true; }
    },
  });
  const ui = {...options.data(), ...options.methods, $refs: {plot: {}}};
  options.mounted.call(ui);
  observer([{contentRect: {width: 0, height: 0}}]);
  assert.equal(callbacks.size, 0);
  assert.equal(ui.plotReady, false);
  observer([{contentRect: {width: 1058.4, height: 450.8}}]);
  observer([{contentRect: {width: 1058.6, height: 451.2}}]);
  assert.equal(callbacks.size, 1);
  const [id, draw] = [...callbacks][0];
  callbacks.delete(id);
  draw();
  assert.equal(ui.plotWidth, 1059);
  assert.equal(ui.plotHeight, 451);
  assert.equal(ui.plotReady, true);
  observer([{contentRect: {width: 0, height: 0}}]);
  assert.equal(ui.plotWidth, 1059);
  assert.equal(ui.plotHeight, 451);
  assert.equal(callbacks.size, 0);
  observer([{contentRect: {width: 900, height: 400}}]);
  options.beforeUnmount.call(ui);
  assert.equal(callbacks.size, 0);
  assert.equal(disconnected, true);
});

test('PineTS VWAP pullback mirrors both sides and matches the distribution bands and ST on intrabar refresh', async () => {
  const calculate = createCalculator(getDefinition('vwap_outer_pullback'), {
    version: require('../package.json').dependencies.pinets,
  });
  const metadata = { timezone: 'America/New_York', minmove: 1, pricescale: 100, session: 'regular' };
  for (const side of [1, -1]) {
    // Retain an outside-band EMA after the final close reverses ST, including with close as its source.
    const bars = Array.from({length: 45}, (_, index) => {
      const p = 100 + side * index * 0.2;
      return {...bar(index, p, 1000), high: p + 0.55, low: p - 0.55};
    });
    const dataset = {symbol: 'NASDAQ:QQQ', bars, metadata};
    const session = await calculate.open(dataset);
    const initial = session.read(1)[0];
    assert.equal(initial.candidate_direction, side);
    assert.equal(initial.entry_signal, 0);
    const last = bars.at(-1);
    const close = initial.entry_price - side * 0.25;
    const touched = {...last, close, high: Math.max(last.high, close), low: Math.min(last.low, close)};
    const current = [...bars.slice(0, -1), touched];
    const live = await session.replaceLast(touched);
    assert.deepEqual(live, await calculate({...dataset, bars: current}, {tail: 2}));
    const output = live.at(-1);
    const graph = calculateVwap1m(current);
    assert.equal(output.entry_signal, 1);
    assert.equal(output.direction, side);
    assert.ok(Math.abs(output.entry_price - (side === 1 ? graph.upper : graph.lower)) <= 0.00501);
    assert.ok(Math.abs(output[side === 1 ? 'long_stop' : 'short_stop'] - graph.st_price) <= 0.00501);
    const next = [...current, {...last, time: last.time + 60}];
    assert.deepEqual(await session.advance(next, 2), await calculate({...dataset, bars: next}, {tail: 2}));
    const previousStop = initial[side === 1 ? 'long_stop' : 'short_stop'];
    const reverseClose = previousStop - side * 0.25;
    const reversed = [...bars.slice(0, -1), {...last, close: reverseClose,
      high: Math.max(last.high, reverseClose), low: Math.min(last.low, reverseClose)}];
    const reverseGraph = calculateVwap1m(reversed);
    assert.equal(reverseGraph.zone, side);
    assert.equal(reverseGraph.st_direction, -side);
    const [reverseOutput] = await calculate({...dataset, bars: reversed}, {tail: 1});
    assert.equal(reverseOutput.direction, -side);
    assert.equal(reverseOutput.candidate_direction, 0);
    assert.equal(reverseOutput.entry_signal, 0);
  }
});

test('publishes complete same-minute realtime batches and freezes the frame at an incomplete rollover', () => {
  const batch = createVwapMinuteBatch(['QQQ', 'NVDA']);
  batch.observe('QQQ', reading(7));
  batch.observe('NVDA', reading(7, null));
  batch.observe('UNSUBSCRIBED', reading(7));
  assert.equal(batch.snapshot().frame, null);
  assert.deepEqual(batch.snapshot().progress, { time: reading(7).time, ready: 1, total: 2 });
  batch.observe('NVDA', reading(7, 2));
  const frame = batch.snapshot().frame;
  assert.deepEqual(frame.rows.map(row => row.symbol), ['QQQ', 'NVDA']);
  batch.observe('QQQ', reading(7, 3));
  batch.observe('NVDA', reading(8));
  assert.equal(batch.snapshot().frame, frame);
  assert.equal(batch.snapshot().progress.ready, 1);
  batch.observe('QQQ', reading(8));
  assert.equal(batch.snapshot().frame.time, reading(8).time);
  assert.equal(batch.snapshot().progress.ready, 2);
});

test('realtime batches coalesce quote bursts, reuse unchanged peers, and preserve published objects', () => {
  const batch = createVwapMinuteBatch(['QQQ', 'NVDA']);
  batch.observe('QQQ', reading(7));
  batch.observe('NVDA', reading(7, 2));
  const first = batch.snapshot().frame;
  assert.ok(Number.isSafeInteger(first.revision) && first.revision > 1_000_000_000_000);
  assert.equal(batch.snapshot().frame, first);
  for (let speed = 3; speed <= 100; speed++) batch.observe('QQQ', reading(7, speed));
  assert.equal(first.rows[0].speed, 1);
  const second = batch.snapshot().frame;
  assert.equal(second.time, first.time);
  assert.ok(second.revision > first.revision);
  assert.deepEqual(second.rows.map(row => row.speed), [100, 2]);
  assert.ok(second.rows.every(row => row.time === second.time));
  assert.equal(batch.snapshot().frame, second);
  batch.observe('QQQ', reading(7, 100));
  batch.observe('NVDA', reading(7, 2));
  assert.equal(batch.snapshot().frame, second);
});

test('invalid current readings hold the whole batch until recovery and cannot be hidden by a cached peer', () => {
  const batch = createVwapMinuteBatch(['QQQ', 'NVDA']);
  batch.observe('QQQ', reading(7));
  batch.observe('NVDA', reading(7));
  const first = batch.snapshot().frame;
  batch.observe('NVDA', reading(7, null));
  batch.observe('QQQ', reading(7, 3));
  assert.equal(batch.snapshot().frame, first);
  assert.equal(batch.snapshot().progress.ready, 1);
  batch.observe('NVDA', reading(7, 4));
  const recovered = batch.snapshot().frame;
  assert.ok(recovered.revision > first.revision);
  assert.deepEqual(recovered.rows.map(row => row.speed), [3, 4]);
  batch.observe('NVDA', reading(8, null));
  batch.observe('QQQ', reading(7, 5));
  assert.equal(batch.snapshot().frame, recovered);
  assert.equal(batch.snapshot().progress.time, reading(8).time);
});

test('assembles delayed readings by minute and never regresses to an older frame', () => {
  const batch = createVwapMinuteBatch(['QQQ', 'NVDA']);
  batch.observe('QQQ', reading(7));
  batch.observe('QQQ', reading(8));
  batch.observe('NVDA', reading(7));
  assert.equal(batch.snapshot().frame.time, reading(7).time);
  assert.equal(batch.snapshot().progress.time, reading(8).time);
  batch.observe('NVDA', reading(8));
  batch.observe('QQQ', reading(7));
  assert.equal(batch.snapshot().frame.time, reading(8).time);
});

test('an opening preview is confirmed only after rollover and uses the final three-bar midpoint', () => {
  const candle = (i, open, high, low, close) => ({...bar(i, open), high, low, close});
  const firstTwo = [candle(0, 100, 100.5, 99.9, 100.4), candle(1, 100.4, 100.9, 100.35, 100.8)];
  const live = candle(2, 100.8, 102, 100.7, 101.9);
  const preview = openingVwapSnapshot([...firstTwo, live], first + 155).signals[0];
  assert.equal(preview.side, 'long');
  assert.equal(preview.confirmed, false);
  assert.equal(preview.stop_px, 100.95);

  const final = openingVwapSnapshot([...firstTwo, {...live, high: 103}, bar(3, 102)], first + 181).signals[0];
  assert.equal(final.confirmed, true);
  assert.equal(final.stop_px, 101.45);
  assert.equal(final.candles.length, 3);

  const failed = openingVwapSnapshot([...firstTwo, {...live, close: 100.75}, bar(3, 102)], first + 181);
  assert.equal(failed.signals.some(row => row.bar_time === live.time), false);
  assert.equal(openingVwapSnapshot([firstTwo[0], live, bar(3, 102)], first + 181).signals.length, 0);

  const mirror = [ ...firstTwo, live, bar(3, 102) ].map(row => ({
    ...row, open: 200 - row.open, close: 200 - row.close,
    high: 200 - row.low, low: 200 - row.high,
  }));
  assert.equal(openingVwapSnapshot(mirror, first + 181).signals[0].side, 'short');
});
