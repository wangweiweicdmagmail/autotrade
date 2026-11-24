'use strict';

// One stock-session clock for subscriptions, VWAP accumulation and warm-up.
// Broker entry windows are separate and continue to use regular trading hours.
const STOCK_SESSION='regular';
const MINUTE_HISTORY=4000;
const eastern=new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',
  month:'2-digit',day:'2-digit',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const clocks=new Map();
function stockSession(time,marketSession=STOCK_SESSION){
  if(!['regular','extended','24h'].includes(marketSession))throw Error('Unsupported stock session');
  const key=marketSession+'/'+time;
  if(clocks.has(key))return clocks.get(key);
  const p=Object.fromEntries(eastern.formatToParts(time*1000).map(p=>[p.type,p.value]));
  const minute=+p.hour*60 + +p.minute,weekday=!['Sat','Sun'].includes(p.weekday);
  const start=marketSession==='regular'?570:marketSession==='extended'?240:0;
  const end=marketSession==='regular'?960:marketSession==='extended'?1200:1440;
  const result=Object.freeze({day:`${p.year}-${p.month}-${p.day}`,minute,regular:weekday&&minute>=570&&minute<960,
    active:weekday&&minute>=start&&minute<end,opening:time-(minute-start)*60});
  clocks.set(key,result);
  if(clocks.size>32768)clocks.delete(clocks.keys().next().value);
  return result;
}
function hasSessionPrefix(firstTime,time,marketSession=STOCK_SESSION){
  return Number.isFinite(firstTime)&&firstTime<=stockSession(time,marketSession).opening;
}
module.exports={STOCK_SESSION,MINUTE_HISTORY,stockSession,hasSessionPrefix};
