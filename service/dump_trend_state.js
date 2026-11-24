"use strict";
// Historical CLI: TV K-lines -> shared local PineTS -> compatible research rows.
const fs=require('node:fs'),path=require('node:path');
const {fetchDataset}=require('./history_bars');
const {computeHistory}=require('./local_history');
async function main(){
 const args={range:601,session:'regular',to:0,creds:path.join(__dirname,'creds.json'),symbols:[]};
 for(let i=2;i<process.argv.length;i+=2){const key=process.argv[i].replace(/^--/,'');args[key]=key==='symbols'?process.argv[i+1].split(','):['range','to'].includes(key)?Number(process.argv[i+1]):process.argv[i+1];}
 if(!args.symbols.length||!Number.isSafeInteger(args.range)||args.range<1||args.range>5001)throw new Error('无效的历史K线参数');
 if(!['regular','extended','24h','default'].includes(args.session))throw new Error('不支持的行情时段');
 const {createMarketClient}=require('./tv_market_proxy');
 const client=await createMarketClient(require('@mathieuc/tradingview'),JSON.parse(fs.readFileSync(args.creds,'utf8')));
 try{
  const result={};
  for(const symbol of args.symbols){
   const dataset=await fetchDataset(client,symbol,args);
   result[symbol]=await computeHistory(dataset);
   console.error(symbol+': '+result[symbol].length+' 根K线，PineTS本地计算，TV指标订阅0');
  }
  const json=JSON.stringify(result);
  if(args.out)fs.writeFileSync(args.out,json);else await new Promise(resolve=>process.stdout.write(json,resolve));
 }finally{await client.end();}
}
if(require.main===module)main().catch(error=>{console.error('本地历史计算失败:',error.message);process.exitCode=1;});
