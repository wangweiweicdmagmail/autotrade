'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {SCORE_CONTRACT,fiveMinuteHistory,scoreInputs,applyScore,compareScores}=require('../service/rhythm_score');
const {createMarketRhythm,buildFrame,calculateSeries}=require('../service/market_rhythm');
const OPEN=Date.parse('2026-11-18T14:30:00Z')/1000;
function history(){return Array.from({length:9},(_,i)=>{const known=OPEN+(i-3)*300;return{time:known-300,known_at:known,ema9:100+i*10,atr:2};});}
function points(start=OPEN,end=OPEN+900,hs=history()){
  return Array.from({length:(end-start)/60},(_,i)=>{const t=start+i*60,e=hs.find(q=>q.known_at===Math.floor((t+60)/300)*300).ema9;
    return[t,0,0,1,e+1,e+2,e,e+1,2,'outside_progress',1];});
}
function rawFive(){
  const periods=Array.from({length:100},(_,i)=>({time:OPEN+900-i*300,open:100,max:101,min:99,close:100}));
  return{periods,atr:new Map(periods.map(p=>[p.time,2]))};
}
function minuteBars(){
  const bar=(time,close)=>({time,open:close,high:close+.1,low:close-.1,close,volume:100});
  return[...Array.from({length:40},(_,i)=>bar(OPEN-86400+i*60,100)),...Array.from({length:16},(_,i)=>bar(OPEN+i*60,100+i*2))];
}
test('classification uses fifteen closed minutes and each minute historical EMA9, never current EMA backcasting',()=>{
  const hs=history(),ps=points(),f=scoreInputs(ps,hs,1,OPEN+900);
  assert(f.ready);assert.equal(f.classification_samples,15);assert.equal(f.classification_correct,15);assert.equal(f.classification_rate,1);
  assert.equal(ps.filter(p=>p[7]>f.ema9_5m).length,1);
  assert.equal(f.classification_start_bar,OPEN);assert.equal(f.classification_end_bar,OPEN+840);
});
test('five-minute boundary is available at confirmation, forming/future values cannot affect a past rank',()=>{
  const hs=history(),ps=points(),expected=scoreInputs(ps,hs,1,OPEN+900);
  const changed=hs.map(q=>q.known_at>OPEN+900?{...q,ema9:99999,atr:1000}:q);
  assert.deepEqual(scoreInputs([...ps,[OPEN+900,0,0,1,0,0,0,99999]],changed,1,OPEN+900),expected);
  assert.equal(expected.ema9_known_at,OPEN+900);
  assert.equal(scoreInputs(ps,hs.filter(q=>q.known_at!==OPEN+900),1,OPEN+900).ready,false);
});
test('long/short score symmetry, equality is not success and opposite slope earns no slope points',()=>{
  const hs=history(),ps=points(),long=scoreInputs(ps,hs,1,OPEN+900);
  const mirror=scoreInputs(ps.map(p=>[...p.slice(0,7),400-p[7],...p.slice(8)]),hs.map(q=>({...q,ema9:400-q.ema9})),-1,OPEN+900);
  assert.equal(mirror.classification_rate,long.classification_rate);assert.equal(mirror.ema9_slope_score,long.ema9_slope_score);
  assert.equal(scoreInputs(ps,hs,-1,OPEN+900).ema9_slope_score,0);
  const equals=ps.map(p=>{const e=hs.find(q=>q.known_at===Math.floor((p[0]+60)/300)*300).ema9;return[...p.slice(0,7),e,...p.slice(8)];});
  assert.equal(scoreInputs(equals,hs,1,OPEN+900).classification_correct,0);
});
test('opening partial sample is explicit, while missing minutes, ATR, stale EMA and malformed data never fall back to VWAP-only scoring',()=>{
  const hs=history(),ps=points();
  assert.equal(scoreInputs(ps.slice(0,1),hs,1,OPEN+60).classification_samples,1);
  for(const [minutes,five]of [
    [ps.filter((_,i)=>i!==4),hs], [ps,hs.map(q=>({...q,atr:null}))],
    [ps,hs.filter(q=>q.known_at<OPEN+900)], [ps.map((p,i)=>i===2?[...p.slice(0,7),Infinity,...p.slice(8)]:p),hs],
    [[...ps,ps[2]],hs], [ps,[...hs,hs[3]]],
  ])assert.equal(scoreInputs(minutes,five,1,OPEN+900).ready,false);
});
test('bias changes neither score nor tie-breaking, and the declared weights match the score',()=>{
  const f=scoreInputs(points(),history(),1,OPEN+900),a={symbol:'A',...applyScore(80,f)};
  assert.equal(a.score_parts.vwap,32);assert.equal(a.score_parts.classification,20);
  assert.equal(a.score,32+.4*f.ema9_slope_score+20);assert.equal(SCORE_CONTRACT.bias_scored,false);
  assert.equal(applyScore(80,{...f,ema9_bias_atr:99999}).score,a.score);
  assert(compareScores({...a,ema9_bias_atr:-999},{...a,symbol:'B',ema9_bias_atr:999})<0);
});
test('EMA9 history reuses the nine-period close helper and Pine ATR mapping, excluding future five-minute prices',()=>{
  const {periods,atr}=rawFive(),past=fiveMinuteHistory(periods,atr,OPEN+900);
  assert.equal(past.at(-1).known_at,OPEN+900);assert.equal(past.at(-1).ema9,100);assert.equal(past.at(-1).atr,2);
  assert.deepEqual(fiveMinuteHistory(periods.map((p,i)=>i===0?{...p,close:999}:p),atr,OPEN+900),past);
  assert.equal(fiveMinuteHistory(periods.slice(0,20),atr,OPEN+900).length,0);
  const broken=periods.map((p,i)=>i===4?{...p,close:NaN}:p);
  assert(!fiveMinuteHistory(broken,atr,OPEN+900).some(q=>q.known_at===OPEN+900));
});

