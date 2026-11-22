'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {createCalculator,getDefinition}=require('../service/pinets_runtime');
const OPEN=Date.parse('2026-11-20T14:30:00Z')/1000;
const metadata={timezone:'America/New_York',minmove:1,pricescale:100,session:'extended',type:'stock'};
const daily=Array.from({length:12},(_,i)=>({time:OPEN+(i-10)*86400,open:100,high:120,low:80,close:101,volume:1000}));
const candle=(time,p=100)=>({time,open:p,high:p+1,low:p-1,close:p+.2,volume:100});
function data(){return Array.from({length:60},(_,i)=>{
  const b=candle(OPEN+(i-30)*60,100+i*.1),minute=i-30;
  if(minute===-1){b.high=180;b.low=30;}
  if(minute===7)b.low=70;
  if(minute===14)b.high=150;
  if(minute>=15){b.high=300;b.low=10;}
  return b;
});}
function aggregate(rows,seconds){
  const groups=new Map();for(const b of rows){const t=Math.floor(b.time/seconds)*seconds;if(!groups.has(t))groups.set(t,[]);groups.get(t).push(b);}
  return [...groups].map(([time,bs])=>({time,open:bs[0].open,close:bs.at(-1).close,high:Math.max(...bs.map(b=>b.high)),low:Math.min(...bs.map(b=>b.low)),volume:bs.reduce((s,b)=>s+b.volume,0)}));
}
function calculator(seconds,visual=true){
  const base=getDefinition('attack'),source=fs.readFileSync(path.join(__dirname,'../indicators/attack-5m.pine'),'utf8');
  assert.equal(source,base.source);
  return createCalculator({...base,model:{interval_seconds:seconds},source:source+'\nplot(regularBarCount,"Test_Count")\nplot(opening3Locked?1:0,"Test_Locked")\nplot(opening3RangeJustCompleted?1:0,"Test_Completed")\nplot(firstRegularBar?1:0,"Test_First")',plots:{
    high:'开盘前三根K线高点',low:'开盘前三根K线低点',pre_high:'盘前高点',pre_low:'盘前低点',distance:'Opening3_Distance',
    count:'Test_Count',locked:'Test_Locked',completed:'Test_Completed',first:'Test_First'}},
    {version:require('../package.json').dependencies.pinets,inputs:{showLargeRange:visual,showSmallRange:visual,showOpening3Range:visual}});
}
const dataset=bars=>({symbol:'NASDAQ:TEST',metadata,bars,daily});

test('one-minute and five-minute attack ranges cover exactly the same opening fifteen minutes',async()=>{
  const minutes=data(),one=await calculator(60)(dataset(minutes)),five=await calculator(300)(dataset(aggregate(minutes,300)));
  for(const cutoff of [OPEN+300,OPEN+600,OPEN+900,OPEN+1200]){
    const a=one.find(r=>r.time===cutoff-60),b=five.find(r=>r.time===cutoff-300);
    assert.equal(a.high,b.high);assert.equal(a.low,b.low);
    assert.equal(a.pre_high,b.pre_high);assert.equal(a.pre_low,b.pre_low);
  }
  const a=one.find(r=>r.time===OPEN+840),b=five.find(r=>r.time===OPEN+600);
  assert.equal(a.count,15);assert.equal(b.count,3);
  assert.equal(a.high,150);assert.equal(a.low,70);assert.equal(a.pre_high,180);assert.equal(a.pre_low,30);
  assert.equal(a.locked,1);assert.equal(b.locked,1);
  assert.equal(one.find(r=>r.time===OPEN+120).locked,0); // Three one-minute bars do not finish the range.
  for(const r of one.filter(r=>r.time>=OPEN+900)){assert.equal(r.high,150);assert.equal(r.low,70);}
});

test('the normalized distance is unavailable until 09:45 and the visual switches never change exports',async()=>{
  const input=data(),shown=await calculator(60)(dataset(input)),hidden=await calculator(60,false)(dataset(input));
  assert.deepEqual(shown,hidden);
  for(const r of shown.filter(r=>r.time>=OPEN&&r.time<OPEN+840))assert(!Number.isFinite(r.distance));
  const r=shown.find(r=>r.time===OPEN+900),price=input.find(b=>b.time===r.time).close;
  assert.equal(r.distance,(price-110)/40);
  assert.equal(shown.filter(r=>r.completed===1).length,1);
});

test('RTH-only charts reset and recognize the first bar across adjacent days without any premarket bars',async()=>{
  const first=data().filter(b=>b.time>=OPEN),next=first.map(b=>({...b,time:b.time+3*86400,high:b.high+10,low:b.low+10,open:b.open+10,close:b.close+10}));
  for(const seconds of [60,300]){
    const rows=await calculator(seconds)(dataset(aggregate([...first,...next],seconds)));
    const opening=rows.find(r=>r.time===OPEN+3*86400);
    assert.equal(opening.first,1);assert.equal(opening.count,1);assert.equal(opening.locked,0);
    const locked=rows.find(r=>r.time===OPEN+3*86400+900);
    assert.equal(locked.high,160);assert.equal(locked.low,80);
  }
});

test('live window updates replace the same bar and never freeze after only three one-minute candles',async()=>{
  let input=data().filter(b=>b.time<=OPEN+840);const calc=calculator(60,false),live=await calc.open(dataset(input));
  for(const high of [170,155]){
    const last={...input.at(-1),high};input=[...input.slice(0,-1),last];
    const actual=(await live.replaceLast(last,1))[0],expected=(await calc(dataset(input),{tail:1}))[0];
    assert.deepEqual(actual,expected);assert.equal(actual.count,15);assert.equal(actual.high,high);
  }
  input=[...input,candle(OPEN+900,250)];
  const actual=(await live.advance(input,1))[0];
  assert.equal(actual.high,155);assert.equal(actual.low,70);assert.equal(actual.locked,1);
});
