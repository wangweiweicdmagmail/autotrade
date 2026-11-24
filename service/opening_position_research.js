'use strict';
// Explicit offline research only. Never imported by the live ranking or executor.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {createCalculator,getDefinition}=require('./pinets_runtime');
const {calculateSeries,REFERENCES}=require('./market_rhythm');
const {scorePosition}=require('./rhythm_score');
const {stockSession}=require('./market_session');
const definition=getDefinition('opening_position');
const source=definition.source;
const version=require('../package-lock.json').packages['node_modules/pinets'].version;
const near=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(a-b)<=Math.max(1e-7,Math.abs(a)*1e-10);
const valid=b=>b&&[b.time,b.open,b.high,b.low,b.close,b.volume].every(Number.isFinite)&&b.time%60===0
 &&Math.min(b.open,b.close)>=b.low&&Math.max(b.open,b.close)<=b.high&&b.low>0&&b.volume>=0;

async function replayOpeningPositions({datasets,as_of,day}){
 if(!Number.isSafeInteger(as_of)||as_of%60!==0)throw Error('须指定一分钟收盘确认时刻');
 const scope=stockSession(as_of-60,'regular'),opening=as_of-60-(scope.minute-570)*60;
 if(!scope.regular||as_of<opening+900)throw Error('固定小区间09:45后才可用于确认排名');
 if(day!==scope.day)throw Error('指定日期与收盘时刻不一致');
 const calculate=createCalculator(definition,{version}),ranks=[],missing=[],vwapRanks=[],vwapMissing=[];
 for(const [symbol,data]of Object.entries(datasets)){
  if(REFERENCES.has(symbol))continue;
  try{
   if(data.metadata?.session!=='regular')throw Error('研究输入必须明确为regular');
   const bars=data.bars.filter(b=>b.time>=opening&&b.time+60<=as_of).sort((a,b)=>a.time-b.time);
   if(new Set(bars.map(b=>b.time)).size!==bars.length||bars.some(b=>!valid(b)))throw Error('K线重复或OHLCV非法');
   const early=bars.filter(b=>b.time<opening+900),last=bars.at(-1);
   if(early.length!==15||early.some((b,i)=>b.time!==opening+i*60))throw Error('opening15_minute_gap');
   if(last?.time!==as_of-60)throw Error('target_minute_missing');
   const five=(data.five_bars||[]).filter(b=>b.time>=opening&&b.time<opening+900&&b.time+300<=as_of).sort((a,b)=>a.time-b.time);
   let verified=false;
   if(data.five_bars!==undefined){
    if(five.length!==3||five.some((b,i)=>b.time!==opening+i*300||!valid(b)))throw Error('opening_5m_missing_or_invalid');
    verified=near(five[0].open,early[0].open)&&near(Math.max(...five.map(b=>b.high)),Math.max(...early.map(b=>b.high)))
     &&near(Math.min(...five.map(b=>b.low)),Math.min(...early.map(b=>b.low)));
    if(!verified)throw Error('opening_1m_5m_mismatch');
   }
   const outputs=await calculate({symbol,metadata:data.metadata,bars,daily:[]}),p=outputs.at(-1);
   if(p?.time!==as_of-60||p.ready!==1||!Number.isFinite(p.position))throw Error('opening_range_not_ready');
   ranks.push({symbol,score:Math.abs(p.position),coordinate:p.position,range_position:p.range_position,from_open_position:p.from_open_position,direction:p.direction,close:last.close,
    day_open:p.day_open,opening_midpoint:p.midpoint,opening_high:p.opening_high,opening_low:p.opening_low,opening_width:p.width,
    opening_width_pct:100*p.width/p.day_open,move_pct:100*(last.close/p.day_open-1),
    opening_high_coordinate:p.high_position,opening_low_coordinate:p.low_position,
    bar_time:p.time,confirmed_at:as_of,range_confirmed_at:opening+900,cross_timeframe_checked:verified,
    outside_range:last.close>p.opening_high?'above':last.close<p.opening_low?'below':'inside'});
  }catch(error){missing.push({symbol,reason:error.message});}
  try{
   const series=calculateSeries(symbol,data.bars,as_of,[],{marketSession:'regular'}),p=scorePosition(series.points.at(-1),as_of);
   if(series.issue||!p)throw Error(series.issue||'vwap_not_ready');
   vwapRanks.push({symbol,z:p.z,score:Math.abs(p.z),direction:p.direction_sign});
  }catch(error){vwapMissing.push({symbol,reason:error.message});}
 }
 const sort=rows=>{rows.sort((a,b)=>b.score-a.score||a.symbol.localeCompare(b.symbol));rows.forEach((r,i)=>r.rank=i+1);};
 sort(ranks);sort(vwapRanks);
 for(const row of ranks)row.vwap_position_rank=vwapRanks.find(r=>r.symbol===row.symbol)?.rank??null;
 const directional=sign=>ranks.filter(row=>row.direction===sign).slice(0,10).map((row,i)=>({...row,combined_rank:row.rank,rank:i+1}));
 const long_top10=directional(1),short_top10=directional(-1);
 const side_counts={long:ranks.filter(r=>r.direction===1).length,short:ranks.filter(r=>r.direction===-1).length,neutral:ranks.filter(r=>r.direction===0).length};
 const expected=Object.keys(datasets).filter(s=>!REFERENCES.has(s)).length;
 return {day,as_of,model:'opening15_midpoint_half_position_research_v4',zero_anchor:'opening15_midpoint',normalization:'(close-(opening15_high+opening15_low)/2)/((opening15_high-opening15_low)/2)',
  score_unit:'opening_range_half_width',sort:'abs_position_desc_then_symbol',range_confirmed_at:opening+900,
  ranks,long_top10,short_top10,side_counts,missing,reference_symbols:[...REFERENCES],eligible:expected,complete:expected>0&&ranks.length===expected&&missing.length===0,
  vwap_baseline:{model:'vwap_close_abs_z_research',ranks:vwapRanks,missing:vwapMissing,complete:expected>0&&vwapRanks.length===expected},
  baseline_source_sha256:crypto.createHash('sha256').update(fs.readFileSync(require.resolve('./market_rhythm'))).update(fs.readFileSync(require.resolve('./rhythm_score'))).digest('hex'),
  indicator_source_sha256:crypto.createHash('sha256').update(source).digest('hex'),pinets_version:version,
  ranking_source_sha256:crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex')};
}
if(require.main===module){const chunks=[];process.stdin.on('data',c=>chunks.push(c));process.stdin.on('end',()=>
 replayOpeningPositions(JSON.parse(Buffer.concat(chunks))).then(r=>process.stdout.write(JSON.stringify(r)))
 .catch(error=>{process.stderr.write(error.message+'\n');process.exitCode=1;}));}
module.exports={replayOpeningPositions,definition};
