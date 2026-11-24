'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {calculateVwap1m:sessionVwap,candidateProtectionSnapshot:sessionProtection,openingVwapSnapshot:sessionOpening}=require('../service/vwap_1m');
const {calculateSeries:sessionSeries,createMarketRhythm,minuteSubscriptionOptions}=require('../service/market_rhythm');
const {createCalculator,getDefinition}=require('../service/pinets_runtime');
const {emaFromPeriods}=require('../service/ema');
// Extended remains an explicit historical-input compatibility option. Production defaults are regular.
const calculateVwap1m=(bars,options={})=>sessionVwap(bars,{marketSession:'extended',...options});
const candidateProtectionSnapshot=(bars,now)=>sessionProtection(bars,now,{marketSession:'extended'});
const openingVwapSnapshot=(bars,now)=>sessionOpening(bars,now,{marketSession:'extended'});
const calculateSeries=(symbol,bars,now,daily=[])=>sessionSeries(symbol,bars,now,daily,{marketSession:'extended'});
const OPEN=Date.parse('2026-11-20T14:30:00Z')/1000;
const bar=(time,p,i=0)=>({time,open:p-.12,high:p+.4,low:p-.3,close:p,volume:100+i*3});
const minutes=(n=400)=>Array.from({length:n},(_,i)=>bar(OPEN-19800+i*60,100+i*.02+Math.sin(i/13),i));
const meta={timezone:'America/New_York',minmove:1,pricescale:100,session:'extended',type:'fund'};
const near=(a,b,tolerance=1e-6)=>assert(Math.abs(a-b)<tolerance,`${a} != ${b}`);

test('both production resolutions request regular hours, while index minute requests omit session',async()=>{
  for(const symbol of ['NASDAQ:QQQ','CAPITALCOM:NAS100']){
    const charts=[],timers=[];
    class Chart{constructor(){charts.push(this);}onUpdate(){}onError(){}setMarket(symbol,options){this.symbol=symbol;this.options=options;}}
    const client={Session:{Chart},onConnected(){},onDisconnected(){},onError(){}};
    const argv=['node','daemon','--symbols',symbol,'--rhythm-cache','/unused',
      '--default-session-symbols','CAPITALCOM:NAS100'];
    const process={argv,stdout:{write(){}},exit(c){throw Error(String(c));}};
    const file=path.join(__dirname,'../service/tv_cloud_daemon.js'),original=require('node:module').createRequire(file);
    const context=vm.createContext({process,__dirname:path.dirname(file),console:{error(){}},Date,clearTimeout(){},
      setTimeout(fn,delay){const t={fn,delay,unref(){}};timers.push(t);return t;},setInterval(){return{unref(){}};},
      require:name=>name==='fs'?{readFileSync:()=>JSON.stringify({sessionid:'test',sessionid_sign:'test'})}:
        name==='@mathieuc/tradingview'?{}:name==='./tv_market_proxy'?{createMarketClient:async()=>client}:
        name==='./pinets_live'?{BAR_LIMIT:601,candle:x=>x,createLocalIndicators:()=>({reset(){}})}:original(name)});
    vm.runInContext(fs.readFileSync(file,'utf8').replace('main().catch','globalThis.done=main().catch'),context);
    await context.done;timers.find(t=>t.fn.name==='subscribeMinute').fn();
    const five=charts[0].options,minute=charts[1].options;
    if(symbol==='NASDAQ:QQQ'){assert.equal(five.session,'regular');assert.equal(minute.session,'regular');assert.equal(minute.range,4000);}
    else{assert.equal(five.session,undefined);assert(!Object.hasOwn(minute,'session'));}
  }
  assert.equal(minuteSubscriptionOptions(false).session,'regular');
});

test('EMA7, standard session VWAP bands, position and seven-minute speed agree with the Pine original across the open',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../indicators/vwap-ema7-position-1m.pine'),'utf8');
  const calculate=createCalculator({key:'eth_display',source,model:{interval_seconds:60},plots:{
    ema:'EMA7_Close',vwap:'VWAP',upper:'VWAP_Upper',lower:'VWAP_Lower',position:'VWAP_Position',speed:'EMA7_Speed_7m'}},
    {version:require('../package.json').dependencies.pinets});
  const bars=minutes(400),rows=await calculate({symbol:'NASDAQ:QQQ',metadata:meta,bars});
  // PineTS ta.ema has a short initial seed transient; use the real premarket
  // prefix to warm it, then compare on both sides of the regular opening.
  for(const i of [60,329,330,331,345,399]){
    const r=calculateVwap1m(bars.slice(0,i+1));
    for(const k of ['ema','vwap','upper','lower','position','speed'])near(r[k],rows[i][k]);
  }
  const withPre=calculateVwap1m(bars.slice(0,331)),withoutPre=calculateVwap1m(bars.slice(330,331),{marketSession:'regular'});
  assert.notEqual(withPre.ema,withoutPre.ema);assert.notEqual(withPre.vwap,withoutPre.vwap);
  assert(Number.isFinite(withPre.speed));assert.equal(withoutPre.speed,null);
});

test('ST(10,3) consumes the real extended prefix but emits its confirmed protection reference only in RTH',async()=>{
  const source='//@version=6\nindicator("ST")\n[p,d]=ta.supertrend(3.0,10)\nplot(p,"Price")\nplot(-d,"Side")';
  const calc=createCalculator({key:'eth_st',source,model:{interval_seconds:60},plots:{price:'Price',direction:'Side'}},
    {version:require('../package.json').dependencies.pinets});
  const bars=minutes(350),rows=await calc({symbol:'NASDAQ:QQQ',metadata:meta,bars});
  assert.equal(candidateProtectionSnapshot(bars.slice(0,330),OPEN),null);
  for(const i of [330,331,345]){
    const r=candidateProtectionSnapshot(bars.slice(0,i+1),bars[i].time+65);
    assert(r);assert.equal(r.session,'extended');near(r.price,rows[i].price);assert.equal(r.direction,rows[i].direction);
  }
});

