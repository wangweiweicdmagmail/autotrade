'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {calculateSeries,buildFrame,createMarketRhythm}=require('../service/market_rhythm');
const {rhythmChartModel,rhythmFormingPoint}=require('../console/static/market-rhythm');
const OPEN=Date.parse('2026-11-18T14:30:00Z')/1000;
const prior=OPEN-86400;


const bar=(time,close)=>({time,open:close,high:close+.1,low:close-.1,close,volume:100});
const warm=Array.from({length:40},(_,i)=>bar(prior+i*60,100));
const rising=Array.from({length:15},(_,i)=>bar(OPEN+i*60,100+i*2));
const series=(bars,now=OPEN+900,symbol='NASDAQ:TEST')=>calculateSeries(symbol,[...warm,...bars],now,[{time:prior},{time:OPEN}]);
const scoreHistory=time=>Array.from({length:9},(_,i)=>({time:Math.floor(time/300)*300-(i+1)*300,
  known_at:Math.floor(time/300)*300-i*300,ema9:100-i,atr:2}));

test('confirmed prefix is invariant to future prices; single sample cannot create a breakout',()=>{
  const cutoff=OPEN+600;
  const expected=series(rising.filter(b=>b.time+60<=cutoff),cutoff);
  const full=series(rising.map(b=>b.time+60>cutoff?bar(b.time,900):b),cutoff);
  assert.deepEqual(full,expected);
  assert.equal(series([rising[0]],OPEN+60).points[0][3],0);
  assert.equal(series([rising[0]],OPEN+60).events.length,0);
  assert(expected.metrics);
  assert(expected.metrics.episode_start_bar>OPEN);
  assert.equal(expected.metrics.rail_speed>=0,true);
});

test('crossing VWAP ends episode and next breakout starts a separate episode',()=>{
  const first=series(rising);
  assert(first.metrics);
  const fail=[...rising,bar(OPEN+900,80)];
  const ended=series(fail,OPEN+960);
  assert.equal(ended.metrics,null);
  assert(ended.events.some(e=>e.type==='crossed_vwap'));
  const down=series([...fail,...Array.from({length:12},(_,i)=>bar(OPEN+960+i*60,75-i*2))],OPEN+1680);
  assert.equal(down.metrics.direction_sign,-1);
  assert.notEqual(down.metrics.episode_id,first.metrics.episode_id);
  assert(down.metrics.episode_start_bar>=OPEN+900);
});

test('known minute gaps or missing open stop ranking and do not draw fabricated VWAP',()=>{
  const gap=series(rising.filter((_,i)=>i!==4));
  assert.equal(gap.issue,'minute_gap');assert.equal(gap.metrics,null);
  assert.equal(gap.points.at(-1)[2],null);
  assert.equal(series(rising.slice(1)).issue,'missing_open');
  const noWarm=calculateSeries('TEST',rising,OPEN+900);
  assert.equal(noWarm.issue,'ema_warmup');assert.equal(noWarm.metrics,null);
});

test('rank only common-time stocks; ETFs and beta are references',()=>{
  const a=series(rising,OPEN+900,'NASDAQ:A');
  const b=series(rising.slice(0,-1),OPEN+900,'NASDAQ:B');
  const q=series(rising,OPEN+900,'NASDAQ:QQQ');
  const frame=buildFrame({'NASDAQ:A':a,'NASDAQ:B':b,'NASDAQ:QQQ':q},['NASDAQ:A','NASDAQ:B','NASDAQ:QQQ'],OPEN+900,{'NASDAQ:A':scoreHistory(OPEN+900)});
  assert.deepEqual(frame.ranks.map(r=>r.symbol),['NASDAQ:A']);
  assert.deepEqual(frame.coverage.missing,['NASDAQ:B']);
  assert.equal(frame.ranks[0].rank,1);
});

test('prior-day proximity is a factual beta event and frame rebuilding is idempotent',()=>{
  const row={...series(rising,OPEN+900,'CAPITALCOM:SPX500'),previous_range:{high:104,low:102}};
  const frame=buildFrame({[row.symbol]:row},[row.symbol],OPEN+900);
  const events=frame.series[row.symbol].events;
  assert(events.some(e=>e.type==='prior_high_touch'));
  assert(events.some(e=>e.type==='prior_low_touch'));
  assert.deepEqual(buildFrame(frame.series,[row.symbol],OPEN+900).series[row.symbol].events,events);
});

