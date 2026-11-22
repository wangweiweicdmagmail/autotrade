'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {createCalculator}=require('../service/pinets_runtime');

const source=fs.readFileSync(path.join(__dirname,'../indicators/session-ema9-5m.pine'),'utf8');
const definition={key:'session_ema_preview',source,model:{interval_seconds:300},plots:{
  ema:'Session_EMA',bars:'Session_Bars',length:'Effective_Length',ready:'Session_Ready',
  line_a:'Session_EMA_Line_A',line_b:'Session_EMA_Line_B',first:'Session_First_Close',
  entry_signal:'Entry_Signal',direction:'Direction',entry_price:'Entry_Price',
  long_stop:'Long_Stop',short_stop:'Short_Stop',atr:'ATR',
}};
const calculate=createCalculator(definition,{version:require('../package.json').dependencies.pinets});
const OPEN=Date.parse('2026-11-20T14:30:00Z')/1000;
const metadata={timezone:'America/New_York',minmove:1,pricescale:100,type:'stock',session:'extended'};
const candle=(time,close)=>({time,open:close-.2,high:close+.3,low:close-.4,close,volume:100});
const bars=(start=OPEN,n=14)=>Array.from({length:n},(_,i)=>candle(start+i*300,100+i*.7+Math.sin(i)*2));
const dataset=rows=>({symbol:'NASDAQ:QQQ',metadata,bars:rows});
const finite=Number.isFinite;
function reference(closes,length){
  const alpha=2/(length+1);
  return closes.slice(1).reduce((previous,price)=>alpha*price+(1-alpha)*previous,closes[0]);
}
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-8,`${actual} != ${expected}`);

test('session EMA selects the independent fixed EMA1 through EMA9, then keeps EMA9',async()=>{
  const input=bars(),output=await calculate(dataset(input));
  for(let i=0;i<input.length;i++){
    const length=Math.min(i+1,9),row=output[i];
    assert.equal(row.bars,i+1);assert.equal(row.length,length);assert.equal(row.ready,1);
    near(row.ema,reference(input.slice(0,i+1).map(b=>b.close),length));
    assert.equal(row.entry_signal,0);assert.equal(row.direction,0);
    for(const key of ['entry_price','long_stop','short_stop','atr'])assert(!finite(row[key]));
  }
  assert.equal(output[0].ema,input[0].close);
  assert.notEqual(output[0].ema,input[0].open);
  const variablePeriod=input.reduce((previous,b,i)=>{const a=2/(Math.min(i+1,9)+1);return previous==null?b.close:a*b.close+(1-a)*previous;},null);
  assert(Math.abs(output.at(-1).ema-variablePeriod)>1e-5);
});

test('prior day, overnight and extended-hour prices never enter the current session EMA',async()=>{
  const today=bars(),tomorrow=bars(OPEN+3*86400,12).map(b=>candle(b.time,b.close+400));
  const mixed=[candle(OPEN-600,9999),candle(OPEN-300,8888),...today,
    candle(OPEN+23400,7777),candle(tomorrow[0].time-300,6666),...tomorrow];
  const output=await calculate(dataset(mixed));
  for(const session of [today,tomorrow]){
    const expected=await calculate(dataset(session));
    const actual=output.filter(r=>session.some(b=>b.time===r.time));
    for(let i=0;i<actual.length;i++){
      assert.equal(actual[i].bars,expected[i].bars);assert.equal(actual[i].length,expected[i].length);
      near(actual[i].ema,expected[i].ema);
    }
  }
  for(const time of [OPEN-600,OPEN-300,OPEN+23400,tomorrow[0].time-300]){
    const row=output.find(r=>r.time===time);assert.equal(row.ready,0);assert(!finite(row.ema));
  }
});

test('only real bars count, with neither synthetic prices nor a wall-clock-based period',async()=>{
  const input=bars().filter((_,i)=>![1,3,4,6].includes(i)),output=await calculate(dataset(input));
  for(let i=0;i<input.length;i++){
    assert.equal(output[i].bars,i+1);assert.equal(output[i].length,Math.min(i+1,9));
    near(output[i].ema,reference(input.slice(0,i+1).map(b=>b.close),Math.min(i+1,9)));
  }
});

test('a truncated session waits for a real opening bar; a later complete session initializes normally',async()=>{
  const partial=bars().slice(3),next=bars(OPEN+3*86400,3),output=await calculate(dataset([...partial,...next]));
  for(const row of output.slice(0,partial.length)){assert.equal(row.ready,0);assert(!finite(row.ema));}
  assert.equal(output[partial.length].bars,1);near(output[partial.length].ema,next[0].close);
});

test('DST changes preserve the 09:30 New York reset and no line bridges adjacent regular sessions',async()=>{
  const summer=bars(Date.parse('2026-10-30T13:30:00Z')/1000,3);
  const winter=bars(Date.parse('2026-11-02T14:30:00Z')/1000,3);
  const output=await calculate(dataset([...summer,...winter]));
  for(const i of [0,3]){assert.equal(output[i].bars,1);near(output[i].ema,i===0?summer[0].close:winter[0].close);}
  assert(finite(output[2].line_a)!==finite(output[3].line_a));
  assert(finite(output[2].line_b)!==finite(output[3].line_b));
});

test('same-bar revisions and rollover match a fresh replay without increasing the real-bar count',async()=>{
  for(const n of [1,2,5,9,10]){
    let input=bars(OPEN,n);const context=await calculate.open(dataset(input));
    for(const delta of [4,-3,1]){
      const revised=candle(input.at(-1).time,input.at(-1).close+delta);
      input=[...input.slice(0,-1),revised];
      const live=await context.replaceLast(revised,2),fresh=await calculate(dataset(input));
      assert.equal(live.at(-1).bars,n);assert.equal(live.at(-1).length,Math.min(n,9));
      near(live.at(-1).ema,fresh.at(-1).ema);
      if(n>1)near(live.at(-2).ema,fresh.at(-2).ema);
    }
    input=[...input,candle(input.at(-1).time+300,102.35)];
    const live=await context.advance(input,2),fresh=await calculate(dataset(input));
    assert.equal(live.at(-1).bars,n+1);near(live.at(-1).ema,fresh.at(-1).ema);
  }
});

test('a non-five-minute chart is rejected rather than silently changing the indicator meaning',async()=>{
  const oneMinute=createCalculator({...definition,model:{interval_seconds:60}},
    {version:require('../package.json').dependencies.pinets});
  await assert.rejects(oneMinute(dataset(bars())),/五分钟/);
});
