'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {createLocalIndicators}=require('../service/pinets_live');
const {createCalculator,getDefinition}=require('../service/pinets_runtime');
const {fetchChart}=require('../service/history_bars');
const version=require('../package-lock.json').packages['node_modules/pinets'].version;

test('production daemon never accesses indicator registration, including obsolete environment settings',async()=>{
 const charts=[],factories=[],output=[],errors=[];
 class Chart{constructor(){charts.push(this);}onUpdate(){}onError(){}setMarket(symbol,options){this.symbol=symbol;this.options=options;}get Study(){throw Error('cloud Study forbidden');}}
 const client={Session:{Chart},onConnected(){},onDisconnected(){},onError(){}};
 const fakeProcess={argv:['node','daemon','--symbols','NASDAQ:QQQ'],env:{AUTOTRADE_EMA7_SHADOW_LOG:'/ignored'},stdout:{write:s=>output.push(JSON.parse(s))},exit:c=>{fakeProcess.code=c;}};
 const source=fs.readFileSync(path.join(__dirname,'../service/tv_cloud_daemon.js'),'utf8');
 const originalRequire=require('node:module').createRequire(path.join(__dirname,'../service/tv_cloud_daemon.js'));
 const context=vm.createContext({process:fakeProcess,__dirname:path.join(__dirname,'../service'),console:{error:s=>errors.push(s)},
  setTimeout:()=>({unref(){}}),setInterval:()=>({unref(){}}),clearTimeout(){},Date,
  require:name=>name==='fs'?{readFileSync:()=>JSON.stringify({sessionid:'test',sessionid_sign:'test'})}:
   name==='@mathieuc/tradingview'?{get getIndicator(){throw Error('indicator metadata forbidden');}}:
   name==='./tv_market_proxy'?{createMarketClient:async()=>client}:
   name==='./pinets_live'?{BAR_LIMIT:601,candle:x=>x,createLocalIndicators:options=>{factories.push(options);return {reset(){}};}}:originalRequire(name)});
 vm.runInContext(source.replace('main().catch','globalThis.done=main().catch'),context);
 await context.done;
 assert.equal(fakeProcess.code,undefined);
 assert.equal(charts.length,1);
 assert.equal(charts[0].options.range,601);
 assert.equal(factories.length,2);
 assert.match(errors.at(-1),/0 个 TV 指标订阅.*PineTS/);
 fakeProcess.argv.push('--benchmark-study','USER;retired');
 const retired=vm.createContext({...context});
 vm.runInContext(source.replace('main().catch','globalThis.done=main().catch'),retired);
 await retired.done;
 assert.equal(fakeProcess.code,1);assert.equal(charts.length,1);
});

test('historical export uses local calculations and preserves per-bar strategy timestamps',async()=>{
 const {computeHistory}=require('../service/local_history');
 const symbol='NASDAQ:MSFT',last=Date.parse('2026-11-20T20:55:00Z')/1000;
 const bars=Array.from({length:420},(_,i)=>({time:last-(419-i)*300,open:100+i*.01,high:101+i*.01,low:99+i*.01,close:100.3+i*.01,volume:1000+i}));
 const daily=Array.from({length:25},(_,i)=>({time:last-(25-i)*86400,open:100,high:110,low:90,close:101,volume:10000}));
 const dataset={symbol,bars,daily,metadata:{timezone:'America/New_York',minmove:1,pricescale:100,session:'extended'}};
 const rows=await computeHistory(dataset);
 const expected=await createCalculator(getDefinition('e7v2'),{version})(dataset);
 assert.equal(rows.length,bars.length);
 assert.deepEqual(rows.at(-1).e7v2,expected.at(-1));
 for(const row of rows){assert.equal(row.ts.time,row.time);assert.equal(row.fvg.time,row.time);assert.equal(row.e9.time,row.time);}
});

