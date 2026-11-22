"use strict";
// Compatibility CLI for K-lines only. Indicator calculation is local elsewhere.
const fs=require('node:fs'),path=require('node:path');
const {fetchChart,symbolSession}=require('./history_bars');
function parseArgs(argv){
 const args={symbols:['NASDAQ:QQQ'],timeframe:'5',range:300,session:'extended',creds:path.join(__dirname,'creds.json'),timeout:25000};
 for(let i=2;i<argv.length;i+=2){const key=argv[i].replace(/^--/,'');args[key]=key==='symbols'?argv[i+1].split(','):['range','timeout','to'].includes(key)?Number(argv[i+1]):argv[i+1];}
 if(args.indicators)throw new Error('TV指标订阅已退役；此入口只拉K线，指标使用PineTS');
 if(!['regular','extended','24h','default'].includes(args.session))throw new Error('不支持的行情时段');
 if(!Number.isSafeInteger(args.range)||args.range<1||args.range>5001)throw new Error('无效K线数量');
 if(args.timeframe==='1D')args.timeframe='D';
 return args;
}
async function main(){
 const args=parseArgs(process.argv);
 const {createMarketClient}=require('./tv_market_proxy');
 const client=await createMarketClient(require('@mathieuc/tradingview'),JSON.parse(fs.readFileSync(args.creds,'utf8')));
 try{
  const result={generated_at:Date.now()/1000,symbols:{}};
  for(const symbol of args.symbols){const data=await fetchChart(client,symbol,{...args,session:symbolSession(symbol,args)});result.symbols[symbol]={...data,reason:'ok',source:'tv_bars',studies:{}};}
  const json=JSON.stringify(result);
  if(args.out)fs.writeFileSync(args.out,json);else await new Promise(resolve=>process.stdout.write(json,resolve));
 }finally{await client.end();}
}
if(require.main===module)main().catch(error=>{console.error('K线采集失败:',error.message);process.exitCode=1;});
module.exports={parseArgs};
