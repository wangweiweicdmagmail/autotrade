'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {replayOpeningPositions,definition}=require('../service/opening_position_research');
const {createCalculator}=require('../service/pinets_runtime');
const version=require('../package-lock.json').packages['node_modules/pinets'].version;
const OPEN=Date.parse('2026-10-07T13:30:00Z')/1000;
const metadata={timezone:'America/New_York',minmove:1,pricescale:100,type:'stock',session:'regular'};
const candle=(time,close=100,open=100,high=Math.max(open,close)+1,low=Math.min(open,close)-1)=>({time,open,high,low,close,volume:100});
const bars=(start=OPEN,n=20)=>Array.from({length:n},(_,i)=>candle(start+i*60,i<15?100:103));
const data=rows=>({symbol:'NASDAQ:TEST',metadata,bars:rows});
const near=(a,b)=>assert(Math.abs(a-b)<1e-8,`${a} != ${b}`);
const calc=createCalculator(definition,{version});
const rank=(rows,extra={})=>replayOpeningPositions({day:'2026-10-07',as_of:OPEN+1200,datasets:{'NASDAQ:TEST':{bars:rows,metadata,...extra}}});
const aggregate=rows=>Array.from({length:Math.floor(rows.length/5)},(_,i)=>{
 const five=rows.slice(i*5,i*5+5);return{time:five[0].time,open:five[0].open,high:Math.max(...five.map(b=>b.high)),
  low:Math.min(...five.map(b=>b.low)),close:five.at(-1).close,volume:five.reduce((s,b)=>s+b.volume,0)};
});

test('fixed range is unavailable before 09:45 and locks at the actual confirmation',async()=>{
 const input=bars(),output=await calc(data(input));
 for(const r of output.slice(0,14)){assert.equal(r.ready,0);assert.equal(r.position,null);}
 assert.equal(output[14].ready,1);near(output[14].position,0);
 for(const r of output.slice(15)){near(r.width,2);near(r.position,3);near(r.day_open,100);}
 await assert.rejects(replayOpeningPositions({day:'2026-10-07',as_of:OPEN+840,datasets:{}}),/09:45/);
 for(const r of output){assert.equal(r.entry_signal,0);assert.equal(r.execution_direction,0);assert.equal(r.long_stop,null);}
});

test('signed midpoint position is symmetric and scale invariant with half-width units',async()=>{
 const input=bars(),up=(await rank(input)).ranks[0];
 const mirrored=input.map(b=>({...b,open:200-b.open,high:200-b.low,low:200-b.high,close:200-b.close}));
 const down=(await rank(mirrored)).ranks[0];
 near(up.coordinate,3);near(down.coordinate,-3);near(up.score,down.score);
 near(up.opening_high_coordinate,1);near(up.opening_low_coordinate,-1);
 const scaled=input.map(b=>({...b,open:10*b.open,high:10*b.high,low:10*b.low,close:10*b.close}));
 near((await rank(scaled)).ranks[0].coordinate,up.coordinate);
});

test('one-minute and five-minute versions use exactly the same fifteen-minute interval',async()=>{
 const one=bars(),five=aggregate(one);
 const out=await createCalculator({...definition,model:{interval_seconds:300}},{version})(data(five));
 near(out.at(-1).position,(await calc(data(one))).at(-1).position);
 const result=await rank(one,{five_bars:five});assert.equal(result.ranks[0].cross_timeframe_checked,true);
 const wrong=five.map((b,i)=>i?b:{...b,high:b.high+1});
 assert.equal((await rank(one,{five_bars:wrong})).missing[0].reason,'opening_1m_5m_mismatch');
});

test('opening gaps or a zero-width range are rejected without synthetic candles',async()=>{
 for(const index of [0,6,14]){
  const result=await rank(bars().filter((_,i)=>i!==index));
  assert(!result.complete);assert.equal(result.ranks.length,0);assert.match(result.missing[0].reason,/opening15/);
 }
 const flat=bars().map(b=>({...b,open:100,high:100,low:100,close:100}));
 assert.equal((await rank(flat)).missing[0].reason,'opening_range_not_ready');
});

