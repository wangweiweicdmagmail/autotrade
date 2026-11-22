'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {rhythmTradePreview}=require('../console/static/market-rhythm');
const now=Date.parse('2026-11-18T15:15:05Z')/1000;
const minute=now-5;
function setup(storage={}){
  let options;const requests=[],charts=[];
  class Clock extends Date{constructor(...args){super(...(args.length?args:[now*1000]));}static now(){return now*1000;}}
  vm.runInNewContext(fs.readFileSync(require.resolve('../console/static/app.js'),'utf8'),{
    Vue:{createApp(o){options=o;return{use(){return this;},mount(){}};}},ElementPlus:{},
    localStorage:{getItem(k){return storage[k]??null;},setItem(k,v){storage[k]=v;}},Date:Clock,rhythmTradePreview,
    fetch:async(url,r)=>{const body=JSON.parse(r.body);requests.push({url,body});return{ok:true,json:async()=>url==='/api/settings'?{candidate_order_protection:body.candidate_order_protection}:{lot:{}}};},
  });
  const symbol='NASDAQ:NVDA';
  const ui={...options.data(),...options.methods,equity:100000,riskUsd:1000,
    market:{[symbol]:{bar:{time:minute,close:100,low:99,high:101},ts_c:{atr:2},ts:{atr:50,ema_9:99},price_observed_at:now,
      market_1m:{bar_c:{time:minute-60}},candidate_st_1m_c:{version:1,time:minute-60,price:98,direction:1,atr_period:10,multiplier:3,interval_seconds:60,session:'regular'}}},
    status:{candidate_protection_live_version:1,candidate_order_policy_version:1,max_order_qty:100000,manual_stop_modes:['atr_2','vwap_sigma'],connected:true,market_running:true,market_stale:false,total_risk_usd:0,long_risk_usd:0,short_risk_usd:0,max_total_risk_usd:2200,max_direction_risk_usd:1100},lots:[],tvShow:s=>charts.push(s),
    $message:{success(){},warning(){},error(e){throw Error(e);}},tradeHeaders:()=>({}),checkWrite403(){},
    confirmSlowMarketEntry:async()=>true};
  // Existing tests isolate their original gates; the new filter has explicit coverage below.
  ui.slots[0].vwapOutsideOnly=false;
  const frame={tracks:[{symbol,aligned:true,points:[[minute-60,100,99,2,100,101,99,100,2,'outside_progress',1]]}]};
  return{ui,frame,symbol,requests,charts};
}

test('panel open previews a five-minute 2ATR stop and submits only after the manual action',async()=>{
  const {ui,frame,symbol,requests,charts}=setup();
  ui.deckCollapsed=true;
  ui.openRhythmTrade({symbol,frame});
  assert.equal(ui.deckCollapsed,false);
  assert.equal(ui.slots[0].limitType,'rhythm_market');
  assert.equal(ui.slots[0].symbol,symbol);
  assert.equal(requests.length,0);
  assert.deepEqual(charts,[]);
  const row=ui.calcSlot(ui.slots[0]);
  assert(row.ready);assert.equal(row.stopPx,96);assert.equal(row.qty,250);assert.equal(row.atr,2);
  await ui.placeOrder(row);
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,'/api/lots/limit-stop');
  assert.equal(requests[0].body.limit_type,'market');
  assert.equal(requests[0].body.entry_mode,'rhythm_atr');
  assert.equal(requests[0].body.side,'long');
  assert.equal(requests[0].body.stop_px,96);
  assert.match(requests[0].body.reason,/5分钟2×ATR/);
});

test('rhythm pricing follows updated five-minute ATR and ignores VWAP width, neutral plans cannot submit',async()=>{
  const {ui,frame,symbol,requests}=setup();
  ui.openRhythmTrade({symbol,frame});
  ui.market[symbol].bar.close=103;
  ui.market[symbol].bar.low=101;
  ui.market[symbol].bar.high=104;
  ui.market[symbol].ts_c.atr=1.5;
  frame.tracks[0].points[0][3]=300;
  const row=ui.calcSlot(ui.slots[0]);
  assert.equal(row.limitPx,103);assert.equal(row.stopPx,100);
  frame.tracks[0].points[0][10]=0;
  const neutral=ui.calcSlot(ui.slots[0]);assert.equal(neutral.ready,false);
  await ui.placeOrder(neutral);
  assert.equal(requests.length,0);
});

test('rhythm card uses the ordinary market stop outside the five-minute candle on both sides',()=>{
  const {ui,frame,symbol}=setup();ui.openRhythmTrade({symbol,frame});
  ui.market[symbol].bar.low=95;ui.market[symbol].bar.high=105;
  let row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.stopPx,94.99);assert.equal(row.qty,199);
  frame.tracks[0].points[0][10]=-1;
  row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.stopPx,105.01);assert.equal(row.qty,199);
  ui.market[symbol].ts_c.atr=null;
  assert.equal(ui.calcSlot(ui.slots[0]).ready,false);
});

