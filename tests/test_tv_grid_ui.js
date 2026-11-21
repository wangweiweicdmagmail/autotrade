'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){let options;const document={visibilityState:'visible',documentElement:{dataset:{}}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../console/static/app.js'),'utf8'),{
  Vue:{createApp(o){options=o;return{use(){return this;},mount(){}};}},ElementPlus:{},document,
  localStorage:{getItem(){return null;},setItem(){}},
 });
 return {batchRows:options.computed.sortedLots,markerSync:options.methods.syncTvEntryMarkers,ui:{...options.data(),...options.methods,identity:'ok',traderName:'alice',bottomTab:'rhythm',syncTvEntryMarkers(){}},document};
}
const symbols=['NASDAQ:S9','NASDAQ:S7','NASDAQ:S5','NASDAQ:S3','NASDAQ:S10','NASDAQ:S8','NASDAQ:S6','NASDAQ:S4'];
const success={ok:true,panes:Array.from({length:8},()=>({ok:true}))};

test('a queued return to the previous arrangement wins over a slower in-flight grid change',async()=>{
 const {ui}=setup();const requests=[],finish=[];
 ui.tvGridLastKey='candidates:'+symbols.join(',');
 ui.requestTvBridge=detail=>{requests.push(detail);return new Promise(resolve=>finish.push(resolve));};
 const b=[...symbols];b[5]='NYSE:UNH';
 const pending=ui.syncRhythmGrid({symbols:b});
 await ui.syncRhythmGrid({symbols});
 assert.equal(requests.length,1);
 finish[0](success);await pending;
 assert.equal(requests.length,2);assert.deepEqual(Array.from(requests[1].symbols),symbols);
 finish[1](success);await Promise.resolve();await Promise.resolve();
 assert.equal(ui.tvGridLastKey,'candidates:'+symbols.join(','));
 await ui.syncRhythmGrid({symbols});assert.equal(requests.length,2);
});

test('hidden or inactive four-panel pages never drive a chart',async()=>{
 const {ui,document}=setup();let calls=0;ui.requestTvBridge=async()=>{calls++;return success;};
 document.visibilityState='hidden';await ui.syncRhythmGrid({symbols});assert.equal(calls,0);
 document.visibilityState='visible';ui.bottomTab='orders';await ui.syncRhythmGrid({symbols});assert.equal(calls,0);
});

test('a stale six-pane response cannot confirm the new eight-pane layout',async()=>{
 const {ui}=setup();ui.requestTvBridge=async()=>({ok:true,panes:Array.from({length:6},()=>({ok:true}))});
 await ui.syncRhythmGrid({symbols});
 assert.equal(ui.tvGridLastKey,'');assert.match(ui.tvGridStatus,/八格尚未全部确认/);
});

test('global ticker clicks use the same single-cell route even in rhythm mode with another panel selected',async()=>{
 const {ui}=setup();const picked=[],switched=[];ui.$refs={rhythmPanel:{choose:s=>picked.push(s)}};
 ui.switchLocalTvChart=async symbol=>{switched.push(symbol);return{ok:true,shown:symbol,cell:5};};
 ui.$message={success(){},warning(){}};ui.syncTvEntryMarkers=()=>{};
 await ui.tvShow('NASDAQ:NVDA');assert.deepEqual(picked,[]);assert.deepEqual(switched,['NASDAQ:NVDA']);
 ui.selectRhythmStock('NASDAQ:AMD');assert.equal(ui.slots[0].symbol,'NASDAQ:AMD');
});

 test('both TV windows receive independent targets and one failure does not suppress the other',async()=>{
 const {ui}=setup(),calls=[],focusSymbols=['NASDAQ:S1','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S2','NASDAQ:QQQ','AMEX:SPY'];
 ui.requestTvBridge=async detail=>{calls.push(detail);return detail.layout==='candidates'?{ok:false,reason:'八格未打开'}:{ok:true,panes:Array.from({length:6},()=>({ok:true}))};};
 await ui.syncRhythmGrid({symbols,focusSymbols});
 assert.deepEqual(calls.map(c=>c.layout),['candidates','focus']);
 assert.deepEqual(calls[1].symbols,focusSymbols);
 assert.match(ui.tvGridStatus,/六格已确认/);assert.equal(ui.tvGridLastKey,'');
 const count=calls.length;await ui.syncRhythmGrid({symbols,focusSymbols});assert.equal(calls.length,count);
 await ui.syncRhythmGrid({symbols:[],focusSymbols,force:true});assert.equal(calls.length,count+1);
 });

