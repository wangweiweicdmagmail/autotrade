'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {rhythmTradePreview,rhythmTdFact,rhythmVwapFact}=require('../console/static/market-rhythm');
const now=Date.parse('2026-11-18T15:15:05Z')/1000;
const minute=now-5;
function tdFact(count,at=minute){return {version:1,source:'confirmed_minute_v1',id:'fact-'+at+'-'+count,known:Number.isSafeInteger(count),ready:Number.isSafeInteger(count),bar_time:at-60,confirmed_at:at,quote_bar_time:at,observed_at:at+1,count};}
function directionFact(direction=1,vwap=direction===1?99:101){return {version:1,source:'confirmed_vwap_direction_v1',id:'vwap-'+minute,known:true,ready:true,bar_time:minute-60,confirmed_at:minute,quote_bar_time:minute,observed_at:now,close:100,vwap,direction};}
function neutralBeta(td=1){return {data_contract_version:4,entry_policy:'stock_vwap_side_v1',direction_source:'confirmed_vwap_direction_v1',td_required:false,ema9_required:false,vwap_direction_required:true,td_source:'confirmed_minute_v1',evaluated_at:now,ranking_snapshot:{id:'test-minute'},beta_filter_required:false,
 beta_filter:{td_source:'confirmed_minute_v1',version:1,rule:'both_indices_absolute',ready:true,as_of:minute,snapshot_id:'test-minute',direction:0,
 absolute_ready:true,absolute_long_allowed:true,absolute_short_allowed:true,
 absolute_long_reason:'',absolute_short_reason:'',td_required:true,td_rule:'any_index_same_sign',td_ready:true,
 td_long_allowed:td>0,td_short_allowed:td<0,td_long_reason:td>0?'':'做多要求至少一个指数TD > 0',td_short_reason:td<0?'':'做空要求至少一个指数TD < 0',
 long_allowed:td>0,short_allowed:td<0,long_reason:td>0?'':'做多要求至少一个指数TD > 0',short_reason:td<0?'':'做空要求至少一个指数TD < 0',rows:[
 {symbol:'CAPITALCOM:NAS100',ready:true,direction:0,price:100,upper:102,lower:98,ema9:100,td_ready:true,td_count:td,td_fact:tdFact(td)},
 {symbol:'CAPITALCOM:SPX500',ready:true,direction:0,price:100,upper:102,lower:98,ema9:100,td_ready:true,td_count:td,td_fact:tdFact(td)}]}};}
function setup(storage={}){
  let options;const requests=[],charts=[];
  class Clock extends Date{constructor(...args){super(...(args.length?args:[now*1000]));}static now(){return now*1000;}}
  vm.runInNewContext(fs.readFileSync(require.resolve('../console/static/app.js'),'utf8'),{
    Vue:{createApp(o){options=o;return{use(){return this;},mount(){}};}},ElementPlus:{},
    localStorage:{getItem(k){return storage[k]??null;},setItem(k,v){storage[k]=v;}},Date:Clock,rhythmTradePreview,rhythmTdFact,rhythmVwapFact,
    fetch:async(url,r)=>{const body=JSON.parse(r.body);requests.push({url,body});return{ok:true,json:async()=>url==='/api/settings'?{candidate_order_protection:body.candidate_order_protection,intelligent_exit:body.intelligent_exit}:{lot:{}}};},
  });
  const symbol='NASDAQ:NVDA';
  const ui={...options.data(),...options.methods,equity:100000,riskUsd:1000,
    market:{[symbol]:{bar:{time:minute,close:100,low:99,high:101},ts_c:{atr:2},ts:{atr:50,ema_9:99},price_observed_at:now,
      market_1m:{bar_c:{time:minute-60}},candidate_st_1m_c:{version:1,time:minute-60,price:98,direction:1,atr_period:10,multiplier:3,interval_seconds:60,session:'regular'}}},
    status:{candidate_beta_filter_version:1,intelligent_exit_version:1,candidate_protection_live_version:1,candidate_order_policy_version:1,max_order_qty:100000,manual_stop_modes:['atr_2','vwap_sigma'],connected:true,market_running:true,market_stale:false,total_risk_usd:0,long_risk_usd:0,short_risk_usd:0,max_total_risk_usd:2200,max_direction_risk_usd:1100},lots:[],tvShow:s=>charts.push(s),
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
 ui.entryCandidates={...neutralBeta(),ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,side:'long',direction:1,close:100,direction_fact:directionFact(),vwap:99,sigma:2,rank:1,td_count:3,td_fact:tdFact(3),td_ready:true,bar_time:minute-60,confirmed_at:minute}]};
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
 ui.entryCandidates={...neutralBeta(),ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,side:'long',direction:1,close:100,direction_fact:directionFact(),vwap:99,sigma:2,td_count:3,td_fact:tdFact(3),td_ready:true,bar_time:minute-60,confirmed_at:minute}]};ui.slots[0].symbol=symbol;
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
 ui.entryCandidates={...neutralBeta(direction),ready:true,vwap_direction_required:true,td_threshold:1,ema9_period:9,ema9_interval_seconds:300,as_of:minute,rows:[{symbol,rank:1,side:direction===1?'long':'short',direction,close:100,direction_fact:directionFact(direction),vwap:direction===1?99:101,sigma:2,
   td_count:td,td_fact:tdFact(td),td_ready:td!=null,bar_time:minute-60,confirmed_at:minute}]};
}