test('atomic local cache is revised by closed bars, not live current-minute ticks',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'market-rhythm-'));
  try{
    const file=path.join(dir,'frame.json'),feed=createMarketRhythm({symbols:['NASDAQ:TEST'],cachePath:file});
    feed.observe('NASDAQ:TEST',[...warm,...rising],OPEN+840);
    await feed.flush(OPEN+844);
    const original=await fs.readFile(file,'utf8');
    feed.observe('NASDAQ:TEST',[...warm,...rising.slice(0,-1),bar(OPEN+840,9999)],OPEN+845);
    await feed.flush(OPEN+850);
    assert.equal(await fs.readFile(file,'utf8'),original);
    feed.observe('NASDAQ:TEST',[...warm,...rising],OPEN+901);
    await feed.flush(OPEN+905);
    assert.notEqual(await fs.readFile(file,'utf8'),original);
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('chart gaps break paths and synchronized cursor never borrows a different minute',()=>{
  const points=[[OPEN,100,100,1,100,101,99,100,0,'waiting',0],
    [OPEN+120,102,100,1,102,103,101,102,2,'new_breakout',1]];
  const plot=rhythmChartModel({points},OPEN,OPEN+120,'position',500,100,[-2,3],OPEN+60);
  assert.equal((plot.price.match(/M/g)||[]).length,2);
  assert.equal(plot.cursor,null);
  assert.equal(plot.priceSegments.length,2);
  assert(plot.priceSegments.every(s=>s.state==='unknown'&&!s.d.includes('L')));
  assert.deepEqual(plot.ticks.map(t=>t.label),['-1','0','+1']);
});

test('TD display counts match the validated Pine source, keep overnight context and exclude open bars',async()=>{
  const {createCalculator}=require('../service/pinets_runtime');
  const source=await fs.readFile(path.join(__dirname,'../indicators/td-price-line.pine'),'utf8');
  const version=require('../package-lock.json').packages['node_modules/pinets'].version;
  const calculate=createCalculator({key:'td_display_check',source,model:{interval_seconds:60},
    plots:{count:'TD_Count',direction:'Direction'}},{version});
  const prefix=Array.from({length:40},(_,i)=>bar(prior+i*60,100+i));
  const closes=[140,141,142,143,144,145,146,147,144,143,142,141,140,139,138,137,136,135,134,133,
    138,139,140,141,142,143,144,145,146,147,148,149,150,151];
  const today=closes.map((close,i)=>bar(OPEN+i*60,close)),bars=[...prefix,...today];
  const cutoff=OPEN+closes.length*60;
  const row=calculateSeries('NASDAQ:TEST',[...bars,bar(OPEN-60,99999),bar(cutoff,1)],cutoff);
  const pine=await calculate({symbol:'NASDAQ:TEST',metadata:{timezone:'America/New_York',
    minmove:1,pricescale:100,session:'regular'},bars});
  assert.deepEqual(row.td.counts,pine.slice(prefix.length).map(p=>[p.time,p.count]));
  assert(row.td.counts[0][1]>9); // The opening minute is not reset or re-warmed.
  assert.equal(row.td.counts.length,today.length);
  assert.equal(row.points.every(p=>p.length===11),true); // Execution consumers retain their schema.
  for(let i=0;i<today.length;i++){
    const count=row.td.counts[i][1];
    assert.equal(pine[prefix.length+i].direction,count>=3?1:count<=-3?-1:0);
  }
});

test('TD segments color the arriving edge and retain identical price, bands and normalized coordinates',()=>{
  const counts=[0,1,2,3,4,-1,-2,-3,-4];
  const points=counts.map((_,i)=>[OPEN+i*60,100+i,100,2,100+i,101+i,99+i,120-i,i/2,'outside_progress',1]);
  const track={points,td:{version:1,comparison_bars:4,trend_threshold:3,counts:counts.map((n,i)=>[points[i][0],n])}};
  for(const view of ['price','position']){
    const args=[OPEN,OPEN+480,view,500,150,[-2,5],OPEN+420];
    const plot=rhythmChartModel(track,...args),legacy=rhythmChartModel({points},...args);
    for(const key of ['price','center','upper','lower','cursor'])assert.deepEqual(plot[key],legacy[key]);
    assert.deepEqual(plot.priceSegments.map(s=>s.state),['range','up','range','down']);
    const coords=plot.price.match(/[ML][\d.-]+,[\d.-]+/g).map(p=>p.slice(1));
    assert(plot.priceSegments[1].d.startsWith('M'+coords[2]+'L'+coords[3]));
    assert(plot.priceSegments[2].d.startsWith('M'+coords[4]+'L'+coords[5]));
    assert(plot.priceSegments[3].d.startsWith('M'+coords[6]+'L'+coords[7]));
    assert.equal(plot.tdCount,-3);assert.equal(plot.tdState,'down');
    const tail=rhythmChartModel(track,OPEN+420,OPEN+480,view,500,150,[-2,5],OPEN+420);
    assert.equal(tail.priceSegments[0].state,'down'); // Zooming never restarts TD counts.
    assert.equal(legacy.tdCount,null);assert(legacy.priceSegments.every(s=>s.state==='unknown'));
  }
});

test('candidate badges use the latest confirmed TD regardless of a locked historical cursor',()=>{
  const {MarketRhythm}=require('../console/static/market-rhythm');
  const counts=[3,-3,2,0,null];
  const frame={as_of:OPEN+120,ranks:counts.map((_,i)=>({symbol:'S'+i,rank:i+1})),
    tracks:counts.map((n,i)=>({symbol:'S'+i,points:[[OPEN+60]],
      td:{version:1,comparison_bars:4,trend_threshold:3,counts:[[OPEN,99],...(n==null?[]:[[OPEN+60,n]])]}}))};
  const ui={frame,focus:OPEN,locked:true,positions:[],displayState:{order:frame.ranks.map(r=>r.symbol)}};
  ui.sourceTracks=MarketRhythm.computed.sourceTracks.call(ui);
  const rows=MarketRhythm.computed.rows.call(ui);
  assert.deepEqual(rows.map(r=>r.tdLabel),['TD +3','TD -3','TD +2','TD 0','TD —']);
  assert.deepEqual(rows.map(r=>r.tdState),['up','down','range','range','unknown']);
  assert.deepEqual(rows.map(r=>r.rank),[1,2,3,4,5]);
});

test('accepted score facts retain current strength rank independently of fixed display slots',()=>{
  const {MarketRhythm,rhythmDisplaySlots}=require('../console/static/market-rhythm');
  const frame={day:'2026-11-18',as_of:OPEN+900,coverage:{total:2,aligned:2,missing:[]},tracks:[],
    ranks:[{symbol:'NFLX',rank:1,direction_sign:-1,score:70},{symbol:'NVDA',rank:2,direction_sign:-1,score:69}]};
  const prior=rhythmDisplaySlots(null,frame,[]);
  const next={...frame,as_of:frame.as_of+60,ranks:[{symbol:'NVDA',rank:1,direction_sign:-1,score:88,
    classification_rate:1,classification_samples:15,classification_correct:15,ema9_slope_atr:-.5,ema9_bias_atr:-2,
    score_parts:{vwap:33,slope:35,classification:20}},{symbol:'NFLX',rank:2,direction_sign:-1,score:60}]};
  const display=rhythmDisplaySlots(prior,next,[]);
  assert.deepEqual(display.order,['NFLX','NVDA']); // Same ten-minute bucket: no chart reshuffle.
  const ui={frame:next,sourceTracks:{},positions:[],displayState:display};
  const rows=MarketRhythm.computed.rows.call(ui),nvda=rows[1];assert.equal(nvda.rank,2);assert.equal(nvda.candidateRank,1);
  const methods={...MarketRhythm.methods};
  assert(methods.rowDescription.call(methods,nvda).includes('强度#1'));
  assert(methods.scoreDescription.call(methods,nvda).includes('15/15'));
  assert(methods.scoreDescription.call(methods,nvda).includes('仅位置参考'));
});

test('forming EMA3 is yellow-only data, while TD and confirmed rows remain unchanged',()=>{
  const time=OPEN+900,point=[time-60,100,99,2,100,101,99,100,.5,'outside_progress',1];
  const track={symbol:'NASDAQ:TEST',aligned:true,points:[point],
    td:{version:1,comparison_bars:4,trend_threshold:3,counts:[[point[0],4]]}};
  const saved=JSON.stringify(track);
  const raw={bar:{time,open:100,high:105,low:99,close:104},price_observed_at:time+5};
  const forming=rhythmFormingPoint(track,raw,time,time+5);
  assert.equal(forming.ema3,102);assert.equal(forming.z,1.5);
  assert.deepEqual(Object.keys(forming).sort(),['ema3','time','z']);
  for(const view of ['price','position']){
    const plot=rhythmChartModel({...track,forming},time-60,time,view,500,150,[-2,3],point[0]);
    assert(plot.forming?.d.includes('L'));assert.equal(plot.forming.time,time);
    assert.equal(plot.tdCount,4);assert.equal(plot.priceSegments[0].state,'up');
  }
  assert.equal(JSON.stringify(track),saved);
  for(const change of [
    {...raw,price_observed_at:time-100}, {...raw,bar:{...raw.bar,time:time+60}},
    {...raw,bar:{...raw.bar,low:105}}, {...raw,price_observed_at:time+6},
  ])assert.equal(rhythmFormingPoint(track,change,time,time+5),null);
  assert.equal(rhythmFormingPoint(track,raw,time,time+60),null);
  assert.equal(rhythmFormingPoint({...track,aligned:false},raw,time,time+5),null);
  assert.equal(rhythmFormingPoint({...track,issue:'minute_gap'},raw,time,time+5),null);
});

test('the Pine indicator holds confirmed TD through opposite live prices and colors only its yellow tail',async t=>{
  const {PineTS,Indicator}=require('pinets');
  const current=Math.floor(Date.now()/60000)*60000;
  const actualNow=Date.now;let clock=current+30000;Date.now=()=>clock;
  t.after(()=>{Date.now=actualNow;});
  let candles=Array.from({length:12},(_,i)=>({openTime:current+(i-11)*60000,closeTime:current+(i-10)*60000,
    open:100+i,high:101+i,low:99+i,close:100+i,volume:100}));
  const provider={configure(){},async getMarketData(){return candles;},async getSymbolInfo(){
    return {tickerid:'NASDAQ:TEST',timezone:'America/New_York',minmove:1,pricescale:100,mintick:.01};}};
  const pine=new PineTS(provider,'NASDAQ:TEST','1',candles.length);
  const source=await fs.readFile(path.join(__dirname,'../indicators/td-price-line.pine'),'utf8');
  const context=await pine.run(new Indicator(source));context.pine.barstate.setLive();
  for(const close of [111,90,115,111]){
    candles=[...candles.slice(0,-1),{...candles.at(-1),close,high:Math.max(112,close),low:Math.min(110,close)}];
    assert(await pine.updateTail(context));
    assert.equal(context.plots.TD_Count.data.at(-1).value,7);
    assert.equal(context.plots.TD_Count_Confirmed.data.at(-1).value,7);
    assert(Number.isNaN(context.plots.TD_Price.data.at(-1).value));
    assert.equal(context.plots.Forming_Price.data.at(-1).value,close);
    assert.equal(context.plots.Forming_Line.data.at(-1).options.color,'rgb(241, 196, 15)');
  }
  clock=current+90000;
  candles=[...candles,{openTime:current+60000,closeTime:current+120000,open:112,close:112,high:113,low:111,volume:100}];
  assert(await pine.updateTail(context));
  assert.equal(context.plots.TD_Count.data.at(-1).value,8);
  assert.equal(context.plots.TD_Price.data.at(-2).value,111);
  assert.equal(context.plots.Forming_Price.data.at(-1).value,112);
});

test('a hidden tab invalidates pending reads; late responses cannot overwrite a new selection',async()=>{
  const {MarketRhythm}=require('../console/static/market-rhythm');
  const original=global.fetch;let finish;
  global.fetch=()=>new Promise(resolve=>{finish=resolve;});
  try{
    const ui={...MarketRhythm.data(),active:true,requestId:0};
    const pending=MarketRhythm.methods.refresh.call(ui);
    MarketRhythm.methods.stop.call(ui);
    finish({ok:true,json:async()=>({status:'ready',selected:'OLD',revision:1})});
    await pending;
    assert.equal(ui.frame,null);assert.equal(ui.loading,false);
    assert.equal(ui.error,'');
  }finally{global.fetch=original;}
});

test('index minute requests omit session and preload enough overnight history for yesterday RTH warmup',()=>{
  const {minuteSubscriptionOptions}=require('../service/market_rhythm');
  const index=minuteSubscriptionOptions(true);
  assert.equal(index.timeframe,'1');
  assert.equal('session' in index,false);
  assert(index.range>=3000);
  assert.deepEqual(minuteSubscriptionOptions(false),{timeframe:'1',range:850,session:'regular'});
});

test('premarket historical curves are distinct from stale RTH data',()=>{
  const {rhythmSessionState}=require('../console/static/market-rhythm');
  assert.equal(rhythmSessionState(OPEN-86400,OPEN-60),'history');
  assert.equal(rhythmSessionState(OPEN+240,OPEN+300),'confirmed');
  assert.equal(rhythmSessionState(OPEN-86400,OPEN+300),'stale');
  assert.equal(rhythmSessionState(OPEN+23400,OPEN+23500),'history');
});

test('native six-pane focus and eight-pane candidate targets retain their separate ranking maps',()=>{
  const {MarketRhythm}=require('../console/static/market-rhythm');
  const rows=Array.from({length:10},(_,i)=>({symbol:'S'+i,rank:i+1}));
  const emitted=[];
  const ui={...MarketRhythm.data(),active:true,frame:{day:'2026-11-18',as_of:OPEN+60,coverage:{total:12,aligned:12,missing:[]},ranks:rows,tracks:[{symbol:'CAPITALCOM:NAS100',is_beta:true},{symbol:'CAPITALCOM:SPX500',is_beta:true},...rows]},
    ...MarketRhythm.methods,refresh:()=>{},$emit:(...args)=>emitted.push(args)};
  for(const key of ['positions','sourceTracks','rows','tracks','tvSymbols','focusSymbols','gridKey'])Object.defineProperty(ui,key,{get:()=>MarketRhythm.computed[key].call(ui)});
  ui.reconcileDisplay();
  assert.equal(ui.windowMinutes,390);
  assert.equal(ui.tracks.length,4);
  assert.deepEqual(ui.tracks.map(t=>t.symbol),['CAPITALCOM:NAS100','CAPITALCOM:SPX500','S0','S1']);
  assert.deepEqual(ui.tvSymbols,['S8','S6','S4','S2','S9','S7','S5','S3']);
  ui.choose('S5');
  assert.deepEqual(ui.tracks.slice(2).map(t=>t.symbol),['S0','S1']);
  assert.deepEqual(emitted.at(-1),['select','S5']);
  ui.publishGrid();assert.deepEqual(emitted.at(-1),['grid',{symbols:ui.tvSymbols,focusSymbols:ui.focusSymbols,force:false}]);
  const oldKey=ui.gridKey;
  ui.frame.ranks=rows.map(r=>({...r,rank:r.rank===3?4:r.rank===4?3:r.rank}));
  ui.reconcileDisplay();assert.equal(ui.gridKey,oldKey);
  ui.frame.as_of+=600;ui.reconcileDisplay();
  assert.notEqual(ui.gridKey,oldKey);assert.equal(ui.tvSymbols[3],'S3');assert.equal(ui.tvSymbols[7],'S2');
  assert.deepEqual(ui.tracks.slice(2).map(t=>t.symbol),['S0','S1']);
  const count=emitted.length;ui.frame.ranks=rows.slice(0,6);ui.frame.as_of+=60;ui.reconcileDisplay();ui.publishGrid();
  assert.equal(emitted.length,count+1); // The six-pane focus layout remains usable while eight ranks are incomplete.
  assert.deepEqual(emitted.at(-1)[1].symbols,[]);
  assert.deepEqual(emitted.at(-1)[1].focusSymbols,['S0','CAPITALCOM:NAS100','CAPITALCOM:SPX500','S1','NASDAQ:QQQ','AMEX:SPY']);
  assert.equal(ui.tracks.length,4);
});

test('ranking actions select independently and open the manual card for any of the top ten',()=>{
  const {MarketRhythm,RhythmTrack}=require('../console/static/market-rhythm');
  const emitted=[],frame={ranks:[]};
  const ui={frame,$emit:(...event)=>emitted.push(event)};
  MarketRhythm.methods.openRank.call(ui,{symbol:'NASDAQ:NVDA',direction_sign:1,rank:10});
  assert.equal(ui.selected,'NASDAQ:NVDA');
  assert.deepEqual(emitted,[['select','NASDAQ:NVDA'],['open',{symbol:'NASDAQ:NVDA',frame}]]);
  MarketRhythm.methods.openRank.call(ui,{symbol:'NASDAQ:MSFT',direction_sign:0});
  assert.equal(emitted.length,2);
  assert(!RhythmTrack.template.includes('rhythm-open-order'));
  assert(!MarketRhythm.template.includes('spark('));
  assert(MarketRhythm.template.includes('class="rhythm-rank-list"'));
});

test('beta accumulates the full provider daily session, keeps EMA continuous and never resets at 09:30',()=>{
  const start=OPEN-16.5*3600;
  const bars=Array.from({length:1005},(_,i)=>bar(start+i*60,100+i*.01));
  const daily=[{time:start},{time:start+86400}];
  const result=calculateSeries('CAPITALCOM:NAS100',bars,OPEN+900,daily);
  assert.equal(result.points.length,1005);
  const p=result.points.find(p=>p[0]===OPEN);
  assert(p[3]>1);
  const prefix=bars.filter(b=>b.time<=OPEN);
  const mean=prefix.reduce((n,b)=>n+(b.high+b.low+b.close)/3,0)/prefix.length;
  assert(Math.abs(p[2]-mean)<1e-6);
  assert(p[1]>bars.at(-16).close-.02);
  assert.equal(result.issue,null);
  const future=calculateSeries('CAPITALCOM:NAS100',[...bars,bar(start+86400,200)],start+86460,daily);
  assert.equal(future.points.at(-1)[2],200);
  assert(future.points.at(-1)[1]<200); // EMA carries into next session.
});

test('overnight beta cannot wipe yesterday stock candidates or leak tomorrow into synchronized panels',()=>{
  const stock=series(rising,OPEN+900,'NASDAQ:A');
  const beta=calculateSeries('CAPITALCOM:NAS100',
    [...rising,bar(OPEN+86400,999)],OPEN+86460,[{time:OPEN},{time:OPEN+86400}]);
  const frame=buildFrame({[stock.symbol]:stock,[beta.symbol]:beta},[stock.symbol,beta.symbol],OPEN+86460,{[stock.symbol]:scoreHistory(stock.last_closed+60)});
  assert.equal(frame.as_of,OPEN+900);
  assert.equal(frame.ranks[0].symbol,'NASDAQ:A');
  assert(frame.series[beta.symbol].points.every(p=>p[0]+60<=frame.as_of));
  assert(frame.series[beta.symbol].td.counts.every(p=>p[0]+60<=frame.as_of));
  assert.equal(frame.series[beta.symbol].points.at(-1)[7],128);
});

test('rhythm preview delegates stops with confirmed ATR and retains perception freshness checks',()=>{
  const {rhythmTradePreview}=require('../console/static/market-rhythm');
  const point=[OPEN+600,100,99,2,100,101,99,100,2,'outside_progress',1];
  const row={symbol:'A',points:[point],aligned:true};
  const frame={tracks:[row]}, data={bar:{close:103,time:OPEN+600},ts_c:{atr:1},ts:{atr:50},price_observed_at:OPEN+665};
  const stop=(side,atr)=>{assert.equal(atr,1);return side==='long'?101:105;};
  const long=rhythmTradePreview(frame,'A',data,OPEN+665,stop);
  assert.equal(long.side,'long');assert.equal(long.stopPx,101);assert(long.ready);
  point[10]=-1;
  const short=rhythmTradePreview(frame,'A',data,OPEN+665,stop);
  assert.equal(short.stopPx,105);assert.equal(short.side,'short');
  assert.equal(rhythmTradePreview(frame,'A',data,OPEN+800,stop).ready,false);
  row.issue='minute_gap';
  assert.equal(rhythmTradePreview(frame,'A',data,OPEN+665,stop).ready,false);
});

test('a provider maintenance break at the daily anchor does not force a fake 09:30 reset',()=>{
  const anchor=OPEN-16.5*3600;
  const bars=[bar(anchor-60,100),...Array.from({length:10},(_,i)=>bar(anchor+300+i*60,101+i))];
  const result=calculateSeries('CAPITALCOM:SPX500',bars,anchor+960,[{time:anchor-86400},{time:anchor}]);
  assert.equal(result.points[1][2],101);
  assert(result.points.at(-1)[3]>0);
  const partial=calculateSeries('CAPITALCOM:SPX500',bars.slice(1),anchor+960,[{time:anchor}]);
  assert.equal(partial.issue,'missing_session_anchor');
  assert.equal(partial.points.at(-1)[2],null);
});

const {rhythmPositions,rhythmDisplaySlots,rhythmRankingReady}=require('../console/static/market-rhythm');
const rankedFrame=(symbols,as_of=OPEN+60)=>({day:'2026-11-18',as_of,coverage:{total:53,aligned:53,missing:[]},ranks:symbols.map((symbol,i)=>({symbol,rank:i+1,direction_sign:1}))});
const rankedSymbols=Array.from({length:10},(_,i)=>'NASDAQ:S'+i);
const held=(symbol,openedAt=1)=>({symbol,qty:10,side:'long',openedAt});

test('positions take the first three display slots and never duplicate candidates or mutate raw ranks',()=>{
 const raw=rankedFrame(rankedSymbols),saved=JSON.stringify(raw);
 const state=rhythmDisplaySlots(null,raw,[held('NASDAQ:X'),held('NASDAQ:Y'),held('NASDAQ:S7')]);
 assert.deepEqual(state.order.slice(0,3),['NASDAQ:X','NASDAQ:Y','NASDAQ:S7']);
 assert.equal(state.order.length,10);assert.equal(new Set(state.order).size,10);assert.equal(JSON.stringify(raw),saved);
 const changed=rankedFrame([...rankedSymbols].reverse(),OPEN+300);
 assert.deepEqual(rhythmDisplaySlots(state,changed,[held('NASDAQ:X'),held('NASDAQ:Y'),held('NASDAQ:S7')]).order,state.order);
});

test('score changes do not switch charts until the next ten-minute confirmation boundary',()=>{
 const state=rhythmDisplaySlots(null,rankedFrame(rankedSymbols),[]);
 const reversed=[...rankedSymbols].reverse();
 assert.deepEqual(rhythmDisplaySlots(state,rankedFrame(reversed,OPEN+540),[]).order,state.order);
 assert.deepEqual(rhythmDisplaySlots(state,rankedFrame(reversed,OPEN+600),[]).order,reversed);
});

test('a departing candidate is replaced in its own slot immediately, while an out-of-rank position stays pinned',()=>{
 const positions=[held('NASDAQ:X'),held('NASDAQ:Y'),held('NASDAQ:S7')];
 const state=rhythmDisplaySlots(null,rankedFrame(rankedSymbols),positions),departed=state.order[5];
 const next=rankedFrame(['NASDAQ:NEW',...rankedSymbols.filter(s=>s!==departed)].slice(0,10),OPEN+120);
 const updated=rhythmDisplaySlots(state,next,positions);
 assert.equal(updated.order[5],'NASDAQ:NEW');
 updated.order.forEach((s,i)=>{if(i!==5)assert.equal(s,state.order[i]);});
});

test('new positions immediately claim the leading slots, partial exits retain priority, and flat positions release them',()=>{
 const raw=rankedFrame(rankedSymbols),initial=rhythmDisplaySlots(null,raw,[]);
 const positions=rhythmPositions([{symbol:'NASDAQ:X',state:'ACTIVE',side:'long',filled_qty:5,entry_time:100}],rankedSymbols);
 const added=rhythmDisplaySlots(initial,raw,positions);assert.equal(added.order[0],'NASDAQ:X');
 const reduced=rhythmPositions([{symbol:'NASDAQ:X',state:'CLOSING',side:'long',filled_qty:1,entry_time:100}],rankedSymbols);
 assert.deepEqual(rhythmDisplaySlots(added,raw,reduced).order,added.order);
 const flat=rhythmDisplaySlots(added,raw,[]);assert(!flat.order.includes('NASDAQ:X'));assert.equal(flat.order.length,10);
});

test('actual filled positions merge symbol aliases, ignore unfilled and closed lots, and preserve both sides',()=>{
 const base={symbol:'NASDAQ:NVDA',state:'ACTIVE',side:'long',filled_qty:5,entry_time:100};
 const rows=rhythmPositions([base,{...base,symbol:'BATS:NVDA',side:'short',filled_qty:2},
  {...base,state:'CLOSED',filled_qty:100}, {...base,state:'PENDING',filled_qty:0},
  {...base,symbol:'NVDA',state:'PENDING',filled_qty:1}],['BATS:NVDA','NASDAQ:NVDA']);
 assert.equal(rows.length,1);assert.equal(rows[0].symbol,'NASDAQ:NVDA');assert.equal(rows[0].qty,8);assert.equal(rows[0].side,'mixed');
});

test('persisted slots restore without reshuffling and a new session starts a fresh candidate ordering',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols),[held('NASDAQ:X')]);
 const changed=rankedFrame([...rankedSymbols].reverse(),OPEN+300);
 assert.deepEqual(rhythmDisplaySlots(JSON.parse(JSON.stringify(initial)),changed,[held('NASDAQ:X')]).order,initial.order);
 const next=rhythmDisplaySlots(initial,{...changed,day:'2026-11-19',as_of:OPEN+86460},[held('NASDAQ:X')]);
 assert.deepEqual(next.order,['NASDAQ:X',...changed.ranks.slice(0,9).map(r=>r.symbol)]);
});

