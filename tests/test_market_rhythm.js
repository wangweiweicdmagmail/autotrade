'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {calculateSeries: calculateSessionSeries,buildFrame:buildRealFrame,createMarketRhythm}=require('../service/market_rhythm');
const calculateSeries=(symbol,bars,now,daily=[])=>calculateSessionSeries(symbol,bars,now,daily,{marketSession:'regular'});
const {rhythmChartModel,rhythmFormingPoint}=require('../console/static/market-rhythm');
const OPEN=Date.parse('2026-11-18T14:30:00Z')/1000;
const prior=OPEN-86400;


const bar=(time,close)=>({time,open:close,high:close+.1,low:close-.1,close,volume:100});
const warm=Array.from({length:40},(_,i)=>bar(prior+i*60,100));
const rising=Array.from({length:15},(_,i)=>bar(OPEN+i*60,100+i*2));
const series=(bars,now=OPEN+900,symbol='NASDAQ:TEST')=>calculateSeries(symbol,[...warm,...bars],now,[{time:prior},{time:OPEN}]);
const scoreHistory=time=>Array.from({length:9},(_,i)=>({time:Math.floor(time/300)*300-(i+1)*300,
  known_at:Math.floor(time/300)*300-i*300,ema9:100-i,atr:2}));

// Synthetic confirmed Pine outputs isolate chart, TD, TV-seat and snapshot behavior.
function buildFrame(series,symbols,now,histories={},options={}){
 const openingPositions=Object.fromEntries(Object.values(series).filter(r=>!r.symbol.startsWith('CAPITALCOM:')).map(row=>{
  const points=row.points||[],p=points.at(-1),first=points.find(q=>q[0]===OPEN);
  const early=points.filter(q=>q[0]>=OPEN&&q[0]<OPEN+900);
  const high=Math.max(...early.map(q=>q[5])),low=Math.min(...early.map(q=>q[6])),width=high-low;
  const position=p&&first?(p[7]-(high+low)/2)/(width/2):NaN;
  return [row.symbol,{time:p?.[0],ready:early.length===15?1:0,day_open:first?.[4],opening_high:high,opening_low:low,width,
   position,range_position:position,direction:Math.sign(position),opening_start:OPEN,range_confirmed_at:OPEN+900,range_bars:early.length,midpoint:(high+low)/2}];
 }));
 return buildRealFrame(series,symbols,now,histories,{...options,openingPositions});
}

function benchmarkFrame(){
 const at=OPEN+900;
 return {status:'ready',day:'2026-11-18',as_of:at,ranking_snapshot:{version:1,id:'index-minute',as_of:at,day:'2026-11-18'},
  tracks:['CAPITALCOM:NAS100','CAPITALCOM:SPX500'].map((symbol,i)=>({symbol,is_beta:true,aligned:true,
   points:[[at-60,100,100,2,100,101,99,100,0,'waiting',0]],
   td:{version:1,comparison_bars:4,trend_threshold:3,counts:[[at-120,99],[at-60,i?-4:3],[at,25]]}}))};
}

function liveUi(frame,extra={}) {
 const {MarketRhythm,rhythmConfirmedTd}=require('../console/static/market-rhythm');
 const entries=[...(frame?.tracks||[]).map(row=>[row.symbol,rhythmConfirmedTd(row,frame.as_of-60)]),
  ...(frame?.td_ranking?.ranks||[]).map(r=>[r.symbol,r.td_count])];
 const rows=Object.fromEntries(entries.map(([symbol,count])=>[symbol,{version:1,source:'confirmed_minute_v1',id:'fact-'+symbol,
  known:Number.isSafeInteger(count),ready:Number.isSafeInteger(count),count,bar_time:frame.as_of-60,confirmed_at:frame.as_of,
  quote_bar_time:frame.as_of,observed_at:frame.as_of+1}]));
 return {...MarketRhythm.methods,frame,now:(frame?.as_of||OPEN)+5,tdSnapshot:{version:1,rows},...extra};
}

test('a newly accepted snapshot refreshes the visible ranking immediately without repeat TV churn',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const calls=[],ui={active:true,frame:{ranking_snapshot:{id:'previous'}},refresh:force=>calls.push(force)};
 MarketRhythm.watch.rankingSnapshotId.call(ui,'new-minute');assert.deepEqual(calls,[true]);
 ui.frame.ranking_snapshot.id='new-minute';
 MarketRhythm.watch.rankingSnapshotId.call(ui,'new-minute');
 MarketRhythm.watch.rankingSnapshotId.call(ui,'');
 ui.active=false;MarketRhythm.watch.rankingSnapshotId.call(ui,'later-minute');
 assert.deepEqual(calls,[true]);
});