test('manual entries take only candidate direction and can choose sigma without changing the ATR default',async()=>{
 const {ui,symbol,requests}=setup();ui.status.candidate_entry_required=true;
 ui.entryCandidates={ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,side:'long',direction:1,vwap:99,sigma:2,rank:1,td_count:3,td_ready:true,bar_time:minute-60,confirmed_at:minute}]};
 ui.slots[0].symbol=symbol;ui.slots[0].limitType='market';
 ui.market[symbol].ts.direction=-1; // ST cannot reverse the selected candidate trend.
 ui.market[symbol].bar.low=95;ui.market[symbol].bar.high=105;
 let row=ui.calcSlot(ui.slots[0]);assert.equal(row.side,'long');assert.equal(row.stopPx,94.99);
 ui.slots[0].stopMode='vwap_sigma';row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.stopPx,98);assert.equal(row.qty,500);
 await ui.placeOrder(row);assert.equal(requests[0].body.manual_stop_mode,'vwap_sigma');
 assert.equal(requests[0].body.market_direction_mode,'candidate');
 ui.entryCandidates.rows=[];assert.equal(ui.calcSlot(ui.slots[0]).ready,false);
});

test('candidate reversal during confirmation cancels a manual order',async()=>{
 const {ui,symbol,requests}=setup();ui.status.candidate_entry_required=true;
 ui.entryCandidates={ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,side:'long',direction:1,vwap:99,sigma:2,td_count:3,td_ready:true,bar_time:minute-60,confirmed_at:minute}]};ui.slots[0].symbol=symbol;
 ui.confirmSlowMarketEntry=async()=>{ui.entryCandidates.rows[0].side='short';return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,0);
});

test('an old server cannot silently accept the new sigma stop choice',()=>{
 const {ui,symbol}=setup();delete ui.status.manual_stop_modes;
 ui.slots[0].symbol=symbol;ui.slots[0].stopMode='vwap_sigma';
 const row=ui.calcSlot(ui.slots[0]);assert.equal(row.ready,false);assert.match(row.blockReason,/重启控制台/);
});

function productionAdmission(ui,symbol,td=1,direction=1){
 ui.market[symbol].candidate_st_1m_c.direction=direction;
 ui.market[symbol].ts.ema_9=direction===1?99:101;
 ui.status.candidate_entry_required=true;ui.slots[0].symbol=symbol;
 ui.entryCandidates={ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,rank:1,side:direction===1?'long':'short',direction,vwap:direction===1?99:101,sigma:2,
   td_count:td,td_ready:td!=null,bar_time:minute-60,confirmed_at:minute}]};
}

test('candidate card defaults, timer/trailing choices and directional VWAP filter are sent per order',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 const defaults=ui.candidateOrderOptions({});
 assert.equal(defaults.timed_breakeven_enabled,true);assert.equal(defaults.breakeven_minutes,5);
 assert.equal(defaults.vwap_outside_only,true);assert.equal(defaults.trailing_enabled,false);
 ui.slots[0].vwapOutsideOnly=true;ui.entryCandidates.rows[0].vwap=100;ui.entryCandidates.rows[0].sigma=2;
 let row=ui.calcSlot(ui.slots[0]);assert(!row.ready);assert.match(row.blockReason,/VWAP/);
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='vwap').ok);
 ui.market[symbol].bar.close=103;ui.market[symbol].bar.high=104;ui.candidateProtection.breakeven_minutes=7;
 ui.candidateProtection.trailing_enabled=true;ui.candidateProtection.trailing_source='st_1m_10_3';
 row=ui.calcSlot(ui.slots[0]);assert(row.ready);
 await ui.placeOrder(row);assert.equal(requests.length,1);
 assert.equal(requests[0].body.candidate_order.breakeven_minutes,7);
 assert.equal(requests[0].body.candidate_order.trailing_source,'st_1m_10_3');
 assert.equal(requests[0].body.candidate_order.trailing_enabled,true);
});

test('candidate filter mirrors short boundaries and can be explicitly disabled',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol,-1,-1);
 ui.market[symbol].bar.low=96;
 ui.slots[0].vwapOutsideOnly=true;ui.entryCandidates.rows[0].vwap=100;
 for(const [price,ready] of [[99,false],[98,false],[97,true]]){
  ui.market[symbol].bar.close=price;assert.equal(ui.calcSlot(ui.slots[0]).ready,ready);
 }
 ui.market[symbol].bar.close=99;ui.slots[0].vwapOutsideOnly=false;
 assert(ui.calcSlot(ui.slots[0]).ready);
});