test('positions win even if more than ten exist, and candidate shortages never invent symbols',()=>{
 const positions=Array.from({length:12},(_,i)=>held('NASDAQ:P'+i));
 assert.deepEqual(rhythmDisplaySlots(null,rankedFrame(rankedSymbols),positions).order,positions.slice(0,10).map(p=>p.symbol));
 const empty=rankedFrame([]);assert.deepEqual(rhythmDisplaySlots(null,empty,[held('NASDAQ:X')]).order,['NASDAQ:X']);
});

test('holding management filters the order batches instead of opening another entry card',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm'),emitted=[];
 const ui={...MarketRhythm.methods,$emit:(...e)=>emitted.push(e)};
 ui.openRank({symbol:'NASDAQ:X',isHeld:true,direction_sign:1});
 assert.deepEqual(emitted,[['select','NASDAQ:X'],['manage','NASDAQ:X']]);
});

test('portfolio-first TV maps agree with the displayed slots and wait for the first actual position snapshot',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm'),emitted=[];
 const ui={...MarketRhythm.data(),...MarketRhythm.methods,active:true,positionsReady:false,lots:[],
  frame:{...rankedFrame(rankedSymbols),tracks:[]},$emit:(...e)=>emitted.push(e)};
 for(const key of ['positions','sourceTracks','rows','tvSymbols','focusSymbols','gridKey'])Object.defineProperty(ui,key,{get:()=>MarketRhythm.computed[key].call(ui)});
 ui.reconcileDisplay();ui.publishGrid();assert.equal(ui.displayState,null);assert.equal(emitted.length,0);
 ui.lots=['NASDAQ:X','NASDAQ:Y','NASDAQ:S7'].map((symbol,i)=>({symbol,state:'ACTIVE',filled_qty:10,side:'long',entry_time:i+1}));
 ui.positionsReady=true;ui.reconcileDisplay();ui.publishGrid();
 assert.deepEqual(ui.rows.slice(0,3).map(r=>r.symbol),['NASDAQ:X','NASDAQ:Y','NASDAQ:S7']);
 assert(ui.rows.slice(0,3).every(r=>r.isHeld));
 assert.deepEqual(ui.focusSymbols,['NASDAQ:X','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:Y','NASDAQ:QQQ','AMEX:SPY']);
 assert.equal(ui.tvSymbols[3],'NASDAQ:S7');
 const key=ui.gridKey;ui.frame=rankedFrame([...rankedSymbols].reverse(),OPEN+300);ui.reconcileDisplay();
 assert.equal(ui.gridKey,key);assert.equal(ui.rows[2].candidateRank,3);
});