test('boundary confirmation preserves the actual receive timestamp independently of calculation time',()=>{
 const feed=createMarketRhythm({symbols:['NASDAQ:TEST'],clock:()=>OPEN+900});
 feed.observe('NASDAQ:TEST',rising,OPEN+900,null,[],OPEN+899.472);
 const fact=feed.confirmedState('NASDAQ:TEST',OPEN+900);
 assert.equal(fact.time,OPEN+840);assert.equal(fact.received_at,OPEN+899.472);
 assert.equal(fact.computed_at,OPEN+900);assert.equal(fact.published_at,OPEN+900);
});

test('two fixed index TD rows use the accepted minute and keep their order through a pending batch',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const ui=liveUi(benchmarkFrame(),{displayState:{candidateSnapshotId:'index-minute'},locked:true,focus:OPEN});
 const rows=MarketRhythm.computed.benchmarkRows.call(ui);
 assert.deepEqual(rows.map(r=>r.symbol),['CAPITALCOM:NAS100','CAPITALCOM:SPX500']);
 assert.deepEqual(rows.map(r=>r.tdLabel),['TD +3','TD -4']);
 assert.deepEqual(rows.map(r=>r.tdState),['up','down']);
 assert(rows.every(r=>r.confirmedAt===OPEN+900));
 ui.frame.batch_progress={waiting:true,coverage:{aligned:1,total:53}};
 ui.frame.tracks.reverse();
 assert.deepEqual(MarketRhythm.computed.benchmarkRows.call(ui),rows);
 ui.frame.as_of+=60;ui.frame.ranking_snapshot={...ui.frame.ranking_snapshot,id:'next-minute',as_of:ui.frame.as_of};
 assert.deepEqual(MarketRhythm.computed.benchmarkRows.call(ui),rows); // TD is independent of ranking batches.
});

test('invalid legacy ranking TD cannot suppress separately published index confirmation values',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 for(const fault of ['missing','duplicate_track','duplicate_count','wrong_minute','gap','wrong_contract','boolean']){
  const frame=benchmarkFrame(),ui=liveUi(frame),row=frame.tracks[0];
  if(fault==='missing')frame.tracks.shift();
  else if(fault==='duplicate_track')frame.tracks.push(row);
  else if(fault==='duplicate_count')row.td.counts.push([frame.as_of-60,7]);
  else if(fault==='wrong_minute')row.td.counts.splice(1,1);
  else if(fault==='gap')row.issue='minute_gap';
  else if(fault==='wrong_contract')row.td.comparison_bars=3;
  else row.td.counts[1][1]=true;
  const rows=MarketRhythm.computed.benchmarkRows.call(ui);
  assert.equal(rows.length,2);assert.equal(rows[0].tdLabel,'TD +3');assert.equal(rows[1].tdLabel,'TD -4');
 }
 assert(MarketRhythm.computed.benchmarkRows.call(liveUi(null)).every(r=>r.tdCount===null));
 const template=MarketRhythm.template;
 assert(template.indexOf('v-for="row in benchmarkRows"')<template.indexOf('v-for="row in visibleRankingRows"'));
 const fixed=template.split('v-for="row in benchmarkRows"')[1].split('v-if="positionOverflow"')[0];
 assert(!fixed.includes('openRank')&&!fixed.includes('rhythm-open-order'));
});

test('index EMA9 is projected onto the stock cutoff, without borrowing a later five-minute value',()=>{
 const now=OPEN+960,stock=series(rising,OPEN+900,'NASDAQ:A');
 const index=series([...rising,bar(OPEN+900,200)],now,'CAPITALCOM:NAS100');
 const history=scoreHistory(now);history.push({time:OPEN+900,known_at:OPEN+1200,ema9:999,atr:2});
 const f=buildFrame({'NASDAQ:A':stock,'CAPITALCOM:NAS100':index},['NASDAQ:A','CAPITALCOM:NAS100'],now,
  {'CAPITALCOM:NAS100':history,'NASDAQ:A':scoreHistory(OPEN+900)});
 assert.equal(f.as_of,OPEN+900);
 assert.equal(f.series['CAPITALCOM:NAS100'].beta_ema9.value,100);
 assert.equal(f.series['CAPITALCOM:NAS100'].beta_ema9.known_at,f.as_of);
 assert.equal(f.series['CAPITALCOM:NAS100'].points.at(-1)[0],OPEN+840);
 assert.deepEqual(f.ranks.map(r=>r.symbol),['NASDAQ:A']);
});