test('retired TD board cannot open a card or reuse saved preferences',()=>{
 const {ui,symbol,frame}=setup();ui.deckCollapsed=true;
 ui.openRhythmTrade({symbol,frame,ranking:'td'});
 assert.equal(ui.deckCollapsed,true);assert.equal(ui.slots[0].candidateRanking,'trend');
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

test('TD counts do not gate either direction or either stop mode',()=>{
 for(const [direction,td,allowed] of [[1,0,true],[1,1,true],[1,2,true],[1,-1,true],[-1,0,true],[-1,-1,true],[-1,-2,true],[-1,1,true],[1,null,true]]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,td,direction);
  for(const mode of ['atr_2','vwap_sigma']){
   ui.slots[0].stopMode=mode;const row=ui.calcSlot(ui.slots[0]);assert.equal(row.ready,allowed);
   if(!allowed){assert.equal(row.qty,0);assert.match(row.blockReason,/TD/);}
  }
 }
});

test('VWAP direction change while confirming cancels submission; old contract is blocked',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.confirmSlowMarketEntry=async()=>{ui.entryCandidates.rows[0].direction_fact.vwap=101;return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,0);
 ui.entryCandidates.data_contract_version=3;
 assert(!ui.calcSlot(ui.slots[0]).ready);
});
test('track market sizing respects the configured 140 percent cap on both sides',()=>{
 for(const direction of [1,-1]){
  const {ui,symbol}=setup();productionAdmission(ui,symbol,3*direction,direction);
  ui.market[symbol].ts_c.atr=.05;ui.market[symbol].bar.low=99.95;ui.market[symbol].bar.high=100.05;
  const row=ui.calcSlot(ui.slots[0]);assert(row.ready);assert.equal(row.qty,1400);
  assert.equal(row.amount,140000);assert.equal(row.equityRatio,140);
 }
});



test('budget excess blocks entry, while an existing same-symbol batch permits a separate addition',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 const row=ui.calcSlot(ui.slots[0]);assert(ui.manualEntryChecks(row).every(c=>c.ok));
 ui.status.long_risk_usd=200;
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='risk').ok);
 await ui.placeOrder(row);assert.equal(requests.length,0);
 ui.status.long_risk_usd=0;ui.lots=[{symbol,state:'PENDING',filled_qty:0}];
 assert(ui.manualEntryChecks(row).find(c=>c.key==='position').ok);
 await ui.placeOrder(row);assert.equal(requests.length,1);
});

test('the order card uses server-wide direction slots even when risk is zero and local lots are filtered',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.status.max_direction_positions=1;
 ui.status.direction_position_counts={long:1,short:0};
 const row=ui.calcSlot(ui.slots[0]);
 assert.equal(ui.lots.length,0);assert.equal(ui.status.long_risk_usd,0);
 const check=ui.manualEntryChecks(row).find(c=>c.key==='direction_position');
 assert(!check.ok);assert.match(check.detail,/1\/1.*保本不释放/);
 await ui.placeOrder(row);assert.equal(requests.length,0);
 ui.status.direction_position_counts.long=0;
 assert(ui.manualEntryChecks(row).find(c=>c.key==='direction_position').ok);
 delete ui.status.direction_position_counts;
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='direction_position').ok);
});

test('with the direction count limit disabled, the order card has no slot blocker and can submit',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.status.max_direction_positions=0;
 ui.status.direction_position_counts={long:3,short:2};
 const row=ui.calcSlot(ui.slots[0]);
 assert(!ui.manualEntryChecks(row).some(c=>c.key==='direction_position'));
 delete ui.status.direction_position_counts;
 assert(ui.manualEntryChecks(row).every(c=>c.ok));
 await ui.placeOrder(row);assert.equal(requests.length,1);
});