test('gaps after range lock do not change midpoint position, but a missing target close cannot be reused',async()=>{
 const full=await rank(bars()),gap=await rank(bars().filter((_,i)=>i!==16));
 assert(gap.complete);near(gap.ranks[0].coordinate,full.ranks[0].coordinate);
 assert(!gap.vwap_baseline.complete);assert.equal(gap.vwap_baseline.missing[0].reason,'minute_gap');
 const missing=await rank(bars().slice(0,-1));assert.equal(missing.ranks.length,0);assert.equal(missing.missing[0].reason,'target_minute_missing');
});

test('premarket and future prices never affect the fixed opening range or target close',async()=>{
 const input=bars(),extra=[candle(OPEN-60,5000),...input,candle(OPEN+1200,6000),candle(OPEN+1260,7000)];
 const actual=await rank(extra),expected=await rank(input);near(actual.ranks[0].coordinate,expected.ranks[0].coordinate);
 const mixed=await calc(data([candle(OPEN-60,5000),...input]));near(mixed.at(-1).width,2);
 assert.equal((await rank(input,{metadata:{...metadata,session:'extended'}})).ranks.length,0);
});

test('session reset remains 09:30 Eastern across DST and never carries yesterday range',async()=>{
 const summer=bars(Date.parse('2026-10-30T13:30:00Z')/1000),winter=bars(Date.parse('2026-11-02T14:30:00Z')/1000)
  .map(b=>({...b,open:b.open+50,high:b.high+50,low:b.low+50,close:b.close+50}));
 const output=await calc(data([...summer,...winter]));
 assert.equal(output[20].ready,0);near(output.at(-1).day_open,150);near(output.at(-1).width,2);near(output.at(-1).position,3);
});

test('live close revisions never alter the locked range and match an independent replay',async()=>{
 let input=bars();const context=await calc.open(data(input));
 for(const close of [104,98,105]){
  const last=candle(input.at(-1).time,close);input=[...input.slice(0,-1),last];
  const live=await context.replaceLast(last,2),fresh=await calc(data(input));
  near(live.at(-1).position,fresh.at(-1).position);near(live.at(-1).width,2);near(live.at(-1).day_open,100);
 }
});

test('an early 09:45 rollover confirms the first ranking without waiting for another quote or minute',async()=>{
 const {createCoalescedUpdater}=require('../service/chart_snapshot');
 const {calculateSeries,buildFrame}=require('../service/market_rhythm');
 const real=Date.now;let now=(OPEN+870)*1000;Date.now=()=>now;
 try{
  const input=bars(),context=await calc.open(data(input.slice(0,15)));
  assert.equal(context.read(1)[0].ready,0); // 09:44 is still forming.
  const tasks=[];let result;
  const work=createCoalescedUpdater(()=>{result=context.advance(input.slice(0,16),2);},{clock:()=>now,
   schedule:(fn,delay)=>{const task={fn,delay};tasks.push(task);return task;}});
  now=(OPEN+900)*1000-528; // Recorded APP rollover arrived 528 ms early.
  work.notify(input[15].time*1000);
  assert.equal(tasks[0].delay,528);assert.equal(result,undefined);
  now=(OPEN+900)*1000;tasks.shift().fn();
  const output=(await result)[0],fresh=(await calc(data(input.slice(0,16)),{tail:2}))[0];
  assert.equal(output.ready,1);assert.deepEqual(output,fresh);
  const row=calculateSeries('NASDAQ:TEST',input.slice(0,16),now/1000);
  const frame=buildFrame({'NASDAQ:TEST':row},['NASDAQ:TEST'],now/1000,{}, {openingPositions:{'NASDAQ:TEST':output}});
  assert.equal(frame.as_of,OPEN+900);assert.equal(frame.coverage.aligned,1);
  assert.deepEqual(frame.scoring.pending,[]);assert.equal(frame.scoring.ready,1);
  assert.equal(tasks.length,0); // No later trade is needed to release this batch.
 }finally{Date.now=real;}
});