test('default-session index history supplies confirmed EMA9 and archives it with the minute context',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'beta-ema-'));
 try{
  const symbol='CAPITALCOM:NAS100',file=path.join(dir,'frame.json');
  const feed=createMarketRhythm({symbols:[symbol],cachePath:file});
  feed.observe(symbol,[...warm,...rising],OPEN+900,null,[{time:prior},{time:OPEN}]);
  const periods=Array.from({length:80},(_,i)=>bar(OPEN+600-i*300,100+i*.01));
  feed.observeFiveMinute(symbol,periods,new Map(),OPEN+900);await feed.flush(OPEN+905);
  const frame=JSON.parse(await fs.readFile(file,'utf8')),ref=frame.series[symbol].beta_ema9;
  assert(ref.value>0);assert.equal(ref.period,9);assert.equal(ref.known_at,OPEN+900);
  assert.equal(frame.series[symbol].source.session,'default');
  const {rankingRecord}=require('../service/research_journal');
  assert.deepEqual(rankingRecord(frame).context[symbol].beta_ema9,ref);
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});

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
  assert.equal(ended.metrics.direction_sign,-1);
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
  assert.equal(noWarm.issue,null);assert.equal(noWarm.metrics,null);
  assert.equal(buildFrame({TEST:noWarm},['TEST'],OPEN+900).ranks.length,1);
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
    const file=path.join(dir,'frame.json'),feed=createMarketRhythm({marketSession:'regular',symbols:['NASDAQ:TEST'],cachePath:file});
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
  const next={...frame,as_of:frame.as_of+60,ranks:[{symbol:'NVDA',rank:1,direction_sign:-1,score:2,z:-2,
    classification_rate:1,classification_samples:15,classification_correct:15,ema9_slope_atr:-.5,ema9_bias_atr:-2,
    vwap_open:100,vwap_aligned_progress_sigma:1,classification_correct:15,
    score_parts:{position:33,vwap_progress:15,classification:20}},{symbol:'NFLX',rank:2,direction_sign:-1,score:60}]};
  const display=rhythmDisplaySlots(prior,next,[]);
  assert.deepEqual(display.order,['NFLX','NVDA']); // Same ten-minute bucket: no chart reshuffle.
  const ui={frame:next,sourceTracks:{},positions:[],displayState:display};
  const rows=MarketRhythm.computed.rows.call(ui),nvda=rows[1];assert.equal(nvda.rank,2);assert.equal(nvda.candidateRank,1);
  const methods={...MarketRhythm.methods};
  assert(methods.rowDescription.call(methods,nvda).includes('强度#1'));
  assert(methods.scoreDescription.call(methods,nvda).includes('close'));
  assert(methods.scoreDescription.call(methods,nvda).includes('VWAP上方只做多'));
});

test('forming EMA3 is yellow-only data, while TD and confirmed rows remain unchanged',()=>{
  const time=OPEN+900,point=[time-60,100,99,2,100,101,99,100,.5,'outside_progress',1];
  const track={symbol:'NASDAQ:TEST',aligned:true,points:[point],
    td:{version:1,comparison_bars:4,trend_threshold:3,counts:[[point[0],4]]}};
  const saved=JSON.stringify(track);
  const raw={bar:{time,open:100,high:105,low:99,close:104},price_observed_at:time+5};
  const forming=rhythmFormingPoint(track,raw,time,time+5);
  assert.equal(forming.ema3,102);assert.equal(forming.z,2.5);
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
  assert.deepEqual(minuteSubscriptionOptions(false),{timeframe:'1',range:4000,session:'regular'});
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
  assert.deepEqual(emitted.at(-1),['select','S5',{view:'mixed'}]);
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
  assert.deepEqual(emitted.at(-1)[1].focusSymbols,['S0','CAPITALCOM:NAS100','CAPITALCOM:SPX500','S1','NASDAQ:QQQ','NASDAQ:NVDA']);
  assert.equal(ui.tracks.length,4);
});