test('a partially arriving minute cannot evict candidates; its final batch replaces only the departing slot',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols));
 const partial={...rankedFrame(['NASDAQ:TEMP',...rankedSymbols.slice(0,9)],OPEN+120),
   coverage:{total:53,aligned:10,missing:rankedSymbols.slice(1)}};
 assert(!rhythmRankingReady(partial));
 const waiting=rhythmDisplaySlots(initial,partial);
 assert.deepEqual(waiting,initial);
 const final=rankedFrame(['NASDAQ:NEW',...rankedSymbols.filter((s,i)=>i!==4)],OPEN+120);
 const accepted=rhythmDisplaySlots(waiting,final);
 accepted.order.forEach((s,i)=>assert.equal(s,i===4?'NASDAQ:NEW':initial.order[i]));
 assert.equal(accepted.candidateAsOf,OPEN+120);
});

test('visible minute ranking reorders independently of ten-minute TV seats and replaces departures locally',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const ui={...MarketRhythm.data(),...MarketRhythm.methods,positionsReady:true,lots:[],market:{},
  frame:rankedFrame(rankedSymbols),trader:'isolated'};
 for(const key of ['positions','sourceTracks','rows','rankingRows','tvSymbols','focusSymbols','gridKey'])
  Object.defineProperty(ui,key,{get:()=>MarketRhythm.computed[key].call(ui)});
 ui.reconcileDisplay();const seats=ui.rows.map(r=>r.symbol),key=ui.gridKey;
 ui.frame=rankedFrame([...rankedSymbols].reverse(),OPEN+120);ui.reconcileDisplay();
 assert.deepEqual(ui.rankingRows.map(r=>r.symbol),[...rankedSymbols].reverse());
 assert.deepEqual(ui.rankingRows.map(r=>r.rank),[1,2,3,4,5,6,7,8,9,10]);
 assert.deepEqual(ui.rows.map(r=>r.symbol),seats);assert.equal(ui.gridKey,key);
 const departed=seats[4],replacement='NASDAQ:NEW';
 ui.frame=rankedFrame([replacement,...[...rankedSymbols].reverse().filter(s=>s!==departed)],OPEN+180);
 ui.reconcileDisplay();
 ui.rows.forEach((r,i)=>assert.equal(r.symbol,i===4?replacement:seats[i]));
 assert.equal(ui.rankingRows[0].symbol,replacement);
 ui.frame=rankedFrame([...rankedSymbols].reverse(),OPEN+600);ui.reconcileDisplay();
 assert.deepEqual(ui.rows.map(r=>r.symbol),[...rankedSymbols].reverse());
});

