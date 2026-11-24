'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {SCORE_CONTRACT,scoreTrend,rankingViews}=require('../service/rhythm_score');
const {buildFrame,calculateSeries,createMarketRhythm}=require('../service/market_rhythm');
const {createCalculator,getDefinition}=require('../service/pinets_runtime');
const OPEN=Date.parse('2026-11-18T14:30:00Z')/1000,AT=OPEN+1200;
const near=(a,b)=>assert(Math.abs(a-b)<1e-7,`${a} != ${b}`);
const bar=(time,close=100)=>({time,open:100,high:Math.max(102,close),low:Math.min(98,close),close,volume:100});
function sample(symbol='NASDAQ:A',position=2,at=AT){
 const bars=Array.from({length:(at-OPEN)/60},(_,i)=>bar(OPEN+i*60,i===((at-OPEN)/60)-1?100+2*position:100));
 const row=calculateSeries(symbol,bars,at);
 return {bars,row,output:{time:at-60,ready:1,day_open:100,opening_high:102,opening_low:98,width:4,position,
  direction:Math.sign(position),opening_start:OPEN,range_confirmed_at:OPEN+900,range_bars:15,midpoint:100,range_position:position}};
}
const calc=createCalculator(getDefinition('opening_position'),{version:require('../package.json').dependencies.pinets});
const metadata={timezone:'America/New_York',minmove:1,pricescale:100,type:'stock',session:'regular'};

test('ranking contract is only distance from opening midpoint in half-range units',()=>{
 assert.equal(SCORE_CONTRACT.model,'opening15_midpoint_v7');assert.equal(SCORE_CONTRACT.version,7);
 assert.deepEqual(SCORE_CONTRACT.weights,{opening_position:1});assert.equal(SCORE_CONTRACT.opening_window_minutes,15);
 assert.equal(SCORE_CONTRACT.zero_anchor,'opening15_midpoint');assert.deepEqual(SCORE_CONTRACT.views,['mixed','long','short']);
});
test('score consumes the same Pine output, without VWAP, EMA, ATR or a prior session requirement',async()=>{
 const s=sample(),out=await calc({symbol:s.row.symbol,bars:s.bars,metadata});
 const result=scoreTrend(s.row,out.at(-1),AT);assert(result.ready);near(result.score,2);near(result.opening_position,2);
 near(result.range_position,2);assert.equal(result.direction_sign,1);assert.equal(result.range_confirmed_at,OPEN+900);
 for(const p of s.row.points){p[2]=null;p[3]=0;p[8]=null;}
 assert.equal(scoreTrend(s.row,out.at(-1),AT).score,result.score);
});
test('ranking rejects every minute before 09:45 even if a caller supplies a finished future range',()=>{
 for(const n of [1,5,14]){
  const s=sample('NASDAQ:A',.1,OPEN+n*60),r=scoreTrend(s.row,s.output,OPEN+n*60);
  assert(!r.ready);assert.equal(r.reason,'opening_range_forming');assert.equal(r.completed_minutes,n);
 }
 const s=sample('NASDAQ:A',.1,OPEN+900);assert(scoreTrend(s.row,s.output,OPEN+900).ready);
});
test('a late Pine result recovers the same minute and a later or earlier result cannot be borrowed',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'rank-opening-'));
 try{
  const s=sample(),file=path.join(dir,'frame.json'),feed=createMarketRhythm({symbols:[s.row.symbol],cachePath:file});
  feed.observe(s.row.symbol,s.bars,AT);await feed.flush(AT+4);
  let f=JSON.parse(await fs.readFile(file));assert.equal(f.ranks.length,0);assert.equal(f.scoring.pending[0].reason,'opening_position_pinets_pending');
  feed.observeOpeningPosition(s.row.symbol,s.output,AT+5);await feed.flush(AT+9);
  f=JSON.parse(await fs.readFile(file));assert.equal(f.ranks[0].opening_position,2);assert.deepEqual(f.scoring.pending,[]);
  for(const offset of [-60,60])assert(!scoreTrend(s.row,{...s.output,time:s.output.time+offset},AT).ready);
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('illegal, incomplete, wrong-day or wrong-width ranges cannot create rank values',()=>{
 const s=sample();
 for(const edit of [{ready:0},{width:0},{width:3},{position:NaN},{direction:-1},{opening_start:OPEN-86400},
  {range_confirmed_at:OPEN+1200},{range_bars:14},{day_open:0},{opening_high:99}])assert(!scoreTrend(s.row,{...s.output,...edit},AT).ready,JSON.stringify(edit));
});
test('zero is neutral; both signed views are independent full-pool top tens',()=>{
 const rows=Array.from({length:25},(_,i)=>({symbol:'NASDAQ:S'+String(i).padStart(2,'0'),score:Math.abs(i-12),direction_sign:Math.sign(i-12),opening_position:i-12}));
 const views=rankingViews(rows);assert.equal(views.mixed.length,10);assert.equal(views.long.length,10);assert.equal(views.short.length,10);
 assert(views.long.every(r=>r.opening_position>0));assert(views.short.every(r=>r.opening_position<0));
 assert.equal(views.long[0].symbol,'NASDAQ:S24');assert.equal(views.short[0].symbol,'NASDAQ:S00');
 assert(views.long.some(r=>!views.mixed.some(m=>m.symbol===r.symbol)));
 assert(views.long.every((r,i)=>r.rank===i+1));
});
test('whole-frame scoring preserves reference exclusions and does not wait for five-minute EMA',()=>{
 const a=sample(),b=sample('NASDAQ:B',-1),q=sample('NASDAQ:QQQ',20);
 const f=buildFrame({A:a.row,B:b.row,Q:q.row},[a.row.symbol,b.row.symbol,q.row.symbol],AT,{},
  {openingPositions:{[a.row.symbol]:a.output,[b.row.symbol]:b.output,[q.row.symbol]:q.output}});
 assert.deepEqual(f.ranks.map(r=>r.symbol),['NASDAQ:A','NASDAQ:B']);assert.deepEqual(f.rankings.long.map(r=>r.symbol),['NASDAQ:A']);
 assert.equal(f.rankings.short[0].symbol,'NASDAQ:B');assert.deepEqual(f.scoring.pending,[]);
});
test('range remains fixed while current close changes; unclosed future candles cannot alter an earlier rank',async()=>{
 const s=sample(),data={symbol:s.row.symbol,metadata,bars:s.bars};
 const base=(await calc(data)).at(-1);
 const future=[...s.bars,bar(AT,5000)];
 const visible=future.filter(b=>b.time+60<=AT),out=(await calc({...data,bars:visible})).at(-1);
 near(out.position,base.position);near(out.width,base.width);
 const changed=[...s.bars.slice(0,-1),bar(AT-60,112)],next=(await calc({...data,bars:changed})).at(-1);
 near(next.width,4);near(next.position,6);
});
test('independent research and production registry use exactly the same Pine definition',()=>{
 const independent=require('../service/opening_position_research').definition;
 assert.deepEqual(independent,getDefinition('opening_position'));
});
