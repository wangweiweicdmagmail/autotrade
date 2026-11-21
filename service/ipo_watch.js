'use strict';
// Read-only first observed trade monitor. No Pine studies or broker APIs.
const clock=new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',weekday:'short',hourCycle:'h23'});
function et(t){const p=Object.fromEntries(clock.formatToParts(t*1000).map(p=>[p.type,p.value]));return{date:p.year+'-'+p.month+'-'+p.day,minute:+p.hour*60 + +p.minute,weekday:p.weekday};}
const positive=n=>typeof n==='number'&&Number.isFinite(n)&&n>0;
function tradeCandidate(symbol,date,row,source,now){
  if(!['quote','bar'].includes(source))return null;
  const market=et(now);
  if(market.date!==date||market.minute<570||market.minute>=960||['Sat','Sun'].includes(market.weekday))return null;
  const price=source==='quote'?row.lp:row.close, stamp=source==='quote'?row.lp_time:row.time;
  if(!positive(price)||!positive(stamp)||!positive(row.volume)||stamp>now||now-stamp>90)return null;
  const time=et(stamp);if(time.date!==date||time.minute<570||time.minute>=960)return null;
  if(source==='bar'&&(!positive(row.open)||!positive(row.low)||!positive(row.high)||row.low>Math.min(row.open,price)||row.high<Math.max(row.open,price)))return null;
  if(row.pro_name&&row.pro_name!==symbol&&row.pro_name!=='BATS:'+symbol.split(':')[1])return null;
  return{type:'ipo_trade',symbol,listing_date:date,price,volume:row.volume,price_time:stamp,
    observed_at:now,source,time_kind:source==='quote'?'trade_time':'bar_open',update_mode:row.update_mode||null};
}
function startIpoWatch(client,{watches,emit,now=()=>Date.now()/1000,setTimer=setTimeout,clearTimer=clearTimeout}){
  if(!Object.keys(watches||{}).length)return{stop(){}};
  const quote=new client.Session.Quote({customFields:['lp','lp_time','volume','current_session','pro_name','update_mode']});
  const slots=new Map();let stopped=false;
  function report(symbol,phase,reason=null){const slot=slots.get(symbol);if(stopped||!slot)return;const key=phase+'|'+reason;
    if(slot.statusKey===key)return;slot.statusKey=key;emit({type:'ipo_status',symbol,listing_date:slot.date,phase,reason,observed_at:now()});}
  function accept(symbol,row,source){const slot=slots.get(symbol);if(stopped||!slot||slot.sent)return;
    const candidate=tradeCandidate(symbol,slot.date,row,source,now());if(!candidate)return;
    slot.sent=true;emit(candidate);report(symbol,'trade_observed');
    clearTimer(slot.quoteRetry);clearTimer(slot.chartRetry);
    slot.market?.close();slot.chart?.delete();slot.market=null;slot.chart=null;
  }
  for(const [symbol,date] of Object.entries(watches||{})){
    if(!/^[A-Z0-9_.-]+:[A-Z0-9_.!-]+$/.test(symbol)||!/^\d{4}-\d{2}-\d{2}$/.test(date))continue;
    const slot={date,sent:false};slots.set(symbol,slot);
    if(et(now()).date>date){report(symbol,'expired');continue;}
    const subscribeQuote=()=>{if(stopped||slot.sent||et(now()).date>date)return;
      const market=slot.market=new quote.Market(symbol,'regular');
      market.onData(row=>{if(slot.market!==market)return;report(symbol,'waiting_trade');accept(symbol,row,'quote');});
      market.onError(()=>{if(stopped||slot.market!==market||slot.sent)return;market.close();slot.market=null;
        report(symbol,'reconnecting','IPO报价暂未就绪，持续重试');slot.quoteRetry=setTimer(subscribeQuote,15000);});
    };
    const subscribeChart=()=>{if(stopped||slot.sent||et(now()).date>date)return;
      const chart=slot.chart=new client.Session.Chart();
      chart.onUpdate(changes=>{if(slot.chart!==chart||stopped||Array.isArray(changes)&&!changes.includes('$prices'))return;
        const p=chart.periods?.[0];if(!p||chart.infos?.subsession_id!=='regular')return;
        const resolved=chart.infos?.full_name;
        if(resolved&&resolved!==symbol&&resolved!=='BATS:'+symbol.split(':')[1])return;
        accept(symbol,{time:p.time,open:p.open,high:p.max,low:p.min,close:p.close,volume:p.volume},'bar');});
      chart.onError(()=>{if(stopped||slot.chart!==chart||slot.sent)return;clearTimer(slot.chartRetry);chart.delete();slot.chart=null;
        // No history before an IPO is expected, and never consumes main-feed retries.
        slot.chartRetry=setTimer(subscribeChart,30000);});
      slot.chartRetry=setTimer(()=>{if(stopped||slot.sent||slot.chart!==chart)return;
        chart.delete();slot.chart=null;subscribeChart();},45000);
      chart.setMarket(symbol,{timeframe:'1',range:1,session:'regular'});
    };
    report(symbol,'connecting');subscribeQuote();subscribeChart();
  }
  return{stop(){stopped=true;for(const s of slots.values()){clearTimer(s.quoteRetry);clearTimer(s.chartRetry);s.market?.close();s.chart?.delete();}quote.delete();}};
}
module.exports={tradeCandidate,startIpoWatch,et};