test('perception warms EMA3 and TD on premarket, keeps RTH points only and uses the same VWAP as the distribution',async()=>{
  const bars=minutes(350),now=bars.at(-1).time+60,row=calculateSeries('NASDAQ:QQQ',bars,now);
  assert.equal(row.issue,null);assert.equal(row.warmup,330);assert.equal(row.points.length,20);
  const source='//@version=6\nindicator("EMA and TD")\nvar int n=0\np=n[1]\nn:=close>close[4]?(nz(p)>0?nz(p)+1:1):close<close[4]?(nz(p)<0?nz(p)-1:-1):0\nplot(ta.ema(close,3),"EMA")\nplot(n,"TD")';
  const calc=createCalculator({key:'eth_ema_td',source,model:{interval_seconds:60},plots:{ema:'EMA',td:'TD'}},
    {version:require('../package.json').dependencies.pinets});
  const expected=await calc({symbol:'NASDAQ:QQQ',metadata:meta,bars});
  for(let i=0;i<row.points.length;i++){
    const p=row.points[i],same=calculateVwap1m(bars.slice(0,331+i));
    near(p[1],expected[330+i].ema);near(p[2],same.vwap);near(p[3],same.sigma);
    assert.equal(row.td.counts[i][1],expected[330+i].td);
  }
  const changed=[...bars,bar(OPEN+23400,1000)];
  assert.deepEqual(calculateSeries('NASDAQ:QQQ',changed,changed.at(-1).time+60).points,row.points);
});

test('sparse premarket is real warm-up, while missing session prefix and missing RTH minutes stay visible',()=>{
  const dense=minutes(350),sparse=dense.filter((_,i)=>i>=330||i%7===0);
  assert.equal(calculateSeries('NASDAQ:QQQ',sparse,OPEN+1200).issue,null);
  const truncated=dense.slice(100);
  assert.equal(calculateVwap1m(truncated).vwap,null);
  assert.equal(calculateSeries('NASDAQ:QQQ',truncated,OPEN+1200).issue,'missing_session_anchor');
  assert.equal(calculateSeries('NASDAQ:QQQ',dense.filter(b=>b.time!==OPEN+120),OPEN+1200).issue,'minute_gap');
});

test('a new day starts a premarket observation ranking and regular ranking still resets at 09:30',()=>{
  const prior=minutes(350),next=minutes(350).map(b=>bar(b.time+3*86400,b.close+20));
  const before=calculateSeries('NASDAQ:QQQ',prior,prior.at(-1).time+60);
  const overnight=calculateSeries('NASDAQ:QQQ',[...prior,...next.slice(0,100)],next[99].time+60);
  assert.equal(overnight.ranking_phase,'premarket');assert.notEqual(overnight.day,before.day);
  assert.equal(overnight.points[0][0],next[0].time);assert.equal(overnight.points.length,100);
  const first=calculateVwap1m([...prior,next[0]]);
  near(first.vwap,(next[0].high+next[0].low+next[0].close)/3);
  assert.notEqual(first.ema,next[0].close);assert.equal(first.speed,null);
  const opened=calculateSeries('NASDAQ:QQQ',[...prior,...next],next.at(-1).time+60);
  assert.equal(opened.issue,null);assert.equal(opened.points[0][0],OPEN+3*86400);
});

test('new extended-session history is invariant to future bars, and premarket cannot create an opening reservation',()=>{
  const bars=minutes(350),now=OPEN+600;
  const prefix=bars.filter(b=>b.time+60<=now),future=bars.map(b=>b.time+60>now?bar(b.time,2000):b);
  assert.deepEqual(calculateSeries('NASDAQ:QQQ',future,now),calculateSeries('NASDAQ:QQQ',prefix,now));
  assert.equal(openingVwapSnapshot(bars.slice(0,330),OPEN-5),null);
});

test('five-minute EMA9 is unchanged standard close EMA and shares the extended input contract',()=>{
  const periods=minutes(500).filter((_,i)=>i%5===0).reverse();
  let expected=periods.at(-1).close;
  for(const b of [...periods].reverse().slice(1))expected=.2*b.close+.8*expected;
  near(emaFromPeriods(periods,0,9),expected,1e-10);
});

test('read-only historical replay is explicit and leaves ordinary chart requests unchanged',async()=>{
 const {fetchChart}=require('../service/history_bars');
 for(const replay of [false,true]){
  let requested,callback;
  class Chart{
   constructor(){this.infos={subsession_id:'regular',timezone:'America/New_York',pricescale:100,minmov:1};this.periods=[{time:300,open:100,max:101,min:99,close:100,volume:100}];}
   onError(){}onUpdate(fn){callback=fn;}delete(){}
   setMarket(symbol,options){requested=options;callback(['$prices']);}
  }
  const result=await fetchChart({Session:{Chart}},'NASDAQ:A',{timeframe:'1',range:1,to:600,session:'regular',replay});
  assert.equal(requested.session,'regular');assert.equal(result.bars[0].time,300);
  assert.equal(requested[replay?'replay':'to'],600);
  assert.equal(requested[replay?'to':'replay'],undefined);
 }
});
