'use strict';

// Read-only perception model. Never imports an execution model or broker adapter.
const fs = require('node:fs/promises');
const path = require('node:path');
const {SCORE_CONTRACT,fiveMinuteHistory,fiveMinuteEma5History,scoreTrend,compareScores,rankingViews}=require('./rhythm_score');
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
  let displayPhase=null;
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
    // Premarket observation is a separate segment. Keep the original regular
    // session algorithm at 09:30; EMA/TD/VWAP themselves stay continuously warm.
    if(!beta&&s.minute>=960)continue;
    const phase=beta?'default':s.minute<570?'premarket':'regular';
    if(key!==day||!beta&&phase!==displayPhase){
      day=key;opened=false;last=null;warmup=warmBefore;
      displayPhase=phase;
      if(!beta){points=[];events=[];tdCounts=[];}
      episode=previous=null;number=0;gap=null;
    }
    if(beta||phase==='premarket'?calculationReady:s.minute===570)opened=true;
    if(!opened||!calculationReady||calculationInvalid||(!beta&&phase==='regular'&&last!=null&&bar.time!==last+60)){
      gap ||= !calculationReady?'missing_session_anchor':!opened?'missing_open':calculationInvalid?'invalid_bar':'minute_gap';
      episode=previous=null;
    }
    last=bar.time;
    const z=!gap && sigma>Math.max(1e-10,Math.abs(mean)*1e-12) ? (bar.close-mean)/sigma : null;
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
    const pointPhase=gap?'data_gap':z==null?'warming':!episode?'waiting':z===0?'touch_vwap':
      episode.points.length<3?'new_breakout':episode.direction*z>1?'outside_progress':'pullback_inside';
    points.push([point.time,...['ema3','vwap','sigma','open','high','low','close','z'].map(k=>round(point[k])),pointPhase,episode?.direction||0]);
    tdCounts.push([point.time,tdCount]);
  }
  let metrics=null;
  if (episode && !gap && (displayPhase==='premarket'?priorCount:warmup)>=30) {
    const ps=episode.points,p=ps.at(-1),first=ps[0],d=episode.direction;
    const ref=ps.find(x=>x.time===Math.max(first.time,p.time-180)), a=ps.find(x=>x.time===p.time-180),b=ps.find(x=>x.time===p.time-360);
    const dt=ref?(p.time-ref.time)/60:0, n=ps.length;
    metrics={episode_id:episode.id,direction_sign:d,episode_start_bar:first.time,breakout_known_at:first.time+60,
      age_points:n,outer_fraction:episode.outside/n,confidence:Math.min(n/10,1),
      rail_speed:dt?d*(p.vwap-ref.vwap)/(dt*p.sigma):0,
      rail_acceleration:a&&b?d*(p.vwap-2*a.vwap+b.vwap)/(9*p.sigma):null,
      maximum_drawdown_sigma:episode.drawdown/p.sigma,drawdown_quality:1/(1+episode.drawdown/p.sigma),
      z:p.z,phase:points.at(-1)[9]};
  }
  return {symbol,day,ranking_phase:displayPhase,points,events:beta?events:events.slice(-48),metrics,warmup,
    td:{version:1,comparison_bars:4,trend_threshold:3,counts:tdCounts},
    issue:gap,last_closed:points.at(-1)?.[0]??null};
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
function buildFrame(series, symbols, now, fiveMinute={}, {marketSession=STOCK_SESSION,openingPositions={}}={}) {
  const all=Object.values(series), stocks=all.filter(s=>!BETA.includes(s.symbol));
  const day=stocks.map(s=>s.day).filter(Boolean).sort().at(-1);
  const current=stocks.filter(s=>s.day===day);
  const asOf=Math.max(0,...(current.length?current:all).map(s=>(s.last_closed||0)+60));
  const premarket=asOf>0&&stockSession(asOf-60,marketSession).active&&!stockSession(asOf-60,marketSession).regular
    &&stockSession(asOf-60,marketSession).minute<570;
  // Always-live indices cannot advance the stock ranking clock overnight.
  // Project their fully calculated default-session history onto the same cutoff.
  for(const row of all.filter(s=>BETA.includes(s.symbol))) {
    const points=row.points.filter(p=>p[0]+60<=asOf&&p[0]>=asOf-36*3600);
    const issue=points.at(-1)?.[2]==null?'missing_session_anchor':null;
    const ema=fiveMinute[row.symbol]?.find(p=>p.known_at===Math.floor(asOf/300)*300);
    const betaEma=ema&&ema.ema9>0?{version:1,period:9,interval_seconds:300,
      time:ema.time,known_at:ema.known_at,value:round(ema.ema9)}:null;
    const td=row.td?{...row.td,counts:row.td.counts.filter(p=>p[0]+60<=asOf&&p[0]>=asOf-36*3600)}:undefined;
    current.push({...row,day,points,td,beta_ema9:betaEma,events:row.events.filter(e=>e.time+60<=asOf),metrics:null,
      issue,last_closed:points.at(-1)?.[0]??null});
  }
  const fresh=current.filter(s=>s.last_closed+60===asOf), candidates=[],ranks=[],pending=[];
  for(const row of fresh){
    if(REFERENCES.has(row.symbol))continue;
    const facts=scoreTrend(row,openingPositions[row.symbol],asOf);
    if(facts?.ready)candidates.push({symbol:row.symbol,...facts});
    else if(facts)pending.push({symbol:row.symbol,reason:facts.reason,...(facts.completed_minutes!=null?{completed_minutes:facts.completed_minutes}:{})});
  }
  ranks.push(...candidates);
  ranks.sort(compareScores);
  ranks.forEach((r,i)=>r.rank=i+1);
  const rankings=rankingViews(ranks);
  return {version:1,generated_at:now,as_of:asOf,day,columns:COLUMNS,beta:BETA,ranks:rankings.mixed,rankings,
    universe:[...symbols],
    ranking_session:{version:1,phase:premarket?'premarket':'regular',
      cohort:premarket?'available':'complete',ready_after:premarket?asOf+10:asOf},
    scoring:{...SCORE_CONTRACT,stock_session:marketSession,pending,eligible:candidates.length+pending.length,ready:ranks.length},
    coverage:{received:current.length,aligned:fresh.length,ready:fresh.filter(s=>!s.issue).length,total:symbols.length,
      missing:symbols.filter(s=>!fresh.some(row=>row.symbol===s)),
      excluded:fresh.filter(s=>s.issue).map(s=>({symbol:s.symbol,reason:s.issue}))},
    series:Object.fromEntries(current.map(s=>[s.symbol,{...s,metrics:undefined,events:referenceEvents(s)}]))};
}
function createMarketRhythm({symbols,cachePath,marketSession=STOCK_SESSION,onFrame=null,onError=()=>{},clock=()=>Date.now()/1000}) {
  const series={},signatures=new Map(),openingPositions={},openingKeys=new Map(),fiveMinute={},fiveKeys=new Map(),published=new Map(); let dirty=false,busy=false,changedAt=0;
  function changed(now){if(!dirty)changedAt=now;dirty=true;}
  return {
    observeOpeningPosition(symbol,output,now){
      if(REFERENCES.has(symbol))return;
      const key=JSON.stringify(output??null);if(openingKeys.get(symbol)===key)return;
      openingKeys.set(symbol,key);openingPositions[symbol]=output??null;changed(now);
    },
    observeFiveMinute(symbol,periods,atrByTime,now,ema5ByTime=new Map()){
      if(!BETA.includes(symbol))return; // Five-minute EMA/ATR remain index observation only, never stock ranking inputs.
      // Overnight/pre-open, 1m RTH still refers to the previous close. Keep
      // the matching history rather than the newest overnight32 bars.
      const cutoff=Math.min(now,(series[symbol]?.last_closed??now-60)+60);
      const closed=(periods||[]).filter(p=>p.time+300<=cutoff);
      const key=JSON.stringify([closed.map(p=>[p.time,p.close]),closed.slice(0,32).map(p=>[p.time,atrByTime?.get(p.time)??null,ema5ByTime?.get(p.time)??null])]);
      if(fiveKeys.get(symbol)===key)return;
      fiveKeys.set(symbol,key);fiveMinute[symbol]=BETA.includes(symbol)?fiveMinuteHistory(periods,atrByTime,cutoff):fiveMinuteEma5History(periods,ema5ByTime,cutoff);changed(now);
    },
    observe(symbol,bars,now,previousRange=null,daily=[],receivedAt=now) {
      const closed=bars.filter(b=>b.time+60<=now);
      const signature=closed.map(b=>[b.time,b.open,b.high,b.low,b.close,b.volume].join(',')).join(';')+JSON.stringify([previousRange,daily.map(b=>b.time)]);
      if (!closed.length || signatures.get(symbol)===signature) return;
      signatures.set(symbol,signature);
      try {series[symbol]={...calculateSeries(symbol,closed,now,daily,{marketSession}),previous_range:previousRange,
        source:{interval_seconds:60,session:BETA.includes(symbol)?'default':marketSession,
          last_bar_time:bars.at(-1)?.time??null,last_closed_bar_time:closed.at(-1)?.time??null,
          received_at:receivedAt,computed_at:clock(),bar_count:bars.length}};
      }catch(error){
        // A perception failure must not terminate candle ingestion for every
        // stock. Remove this row instead of passing its old rank as fresh data.
        delete series[symbol];
        try{onError({symbol,message:String(error.message||error)});}catch{}
      }
      changed(now);
    },
    confirmedState(symbol, now) {
      const row=series[symbol],last=row?.td?.counts?.at(-1),point=row?.points?.at(-1);
      if(!row||!last||!point||last[0]!==row.last_closed||point[0]!==last[0]
          ||now-(last[0]+60)<0||now-(last[0]+60)>105)return null;
      const key=JSON.stringify([last,point.slice(1,4),point[7],row.issue,row.source.computed_at]);
      if(published.get(symbol)?.key!==key)published.set(symbol,{key,at:clock()});
      return {version:1,symbol,time:last[0],td_count:last[1],comparison_bars:4,interval_seconds:60,
        session:row.source.session,issue:row.issue||null,close:point[7],ema3:point[1],vwap:point[2],sigma:point[3],
        received_at:row.source.received_at,computed_at:row.source.computed_at,published_at:published.get(symbol).at};
    },
    confirmedTd(symbol, now) {
      // Reuse the same confirmed series as the chart/ranking; independent of
      // cross-sectional scoring completeness and the stock still being ranked.
      const row=series[symbol],last=row?.td?.counts?.at(-1);
      if(!row||row.issue||!last||last[0]!==row.last_closed
          ||now-(last[0]+60)<0||now-(last[0]+60)>105)return null;
      return {version:1,time:last[0],count:last[1],comparison_bars:4,
        interval_seconds:60,session:row.source.session};
    },
    async flush(now) {
      if (!dirty||busy||!cachePath||now-changedAt<3) return;
      busy=true; dirty=false;
      try {
        const frame=buildFrame(series,symbols,now,fiveMinute,{marketSession,openingPositions});
        // Give simultaneous subscriptions ten seconds to coalesce. No per-stock
        // publication, and no frozen batch containing only the first arrival.
        if(frame.ranking_session.phase==='premarket'&&now<frame.ranking_session.ready_after){
          dirty=true;return;
        }
        // Research is a side channel; it cannot change ranking or prevent publishing.
        try{const id=onFrame?.(frame);if(id)frame.ranking_batch_id=id;}catch{}
        await fs.mkdir(path.dirname(cachePath),{recursive:true});
        const temporary=cachePath+'.'+process.pid+'.tmp';
        await fs.writeFile(temporary,JSON.stringify(frame),{mode:0o600});
        await fs.rename(temporary,cachePath);
      } catch(error) {dirty=true; throw error;} finally {busy=false;}
    },
    reset(symbol) {delete openingPositions[symbol];openingKeys.delete(symbol);signatures.delete(symbol);fiveKeys.delete(symbol);published.delete(symbol);delete fiveMinute[symbol];delete series[symbol];dirty=true;},
  };
}
module.exports={BETA,COLUMNS,REFERENCES,calculateSeries,buildFrame,createMarketRhythm,session,minuteSubscriptionOptions};