test('only actual opposite unfinished orders block entry; TD and closed history do not block additions',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.status.single_direction_only=true;ui.status.max_direction_positions=0;
 ui.status.max_direction_risk_usd=2200;ui.status.long_risk_usd=1000;ui.status.total_risk_usd=1000;
 ui.status.direction_position_counts={long:2,short:1};
 ui.lots=[{symbol,state:'ACTIVE',side:'long',filled_qty:10,trader:'auto',signal_snapshot:{strategy_id:'fixture'}}];
 const row=ui.calcSlot(ui.slots[0]);
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='direction_consistency').ok);
 await ui.placeOrder(row);assert.equal(requests.length,0);
 ui.status.direction_position_counts.short=0;
 ui.lots.push({symbol:'NASDAQ:HISTORY',state:'CLOSED',side:'short',filled_qty:100});
 assert(ui.manualEntryChecks(row).every(c=>c.ok));
 await ui.placeOrder(row);assert.equal(requests.length,1);
 delete ui.status.direction_position_counts;
 assert(!ui.manualEntryChecks(row).find(c=>c.key==='direction_consistency').ok);
});





test('intelligent exit master switch is saved separately and does not open an entry card',async()=>{
 const {ui,requests}=setup();ui.deckCollapsed=true;
 assert.equal(ui.intelligentExit.enabled,false);
 await ui.saveIntelligentExit('enabled',true);
 assert.equal(ui.intelligentExit.enabled,true);assert.equal(ui.deckCollapsed,true);
 assert.deepEqual(Object.keys(requests[0].body),['intelligent_exit']);
 assert.equal(requests[0].body.intelligent_exit.breakeven_minutes,5);
 await ui.saveIntelligentExit('enabled',false);assert.equal(ui.intelligentExit.enabled,false);
 assert.equal(requests.length,2);assert(requests.every(r=>r.url==='/api/settings'));
});

test('an older server cannot accept the new intelligent exit switch',async()=>{
 const {ui,requests}=setup();delete ui.status.intelligent_exit_version;
 await ui.saveIntelligentExit('enabled',true);
 assert.equal(requests.length,0);assert.equal(ui.intelligentExit.enabled,false);
});


test('trend entry ignores TD, indices, EMA9 and ST but requires close and VWAP',async()=>{
 for(const sign of [1,-1]){
  const {ui,symbol,requests}=setup();productionAdmission(ui,symbol,-sign,sign);
  delete ui.entryCandidates.beta_filter;delete ui.market[symbol].ts.ema_9;delete ui.market[symbol].candidate_st_1m_c;
  ui.candidateProtection.trailing_enabled=true;ui.candidateProtection.trailing_source='st_1m_10_3';
  const row=ui.calcSlot(ui.slots[0]);assert(row.ready,row.blockReason);
  const checks=ui.manualEntryChecks(row);assert(checks.every(c=>c.ok));
  assert(!checks.some(c=>['td','beta','beta_td','ema9','trailing_st'].includes(c.key)));
  await ui.placeOrder(row);assert.equal(requests.length,1);
  assert.equal(requests[0].body.side,sign===1?'long':'short');
  ui.entryCandidates.rows[0].vwap=null;assert(!ui.calcSlot(ui.slots[0]).ready);
 }
});
test('the checklist retains quote, risk, stop and account checks while allowing same-symbol additions',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol);
 let row=ui.calcSlot(ui.slots[0]);assert(ui.manualEntryChecks(row).every(c=>c.ok));
 ui.status.total_risk_usd=2100;ui.status.halted=true;ui.lots=[{symbol,state:'PENDING'}];
 const checks=ui.manualEntryChecks(row);
 for(const key of ['risk','account'])assert.equal(checks.find(c=>c.key===key).ok,false);
 assert(checks.find(c=>c.key==='position').ok);
 assert(checks.find(c=>c.key==='vwap_direction').ok);
 assert(checks.find(c=>c.key==='stop').ok);
 const html=fs.readFileSync(require.resolve('../console/static/index.html'),'utf8');
 assert(html.includes("check.ok ? '✓' : '✗'"));
});