test('flat positions clear both chart windows once, transient drawing failures retry with a bound',async()=>{
 const {ui,markerSync}=setup();ui.syncTvEntryMarkers=markerSync;
 const calls=[];let ok=true;
 ui.requestTvBridge=async detail=>{calls.push(detail);return ok?{ok:true,marked:0}:{ok:false,reason:'绘图加载中'};};
 await ui.syncTvEntryMarkers();assert.equal(calls.length,2);assert(calls.every(c=>c.entries.length===0));
 await ui.syncTvEntryMarkers();assert.equal(calls.length,2);
 ui.tvEntryMarkers=[{id:'open-position'}];ok=false;await ui.syncTvEntryMarkers();
 assert.equal(calls.length,4);assert.equal(ui.tvMarkersRetries,1);
 await ui.syncTvEntryMarkers();assert.equal(calls.length,4);
 for(let i=0;i<3;i++){ui.tvMarkersRetryAt=0;await ui.syncTvEntryMarkers();}
 assert.equal(ui.tvMarkersRetries,4);const count=calls.length;
 ui.tvMarkersRetryAt=0;await ui.syncTvEntryMarkers();assert.equal(calls.length,count);
 ok=true;await ui.syncTvEntryMarkers(true);assert.equal(ui.tvMarkersRetries,0);assert.equal(calls.length,count+2);
});

test('a partially failed switch invalidates old success and cold chart readiness retries are bounded',async()=>{
 const {ui}=setup(),calls=[];let ready=false;
 ui.tvGridLastKey='candidates:'+symbols.join(',');
 ui.requestTvBridge=async d=>{calls.push(d);return ready?success:{ok:false,reason:'图表尚未就绪'};};
 const changed=[...symbols];changed[0]='NYSE:UNH';
 await ui.syncRhythmGrid({symbols:changed});assert.equal(ui.tvGridLastKey,'');
 await ui.syncRhythmGrid({symbols:changed});assert.equal(calls.length,1);
 ui.tvGridRetryAt=0;await ui.syncRhythmGrid({symbols:changed});assert.equal(calls.length,2);
 ready=true;await ui.syncRhythmGrid({symbols});assert.equal(calls.length,3);
 assert.equal(ui.tvGridLastKey,'candidates:'+symbols.join(','));
});

test('position management shows all batches for stock aliases and leaves entry controls collapsed',()=>{
 const {ui,batchRows}=setup();ui.deckCollapsed=false;ui.orderKindFilter='pyramid';
 ui.lots=[{symbol:'NASDAQ:NVDA',state:'ACTIVE',lot_id:'a'},{symbol:'BATS:NVDA',state:'ACTIVE',lot_id:'b'},
   {symbol:'NASDAQ:AMD',state:'ACTIVE',lot_id:'c'}];
 ui.manageRhythmPosition('NASDAQ:NVDA');
 assert(ui.deckCollapsed);assert.equal(ui.orderKindFilter,'all');
 assert.deepEqual(batchRows.call(ui).map(r=>r.lot_id),['a','b']);
 assert.equal(ui.sameOrderSymbol('CAPITALCOM:NAS100','OTHER:NAS100'),false);
});

test('a local candidate replacement never sends a grid request to the unchanged six-pane window',async()=>{
 const {ui}=setup(),calls=[],focusSymbols=['NASDAQ:S1','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S2','NASDAQ:QQQ','AMEX:SPY'];
 ui.requestTvBridge=async detail=>{calls.push(detail);return{ok:true,panes:Array.from({length:detail.layout==='focus'?6:8},()=>({ok:true}))};};
 await ui.syncRhythmGrid({symbols,focusSymbols});assert.equal(calls.length,2);
 const next=[...symbols];next[5]='NYSE:UNH';
 await ui.syncRhythmGrid({symbols:next,focusSymbols});
 assert.equal(calls.length,3);assert.equal(calls[2].layout,'candidates');
 await ui.syncRhythmGrid({symbols:next,focusSymbols});assert.equal(calls.length,3);
 const newFocus=[...focusSymbols];newFocus[3]='NASDAQ:NEW';
 await ui.syncRhythmGrid({symbols:next,focusSymbols:newFocus});
 assert.equal(calls.length,4);assert.equal(calls[3].layout,'focus');
 await ui.syncRhythmGrid({symbols:next,focusSymbols:newFocus,force:true});assert.equal(calls.length,6);
});

test('a failed layout retries independently without reapplying the already confirmed window',async()=>{
 const {ui}=setup(),calls=[],focusSymbols=['NASDAQ:S1','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:S2','NASDAQ:QQQ','AMEX:SPY'];
 let ready=false;
 ui.requestTvBridge=async detail=>{calls.push(detail.layout);return detail.layout==='candidates'&&!ready?
   {ok:false,reason:'pending'}:{ok:true,panes:Array.from({length:detail.layout==='focus'?6:8},()=>({ok:true}))};};
 await ui.syncRhythmGrid({symbols,focusSymbols});assert.deepEqual(calls,['candidates','focus']);
 ready=true;ui.tvGridRetryAt=0;
 await ui.syncRhythmGrid({symbols,focusSymbols});assert.deepEqual(calls,['candidates','focus','candidates']);
 assert(ui.tvGridLastKey);assert.equal(ui.tvGridFailedKey,'');
});