test('policy changes while confirming and old backend versions cannot submit',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.confirmSlowMarketEntry=async()=>{ui.candidateProtection.breakeven_minutes=10;return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,0);
 delete ui.status.candidate_order_policy_version;
 assert(!ui.calcSlot(ui.slots[0]).ready);
 assert.match(ui.calcSlot(ui.slots[0]).blockReason,/重启控制台/);
});

test('top toolbar saves shared controls independently of opening the trade card',async()=>{
 const {ui,requests}=setup();ui.deckCollapsed=true;
 await ui.saveCandidateProtection('trailing_enabled',true);
 assert.equal(ui.deckCollapsed,true);assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/settings');
 assert.equal(ui.candidateProtection.trailing_enabled,true);
 assert.equal(ui.candidateOrderOptions({}).trailing_enabled,true);
 const html=fs.readFileSync(require.resolve('../console/static/index.html'),'utf8');
 const card=html.slice(html.indexOf('<div v-if="!deckCollapsed" class="deck">'),html.indexOf('<!-- 自动策略主工作区 -->'));
 assert(!card.includes('timedBreakevenEnabled'));assert(!card.includes('saveCandidateProtection'));
 assert(html.includes('#protection-controls'));
});

test('successful automatic breakeven speaks once and stays deduplicated after a page refresh',()=>{
 const storage={},first=setup(storage).ui,spoken=[];
 first.speak=s=>spoken.push(s);first.$notify=()=>{};
 const lot={lot_id:'historical-test',symbol:'NASDAQ:NVDA',breakeven_reached:false,
  signal_snapshot:{candidate_order_protection:{timed_breakeven_done:true,breakeven_trigger:'timed_breakeven',breakeven_minutes_applied:5}}};
 first.lots=[lot];first.checkBreakeven();assert.equal(spoken.length,0);
 lot.breakeven_reached=true;first.checkBreakeven();first.checkBreakeven();
 assert.equal(spoken.length,1);assert.match(spoken[0],/持仓5分钟，已自动保本/);
 const refreshed=setup(storage).ui;refreshed.speak=s=>spoken.push(s);refreshed.$notify=()=>{};refreshed.lots=[lot];
 refreshed.checkBreakeven();assert.equal(spoken.length,1);
});

test('underwater timed exit speaks after actual closure without claiming a cost-price fill',()=>{
 const {ui}=setup(),spoken=[];ui.speak=s=>spoken.push(s);ui.$notify=()=>{};
 const lot={lot_id:'timed-loss',symbol:'NASDAQ:NVDA',state:'CLOSING',close_reason:'candidate_timed_breakeven_stop',
  signal_snapshot:{candidate_order_protection:{breakeven_trigger:'timed_breakeven_exit',breakeven_minutes_applied:5}}};
 ui.lots=[lot];ui.checkBreakeven();assert.equal(spoken.length,0);
 lot.state='CLOSED';lot.exit_reason=lot.close_reason;ui.checkBreakeven();ui.checkBreakeven();
 assert.equal(spoken.length,1);assert.match(spoken[0],/持仓5分钟，已按保本规则平仓/);
 assert.match(ui.exitReasonLabel(lot.exit_reason),/定时保本退出/);
});

test('the default track market card works from live admission without opening the candidate tab first',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol);
 assert.equal(ui.slots[0].limitType,'rhythm_market');assert.equal(ui.rhythmFrame,null);
 let row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.tdCount,1);
 assert.equal(row.limitPx,100);assert.equal(row.stopPx,96);
});

test('manual TD gating includes thresholds on both sides, rejects unknown and opposite TD, and keeps both stop choices',()=>{
 for(const [direction,td,allowed] of [[1,0,false],[1,1,true],[1,2,true],[1,-1,false],[-1,0,false],[-1,-1,true],[-1,-2,true],[-1,1,false],[1,null,false]]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,td,direction);
  for(const mode of ['atr_2','vwap_sigma']){
   ui.slots[0].stopMode=mode;const row=ui.calcSlot(ui.slots[0]);assert.equal(row.ready,allowed);
   if(!allowed){assert.equal(row.qty,0);assert.match(row.blockReason,/TD/);}
  }
 }
});

test('a TD drop while confirming an order cancels submission and an old server remains blocked',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 let confirmations=0;ui.confirmSlowMarketEntry=async()=>{confirmations++;ui.entryCandidates.rows[0].td_count=0;return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(confirmations,1);assert.equal(requests.length,0);
 delete ui.entryCandidates.td_threshold;
 const row=ui.calcSlot(ui.slots[0]);assert(!row.ready);assert.match(row.blockReason,/重启控制台/);
});

