'use strict';

const {emaFromPeriods}=require('./ema');
const SCORE_MODEL='ema9_15m_v3';
const SCORE_CONTRACT=Object.freeze({model:SCORE_MODEL,version:3,ema_period:9,ema_interval_seconds:300,
  classification_interval_seconds:60,classification_window_seconds:900,
  weights:Object.freeze({vwap:.3,slope:.5,classification:.2}),bias_scored:false,
  direction_rule:'close_vs_vwap_and_ema9'});
const positive=n=>Number.isFinite(n)&&n>0;

// Raw TV periods are newest first. ATR is the existing confirmed PineTS output.
// No new subscription and no local replacement of the Pine ATR calculation.
function fiveMinuteHistory(periods,atrByTime,now){
  if(!Array.isArray(periods))return [];
  const unique=new Map();
  for(const p of periods)if(Number.isFinite(p?.time)&&p.time%300===0&&!unique.has(p.time))unique.set(p.time,p);
  const sorted=[...unique.values()].sort((a,b)=>b.time-a.time),out=[],warm=[];
  for(let i=sorted.length-1,count=0;i>=0;i--){count=positive(sorted[i].close)?count+1:0;warm[i]=count;}
  for(let i=0;i<sorted.length&&out.length<32;i++){
    const p=sorted[i];if(p.time+300>now||!positive(p.close)||warm[i]<30)continue;
    const ema9=emaFromPeriods(sorted,i,9),atr=atrByTime?.get(p.time);
    if(positive(ema9))out.push({time:p.time,known_at:p.time+300,ema9,atr:positive(atr)?atr:null});
  }
  return out.sort((a,b)=>a.time-b.time);
}

function scoreInputs(points,history,d,asOf){
  const unavailable=reason=>({ready:false,reason});
  if(![-1,1].includes(d)||!Number.isFinite(asOf)||asOf%60!==0)return unavailable('score_alignment');
  const byTime=new Map();
  for(const p of history||[])if(Number.isFinite(p?.known_at)&&p.known_at<=asOf
      &&p.time%300===0&&p.known_at===p.time+300){
    if(byTime.has(p.known_at))return unavailable('score_duplicate_5m');
    byTime.set(p.known_at,p);
  }
  const known=Math.floor(asOf/300)*300,b=byTime.get(known);
  // EMA periods count actual bars, including sparse premarket trading. The
  // classification references below still require exact five-minute anchors.
  const prior=[...byTime.values()].filter(q=>positive(q.ema9)).sort((a,b)=>a.known_at-b.known_at).at(-4);
  if(!positive(b?.ema9)||!positive(prior?.ema9)||!positive(b?.atr))return unavailable('score_5m_pending');
  const minutes=new Map();
  for(const p of points||[])if(p[0]>=asOf-900&&p[0]+60<=asOf){
    if(minutes.has(p[0]))return unavailable('score_duplicate_1m');
    minutes.set(p[0],p);
  }
  // The published points contain only the current RTH day. At the open use an
  // explicit partial window; never borrow yesterday/overnight minutes to fill it.
  const first=Math.min(...(points||[]).filter(p=>p[0]+60<=asOf).map(p=>p[0]));
  const start=Math.max(first,asOf-900);
  let n=0,correct=0;
  for(let t=start;t<asOf;t+=60){
    const p=minutes.get(t),ref=byTime.get(Math.floor((t+60)/300)*300);
    if(!positive(p?.[7])||!positive(ref?.ema9))return unavailable('score_classification_pending');
    n++;correct+=d*(p[7]-ref.ema9)>0;
  }
  if(!n||n>15)return unavailable('score_classification_pending');
  const slope=(b.ema9-prior.ema9)/(3*b.atr),u=Math.max(0,d*slope);
  const latest=minutes.get(asOf-60);
  if(latest?.[0]+60!==asOf||!positive(latest?.[7]))return unavailable('score_alignment');
  return {ready:true,reason:null,ema9_5m:b.ema9,ema9_known_at:known,ema9_atr:b.atr,
    ema9_slope_atr:slope,ema9_aligned_slope:d*slope,ema9_slope_score:100*u/(u+.1),
    classification_rate:correct/n,classification_correct:correct,classification_samples:n,
    classification_start_bar:start,classification_end_bar:asOf-60,classification_score:100*correct/n,
    // Latest confirmed 1m close against the latest confirmed 5m EMA9: position only.
    ema9_bias_atr:(latest[7]-b.ema9)/b.atr,ema9_bias_pct:100*(latest[7]/b.ema9-1)};
}
function applyScore(baseScore,facts){
  const weights=SCORE_CONTRACT.weights;
  const parts={vwap:weights.vwap*baseScore,slope:weights.slope*facts.ema9_slope_score,
    classification:weights.classification*facts.classification_score};
  return {...facts,vwap_score:baseScore,score:parts.vwap+parts.slope+parts.classification,score_parts:parts};
}
function compareScores(a,b){return b.score-a.score||b.ema9_aligned_slope-a.ema9_aligned_slope
  ||b.classification_rate-a.classification_rate||b.vwap_score-a.vwap_score||a.symbol.localeCompare(b.symbol);}

module.exports={SCORE_MODEL,SCORE_CONTRACT,fiveMinuteHistory,scoreInputs,applyScore,compareScores};
