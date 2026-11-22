'use strict';

// Read-only perception model. Never imports an execution model or broker adapter.
const fs = require('node:fs/promises');
const path = require('node:path');
const {SCORE_CONTRACT,fiveMinuteHistory,scoreInputs,applyScore,compareScores}=require('./rhythm_score');
const {STOCK_SESSION,MINUTE_HISTORY,stockSession,hasSessionPrefix}=require('./market_session');
const BETA = ['CAPITALCOM:NAS100', 'CAPITALCOM:SPX500'];
const REFERENCES = new Set([...BETA, 'BLACKBULL:WTI', 'NASDAQ:QQQ', 'AMEX:SPY', 'NASDAQ:SMH',
  'AMEX:XLB', 'AMEX:XLP', 'AMEX:XLU', 'AMEX:XLV']);
const COLUMNS = ['time', 'ema3', 'vwap', 'sigma', 'open', 'high', 'low', 'close', 'z', 'phase', 'direction'];
function minuteSubscriptionOptions(referenceOnly, stockHistory=MINUTE_HISTORY,marketSession=STOCK_SESSION) {
  // Overnight, yesterday's full RTH needs its preceding RTH for EMA warm-up.
  // Indices must omit session entirely; regular/24h can return no prices.
  return referenceOnly ? {timeframe:'1',range:4000} : {timeframe:'1',range:stockHistory,session:marketSession};
}
function session(t) {
  return stockSession(t);
}
const round = n => Number.isFinite(n) ? +n.toFixed(7) : null;
const valid = b => [b.time,b.open,b.high,b.low,b.close,b.volume].every(Number.isFinite)
  && b.time % 60 === 0 && b.low > 0 && b.low <= Math.min(b.open,b.close)
  && b.high >= Math.max(b.open,b.close) && b.volume >= 0;