test('ranking actions select independently and open the manual card for any of the top ten',()=>{
  const {MarketRhythm,RhythmTrack}=require('../console/static/market-rhythm');
  const emitted=[],frame={ranks:[]};
  const ui={frame,$emit:(...event)=>emitted.push(event)};
  MarketRhythm.methods.openRank.call(ui,{symbol:'NASDAQ:NVDA',direction_sign:1,rank:10});
  assert.equal(ui.selected,'NASDAQ:NVDA');
  assert.deepEqual(emitted,[['select','NASDAQ:NVDA',{view:'mixed'}],['open',{symbol:'NASDAQ:NVDA',frame,view:'mixed'}]]);
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

test('server snapshot identity replaces legacy cached membership and is immutable for the same minute',()=>{
  const {rhythmDisplaySlots}=require('../console/static/market-rhythm');
  const a=rankedFrame(['A','B']),legacy=rhythmDisplaySlots(null,a,[]);
  const server={...rankedFrame(['C','B']),ranking_snapshot:{version:1,id:'accepted-a',as_of:a.as_of,day:a.day}};
  const one=rhythmDisplaySlots(legacy,server,[]);
  const two=rhythmDisplaySlots(null,server,[]);
  assert.deepEqual(one.candidateRows,two.candidateRows);
  assert.equal(one.candidateSnapshotId,'accepted-a');
  assert.deepEqual(one.candidates,['C','B']);
  const ignored=rhythmDisplaySlots(one,{...server,ranks:rankedFrame(['D','E']).ranks},[]);
  assert.deepEqual(ignored.candidateRows,one.candidateRows);
});

test('pending progress never replaces accepted scores, TD or TV slots',()=>{
  const {rhythmDisplaySlots,MarketRhythm}=require('../console/static/market-rhythm');
  const first=rankedFrame(['A','B']);
  first.ranking_snapshot={version:1,id:'accepted-first',day:first.day,as_of:first.as_of};
  const before=rhythmDisplaySlots(null,first,[]);
  const pending={...first,batch_progress:{waiting:true,as_of:first.as_of+60,
    coverage:{total:53,aligned:52,missing:['LATE']},message:'等待数据'}};
  const after=rhythmDisplaySlots(before,pending,[]);
  assert.deepEqual(after,before);
  assert.equal(MarketRhythm.computed.rankingPending.call({frame:pending}),true);
  assert.equal(MarketRhythm.computed.rankingProgress.call({frame:pending}).aligned,52);
});
const rankedSymbols=Array.from({length:10},(_,i)=>'NASDAQ:S'+i);
const held=(symbol,openedAt=1)=>({symbol,qty:10,side:'long',openedAt});

test('two positions use reference cells and only overflow takes candidate seats without mutating raw ranks',()=>{
 const raw=rankedFrame(rankedSymbols),saved=JSON.stringify(raw);
 const state=rhythmDisplaySlots(null,raw,[held('NASDAQ:X'),held('NASDAQ:Y'),held('NASDAQ:S7')]);
 assert.deepEqual(state.referenceSlots,['NASDAQ:X','NASDAQ:Y']);
 assert.deepEqual(state.order.slice(0,3),['NASDAQ:S7','NASDAQ:S0','NASDAQ:S1']);
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

test('a first holding changes only a reference cell, partial exits keep it and a flat position restores the reference',()=>{
 const raw=rankedFrame(rankedSymbols),initial=rhythmDisplaySlots(null,raw,[]);
 const positions=rhythmPositions([{symbol:'NASDAQ:X',state:'ACTIVE',side:'long',filled_qty:5,entry_time:100}],rankedSymbols);
 const added=rhythmDisplaySlots(initial,raw,positions);
 assert.deepEqual(added.referenceSlots,['NASDAQ:X','']);assert.deepEqual(added.order,initial.order);
 const reduced=rhythmPositions([{symbol:'NASDAQ:X',state:'CLOSING',side:'long',filled_qty:1,entry_time:100}],rankedSymbols);
 assert.deepEqual(rhythmDisplaySlots(added,raw,reduced),added);
 const flat=rhythmDisplaySlots(added,raw,[]);
 assert.deepEqual(flat.referenceSlots,['','']);assert.deepEqual(flat.order,initial.order);
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
 assert.deepEqual(next.order,changed.ranks.map(r=>r.symbol));assert.deepEqual(next.referenceSlots,['NASDAQ:X','']);
});

test('twelve holdings fit across reference and candidate cells and shortages never invent candidate symbols',()=>{
 const positions=Array.from({length:13},(_,i)=>held('NASDAQ:P'+i));
 const state=rhythmDisplaySlots(null,rankedFrame(rankedSymbols),positions);
 assert.deepEqual(state.referenceSlots,positions.slice(0,2).map(p=>p.symbol));
 assert.deepEqual(state.order,positions.slice(2,12).map(p=>p.symbol));
 const {MarketRhythm}=require('../console/static/market-rhythm');
 assert.equal(MarketRhythm.computed.positionOverflow.call({positions}),1);
 const empty=rhythmDisplaySlots(null,rankedFrame([]),[held('NASDAQ:X')]);
 assert.deepEqual(empty.order,[]);assert.deepEqual(empty.referenceSlots,['NASDAQ:X','']);
});

test('reference cells keep their occupants in place, promote overflow on exit and restore QQQ and NVDA when flat',()=>{
 const frame=rankedFrame(rankedSymbols),positions=['X','Y','Z'].map(s=>held('NASDAQ:'+s));
 const initial=rhythmDisplaySlots(null,frame,positions);
 const promoted=rhythmDisplaySlots(initial,frame,positions.slice(1));
 assert.deepEqual(promoted.referenceSlots,['NASDAQ:Z','NASDAQ:Y']);
 const restoredCandidates=[rankedSymbols[9],...rankedSymbols.slice(0,9)];
 assert.deepEqual(promoted.order,restoredCandidates); // Fill the released slot; do not reshuffle the other nine.
 const one=rhythmDisplaySlots(promoted,frame,[positions[1]]);
 assert.deepEqual(one.referenceSlots,['','NASDAQ:Y']);assert.deepEqual(one.order,restoredCandidates);
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const ui={rows:rankedSymbols.map((symbol,i)=>({symbol,rank:i+1})),displayState:one};
 assert.deepEqual(MarketRhythm.computed.focusSymbols.call(ui),
  ['NASDAQ:S0','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S1','NASDAQ:QQQ','NASDAQ:Y']);
 ui.displayState=rhythmDisplaySlots(one,frame,[]);
 assert.deepEqual(MarketRhythm.computed.focusSymbols.call(ui).slice(4),['NASDAQ:QQQ','NASDAQ:NVDA']);
});

test('a holding in the top ten retains its candidate seat as well as the dedicated position cell',()=>{
 const initial=rhythmDisplaySlots(null,rankedFrame(rankedSymbols));
 const updated=rhythmDisplaySlots(initial,rankedFrame(rankedSymbols),[held(rankedSymbols[6])]);
 assert.deepEqual(updated.order,initial.order);assert.deepEqual(updated.referenceSlots,[rankedSymbols[6],'']);
});

test('old portfolio-first preferences are invalidated and v2 preserves reference cells across refreshes',()=>{
 const frame=rankedFrame(rankedSymbols),positions=[held('NASDAQ:X'),held('NASDAQ:Y')];
 const old={version:1,day:frame.day,order:['NASDAQ:X','NASDAQ:Y',...rankedSymbols.slice(0,8)],heldOrder:positions.map(p=>p.symbol)};
 const migrated=rhythmDisplaySlots(old,frame,positions);
 assert.equal(migrated.version,2);assert.deepEqual(migrated.order,rankedSymbols);
 const restored=rhythmDisplaySlots(JSON.parse(JSON.stringify(migrated)),rankedFrame([...rankedSymbols].reverse(),OPEN+120),positions);
 assert.deepEqual(restored.referenceSlots,migrated.referenceSlots);assert.deepEqual(restored.order,migrated.order);
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
 assert.deepEqual(ui.rows.slice(0,3).map(r=>r.symbol),['NASDAQ:S7','NASDAQ:S0','NASDAQ:S1']);
 assert(ui.rows[0].isHeld);
 assert.deepEqual(ui.focusSymbols,['NASDAQ:S7','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S0','NASDAQ:X','NASDAQ:Y']);
 assert.equal(ui.tvSymbols[3],'NASDAQ:S1');
 const key=ui.gridKey;ui.frame=rankedFrame([...rankedSymbols].reverse(),OPEN+300);ui.reconcileDisplay();
 assert.equal(ui.gridKey,key);assert.equal(ui.rows[0].candidateRank,3);
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
 assert.equal(ui.rankingRows[0].gridSeat,null);assert.equal(ui.rankingRows[0].referenceSlot,0);
 assert.equal(ui.gridLocation(ui.rankingRows[0]),'TV六格下中 · 持仓');
 assert.match(ui.scoreDescription(ui.rankingRows[0]),/TV六格下中/);
 assert.deepEqual(ui.rows.map(r=>r.symbol),rankedSymbols);
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
 assert.deepEqual(pinned.referenceSlots,['NASDAQ:POSITION','']);
 assert.deepEqual(pinned.order,rankedSymbols);assert(!pinned.order.includes('NASDAQ:PROVISIONAL'));
 assert.deepEqual(rhythmDisplaySlots(pinned,partial).order,rankedSymbols);
 assert.equal(rhythmDisplaySlots(null,partial).order.length,0);
 const first=rhythmDisplaySlots(null,partial,[held('NASDAQ:POSITION')]);
 assert.deepEqual(first.order,[]);assert.deepEqual(first.referenceSlots,['NASDAQ:POSITION','']);
 const overflow=rhythmDisplaySlots(pinned,partial,[held('NASDAQ:POSITION'),held('NASDAQ:SECOND'),held('NASDAQ:THIRD')]);
 assert.deepEqual(overflow.referenceSlots,['NASDAQ:POSITION','NASDAQ:SECOND']);
 assert.equal(overflow.order[0],'NASDAQ:THIRD');assert(!overflow.order.includes('NASDAQ:PROVISIONAL'));
});

test('position exit TD reuses confirmed individual series without waiting for ranking coverage',()=>{
 const feed=createMarketRhythm({symbols:['NASDAQ:A','NASDAQ:MISSING'],marketSession:'regular'});
 const now=OPEN+900,expected=series(rising,now,'NASDAQ:A').td.counts.at(-1);
 feed.observe('NASDAQ:A',[...warm,...rising,bar(now,9999)],now);
 const td=feed.confirmedTd('NASDAQ:A',now);
 assert.equal(td.time,expected[0]);assert.equal(td.count,expected[1]);assert.equal(td.comparison_bars,4);
 assert.equal(td.interval_seconds,60);assert.equal(td.session,'regular');
 feed.observe('NASDAQ:A',[...warm,...rising,bar(now,1)],now+1);
 assert.deepEqual(feed.confirmedTd('NASDAQ:A',now+1),td);
 assert.equal(feed.confirmedTd('NASDAQ:A',now+106),null);
 assert.equal(feed.confirmedTd('NASDAQ:MISSING',now),null);
 feed.reset('NASDAQ:A');assert.equal(feed.confirmedTd('NASDAQ:A',now),null);
});

test('confirmed entry and exit observations publish without a full ranking or a following candle',()=>{
 const now=OPEN+900;let clock=now+.02;
 const feed=createMarketRhythm({symbols:['NASDAQ:A','NASDAQ:MISSING'],marketSession:'regular',clock:()=>clock});
 feed.observe('NASDAQ:A',[...warm,...rising,bar(now,9999)],now+.01);
 clock=now+.03;const fact=feed.confirmedState('NASDAQ:A',clock);
 assert.equal(fact.time,now-60);assert.equal(fact.close,rising.at(-1).close);
 assert.equal(fact.td_count,feed.confirmedTd('NASDAQ:A',clock).count);
 assert.equal(fact.received_at,now+.01);assert.equal(fact.computed_at,now+.02);assert.equal(fact.published_at,now+.03);
 assert.equal(feed.confirmedState('NASDAQ:MISSING',clock),null);
 clock=now+1;feed.observe('NASDAQ:A',[...warm,...rising,bar(now,1)],clock);
 assert.equal(feed.confirmedState('NASDAQ:A',clock).td_count,fact.td_count); // Unclosed prices never change confirmed TD.
 assert.equal(feed.confirmedState('NASDAQ:A',clock).published_at,fact.published_at);
 feed.reset('NASDAQ:A');assert.equal(feed.confirmedState('NASDAQ:A',clock),null);
});

test('TD display retains the known confirmation while stale or future data cannot grant entry readiness',()=>{
 const {rhythmTdFact}=require('../console/static/market-rhythm');
 const now=OPEN+900,fact={version:1,source:'confirmed_minute_v1',id:'same-confirmed-value',known:true,ready:true,
  count:3,bar_time:now-60,confirmed_at:now,quote_bar_time:now,observed_at:now+1};
 assert(rhythmTdFact(fact,now+5).ready);
 const stale=rhythmTdFact(fact,now+50);assert(stale.known);assert.equal(stale.count,3);assert.equal(stale.ready,false);
 assert.equal(rhythmTdFact({...fact,bar_time:now,confirmed_at:now+60},now+5).known,false);
 assert.equal(rhythmTdFact({...fact,source:'unknown'},now+5).ready,false);
});

test('exit TD rejects a minute gap instead of inventing a fresh count',()=>{
 const feed=createMarketRhythm({symbols:['NASDAQ:A'],marketSession:'regular'});
 feed.observe('NASDAQ:A',[...warm,...rising.filter((_,i)=>i!==4)],OPEN+900);
 assert.equal(feed.confirmedTd('NASDAQ:A',OPEN+900),null);
});

test('index gaps cannot crash the whole feed when the speed reference minute is absent',()=>{
 const make=(time,close)=>({time,open:close,high:close+.1,low:close-.1,close,volume:100});
 const old=Array.from({length:40},(_,i)=>make(OPEN-86400+i*60,100+i));
 const today=Array.from({length:45},(_,i)=>make(OPEN+i*60,200+i*2)).filter(b=>b.time!==OPEN+41*60);
 const row=calculateSessionSeries('CAPITALCOM:NAS100',[...old,...today],OPEN+2700,[{time:OPEN-86400},{time:OPEN}]);
 assert.equal(row.issue,null);assert(row.metrics);assert.equal(row.metrics.rail_speed,0);
 assert.equal(row.last_closed,OPEN+2640);
});

test('explicit extended historical inputs can still calculate premarket curves without changing regular defaults',async()=>{
 const bars=Array.from({length:100},(_,i)=>bar(OPEN-19800+i*120,100+i));
 const now=bars.at(-1).time+60,pre=calculateSessionSeries('NASDAQ:A',bars,now,[],{marketSession:'extended'});
 assert.equal(pre.ranking_phase,'premarket');assert.equal(pre.issue,null);assert(pre.metrics);
 const hs=Array.from({length:12},(_,i)=>({time:Math.floor(now/300)*300-(i+1)*300,
  known_at:Math.floor(now/300)*300-i*300,ema9:100-i,atr:2}));
 const f=buildFrame({'NASDAQ:A':pre},['NASDAQ:A','NASDAQ:MISSING'],now+10,{'NASDAQ:A':hs},{marketSession:'extended'});
 assert.equal(f.ranking_session.phase,'premarket');assert.equal(f.ranking_session.cohort,'available');
 assert.deepEqual(f.ranks,[]);assert.equal(f.scoring.pending[0].reason,'opening_range_regular_only');assert.equal(f.coverage.aligned,1);
 assert.deepEqual(f.coverage.missing,['NASDAQ:MISSING']);
 assert.equal(f.scoring.model,'opening15_midpoint_v7');
 const {rhythmDisplaySlots,rhythmRankingReady}=require('../console/static/market-rhythm');
 f.ranking_snapshot={version:1,id:'pre-accepted',as_of:f.as_of,day:f.day};
 assert(rhythmRankingReady(f));assert.deepEqual(rhythmDisplaySlots(null,f).candidates,[]);
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'pre-batch-'));
 try{
  const file=path.join(dir,'frame.json'),feed=createMarketRhythm({symbols:['NASDAQ:A'],cachePath:file,marketSession:'extended'});
  feed.observe('NASDAQ:A',bars,now);
  await feed.flush(now+4);await assert.rejects(fs.readFile(file),/ENOENT/);
  await feed.flush(now+10);assert.equal(JSON.parse(await fs.readFile(file)).ranking_session.phase,'premarket');
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('one invalid perception series is removed without terminating other candle processing',()=>{
 const errors=[],feed=createMarketRhythm({symbols:['BAD','NASDAQ:A'],marketSession:'regular',onError:e=>errors.push(e)});
 assert.doesNotThrow(()=>feed.observe('BAD',[bar(1e15,100)],1e15+120));
 assert.equal(errors.length,1);assert.equal(errors[0].symbol,'BAD');
 feed.observe('NASDAQ:A',[...warm,...rising],OPEN+900);
 assert(feed.confirmedTd('NASDAQ:A',OPEN+900));
});

test('only trend rows are shown and retired board controls are absent',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const rows=[{symbol:'NASDAQ:A'}];
 assert.equal(MarketRhythm.computed.visibleRankingRows.call({rankingRows:rows}),rows);
 assert.equal(MarketRhythm.methods.setRankMode,undefined);
 assert.equal(MarketRhythm.methods.setTdSide,undefined);
 assert.doesNotMatch(MarketRhythm.template,/setRankMode|setTdSide|TD榜/);
});

test('side filters sync both TV layouts and carry the selected view on manual open',()=>{
 const {MarketRhythm,rhythmDisplaySlots}=require('../console/static/market-rhythm');
 const at=OPEN+1200,make=(symbol,rank,d)=>({symbol,rank,direction_sign:d,opening_position:d*(2-rank*.1)});
 const mixed=[make('NASDAQ:A',1,1),make('NASDAQ:B',2,-1)],long=[make('NASDAQ:A',1,1),make('NASDAQ:L',2,1)],short=[make('NASDAQ:B',1,-1)];
 const frame={status:'ready',day:'2026-11-18',as_of:at,ranking_snapshot:{version:1,id:'same-three-views',as_of:at,day:'2026-11-18'},
  coverage:{total:3,aligned:3,missing:[]},ranks:mixed,rankings:{mixed,long,short},tracks:[],ranking_tracks:[],symbols:['NASDAQ:A','NASDAQ:B','NASDAQ:L']};
 const emitted=[],ui={...MarketRhythm.data(),...MarketRhythm.methods,active:true,frame,lots:[],positionsReady:true,market:{},now:at+5,
  directionSnapshot:{rows:{'NASDAQ:L':{version:1,source:'confirmed_vwap_direction_v1',id:'live-L',known:true,ready:true,close:100,vwap:101,direction:-1,
   bar_time:at-60,confirmed_at:at,quote_bar_time:at,observed_at:at+1}}},tdSnapshot:{rows:{}},$emit:(...args)=>emitted.push(args)};
 for(const [name,get]of Object.entries(MarketRhythm.computed))Object.defineProperty(ui,name,{get:()=>get.call(ui)});
 ui.displayState=rhythmDisplaySlots(null,frame,[]);
 assert.deepEqual(ui.rankingRows.map(r=>r.symbol),['NASDAQ:A','NASDAQ:B']);
 const changeView=view=>{
  ui.rankingView=view;
  MarketRhythm.watch.rankingView.call(ui);
  MarketRhythm.watch.gridKey.call(ui);
  return emitted.at(-1)[1];
 };
 const longGrid=changeView('long');
 assert.deepEqual(ui.rankingRows.map(r=>r.symbol),['NASDAQ:A','NASDAQ:L']);
 assert.deepEqual(longGrid.focusSymbols,['NASDAQ:A','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:L','NASDAQ:QQQ','NASDAQ:NVDA']);
 assert.equal(longGrid.symbols.length,8);
 assert(longGrid.symbols.every(s=>s==='CAPITALCOM:NAS100'||s==='CAPITALCOM:SPX500'));
 const row=ui.rankingRows[1];assert.equal(row.direction_sign,1);assert.equal(row.entryDirection,-1);
 ui.openRank(row);assert.equal(emitted.at(-1)[1].view,'long');assert.equal(emitted.at(-1)[1].symbol,'NASDAQ:L');
 const shortGrid=changeView('short');
 assert.deepEqual(ui.rankingRows.map(r=>r.symbol),['NASDAQ:B']);
 assert.equal(shortGrid.focusSymbols[0],'NASDAQ:B');
 assert(!shortGrid.focusSymbols.includes('NASDAQ:L'));
 assert.equal(changeView('mixed').focusSymbols[3],'NASDAQ:B');
 assert.equal(MarketRhythm.data().rankingView,'mixed');
});

test('the ranking may open a card before the trading window while actual entry timing remains separate',()=>{
 const {rhythmEntryWindow,MarketRhythm}=require('../console/static/market-rhythm');
 const time=s=>Date.parse(s)/1000;
 for(const day of ['2026-10-07','2026-11-18']){
  const offset=day.includes('10-07')?'-04:00':'-05:00';
  assert.equal(rhythmEntryWindow(time(day+'T09:44:59'+offset)),false);
  assert.equal(rhythmEntryWindow(time(day+'T09:45:00'+offset)),true);
  assert.equal(rhythmEntryWindow(time(day+'T16:00:00'+offset)),false);
 }
 const events=[],ui={entryWindowOpen:false,$emit:(...args)=>events.push(args),manageRank:r=>events.push(['manage',r.symbol])};
 MarketRhythm.methods.openRank.call(ui,{symbol:'NASDAQ:A',direction_sign:1});assert.equal(events.length,2);
 assert.equal(events[1][0],'open');
 MarketRhythm.methods.openRank.call(ui,{symbol:'NASDAQ:HELD',isHeld:true});assert.deepEqual(events.at(-1),['manage','NASDAQ:HELD']);
});

test('ranking entry opens a card regardless of TD sign, missing TD or direction readiness',()=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const frame={rankings:{mixed:[{symbol:'NASDAQ:A',rank:1,direction_sign:1}]}},events=[];
 const ui={...MarketRhythm.methods,frame,rankingView:'mixed',positions:[],rows:[],entryWindowOpen:false,
  liveDirection:()=>({known:true,ready:false,direction:1,reason:'freshness is checked in the card'}),
  tdTitle:()=>'', $emit:(...args)=>events.push(args)};
 for(const td of [null,-4,0,4]){
  ui.liveTd=()=>({count:td,ready:false});
  const row=MarketRhythm.computed.rankingRows.call(ui)[0];assert.equal(row.entryDirection,1);
  MarketRhythm.methods.openRank.call(ui,row);assert.equal(events.at(-1)[0],'open');
 }
 ui.liveDirection=()=>({known:false,ready:false,direction:0});
 const row=MarketRhythm.computed.rankingRows.call(ui)[0];assert.equal(row.entryDirection,0);
 MarketRhythm.methods.openRank.call(ui,row);assert.equal(events.at(-1)[0],'open');
 const button=MarketRhythm.template.match(/<button[^>]*class="rhythm-open-order"[\s\S]*?<\/button>/)[0];
 assert(!button.includes(':disabled'));assert(!button.includes('待确认'));
 assert(button.includes('TD仅供参考'));
});

test('new client refuses an old scoring model instead of presenting its rows as the new ranking',async t=>{
 const {MarketRhythm}=require('../console/static/market-rhythm');
 const original=global.fetch;t.after(()=>{global.fetch=original;});
 global.fetch=async()=>({ok:true,json:async()=>({status:'ready',scoring:{model:'vwap_open_ema5_v6',version:6}})});
 const ui={...MarketRhythm.data(),...MarketRhythm.methods,active:true,requestId:0,positions:[],$emit(){}};
 await ui.refresh();assert.equal(ui.frame,null);assert.match(ui.error,/重启控制台/);
 ui.displayState={candidateRows:[{symbol:'NASDAQ:OLD'}]};ui.rows=[];ui.liveTd=()=>({count:null});
 assert.deepEqual(MarketRhythm.computed.rankingRows.call(ui),[]);
});
