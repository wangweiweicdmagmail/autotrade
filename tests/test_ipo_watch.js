'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const {tradeCandidate,startIpoWatch}=require('../service/ipo_watch');
const NOW=Date.parse('2026-11-19T15:12:17Z')/1000;
const symbol='NASDAQ:ANT',date='2026-11-19';
const quote={lp:101.25,lp_time:NOW,volume:12000,pro_name:symbol};

test('an empty IPO watchlist creates no quote, chart or retry timer',()=>{
 const client={get Session(){assert.fail('disabled IPO monitoring must not subscribe');}};
 const watch=startIpoWatch(client,{watches:{},emit:()=>assert.fail('disabled monitor emitted'),
   setTimer:()=>assert.fail('disabled monitor scheduled a retry')});
 watch.stop();
});

test('IPO indicative prices, bid/ask, missing trade time or zero volume cannot announce trading',()=>{
 for(const row of [{lp:83},{bid:100,ask:101},{...quote,volume:0},{...quote,lp_time:undefined},{...quote,lp:NaN}])
  assert.equal(tradeCandidate(symbol,date,row,'quote',NOW),null);
 const event=tradeCandidate(symbol,date,quote,'quote',NOW);assert.equal(event.price,101.25);assert.equal(event.time_kind,'trade_time');
});

test('only current listing-day regular-session trades count; stale, future and wrong-provider values fail',()=>{
 assert.equal(tradeCandidate(symbol,date,{...quote,lp_time:NOW-91},'quote',NOW),null);
 assert.equal(tradeCandidate(symbol,date,{...quote,lp_time:NOW+1},'quote',NOW),null);
 assert.equal(tradeCandidate(symbol,date,quote,'quote',Date.parse('2026-11-19T14:29:00Z')/1000),null);
 assert.equal(tradeCandidate(symbol,date,quote,'quote',NOW+86400),null);
 assert.equal(tradeCandidate(symbol,date,{...quote,pro_name:'NYSE:OTHER'},'quote',NOW),null);
});

function fixture(){const out=[],charts=[],markets=[],timers=[];let quoteOptions;
 class Quote{constructor(o){quoteOptions=o;this.Market=class{constructor(s,session){this.symbol=s;this.session=session;markets.push(this);}onData(fn){this.data=fn;}onError(fn){this.error=fn;}close(){this.closed=true;}};}delete(){}}
 class Chart{constructor(){charts.push(this);this.periods=[];this.infos={subsession_id:'regular'};}onUpdate(fn){this.update=fn;}onError(fn){this.error=fn;}setMarket(s,o){this.symbol=s;this.options=o;}delete(){this.closed=true;}}
 const watch=startIpoWatch({Session:{Quote,Chart}},{watches:{[symbol]:date},emit:e=>out.push(e),now:()=>NOW,setTimer:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimer:()=>{}});
 return{out,charts,markets,timers,watch,quoteOptions};}

test('a single still-forming first minute triggers immediately, without an indicator or closed bar',()=>{
 const f=fixture();assert(f.quoteOptions.customFields.includes('lp_time'));
 assert.deepEqual(f.charts[0].options,{timeframe:'1',range:1,session:'regular'});
 f.markets[0].data({lp:83});assert.equal(f.out.filter(e=>e.type==='ipo_trade').length,0);
 f.charts[0].periods=[{time:NOW-17,open:100,max:102,min:100,close:101.25,volume:500}];f.charts[0].update(['$prices']);
 assert.equal(f.out.filter(e=>e.type==='ipo_trade').length,1);
 assert.equal(f.out.find(e=>e.type==='ipo_trade').time_kind,'bar_open');
 f.markets[0].data(quote);assert.equal(f.out.filter(e=>e.type==='ipo_trade').length,1);
 assert(f.markets[0].closed&&f.charts[0].closed);f.watch.stop();
});

test('quote trade wins once, invalid sessions cannot use a fallback bar, no-data retry is independent',()=>{
 const f=fixture();f.charts[0].infos.subsession_id='extended';
 f.charts[0].periods=[{time:NOW-17,open:100,max:102,min:100,close:101,volume:500}];f.charts[0].update(['$prices']);
 assert.equal(f.out.filter(e=>e.type==='ipo_trade').length,0);
 f.charts[0].error('no_data');assert.equal(f.timers.at(-1).ms,30000);
 f.markets[0].data(quote);f.markets[0].data({...quote,lp:102});
 assert.equal(f.out.filter(e=>e.type==='ipo_trade').length,1);f.watch.stop();
});

test('a silent pre-listing fallback chart is retried without a permanent give-up ceiling',()=>{
 const f=fixture();assert.equal(f.timers[0].ms,45000);
 f.timers[0].fn();assert(f.charts[0].closed);assert.equal(f.charts.length,2);
 f.watch.stop();f.timers.at(-1).fn();assert.equal(f.charts.length,2);
});
