'use strict';
// Isolated, read-only K-lines + shared local PineTS worker. Never a broker.
const path=require('node:path'),fs=require('node:fs');
const {loadPineModels}=require('./pine_models');
const {BAR_LIMIT,createLocalIndicators,candle}=require('./pinets_live');
const {isPriceUpdate}=require('./chart_snapshot');
function parseArgs(argv){
 const args={symbol:'NASDAQ:NVDA',model:'ema9_pyramid',session:'24h',once:false,creds:path.join(__dirname,'creds.json')};
 for(let i=2;i<argv.length;i++){const key=argv[i].replace(/^--/,'');if(key==='once')args.once=true;else args[key]=argv[++i];}
 if(!['regular','extended','24h'].includes(args.session))throw new Error('Unsupported session');
 if(!/^[A-Z0-9_]+:[A-Z0-9_.!\-]+$/.test(args.symbol))throw new Error('Invalid symbol');
 return args;
}
function snapshot(chart,view,model,requested){
 const base=view.base||{},bar=base.bar,barC=base.bar_c;
 const outputs=view.outputs?.[model.realtime_key]||[];
 return {...base,bar,bar_c:barC,bars:base.paperBars||[],history_outputs:outputs.filter(p=>p&&p.time<=bar?.time).slice(0,200),
  price_observed_at:view.observedAt==null?0:view.observedAt/1000,
  [model.realtime_key]:outputs.find(p=>p?.time===bar?.time)||{},
  [model.confirmed_key]:outputs.find(p=>p?.time===barC?.time)||{},
  session:{requested,resolved:chart.infos?.subsession_id||null,timezone:chart.infos?.timezone||null,
   adjustment:'splits',indicator_inputs:'local_defaults',pine_version:model.local_version||null,source:'pinets',tv_study_subscriptions:0,
   symbol:chart.infos?.full_name||chart.infos?.name||null}};
}
async function main(){
 const args=parseArgs(process.argv);
 const registered=loadPineModels().find(m=>m.id===args.model&&(m.interval_seconds??300)===300);
 if(!registered)throw new Error('夜盘模拟目前仅支持已登记的五分钟本地模型');
 const model={...registered,local_version:require('./pinets_runtime').getDefinition(registered.realtime_key).version};
 const {createMarketClient}=require('./tv_market_proxy');
 const client=await createMarketClient(require('@mathieuc/tradingview'),JSON.parse(fs.readFileSync(args.creds,'utf8')));
 const emitError=reason=>process.stdout.write(JSON.stringify({type:'error',reason})+'\n');
 const local=createLocalIndicators({workers:1,indicatorKeys:[model.realtime_key],historyTail:200,
  onError:()=>emitError('PineTS 本地指标计算失败，请检查本地源码与运行环境')});
 const chart=new client.Session.Chart();
 let raw={},key='',observedAt=0,closed=false,timer,probeTimer;
 chart.onUpdate(changes=>{
  if(!isPriceUpdate(changes))return;
  const periods=chart.periods;
  if(!periods.length)return;
  const next=JSON.stringify(candle(periods[0]));
  if(next!==key){key=next;observedAt=Date.now();}
  const bars=periods.slice(0,200).map(candle).reverse();
  raw={bar:bars.at(-1),bar_c:bars.at(-2),paperBars:bars};
  local.observe(args.symbol,periods,chart.infos,raw,[],observedAt);
 });
 chart.onError(()=>emitError('TradingView K线订阅失败，请核对标的与交易时段'));
 client.onDisconnected(()=>emitError('TradingView K线连接已断开，请重新连接模拟数据'));
 client.onError(()=>emitError('TradingView K线连接或授权异常'));
 chart.setMarket(args.symbol,{timeframe:'5',range:BAR_LIMIT,session:args.session,adjustment:'splits'});
 const close=async()=>{
  if(closed)return;closed=true;clearInterval(timer);clearTimeout(probeTimer);
  chart.delete();await local.close();await client.end();
 };
 process.once('SIGTERM',()=>void close());process.once('SIGINT',()=>void close());
 timer=setInterval(()=>{
  const data=snapshot(chart,local.snapshot(args.symbol,raw),model,args.session);
  process.stdout.write(JSON.stringify({type:'tick',ts:Date.now()/1000,symbols:{[args.symbol]:data}})+'\n');
  if(args.once&&data.bar&&data[model.realtime_key]?.time===data.bar.time)void close();
 },1000);
 if(args.once)probeTimer=setTimeout(()=>{emitError('只读本地指标预热超时');void close();process.exitCode=2;},90000);
}
if(require.main===module)main().catch(()=>{process.stderr.write('夜盘本地数据连接失败，请核对凭据、网络与PineTS环境\n');process.exit(1);});
module.exports={parseArgs,snapshot};