test('EMA9 and index changes do not block an unchanged VWAP side',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.confirmSlowMarketEntry=async()=>{ui.market[symbol].ts.ema_9=110;ui.entryCandidates.beta_filter={ready:false};return true;};
 await ui.placeOrder(ui.calcSlot(ui.slots[0]));assert.equal(requests.length,1);
});
test('simple entry still rejects wrong contract, stale stock TD, stale quote, zero and changed source',async()=>{
 for(const fault of ['contract','policy','flags','td_stale','quote','zero','source']){
  const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
  const slot=ui.calcSlot(ui.slots[0]);assert(slot.ready);
  ui.confirmSlowMarketEntry=async()=>{
   const snapshot=ui.entryCandidates,row=snapshot.rows[0];
   if(fault==='contract')snapshot.data_contract_version=2;
   else if(fault==='policy')delete snapshot.entry_policy;
   else if(fault==='flags')snapshot.beta_filter_required=true;
   else if(fault==='td_stale')row.direction_fact.observed_at=now-46;
   else if(fault==='quote')ui.market[symbol].price_observed_at=now-46;
   else if(fault==='zero'){row.direction_fact.vwap=row.vwap=100;row.direction_fact.direction=row.direction=0;}
   else ui.slots[0].candidateRanking='td';
   return true;
  };
  await ui.placeOrder(slot);assert.equal(requests.length,0,fault);
 }
});

test('unrelated aggregate stale state does not override fresh selected-stock evidence',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol);
 ui.status.market_stale=true;
 const row=ui.calcSlot(ui.slots[0]);assert(row.ready);
 assert(ui.manualEntryChecks(row).find(c=>c.key==='data').ok);
 ui.market[symbol].price_observed_at=now-46;
 assert(!ui.calcSlot(ui.slots[0]).ready);
});

test('ATR stop does not require sigma while choosing sigma does',()=>{
 const {ui,symbol}=setup();productionAdmission(ui,symbol);
 ui.entryCandidates.rows[0].sigma=null;
 assert(ui.calcSlot(ui.slots[0]).ready);
 ui.slots[0].stopMode='vwap_sigma';assert(!ui.calcSlot(ui.slots[0]).ready);
 ui.entryCandidates.rows[0].sigma=2;assert(ui.calcSlot(ui.slots[0]).ready);
});

test('VWAP limit choices mirror long and short and submit LMT intent with fixed level',async()=>{
 for(const sign of [1,-1])for(const level of ['vwap',sign===1?'upper':'lower']){
  const {ui,symbol,requests}=setup();productionAdmission(ui,symbol,-sign,sign);
  ui.setCandidateOrderType(0,'LMT');ui.slots[0].limitLevel=level;
  const row=ui.calcSlot(ui.slots[0]);assert(row.ready,row.blockReason);
  const price=level==='vwap'?(sign===1?99:101):(sign===1?101:99);
  assert.equal(row.limitPx,price);assert.equal(row.qty,Math.floor(1000/Math.abs(price-row.stopPx)));
  await ui.placeOrder(row);assert.equal(requests.length,1);
  assert.equal(requests[0].body.limit_type,'vwap_limit');assert.equal(requests[0].body.vwap_limit_level,level);
  assert.equal(requests[0].body.limit_px,price);
 }
});

test('wrong-side level, missing sigma, or changing order type during confirmation never sends',async()=>{
 const {ui,symbol,requests}=setup();productionAdmission(ui,symbol);
 ui.setCandidateOrderType(0,'LMT');ui.slots[0].limitLevel='lower';
 assert(!ui.calcSlot(ui.slots[0]).ready);
 ui.slots[0].limitLevel='upper';ui.entryCandidates.rows[0].sigma=null;
 assert(!ui.calcSlot(ui.slots[0]).ready);
 ui.entryCandidates.rows[0].sigma=2;const row=ui.calcSlot(ui.slots[0]);assert(row.ready);
 ui.confirmSlowMarketEntry=async()=>{ui.setCandidateOrderType(0,'MKT');return true;};
 await ui.placeOrder(row);assert.equal(requests.length,0);
});

test('manual side-view orders retain their source and use VWAP even when ranking direction is opposite',async()=>{
 const {ui,symbol,requests}=setup();ui.status.candidate_entry_required=true;
 const row={symbol,side:'short',direction:-1,ranking_direction:1,rank:7,close:100,vwap:101,sigma:2,direction_fact:directionFact(-1,101),
  td_fact:tdFact(1),bar_time:minute-60,confirmed_at:minute};
 ui.entryCandidates={...neutralBeta(),ready:true,rows:[]};
 ui.entryCandidateViews={long:{...neutralBeta(),ready:true,ranking_view:'long',rows:[row]}};
 ui.openRhythmTrade({symbol,frame:{},view:'long'});
 assert.equal(ui.slots[0].candidateView,'long');
 const preview=ui.calcSlot(ui.slots[0]);assert(preview.ready,preview.blockReason);assert.equal(preview.side,'short');
 assert.equal(preview.candidateView,'long');await ui.placeOrder(preview);
 assert.equal(requests.length,1);assert.equal(requests[0].body.candidate_view,'long');assert.equal(requests[0].body.side,'short');
});
