'use strict';

const {emaFromPeriods}=require('./ema');
const {stockSession}=require('./market_session');
const SCORE_MODEL='opening15_midpoint_v7';
const SCORE_CONTRACT=Object.freeze({model:SCORE_MODEL,version:7,price_source:'close',interval_seconds:60,
  normalization:'(close-(opening15_high+opening15_low)/2)/((opening15_high-opening15_low)/2)',sort:'abs_position_desc_then_symbol',score_unit:'opening_range_half_width',
  weights:Object.freeze({opening_position:1}),direction_rule:'close_vs_opening_midpoint',zero_anchor:'opening15_midpoint',
  opening_window_minutes:15,opening_range_unit:'half_width',source:'pinets:opening_position',
  views:Object.freeze(['mixed','long','short'])});
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


// Consume the same confirmed Z plotted in the perception series. No episode,
// five-minute EMA/ATR, cross-section percentile or persistence window is needed.
function scorePosition(point,asOf){
  if(!Array.isArray(point)||point.length!==11||!Number.isFinite(asOf)||asOf%60!==0
    ||point[0]+60!==asOf||!positive(point[7])||!positive(point[2])||!positive(point[3])
    ||point[3]<=Math.max(1e-10,Math.abs(point[2])*1e-12)||!Number.isFinite(point[8]))return null;
  const z=point[8];if(z===0)return null;
  return {ready:true,z,score:Math.abs(z),score_parts:{vwap:Math.abs(z)},
    direction_sign:z>0?1:-1,direction_price:point[7],direction_vwap:point[2],
    price_source:SCORE_CONTRACT.price_source,phase:point[9],bar_time:point[0],confirmed_at:asOf};
}
// EMA5 is read from the existing PineTS trend worker, never recomputed in JS/Python.
function fiveMinuteEma5History(periods,emaByTime,now){
  if(!Array.isArray(periods)||!(emaByTime instanceof Map))return [];
  const unique=new Map();
  for(const p of periods){
    const value=emaByTime.get(p?.time);
    if(Number.isFinite(p?.time)&&p.time%300===0&&p.time+300<=now&&positive(value)
      &&positive(p.close)&&!unique.has(p.time))unique.set(p.time,{time:p.time,known_at:p.time+300,ema5:value});
  }
  return [...unique.values()].sort((a,b)=>a.time-b.time);
}

const distanceScore=x=>{const n=Math.max(0,x);return 100*(1-1/(1+n));};
function scoreTrend(row,output,asOf){
  const point=row?.points?.findLast(p=>p[0]+60<=asOf);
  if(!point||point[0]+60!==asOf||!positive(point[7]))return {ready:false,reason:'opening_position_minute_pending'};
  const scope=stockSession(point[0],'regular'),opening=point[0]-(scope.minute-570)*60;
  if(!scope.regular)return {ready:false,reason:'opening_range_regular_only'};
  if(asOf<opening+900)return {ready:false,reason:'opening_range_forming',completed_minutes:Math.max(0,(asOf-opening)/60)};
  if(!output||output.time!==point[0])return {ready:false,reason:'opening_position_pinets_pending'};
  if(output.ready!==1)return {ready:false,reason:'opening_range_incomplete'};
  const {day_open:open,opening_high:high,opening_low:low,width,position}=output;
  if(![open,high,low,width].every(positive)||high<open||low>open||high<=low
    ||!Number.isFinite(position)||output.opening_start!==opening||output.range_confirmed_at!==opening+900
    ||output.range_bars!==15||Math.abs(width-(high-low))>Math.max(1e-7,width*1e-9)
    ||!Number.isFinite(output.range_position)||Math.abs(output.range_position-position)>1e-7
    ||Math.abs(position-(point[7]-(high+low)/2)/(width/2))>Math.max(1e-6,Math.abs(position)*1e-8))
      return {ready:false,reason:'opening_position_invalid'};
  const direction=Math.sign(position);
  if(output.direction!==direction)return {ready:false,reason:'opening_position_direction_mismatch'};
  return {ready:true,score:Math.abs(position),score_parts:{opening_position:Math.abs(position)},
    opening_position:position,range_position:output.range_position,from_open_position:output.from_open_position,day_open:open,opening_high:high,opening_low:low,opening_width:width,
    opening_midpoint:output.midpoint,opening_start:opening,range_confirmed_at:opening+900,
    direction_sign:direction,direction_price:point[7],direction_open:open,direction_midpoint:(high+low)/2,
    direction_vwap:point[2],z:point[8],price_source:'close',bar_time:point[0],confirmed_at:asOf,
    source:'pinets:opening_position',outside_range:point[7]>high?'above':point[7]<low?'below':'inside'};
}
function rankingViews(candidates){
  const all=[...candidates].filter(r=>r.direction_sign!==0).sort(compareScores);
  const select=sign=>all.filter(r=>sign===0||r.direction_sign===sign).slice(0,10).map((r,i)=>({...r,rank:i+1}));
  return {mixed:select(0),long:select(1),short:select(-1)};
}
function compareScores(a,b){return b.score-a.score||a.symbol.localeCompare(b.symbol);}

module.exports={SCORE_MODEL,SCORE_CONTRACT,fiveMinuteHistory,fiveMinuteEma5History,scorePosition,scoreTrend,rankingViews,distanceScore,compareScores};
