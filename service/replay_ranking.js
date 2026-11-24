'use strict';
// Same scoring and PineTS EMA5 as production, with caller-supplied causal database candles.
const {calculateSeries,buildFrame}=require('./market_rhythm');
const {createCalculator,getDefinition}=require('./pinets_runtime');
const {stockSession}=require('./market_session');

async function replayRanking({datasets,as_of,day}){
 const version=require('../package-lock.json').packages['node_modules/pinets'].version;
 const calculator=createCalculator(getDefinition('opening_position'),{version}),series={},history={},openingPositions={},errors=[];
 for(const [symbol,data] of Object.entries(datasets)){
  try{
   const minute=data.bars.filter(b=>b.time+60<=as_of);
   if(!minute.length)throw Error('缺少时点之前的真实K线');
   const outputs=await calculator({symbol,metadata:data.metadata,bars:minute.filter(b=>b.time>=stockSession(as_of-60,'regular').opening),daily:[]},{tail:1});
   openingPositions[symbol]=outputs.at(-1);
   series[symbol]=calculateSeries(symbol,minute,as_of,[],{marketSession:'regular'});
  }catch(error){errors.push({symbol,reason:error.message});}
 }
 const frame=buildFrame(series,Object.keys(datasets),as_of,history,{marketSession:'regular',openingPositions});
 if(frame.as_of!==as_of)errors.push({reason:'requested_minute_unavailable',requested_as_of:as_of,available_as_of:frame.as_of});
 const modelSource=require('crypto').createHash('sha256').update(getDefinition('opening_position').source).digest('hex');
 return {day:day||frame.day,source_day:frame.day,as_of,source_as_of:frame.as_of,ranks:frame.as_of===as_of?frame.ranks:[],rankings:frame.as_of===as_of?frame.rankings:{mixed:[],long:[],short:[]},scoring:frame.scoring,coverage:frame.coverage,
  errors,indicator_source_sha256:modelSource,pinets_version:version,
  ranking_source_sha256:require('crypto').createHash('sha256').update(require('fs').readFileSync(require.resolve('./rhythm_score'))).update(require('fs').readFileSync(require.resolve('./market_rhythm'))).digest('hex'),
  excluded:frame.coverage.excluded,complete:!frame.coverage.missing.length&&!frame.coverage.excluded.length&&!frame.scoring.pending.length&&!errors.length};
}
if(require.main===module){const chunks=[];process.stdin.on('data',c=>chunks.push(c));process.stdin.on('end',()=>
 replayRanking(JSON.parse(Buffer.concat(chunks))).then(r=>process.stdout.write(JSON.stringify(r))).catch(e=>{process.stderr.write(e.message);process.exitCode=1}));}
module.exports={replayRanking};