test('track market sizing respects the configured 140 percent cap on both sides',()=>{
 for(const direction of [1,-1]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,3*direction,direction);
  ui.market[symbol].ts_c.atr=.05;ui.market[symbol].bar.low=99.95;ui.market[symbol].bar.high=100.05;
  const row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.qty,1400);
  assert.equal(row.amount,140000);assert.equal(row.equityRatio,140);
 }
});

test('EMA9 admission uses the existing five-minute nine-period value and mirrors both sides strictly',()=>{
 for(const [direction,ema,allowed] of [[1,99,true],[1,100,false],[1,101,false],[-1,101,true],[-1,100,false],[-1,99,false]]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,direction,direction);
  ui.market[symbol].ts.ema_9=ema;ui.market[symbol].e9={ema_9:999};
  const row=ui.calcSlot(ui.slots[0]);assert.equal(row.ready,allowed);
  const checks=ui.manualEntryChecks(row);assert.equal(checks.length,12);
  assert.equal(checks.find(c=>c.key==='ema9').ok,allowed);
  if(!allowed){assert.equal(row.qty,0);assert.match(row.blockReason,/EMA9/);}
 }
});

test('every card check stays visible and reports its own failure instead of hiding checks after the first',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol,0);
 ui.market[symbol].ts.ema_9=101;ui.status.halted=true;ui.status.total_risk_usd=2100;
 const checks=ui.manualEntryChecks(ui.calcSlot(ui.slots[0]));
 assert(checks.find(c=>c.key==='candidate').ok);
 assert(!checks.find(c=>c.key==='td').ok);assert(!checks.find(c=>c.key==='ema9').ok);
 assert(!checks.find(c=>c.key==='risk').ok);assert(!checks.find(c=>c.key==='account').ok);
 const html=fs.readFileSync(require.resolve('../console/static/index.html'),'utf8');
 assert(html.includes("check.ok ? '✓' : '✗'"));
 assert(html.includes('manualEntryChecks(slot).some(c => !c.ok)'));
});

test('all checks pass for a fresh same-side TD1 entry, while budget excess and duplicate batches each show a cross',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 const row=ui.calcSlot(ui.slots[0]);assert(ui.manualEntryChecks(row).every(c=>c.ok));
 ui.status.long_risk_usd=200;
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='risk').ok);
 await ui.placeOrder(row);assert.equal(requests.length,0);
 ui.status.long_risk_usd=0;ui.lots=[{symbol,state:'PENDING',filled_qty:0}];
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='position').ok);
 await ui.placeOrder(row);assert.equal(requests.length,0);
});

test('EMA9 crossing while the confirmation is open prevents the manual HTTP request',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.confirmSlowMarketEntry=async()=>{ui.market[symbol].ts.ema_9=101;return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,0);
});

test('the checklist cannot mark stale or absent EMA9 as a pass and does not borrow the strategy EMA7',()=>{
 for(const fault of ['missing','old','time','version']){
  const {ui,symbol}=setup();productionAdmission(ui,symbol);
  const data=ui.market[symbol];data.e9={ema_9:99};
  if(fault==='missing')delete data.ts.ema_9;
  if(fault==='old')data.price_observed_at=now-46;
  if(fault==='time')data.ts.time=minute-300;
  if(fault==='version')delete ui.entryCandidates.ema9_period;
  const row=ui.calcSlot(ui.slots[0]);assert(!row.ready);assert.equal(row.qty,0);
  assert(!ui.manualEntryChecks(row).find(c=>c.key==='ema9').ok);
 }
});

test('VWAP midline direction is mandatory with outer-band filtering disabled, and both references must agree',()=>{
 for(const [direction,vwap,allowed] of [[1,99,true],[1,100,false],[1,101,false],[-1,101,true],[-1,100,false],[-1,99,false]]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,direction,direction);
  ui.slots[0].vwapOutsideOnly=false;ui.entryCandidates.rows[0].vwap=vwap;
  const row=ui.calcSlot(ui.slots[0]);assert.equal(row.ready,allowed);
  assert.equal(ui.manualEntryChecks(row).find(c=>c.key==='vwap_direction').ok,allowed);
  if(!allowed)assert.match(row.blockReason,/VWAP中线/);
 }
});

test('VWAP crossing during confirmation and missing direction capability both prevent a manual request',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.confirmSlowMarketEntry=async()=>{ui.market[symbol].bar.close=98.5;return true;};
 ui.market[symbol].ts.ema_9=98; // Price still above EMA9 after crossing VWAP 99.
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,0);
 delete ui.entryCandidates.vwap_direction_required;
 assert(!ui.manualEntryChecks(ui.calcSlot(ui.slots[0])).find(c=>c.key==='vwap_direction').ok);
});
