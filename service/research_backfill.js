'use strict';
// Explicit, read-only historical OHLCV request. Credentials never appear in argv or output.
const fs=require('node:fs'),TradingView=require('@mathieuc/tradingview');
const {createMarketClient}=require('./tv_market_proxy');
const {fetchChart}=require('./history_bars');
async function main(){
  const chunks=[];for await(const chunk of process.stdin)chunks.push(chunk);
  const args=JSON.parse(Buffer.concat(chunks).toString());
  if(!/^[A-Z0-9_.-]+:[A-Z0-9_.!+/-]+$/.test(args.symbol)||!['1m','5m','1d'].includes(args.interval)
    ||!['extended','regular','default','24h'].includes(args.session)||!Number.isSafeInteger(args.to)||!Number.isSafeInteger(args.range)
    ||args.range<1||args.range>5000)throw Error('历史补取参数无效');
  const credentials=JSON.parse(fs.readFileSync(args.credentials_path,'utf8'));
  const client=await createMarketClient(TradingView,credentials);
  try{
    const data=await fetchChart(client,args.symbol,{timeframe:{'1m':'1','5m':'5','1d':'D'}[args.interval],
      range:args.range,to:args.to,session:args.session,timeout:25000});
    process.stdout.write(JSON.stringify(data));
  }finally{await client.end();}
}
main().catch(error=>{process.stderr.write(error.message+'\n');process.exitCode=1;});