test('bars-only history fetch waits for complete price data and always cleans up the chart',async()=>{
 let chart;
 class Chart{constructor(){chart=this;this.infos={subsession_id:'24h',timezone:'America/New_York',minmov:1,pricescale:100};}
  onUpdate(fn){this.update=fn;}onError(fn){this.error=fn;}setMarket(symbol,options){this.options=options;}
  delete(){this.deleted=true;}get Study(){throw Error('Study forbidden');}}
 const promise=fetchChart({Session:{Chart}},'NASDAQ:QQQ',{range:2,session:'24h'});
 chart.periods=[{time:300,open:1,max:2,min:1,close:2,volume:1}];chart.update(['$prices']);
 chart.periods.unshift({time:600,open:2,max:3,min:2,close:3,volume:2});chart.update(['$prices']);
 const data=await promise;
 assert.deepEqual(data.bars.map(x=>x.time),[300,600]);assert.equal(chart.deleted,true);
 const wrong=fetchChart({Session:{Chart}},'NASDAQ:QQQ',{range:1,session:'24h'});
 chart.infos.subsession_id='extended';chart.periods=[{time:300}];chart.update(['$prices']);
 await assert.rejects(wrong,/未确认/);assert.equal(chart.deleted,true);
});

test('paper worker computes only its selected model and matches shared PineTS realtime and confirmed values',async()=>{
 const symbol='NASDAQ:QQQ',last=Math.floor(Date.now()/300000)*300;
 const bars=Array.from({length:420},(_,i)=>({time:last-(419-i)*300,open:100+i*.02,high:101+i*.02,low:99+i*.02,close:100.5+i*.02,volume:100+i}));
 const metadata={timezone:'America/New_York',minmove:1,pricescale:100,session:'24h'};
 let finish;
 const done=new Promise(resolve=>{finish=resolve;});
 const errors=[];
 const pool=createLocalIndicators({workers:1,indicatorKeys:['e7v2'],historyTail:200,onResult:finish,onError:(...args)=>errors.push(args)});
 const raw={bar:bars.at(-1),bar_c:bars.at(-2)};
 const timer=setTimeout(()=>finish('timeout'),10000);
 try{
  pool.observe(symbol,[...bars].reverse(),{...metadata,minmov:1,subsession_id:'24h'},raw);
  assert.notEqual(await done,'timeout');
  const view=pool.snapshot(symbol,raw);
  const expected=(await createCalculator(getDefinition('e7v2'),{version})({symbol,bars,daily:[],metadata},{tail:200})).reverse();
  assert.deepEqual(Object.keys(view.outputs),['e7v2']);assert.deepEqual(errors,[]);
  assert.deepEqual(view.outputs.e7v2[0],expected[0]);assert.deepEqual(view.outputs.e7v2[1],expected[1]);
  assert.equal(view.outputs.e7v2.length,200);
 }finally{clearTimeout(timer);await pool.close();}
});

test('historical daily inputs cover the actual intraday date span',async()=>{
 const requests=[];
 class Chart{
  constructor(){this.infos={subsession_id:'regular',timezone:'America/New_York',minmov:1,pricescale:100};}
  onUpdate(fn){this.update=fn;}onError(){}delete(){}
  setMarket(symbol,options){requests.push(options);const last=1700000000;
   this.periods=Array.from({length:options.range},(_,i)=>({time:last-i*(options.timeframe==='D'?86400:1800),open:100,max:101,min:99,close:100,volume:1}));
   this.update(['$prices']);}
 }
 const data=await require('../service/history_bars').fetchDataset({Session:{Chart}},'NASDAQ:QQQ',{range:2000,session:'regular',to:1700000000});
 const span=Math.ceil((data.bars.at(-1).time-data.bars[0].time)/86400);
 assert.equal(requests.length,2);assert.ok(requests[1].range>=span+10);
 assert.equal(requests[1].to,1700000000);assert.equal(data.daily.length,requests[1].range);
});