test('EMA slope counts three actual five-minute bar steps even when premarket bars are sparse',()=>{
  const hs=history().filter(q=>q.known_at!==OPEN+300),ps=points(OPEN+900,OPEN+960,hs);
  const f=scoreInputs(ps,hs,1,OPEN+960);
  assert(f.ready);const closed=hs.filter(q=>q.known_at<=OPEN+960);
  assert.equal(f.ema9_slope_atr,(closed.at(-1).ema9-closed.at(-4).ema9)/6);
});
test('incomplete 5m facts mark a scoring barrier, then late confirmed ATR revises the same minute to ready',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'rhythm-score-'));
  try{
    const cache=path.join(dir,'frame.json'),feed=createMarketRhythm({symbols:['NASDAQ:A'],cachePath:cache}),{periods,atr}=rawFive();
    feed.observe('NASDAQ:A',minuteBars(),OPEN+900);
    feed.observeFiveMinute('NASDAQ:A',periods,null,OPEN+900);await feed.flush(OPEN+904);
    let frame=JSON.parse(await fs.readFile(cache,'utf8'));assert.equal(frame.scoring.pending.length,1);assert.equal(frame.ranks.length,0);
    feed.observeFiveMinute('NASDAQ:A',periods,atr,OPEN+905);await feed.flush(OPEN+909);
    frame=JSON.parse(await fs.readFile(cache,'utf8'));assert.equal(frame.as_of,OPEN+900);assert.equal(frame.scoring.pending.length,0);
    assert.equal(frame.ranks[0].classification_samples,15);assert.equal(frame.scoring.model,SCORE_CONTRACT.model);
    const saved=await fs.readFile(cache,'utf8');
    feed.observeFiveMinute('NASDAQ:A',periods.map((p,i)=>i===0?{...p,close:9999}:p),atr,OPEN+910);await feed.flush(OPEN+914);
    assert.equal(await fs.readFile(cache,'utf8'),saved);
    feed.reset('NASDAQ:A');feed.observe('NASDAQ:A',minuteBars(),OPEN+915);await feed.flush(OPEN+919);
    assert.equal(JSON.parse(await fs.readFile(cache,'utf8')).scoring.pending.length,1);
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('production frame publishes score evidence and never ranks a reference instrument',()=>{
  const a=calculateSeries('NASDAQ:A',minuteBars(),OPEN+900),q=calculateSeries('NASDAQ:QQQ',minuteBars(),OPEN+900);
  const f=buildFrame({'NASDAQ:A':a,'NASDAQ:QQQ':q},['NASDAQ:A','NASDAQ:QQQ'],OPEN+900,{'NASDAQ:A':history()});
  assert.deepEqual(f.ranks.map(r=>r.symbol),['NASDAQ:A']);assert.equal(f.scoring.pending.length,0);
  const r=f.ranks[0];assert.equal(r.score,r.score_parts.vwap+r.score_parts.slope+r.score_parts.classification);
});

test('pre-open and overnight scoring keeps the previous RTH close rather than only the newest night bars',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'rhythm-overnight-'));
  try{
    const file=path.join(dir,'frame.json'),feed=createMarketRhythm({symbols:['NASDAQ:A'],cachePath:file});
    const now=OPEN+86400-1800,close=OPEN+23400;
    const bar=(time,price)=>({time,open:price,high:price+.1,low:price-.1,close:price,volume:100});
    const bars=[...Array.from({length:40},(_,i)=>bar(OPEN-86400+i*60,100)),...Array.from({length:390},(_,i)=>bar(OPEN+i*60,100+i*.02))];
    const periods=Array.from({length:601},(_,i)=>({time:now-i*300,close:100}));
    feed.observe('NASDAQ:A',bars,now);
    feed.observeFiveMinute('NASDAQ:A',periods,new Map(periods.map(p=>[p.time,2])),now);
    await feed.flush(now+4);const frame=JSON.parse(await fs.readFile(file,'utf8'));
    assert.equal(frame.as_of,close);assert.equal(frame.scoring.pending.length,0);
    assert.equal(frame.ranks[0].ema9_known_at,close);assert.equal(frame.ranks[0].classification_samples,15);
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});