function calculateSeries(symbol, bars, now, daily=[],{marketSession=STOCK_SESSION}={}) {
  const beta=BETA.includes(symbol), anchors=daily.filter(b=>Number.isFinite(b.time)).sort((a,b)=>a.time-b.time);
  const input = [...new Map(bars.filter(b => Number.isFinite(b.time) && b.time + 60 <= now)
    .map(b => [b.time, b])).values()].sort((a,b) => a.time-b.time);
  let day,calculationDay, ema, weight=0, mean=0, m2=0, positive=0, opened=false,calculationReady=false,
    calculationInvalid=false,last, warmup=0, priorCount=0;
  let points=[], events=[], episode=null, number=0, previous=null, gap=null;
  // Confirmed TD for coloring and shared entry admission, aligned with indicators/td-price-line.pine defaults.
  // Carry the close history and count across daily VWAP resets.
  let tdCount=0, tdCounts=[]; const tdCloses=[];
  for (const bar of input) {
    const s = stockSession(bar.time,marketSession);
    if (!beta && !s.active) continue;
    const anchor=beta?anchors.findLast(d=>d.time<=bar.time):null;
    const key=beta?(anchor?.time??'missing_anchor'):s.day;
    if (key !== calculationDay) {
      calculationDay=key;weight=mean=m2=positive=0;calculationInvalid=false;
      calculationReady=beta?!!(anchor&&(bar.time===anchor.time||input[0].time<anchor.time))
        :hasSessionPrefix(input[0]?.time,bar.time,marketSession);
    }
    const reference=tdCloses.length===4?tdCloses[0]:null;
    tdCount=!Number.isFinite(bar.close)||!Number.isFinite(reference)?0
      :bar.close>reference?(tdCount>0?tdCount+1:1)
      :bar.close<reference?(tdCount<0?tdCount-1:-1):0;
    tdCloses.push(bar.close);if(tdCloses.length>4)tdCloses.shift();
    if (!valid(bar)){calculationInvalid=true;continue;}
    const warmBefore=priorCount;
    priorCount++;
    ema = ema == null ? bar.close : ema + .5*(bar.close-ema);
    if (bar.volume > 0) {
      const value=(bar.high+bar.low+bar.close)/3, delta=value-mean;
      weight+=bar.volume; mean+=delta*bar.volume/weight; m2+=bar.volume*delta*(value-mean); positive++;
      if (positive===1) {mean=value; m2=0;}
    }
    const sigma=positive >= 2 && weight>0 ? Math.sqrt(Math.max(0,m2/weight)) : 0;
    // Warm indicators on the entire extended session; stock ranking episodes
    // and their displayed clock still start at the regular open.
    if(!beta&&!s.regular)continue;
    if(key!==day){
      day=key;opened=false;last=null;warmup=warmBefore;
      if(!beta){points=[];events=[];tdCounts=[];}
      episode=previous=null;number=0;gap=null;
    }
    if(beta?calculationReady:s.minute===570)opened=true;
    if(!opened||!calculationReady||calculationInvalid||(!beta&&last!=null&&bar.time!==last+60)){
      gap ||= !calculationReady?'missing_session_anchor':!opened?'missing_open':calculationInvalid?'invalid_bar':'minute_gap';
      episode=previous=null;
    }
    last=bar.time;
    const z=!gap && sigma>Math.max(1e-10,Math.abs(mean)*1e-12) ? (ema-mean)/sigma : null;
    const point={time:bar.time,ema3:ema,vwap:gap||!weight?null:mean,sigma:gap?null:sigma,
      open:bar.open,high:bar.high,low:bar.low,close:bar.close,z};
    if (episode && z != null && episode.direction*z < 0) {
      events.push({time:bar.time,known_at:bar.time+60,type:'crossed_vwap',direction:episode.direction});
      episode=null;
    }
    if (z == null) {episode=null; previous=null;}
    else {
      if (!episode) {
        const direction=previous == null ? (z>1?1:z<-1?-1:0)
          : previous.z<=1&&z>1 ? 1 : previous.z>=-1&&z<-1 ? -1 : 0;
        if (direction) {
          episode={id:`${symbol}/${day}/${++number}`, direction, points:[],outside:0,peak:null,drawdown:0};
          events.push({time:bar.time,known_at:bar.time+60,type:previous?'breakout':'first_outside',direction});
        }
      }
      if (episode) {
        const prev=episode.points.at(-1), d=episode.direction;
        if (prev && d*prev.z>1 && d*z<=1) events.push({time:bar.time,known_at:bar.time+60,type:'pullback',direction:d});
        if (prev && d*prev.z<=1 && d*z>1) events.push({time:bar.time,known_at:bar.time+60,type:'reexpansion',direction:d});
        episode.points.push(point); episode.outside+=d*z>1;
        episode.peak=episode.peak==null?d*ema:Math.max(episode.peak,d*ema);
        episode.drawdown=Math.max(episode.drawdown,episode.peak-d*ema);
      }
      previous=point;
    }
    const phase=gap?'data_gap':z==null?'warming':!episode?'waiting':z===0?'touch_vwap':
      episode.points.length<3?'new_breakout':episode.direction*z>1?'outside_progress':'pullback_inside';
    points.push([point.time,...['ema3','vwap','sigma','open','high','low','close','z'].map(k=>round(point[k])),phase,episode?.direction||0]);
    tdCounts.push([point.time,tdCount]);
  }
  let metrics=null;
  if (episode && !gap && warmup>=30) {
    const ps=episode.points,p=ps.at(-1),first=ps[0],d=episode.direction;
    const ref=ps.find(x=>x.time===Math.max(first.time,p.time-180)), a=ps.find(x=>x.time===p.time-180),b=ps.find(x=>x.time===p.time-360);
    const dt=(p.time-ref.time)/60, n=ps.length;
    metrics={episode_id:episode.id,direction_sign:d,episode_start_bar:first.time,breakout_known_at:first.time+60,
      age_points:n,outer_fraction:episode.outside/n,confidence:Math.min(n/10,1),
      rail_speed:dt?d*(p.vwap-ref.vwap)/(dt*p.sigma):0,
      rail_acceleration:a&&b?d*(p.vwap-2*a.vwap+b.vwap)/(9*p.sigma):null,
      maximum_drawdown_sigma:episode.drawdown/p.sigma,drawdown_quality:1/(1+episode.drawdown/p.sigma),
      z:p.z,phase:points.at(-1)[9]};
  }
  return {symbol,day,points,events:beta?events:events.slice(-48),metrics,warmup,
    td:{version:1,comparison_bars:4,trend_threshold:3,counts:tdCounts},
    issue:gap || ((beta?priorCount:warmup)<30?'ema_warmup':null),last_closed:points.at(-1)?.[0]??null};
}
function percentile(value,values) {
  if (!(value>0) || !values.length) return 0;
  if (values.length<2) return .5;
  const equal=x=>Math.abs(x-value)<=Math.max(1e-12,1e-12*Math.max(Math.abs(x),Math.abs(value)));
  const less=values.filter(x=>x<value&&!equal(x)).length, ties=values.filter(equal).length;
  return Math.min(1,Math.max(0,(less+Math.max(0,ties-1)/2)/(values.length-1)));
}
function referenceEvents(row) {
  const range=row.previous_range;
  if (!BETA.includes(row.symbol)||!range) return row.events;
  const events=row.events.filter(e=>!e.type.startsWith('prior_'));
  for(const [side,index] of [['high',5],['low',6]]) {
    const level=range[side]; if(!Number.isFinite(level))continue;
    let previousNear=false;
    for(const p of row.points) {
      if(!(p[3]>0)) {previousNear=false;continue;}
      const touch=p[6]<=level&&p[5]>=level;
      const near=touch||Math.abs(p[index]-level)<=p[3]*.1;
      if(near&&!previousNear) events.push({time:p[0],known_at:p[0]+60,
        type:'prior_'+side+(touch?'_touch':'_near'),level});
      previousNear=near;
    }
  }
  return events.sort((a,b)=>a.time-b.time||a.type.localeCompare(b.type)).slice(-48);
}
function buildFrame(series, symbols, now, fiveMinute={}) {
  const all=Object.values(series), stocks=all.filter(s=>!BETA.includes(s.symbol));
  const day=stocks.map(s=>s.day).filter(Boolean).sort().at(-1);
  const current=stocks.filter(s=>s.day===day);
  const asOf=Math.max(0,...(current.length?current:all).map(s=>(s.last_closed||0)+60));
  // Always-live indices cannot advance the stock ranking clock overnight.
  // Project their fully calculated default-session history onto the same cutoff.
  for(const row of all.filter(s=>BETA.includes(s.symbol))) {
    const points=row.points.filter(p=>p[0]+60<=asOf&&p[0]>=asOf-36*3600);
    const issue=points.at(-1)?.[2]==null?'missing_session_anchor':null;
    const td=row.td?{...row.td,counts:row.td.counts.filter(p=>p[0]+60<=asOf&&p[0]>=asOf-36*3600)}:undefined;
    current.push({...row,day,points,td,events:row.events.filter(e=>e.time+60<=asOf),metrics:null,
      issue,last_closed:points.at(-1)?.[0]??null});
  }
  const fresh=current.filter(s=>s.last_closed+60===asOf), candidates=[],ranks=[],pending=[];
  for (const row of fresh) if (row.metrics && !REFERENCES.has(row.symbol)) {
    const facts=scoreInputs(row.points,fiveMinute[row.symbol],row.metrics.direction_sign,asOf);
    if(!facts.ready){pending.push({symbol:row.symbol,reason:facts.reason});continue;}
    const point=row.points.at(-1),price=point[7],vwap=point[2],ema=facts.ema9_5m;
    const direction=price>vwap&&price>ema?1:price<vwap&&price<ema?-1:0;
    // Keep the breakout episode for continuity, but only rank a confirmed price
    // on the same side of both references. Mixed/equal positions are neutral.
    if(direction!==row.metrics.direction_sign)continue;
    candidates.push({symbol:row.symbol,...row.metrics,direction_price:price,direction_vwap:vwap,facts});
  }
  const speeds=candidates.map(r=>Math.max(0,r.rail_speed)), accelerations=candidates.map(r=>Math.max(0,r.rail_acceleration||0));
  for (const {facts,...row} of candidates) {
    row.rail_component=.75*percentile(Math.max(0,row.rail_speed),speeds)+.25*percentile(Math.max(0,row.rail_acceleration||0),accelerations);
    const base=100*(.4*row.outer_fraction+.2*row.confidence+.2*row.rail_component+.2*row.drawdown_quality);
    ranks.push({...row,...applyScore(base,facts)});
  }
  ranks.sort(compareScores);
  ranks.forEach((r,i)=>r.rank=i+1);
  return {version:1,generated_at:now,as_of:asOf,day,columns:COLUMNS,beta:BETA,ranks:ranks.slice(0,10),
    scoring:{...SCORE_CONTRACT,pending,eligible:candidates.length+pending.length,ready:ranks.length},
    coverage:{received:current.length,aligned:fresh.length,ready:fresh.filter(s=>!s.issue).length,total:symbols.length,
      missing:symbols.filter(s=>!fresh.some(row=>row.symbol===s)),
      excluded:fresh.filter(s=>s.issue).map(s=>({symbol:s.symbol,reason:s.issue}))},
    series:Object.fromEntries(current.map(s=>[s.symbol,{...s,metrics:undefined,events:referenceEvents(s)}]))};
}
function createMarketRhythm({symbols,cachePath,marketSession=STOCK_SESSION,onFrame=null}) {
  const series={},signatures=new Map(),fiveMinute={},fiveKeys=new Map(); let dirty=false,busy=false,changedAt=0;
  function changed(now){if(!dirty)changedAt=now;dirty=true;}
  return {
    observeFiveMinute(symbol,periods,atrByTime,now){
      if(REFERENCES.has(symbol))return;
      // Overnight/pre-open, 1m RTH still refers to the previous close. Keep
      // the matching history rather than the newest overnight32 bars.
      const cutoff=Math.min(now,(series[symbol]?.last_closed??now-60)+60);
      const closed=(periods||[]).filter(p=>p.time+300<=cutoff);
      const key=JSON.stringify([closed.map(p=>[p.time,p.close]),closed.slice(0,32).map(p=>[p.time,atrByTime?.get(p.time)??null])]);
      if(fiveKeys.get(symbol)===key)return;
      fiveKeys.set(symbol,key);fiveMinute[symbol]=fiveMinuteHistory(periods,atrByTime,cutoff);changed(now);
    },
    observe(symbol,bars,now,previousRange=null,daily=[]) {
      const closed=bars.filter(b=>b.time+60<=now);
      const signature=closed.map(b=>[b.time,b.open,b.high,b.low,b.close,b.volume].join(',')).join(';')+JSON.stringify([previousRange,daily.map(b=>b.time)]);
      if (!closed.length || signatures.get(symbol)===signature) return;
      signatures.set(symbol,signature);
      series[symbol]={...calculateSeries(symbol,closed,now,daily,{marketSession}),previous_range:previousRange,
        source:{interval_seconds:60,session:BETA.includes(symbol)?'default':marketSession,
          last_bar_time:bars.at(-1)?.time??null,last_closed_bar_time:closed.at(-1)?.time??null,
          received_at:now,bar_count:bars.length}};
      changed(now);
    },
    async flush(now) {
      if (!dirty||busy||!cachePath||now-changedAt<3) return;
      busy=true; dirty=false;
      try {
        const frame=buildFrame(series,symbols,now,fiveMinute);
        // Research is a side channel; it cannot change ranking or prevent publishing.
        try{const id=onFrame?.(frame);if(id)frame.ranking_batch_id=id;}catch{}
        await fs.mkdir(path.dirname(cachePath),{recursive:true});
        const temporary=cachePath+'.'+process.pid+'.tmp';
        await fs.writeFile(temporary,JSON.stringify(frame),{mode:0o600});
        await fs.rename(temporary,cachePath);
      } catch(error) {dirty=true; throw error;} finally {busy=false;}
    },
    reset(symbol) {signatures.delete(symbol);fiveKeys.delete(symbol);delete fiveMinute[symbol];delete series[symbol];dirty=true;},
  };
}
module.exports={BETA,COLUMNS,REFERENCES,calculateSeries,buildFrame,createMarketRhythm,session,minuteSubscriptionOptions};
