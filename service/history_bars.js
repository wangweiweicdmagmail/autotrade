'use strict';
// Shared read-only K-line request. No cloud indicator metadata or Study API.
const {isPriceUpdate,chartSession}=require('./chart_snapshot');
const {candle}=require('./pinets_live');

async function fetchChart(client,symbol,{timeframe='5',range=601,to=0,session='extended',timeout=25000}={}) {
  const chart=new client.Session.Chart();
  let timer;
  try {
    return await new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(new Error('TradingView K线请求超时或历史不足')),timeout);
      chart.onError(()=>reject(new Error('TradingView K线订阅失败')));
      chart.onUpdate(changes=>{
        if(!isPriceUpdate(changes))return;
        const periods=chart.periods;
        if(periods.length<range)return;
        const infos=chart.infos||{};
        if(!chartSession(infos,session).accepted){reject(new Error('TradingView 未确认所请求的行情时段'));return;}
        resolve({metadata:{timezone:infos.timezone,minmove:infos.minmov??infos.minmove,
          pricescale:infos.pricescale,session:infos.subsession_id},
          bars:periods.slice(0,range).map(candle).sort((a,b)=>a.time-b.time)});
      });
      chart.setMarket(symbol,{timeframe,range,session:session==='default'?undefined:session,
        adjustment:'splits',...(to>0?{to}:{})});
    });
  } finally {clearTimeout(timer);chart.delete();}
}

async function fetchDataset(client,symbol,options={}) {
  const data=await fetchChart(client,symbol,options);
  // Long RTH histories can cover months; cover their full date span so the
  // attack model's previous-day levels do not become missing in early bars.
  const days=Math.max(20,Math.min(5001,Math.ceil((data.bars.at(-1).time-data.bars[0].time)/86400)+10));
  const daily=await fetchChart(client,symbol,{timeframe:'D',range:days,to:options.to,
    session:options.session==='default'?'default':'regular',timeout:options.timeout});
  return {symbol,...data,daily:daily.bars};
}
module.exports={fetchChart,fetchDataset};