test('held stocks remain pinned while all ten current candidates keep their real ranks',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const ui={...MarketRhythm.data(),...MarketRhythm.methods,positionsReady:true,
  lots:[{symbol:'NASDAQ:HELD',state:'ACTIVE',filled_qty:5,side:'long',entry_time:1}],market:{},frame:rankedFrame(rankedSymbols)};
 for(const key of ['positions','sourceTracks','rows','rankingRows'])Object.defineProperty(ui,key,{get:()=>MarketRhythm.computed[key].call(ui)});
 ui.reconcileDisplay();assert.equal(ui.rows.length,10);assert.equal(ui.rankingRows.length,11);
 assert.equal(ui.rankingRows[0].symbol,'NASDAQ:HELD');assert.equal(ui.rankingRows[0].rank,null);
 assert.deepEqual(ui.rankingRows.slice(1).map(r=>r.rank),[1,2,3,4,5,6,7,8,9,10]);
});

test('a closed-minute candidate snapshot is accepted once; later revisions cannot change names or rank metadata',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols));
 const revised=rankedFrame(['NASDAQ:LIVE',...rankedSymbols].slice(0,10));
 const unchanged=rhythmDisplaySlots(initial,revised);
 assert.deepEqual(unchanged,initial);
 revised.as_of+=60;
 const next=rhythmDisplaySlots(initial,revised);
 assert.equal(next.order.at(-1),'NASDAQ:LIVE');
 assert.equal(next.candidateRows[0].symbol,'NASDAQ:LIVE');
 const intrabar={...revised,as_of:revised.as_of+5};
 assert(!rhythmRankingReady(intrabar));assert.deepEqual(rhythmDisplaySlots(next,intrabar),next);
});