test('absolute-position ranking is stable for tied values and reference instruments never occupy seats',async()=>{
 const dataset={bars:bars(),metadata};
 const result=await replayOpeningPositions({day:'2026-10-07',as_of:OPEN+1200,datasets:{'NASDAQ:ZZZ':dataset,'NASDAQ:AAA':dataset,'NASDAQ:QQQ':dataset}});
 assert.deepEqual(result.ranks.map(r=>r.symbol),['NASDAQ:AAA','NASDAQ:ZZZ']);assert.equal(result.eligible,2);assert(result.complete);
});

test('unsupported intervals fail explicitly rather than changing opening-window meaning',async()=>{
 const ten=createCalculator({...definition,model:{interval_seconds:600}},{version});
 await assert.rejects(ten(data(aggregate(bars()).map((b,i)=>({...b,time:OPEN+i*600})))),/一分钟或五分钟/);
});


test('midpoint zero and half-width units put edges at +1/-1 independent of opening price',async()=>{
 const input=bars(),expected=(await rank(input)).ranks[0];
 const shifted=input.map((b,i)=>i?b:{...b,open:100.8}),actual=(await rank(shifted)).ranks[0];
 near(actual.opening_midpoint,100);near(actual.coordinate,expected.coordinate);near(expected.coordinate,3);
 near(actual.opening_high_coordinate,1);near(actual.opening_low_coordinate,-1);
 for(const [close,want]of [[101,1],[99,-1],[100,0]]){
  const rows=shifted.map((b,i)=>i===19?candle(b.time,close):b),row=(await rank(rows)).ranks[0];
  near(row.coordinate,want);assert.equal(row.direction,Math.sign(want));
 }
 const fallingFromOpen=shifted.map((b,i)=>i===19?candle(b.time,100.4):b),row=(await rank(fallingFromOpen)).ranks[0];
 assert(row.move_pct<0);assert.equal(row.direction,1);near(row.coordinate,.4);
 const result=await rank(input);assert.equal(result.model,'opening15_midpoint_half_position_research_v4');
 assert.equal(result.zero_anchor,'opening15_midpoint');
});

test('long and short top tens rank independently with no zero or reference fillers',async()=>{
 const datasets={};
 for(let i=0;i<12;i++)for(const [prefix,direction]of [['L',1],['S',-1]]){
  const rows=bars();rows[19]=candle(rows[19].time,100+direction*(i+1));
  datasets['NASDAQ:'+prefix+String(i).padStart(2,'0')]={bars:rows,metadata};
 }
 const flat=bars().map(b=>({...b,close:100}));
 datasets['NASDAQ:ZERO']={bars:flat,metadata};datasets['NASDAQ:QQQ']={bars:bars(),metadata};
 const result=await replayOpeningPositions({day:'2026-10-07',as_of:OPEN+1200,datasets});
 assert.deepEqual(result.side_counts,{long:12,short:12,neutral:1});
 assert.equal(result.long_top10.length,10);assert.equal(result.short_top10.length,10);
 assert.equal(result.long_top10[0].symbol,'NASDAQ:L11');assert.equal(result.long_top10.at(-1).symbol,'NASDAQ:L02');
 assert.equal(result.short_top10[0].symbol,'NASDAQ:S11');assert.equal(result.short_top10.at(-1).symbol,'NASDAQ:S02');
 for(const [key,sign]of [['long_top10',1],['short_top10',-1]])for(const [i,row]of result[key].entries()){
  assert.equal(row.rank,i+1);assert.equal(row.direction,sign);assert.equal(result.ranks[row.combined_rank-1].symbol,row.symbol);
 }
 assert(result.complete);assert.equal(result.eligible,25);
});