test('an incomplete ten-minute boundary preserves the old order until its complete closed-minute batch arrives',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols,OPEN+540));
 const reversed=[...rankedSymbols].reverse(),partial={...rankedFrame(reversed,OPEN+600),
   coverage:{total:53,aligned:52,missing:['NASDAQ:MISSING']}};
 const waiting=rhythmDisplaySlots(initial,partial);
 assert.deepEqual(waiting.order,initial.order);assert.equal(waiting.bucket,initial.bucket);
 const final=rhythmDisplaySlots(waiting,rankedFrame(reversed,OPEN+600));
 assert.deepEqual(final.order,reversed);assert.notEqual(final.bucket,initial.bucket);
});

test('holdings still take priority during an incomplete minute without admitting provisional candidates',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols));
 const partial={...rankedFrame(['NASDAQ:PROVISIONAL',...rankedSymbols].slice(0,10),OPEN+120),
   coverage:{total:53,aligned:15,missing:['NASDAQ:LATE']}};
 const pinned=rhythmDisplaySlots(initial,partial,[held('NASDAQ:POSITION')]);
 assert.equal(pinned.order[0],'NASDAQ:POSITION');assert(!pinned.order.includes('NASDAQ:PROVISIONAL'));
 assert.deepEqual(rhythmDisplaySlots(pinned,partial).order,rankedSymbols);
 assert.equal(rhythmDisplaySlots(null,partial).order.length,0);
 assert.deepEqual(rhythmDisplaySlots(null,partial,[held('NASDAQ:POSITION')]).order,['NASDAQ:POSITION']);
});
