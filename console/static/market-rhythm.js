'use strict';

const RHYTHM_LABELS = {warming:'指标预热',waiting:'等待突破',new_breakout:'新突破',
  outside_progress:'外轨推进',pullback_inside:'回踩轨道内',touch_vwap:'触及VWAP',data_gap:'数据缺口'};
const RHYTHM_EVENTS = {breakout:'突破外轨',first_outside:'首次有效值在外轨',pullback:'回踩轨道内',
  reexpansion:'恢复外轨推进',crossed_vwap:'越过VWAP，本段结束',
  prior_low_touch:'触及昨低',prior_low_near:'昨低附近',prior_high_touch:'触及昨高',prior_high_near:'昨高附近'};
const RHYTHM_ISSUES = {minute_gap:'历史分钟缺口',missing_session_anchor:'缺少默认交易日锚点',missing_open:'缺少开盘分钟',invalid_bar:'K线无效',ema_warmup:'预热不足',missing_data:'数据待齐'};
const rhythmTimeFormat = new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const rhythmSessionClock = new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
function rhythmSessionState(asOf, now, rankingSession=null) {
  const p=Object.fromEntries(rhythmSessionClock.formatToParts(new Date(now*1000)).map(p=>[p.type,p.value]));
  const minute=+p.hour*60 + +p.minute;
  if(rankingSession?.phase==='premarket')return !['Sat','Sun'].includes(p.weekday)&&minute>=240&&minute<570
    ? now-asOf>120?'stale':'premarket':'history';
  if(['Sat','Sun'].includes(p.weekday)||minute<570||minute>=960)return 'history';
  return now-asOf>120?'stale':'confirmed';
}
function rhythmEntryWindow(now){
  if(!Number.isFinite(now))return false;
  const p=Object.fromEntries(rhythmSessionClock.formatToParts(new Date(now*1000)).map(p=>[p.type,p.value]));
  const minute=+p.hour*60 + +p.minute;
  return !['Sat','Sun'].includes(p.weekday)&&minute>=585&&minute<960;
}
const rhythmTime = t => Number.isFinite(t)&&t>0 ? rhythmTimeFormat.format(new Date(t*1000)) : '—';
const rhythmTicker = s => (s||'').split(':').pop();
const rhythmNumber = n => Number.isFinite(n) ? n.toFixed(2) : '—';
const rhythmSigned = n => Number.isFinite(n) ? (n>0?'+':'')+n.toFixed(2) : '—';
const rhythmTdState = count => count==null?'unknown':count>=3?'up':count<=-3?'down':'range';
function rhythmTdFact(fact,now) {
  const known=!!(fact?.version===1&&fact.source==='confirmed_minute_v1'&&fact.known===true
    &&typeof fact.id==='string'&&fact.id&&Number.isSafeInteger(fact.count)
    &&Number.isFinite(fact.bar_time)&&fact.bar_time%60===0&&fact.confirmed_at===fact.bar_time+60&&fact.confirmed_at<=now);
  const fresh=!!(known&&fact.ready===true&&Number.isFinite(fact.observed_at)&&fact.quote_bar_time===fact.confirmed_at
    &&0<=now-fact.quote_bar_time&&now-fact.quote_bar_time<60
    &&fact.quote_bar_time<=fact.observed_at&&fact.observed_at<=now&&now-fact.observed_at<=45);
  return {...fact,known,ready:fresh,count:known?fact.count:null,
    reason:fresh?'':fact?.reason||'等待本标的有效的一分钟确认TD与新鲜行情'};
}

function rhythmVwapFact(fact,now){
  const known=!!(fact?.version===1&&fact.source==='confirmed_vwap_direction_v1'&&fact.known===true
    &&typeof fact.id==='string'&&fact.id&&Number.isFinite(fact.close)&&fact.close>0&&Number.isFinite(fact.vwap)&&fact.vwap>0
    &&fact.direction===(fact.close>fact.vwap?1:fact.close<fact.vwap?-1:0)
    &&Number.isFinite(fact.bar_time)&&fact.bar_time%60===0&&fact.confirmed_at===fact.bar_time+60&&fact.confirmed_at<=now);
  const ready=!!(known&&fact.ready===true&&Number.isFinite(fact.observed_at)&&fact.quote_bar_time===fact.confirmed_at
    &&0<=now-fact.quote_bar_time&&now-fact.quote_bar_time<60
    &&fact.quote_bar_time<=fact.observed_at&&fact.observed_at<=now&&now-fact.observed_at<=45);
  return {...fact,known,ready,direction:known?fact.direction:0,
    reason:ready?'':fact?.reason||'等待个股最新收盘价、VWAP与新鲜报价'};
}

function rhythmPositions(lots=[], universe=[]) {
  const pool=[...new Set(universe.filter(s=>typeof s==='string'))],byTicker=new Map();
  for(const symbol of pool){const ticker=rhythmTicker(symbol);byTicker.set(ticker,[...(byTicker.get(ticker)||[]),symbol]);}
  const grouped=new Map();
  for(const lot of lots) {
    if(!['ACTIVE','CLOSING','PENDING'].includes(lot.state)||!Number.isFinite(lot.filled_qty)||lot.filled_qty<=0
      ||typeof lot.symbol!=='string'||!['long','short'].includes(lot.side))continue;
    let symbol=lot.symbol.trim().toUpperCase();if(!symbol)continue;
    const matches=byTicker.get(rhythmTicker(symbol))||[];
    if(!symbol.includes(':')||['BATS','NASDAQ','NYSE','AMEX'].includes(symbol.split(':')[0])) {
      const stock=matches.find(s=>['NASDAQ','NYSE','AMEX'].includes(s.split(':')[0]));
      if(stock)symbol=stock;
      else if(!pool.includes(symbol)&&matches.length===1)symbol=matches[0];
    }
    if(!symbol.includes(':'))continue; // Unresolved tickers cannot select a TV data source safely.
    const stamp=[lot.entry_broker_time,lot.entry_time,lot.created_at].find(t=>Number.isFinite(t)&&t>0)??Infinity;
    const row=grouped.get(symbol)||{symbol,qty:0,longQty:0,shortQty:0,openedAt:stamp};
    row.qty+=lot.filled_qty;row[lot.side==='long'?'longQty':'shortQty']+=lot.filled_qty;
    row.openedAt=Math.min(row.openedAt,stamp);grouped.set(symbol,row);
  }
  return [...grouped.values()].sort((a,b)=>a.openedAt-b.openedAt||a.symbol.localeCompare(b.symbol))
    .map(p=>({...p,side:p.longQty&&p.shortQty?'mixed':p.longQty?'long':'short'}));
}

// Remind once per wall-clock minute using the latest accepted mixed board.
function rhythmHoldingRankChanges(previous,current,lots,now) {
  const incoming=current?.snapshot,latest=previous?.board?.snapshot;
  const accepted=incoming?.version===1&&incoming.id&&incoming.day
    &&Number.isSafeInteger(incoming.as_of)&&incoming.as_of>0&&incoming.as_of<=now
    &&Array.isArray(current.top10)&&(!latest||incoming.as_of>latest.as_of);
  const board=accepted?current:previous?.board;
  if(!board||!Number.isFinite(now))return {state:previous,outside:[],remind:false};
  const top10=new Set(board.top10);
  const known=new Set([...top10,...Object.keys(board.positions||{}),...(previous?.heldKnown||[])]);
  const heldKnown=rhythmPositions(lots,[...known]).map(p=>p.symbol).filter(symbol=>known.has(symbol));
  const outside=heldKnown.filter(symbol=>!top10.has(symbol))
    .map(symbol=>({symbol,rank:board.positions?.[symbol]??null}));
  const minute=Math.floor(now/60),lastReminder=previous?.lastReminder??-1;
  const remind=outside.length>0&&minute>lastReminder;
  return {state:{board,heldKnown,lastReminder:remind?minute:lastReminder},outside,remind,
    stale:now-board.snapshot.as_of>=60};
}

// A partial minute is not evidence that a stock has left the cross-section.
function rhythmRankingReady(frame) {
  const c=frame?.coverage;
  if(frame?.ranking_session?.phase==='premarket'&&frame.ranking_session.cohort==='available')
    return Number.isFinite(frame.as_of)&&frame.as_of>0&&frame.as_of%60===0
      &&frame.ranking_snapshot?.version===1&&!!frame.ranking_snapshot.id
      &&frame.ranking_snapshot.as_of===frame.as_of&&frame.ranking_snapshot.day===frame.day
      &&Number.isSafeInteger(c?.total)&&c.total>0&&Number.isSafeInteger(c.aligned)&&c.aligned>0
      &&c.aligned<=c.total&&Array.isArray(c.missing)&&c.missing.length===c.total-c.aligned;
  return Number.isFinite(frame?.as_of)&&frame.as_of>0&&frame.as_of%60===0
    &&!!c&&Number.isSafeInteger(c.total)&&c.total>0&&c.aligned===c.total
    &&Array.isArray(c.missing)&&c.missing.length===0;
}

// Presentation slots only. Raw ranks and execution admission remain independent.
function rhythmDisplaySlots(previous, frame, positions=[], rankingView='mixed') {
  const sourceRows=frame?.rankings?.[rankingView]??(rankingView==='mixed'?frame?.ranks:null);
  const ranked=[...(sourceRows||[])].filter(r=>r?.symbol).sort((a,b)=>a.rank-b.rank).slice(0,10);
  const held=positions.map(p=>p.symbol);
  const day=frame?.day||'',nextBucket=Number.isFinite(frame?.as_of)?Math.floor(frame.as_of/600):0;
  const valid=previous?.version===2&&previous.day===day&&Array.isArray(previous.order)&&Array.isArray(previous.heldOrder)
    &&Array.isArray(previous.referenceSlots)&&previous.referenceSlots.length===2;
  // One accepted, complete, closed-minute snapshot. Later revisions of that minute
  // cannot reshuffle membership while the next candle is forming.
  const accepted=frame?.ranking_snapshot;
  const authoritative=accepted?.version===1&&typeof accepted.id==='string'&&accepted.id
    &&accepted.as_of===frame.as_of&&accepted.day===day;
  const changedView=valid&&(previous.rankingView||'mixed')!==rankingView;
  const ready=rhythmRankingReady(frame)&&Array.isArray(sourceRows);
  if(changedView&&!ready)return previous;
  const accept=ready&&(changedView||(authoritative
    ? !valid||previous.candidateSnapshotId!==accepted.id
    : !valid||!Number.isFinite(previous.candidateAsOf)||frame.as_of>previous.candidateAsOf));
  const eligible=accept?[...new Set(ranked.map(r=>r.symbol))]:valid?
    [...(Array.isArray(previous.candidates)?previous.candidates:previous.order.filter(s=>!previous.heldOrder.includes(s)))]:[];
  const candidateAsOf=accept?frame.as_of:valid?previous.candidateAsOf??null:null;
  const source=new Map([...(frame?.tracks||[]),...(frame?.watch_tracks||[])].map(t=>[t.symbol,t]));
  const candidateRows=accept?ranked.map(r=>({...r,
    tdCount:rhythmConfirmedTd(source.get(r.symbol),frame.as_of-60)})):
    valid&&Array.isArray(previous.candidateRows)?previous.candidateRows:[];
  // Do not consume the ten-minute boundary until its candidate batch is complete.
  const bucket=accept?nextBucket:valid?previous.bucket:null;
  const heldOrder=[...new Set([...(valid?previous.heldOrder:[]).filter(s=>held.includes(s)),...held])];
  // Keep occupied reference cells in place. A flat cell admits the oldest
  // overflow holding before falling back to QQQ / NVDA. Only overflow consumes
  // the ten candidate display seats.
  const referenceSlots=['',''];
  if(valid)previous.referenceSlots.forEach((s,i)=>{if(held.includes(s)&&!referenceSlots.includes(s))referenceSlots[i]=s;});
  const waiting=heldOrder.filter(s=>!referenceSlots.includes(s));
  referenceSlots.forEach((s,i)=>{if(!s)referenceSlots[i]=waiting.shift()||'';});
  const pinned=heldOrder.filter(s=>!referenceSlots.includes(s)).slice(0,10),rest=eligible.filter(s=>!pinned.includes(s));
  let order;
  if(!valid||changedView||previous.bucket!==bucket)order=[...pinned,...rest].slice(0,10);
  else {
    const slots=Array(10).fill('');pinned.forEach((s,i)=>{slots[i]=s;});
    previous.order.slice(0,10).forEach((s,i)=>{if(i>=pinned.length&&rest.includes(s)&&!slots.includes(s))slots[i]=s;});
    const incoming=rest.filter(s=>!slots.includes(s));
    for(let i=pinned.length;i<10;i++)if(!slots[i])slots[i]=incoming.shift()||'';
    order=slots.filter(Boolean);
  }
  const candidateSnapshotId=accept&&authoritative?accepted.id:valid?previous.candidateSnapshotId??null:null;
  return {version:2,day,bucket,rankingView,order,heldOrder,referenceSlots,candidates:eligible,candidateAsOf,candidateSnapshotId,candidateRows};
}

function rhythmConfirmedTd(track,time) {
  const td=track?.td;
  if(td?.version!==1||td.comparison_bars!==4||td.trend_threshold!==3||!Array.isArray(td.counts))return null;
  const rows=td.counts.filter(p=>Array.isArray(p)&&p.length===2&&p[0]===time);
  return rows.length===1&&Number.isSafeInteger(rows[0][1])?rows[0][1]:null;
}

// Display the actual current-minute EMA3 only. Never compute a provisional TD.
function rhythmFormingPoint(track,raw,asOf,now) {
  const bar=raw?.bar,p=track?.points?.at(-1),observed=raw?.price_observed_at;
  if(!track?.aligned||track.issue||!p||!bar||!Number.isFinite(now)||bar.time!==asOf||p[0]+60!==bar.time
    ||!Number.isFinite(observed)||observed<bar.time||observed>now||now-observed>45
    ||!Number.isFinite(p[1])||![bar.time,bar.open,bar.high,bar.low,bar.close].every(Number.isFinite)
    ||bar.time%60!==0||!(bar.time<=now&&now<bar.time+60)||!(0<bar.low&&bar.low<=Math.min(bar.open,bar.close)&&bar.high>=Math.max(bar.open,bar.close))
    ||(!track.is_beta&&rhythmSessionState(asOf,now)!=='confirmed'))return null;
  const ema3=p[1]+.5*(bar.close-p[1]);
  return {time:bar.time,ema3,z:Number.isFinite(p[2])&&p[3]>0?(bar.close-p[2])/p[3]:null};
}

// Stop pricing is delegated to the ordinary market-order policy in app.js.
function rhythmTradePreview(frame,symbol,data,now,stopForSide,entryReference=null) {
  const row=[...(frame?.tracks||[]),...(frame?.watch_tracks||[])].find(t=>t.symbol===symbol&&!t.is_beta);
  const p=row?.points?.at(-1),close=data?.bar?.close,observed=data?.price_observed_at,time=data?.bar?.time;
  const admission=Array.isArray(frame?.rows),candidate=frame?.rows?.find(r=>r.symbol===symbol);
  const pointTime=admission?candidate?.bar_time:p?.[0];
  const direction=admission?candidate?.direction:p?.[10],sigma=admission?candidate?.sigma:p?.[3],side=direction===1?'long':direction===-1?'short':null;
  const aligned=admission?frame.ready&&!!candidate:row?.aligned&&!row.issue;
  const rawAtr=(data?.ts_c||data?.ts||{}).atr;
  const atr=Number.isFinite(rawAtr)&&rawAtr>0?rawAtr:null;
  const reference=entryReference??close;
  const stop=side&&typeof stopForSide==='function'?stopForSide(side,atr):null;
  const ready=!!(aligned&&side&&Number.isFinite(pointTime)&&Number.isFinite(stop)&&Number.isFinite(close)&&close>0
    &&now>=pointTime+60&&now-pointTime-60<=120&&Number.isFinite(observed)&&now>=observed&&now-observed<=45
    &&Number.isFinite(time)&&time<=now&&now<time+300&&Number.isFinite(reference)&&reference>0&&stop>0&&(side==='long'?stop<reference:stop>reference)
    &&rhythmSessionState(pointTime+60,now)!=='history');
  const blockReason=ready?'':!Number.isFinite(pointTime)?'等待候选轨道':admission&&!frame.ready?frame.reason:rhythmSessionState(pointTime+60,now)==='history'
    ?'当前为常规盘历史，仅供查看；等待当日常规盘轨道':now-pointTime-60>120
      ?'轨道确认数据已过期':!side?'当前没有有效趋势方向':!aligned
        ?'轨道数据不完整或未对齐':'等待新鲜价格与有效止损';
  return {side,close,limitPx:reference??null,stopPx:stop,atr,sigma,ready,blockReason};
}

function rhythmChartModel(track,start,end,view,width,height,zDomain,focus,showTime=true) {
  const rows=(track?.points||[]).filter(p=>p[0]>=start&&p[0]<=end);
  const left=7,right=46,top=4,bottom=showTime?18:4;
  const w=Math.max(width,200),h=Math.max(height,32),pw=w-left-right,ph=h-top-bottom;
  const normalized=view==='position', value=p=>normalized?p[8]:p[1];
  const live=track?.forming,liveValue=normalized?live?.z:live?.ema3;
  const livePoint=live&&live.time>=start&&live.time<=end&&Number.isFinite(liveValue)&&rows.at(-1)?.[0]+60===live.time?live:null;
  let low,high;
  if(normalized) [low,high]=zDomain;
  else {
    const vals=rows.flatMap(p=>[p[1],p[2]!=null&&p[3]!=null?p[2]+p[3]:null,p[2]!=null&&p[3]!=null?p[2]-p[3]:null,
      ...(track?.is_beta?[p[5],p[6]]:[])]).filter(Number.isFinite);
    if(livePoint)vals.push(liveValue);
    low=Math.min(...vals);high=Math.max(...vals);
    if(!vals.length) {low=0;high=1;}
    const prior=track?.previous_range;
    if(prior) for(const v of [prior.high,prior.low]) if(v>=low-(high-low)*.2&&v<=high+(high-low)*.2) {low=Math.min(low,v);high=Math.max(high,v);}
    const pad=(high-low)*.1||1;low-=pad;high+=pad;
  }
  const x=t=>left+(t-start)/Math.max(60,end-start)*pw, y=v=>top+(high-v)/(high-low)*ph;
  const path=get=>{let d='',prior=null;for(const p of rows){const v=get(p);if(!Number.isFinite(v)){prior=null;continue;}
    d+=(prior!=null&&p[0]-prior===60?'L':'M')+x(p[0]).toFixed(2)+','+y(v).toFixed(2);prior=p[0];}return d;};
  const price=path(value),center=path(p=>normalized?(p[2]==null?null:0):p[2]);
  const td=track?.td;
  const tdCounts=new Map(td?.version===1&&td.comparison_bars===4&&td.trend_threshold===3&&Array.isArray(td.counts)
    ?td.counts.filter(p=>Array.isArray(p)&&p.length===2&&Number.isFinite(p[0])&&Number.isSafeInteger(p[1])):[]);
  const tdState=p=>rhythmTdState(tdCounts.has(p[0])?tdCounts.get(p[0]):null);
  const priceSegments=[];let priorPrice=null,segment=null;
  const coordinate=p=>x(p[0]).toFixed(2)+','+y(value(p)).toFixed(2);
  for(const p of rows){
    if(!Number.isFinite(value(p))){priorPrice=segment=null;continue;}
    const state=tdState(p),continuous=priorPrice&&p[0]-priorPrice[0]===60;
    if(!continuous||segment?.state!==state){
      segment={time:p[0],state,d:'M'+coordinate(continuous?priorPrice:p)};priceSegments.push(segment);
    }
    if(continuous)segment.d+='L'+coordinate(p);
    priorPrice=p;
  }
  const upper=path(p=>p[2]==null||p[3]==null?null:normalized?1:p[2]+p[3]);
  const lower=path(p=>p[2]==null||p[3]==null?null:normalized?-1:p[2]-p[3]);
  const ticks=(normalized?(ph<40?[0]:[-1,0,1]):ph<40?[low,high]:[low,(low+high)/2,high]).map(v=>({y:y(v),label:normalized?(v>0?'+':'')+v:v.toFixed(v>=1000?0:2)}));
  const tickCount=width<340?3:4;
  const times=Array.from({length:tickCount},(_,i)=>{const t=Math.round((start+(end-start)*i/(tickCount-1))/60)*60;return {x:x(t),label:rhythmTime(t),anchor:i===0?'start':i===tickCount-1?'end':'middle'};});
  const selected=rows.find(p=>p[0]===focus),cursor=selected&&Number.isFinite(value(selected))?{x:x(focus),y:y(value(selected))}:null;
  const last=rows.at(-1);
  const forming=livePoint&&Number.isFinite(value(last))?{time:live.time,price:live.ema3,x:x(live.time),y:y(liveValue),
    d:'M'+coordinate(last)+'L'+x(live.time).toFixed(2)+','+y(liveValue).toFixed(2)}:null;
  const tdCount=selected&&tdCounts.has(selected[0])?tdCounts.get(selected[0]):null;
  const levels=normalized?[]:['high','low'].filter(k=>Number.isFinite(track?.previous_range?.[k])&&track.previous_range[k]>=low&&track.previous_range[k]<=high)
    .map(k=>({key:k,y:y(track.previous_range[k]),label:(k==='high'?'昨高 ':'昨低 ')+rhythmNumber(track.previous_range[k])}));
  const marks=(track?.events||[]).filter(e=>e.time>=start&&e.time<=end).slice(-5).flatMap(e=>{
    const p=rows.find(p=>p[0]===e.time);if(!p||!Number.isFinite(value(p)))return [];
    const v=e.type.startsWith('prior_')?(e.type.startsWith('prior_low')?p[6]:p[5]):p[1];
    return [{...e,x:x(e.time),y:y(normalized?(v-p[2])/p[3]:v),label:RHYTHM_EVENTS[e.type]||e.type}];});
  const groups=[];
  for(const p of rows){const label=track?.is_beta?(p[8]==null?'warming':p[8]>1?'上轨外':p[8]<-1?'下轨外':p[8]>=0?'上区间':'下区间'):p[9];
    const last=groups.at(-1);if(last&&last.key===label&&last.end+60===p[0]){last.end=p[0];last.n++;}else groups.push({key:label,start:p[0],end:p[0],n:1});}
  const wicks=track?.is_beta&&!normalized?rows.map(p=>({x:x(p[0]),high:y(p[5]),low:y(p[6])})):[];
  return {rows,w,h,left,right,top,bottom,price,priceSegments,forming,tdCount,tdState:selected?tdState(selected):'unknown',center,upper,lower,ticks,times,selected,cursor,levels,marks,wicks,
    phases:groups.map(g=>({...g,label:RHYTHM_LABELS[g.key]||g.key,width:g.n/Math.max(1,(end-start)/60+1)*100,
      offset:(g.start-start)/Math.max(60,end-start+60)*100})),plotWidth:pw};
}

const RhythmTrack = {
  props:{track:Object,start:Number,end:Number,view:String,focus:Number,locked:Boolean,zDomain:Array,showTime:Boolean,selected:Boolean,target:Boolean},
  emits:['focus','lock','select'],
  data:()=>({width:450,height:108}),
  mounted(){this.observer=new ResizeObserver(([e])=>{this.width=e.contentRect.width;this.height=e.contentRect.height;});this.observer.observe(this.$refs.plot);},
  beforeUnmount(){this.observer?.disconnect();},
  computed:{
    chart(){return rhythmChartModel(this.track,this.start,this.end,this.view,this.width,this.height,this.zDomain,this.focus,this.showTime);},
    direction(){return this.track?.direction_sign||this.track?.points?.at(-1)?.[10]||0;},
    summary(){const p=this.chart.selected,td=this.chart.tdCount;return p?`${rhythmNumber(p[1])} · ${rhythmSigned(p[8])}σ · TD ${td==null?'待加载':td>0?'+'+td:td}`:this.chart.forming?.time===this.focus?`${rhythmNumber(this.chart.forming.price)} · 当前K形成中`:'该分钟无确认数据';},
  },
  methods:{clock:rhythmTime,ticker:rhythmTicker,
    point(event,lock=false){const r=this.$refs.plot.getBoundingClientRect(),c=this.chart;
      const t=Math.round((this.start+(Math.min(1,Math.max(0,(event.clientX-r.left-c.left)/c.plotWidth)))*(this.end-this.start))/60)*60;
      this.$emit(lock?'lock':'focus',t);},
  },
  template:`<section class="rhythm-track" :data-symbol="track?.symbol" :data-slot="track?.slot_number || ''" :class="{'rhythm-unaligned': !track?.aligned,'rhythm-selected':selected,'rhythm-beta':track?.is_beta,'rhythm-target':target}" :aria-label="(ticker(track?.symbol) || '候选 '+(track?.rank||track?.slot_number||''))+'节奏图'">
    <header><button type="button" class="rhythm-track-pick" :disabled="!track?.symbol && !track?.slot_number" :aria-pressed="selected" @click="$emit('select',track.symbol)"><strong :class="{'rhythm-long':direction===1,'rhythm-short':direction===-1}"><span v-if="track?.slot_number" class="rhythm-rank">格{{ track.slot_number }} · </span><span v-if="track?.rank" class="rhythm-rank">#{{ track.rank }} </span>{{ ticker(track?.symbol) || '候选 '+(track?.rank||track?.slot_number||'') }} <small>{{ track?.is_beta ? '大盘' : direction===1 ? '多' : direction===-1 ? '空' : '等待' }}</small></strong></button><span>{{ summary }}</span></header>
    <div v-if="!track?.points?.length" class="rhythm-track-empty">{{ track?.symbol ? '等待该标的一分钟收盘数据' : '暂无有效趋势候选' }}</div>
    <svg v-show="track?.points?.length" ref="plot" class="rhythm-chart" :viewBox="'0 0 '+chart.w+' '+chart.h" role="img"
      :aria-label="ticker(track.symbol)+'，EMA3与VWAP轨道，美东时间'+clock(start)+'至'+clock(end)"
      @pointermove="point($event)" @click="point($event,true)">
      <title>{{ ticker(track.symbol) }} 收盘确认走势；EMA3按TD连续计数着色；光标联动</title>
      <g class="rhythm-grid"><line v-for="tick in chart.ticks" :key="tick.label" :x1="chart.left" :x2="chart.w-chart.right" :y1="tick.y" :y2="tick.y" /></g>
      <g class="rhythm-axis"><text v-for="tick in chart.ticks" :key="tick.label" :x="chart.w-chart.right+5" :y="tick.y+4">{{ tick.label }}</text><text v-for="tick in showTime ? chart.times : []" :key="tick.label" :x="tick.x" :y="chart.h-5" :text-anchor="tick.anchor">{{ tick.label }}</text></g>
      <g class="rhythm-wicks"><line v-for="wick in chart.wicks" :key="wick.x" :x1="wick.x" :x2="wick.x" :y1="wick.high" :y2="wick.low"/></g>
      <path class="rhythm-upper" :d="chart.upper"/><path class="rhythm-lower" :d="chart.lower"/><path class="rhythm-center" :d="chart.center"/>
      <path v-for="segment in chart.priceSegments" :key="segment.time" class="rhythm-price" :class="'rhythm-td-'+segment.state" :d="segment.d"/>
      <g v-for="level in chart.levels" :key="level.key" :class="'rhythm-prior-'+level.key"><line :x1="chart.left" :x2="chart.w-chart.right" :y1="level.y" :y2="level.y"/><text :x="chart.left+3" :y="level.y-4">{{ level.label }}</text></g>
      <g v-for="mark in chart.marks" :key="mark.time+mark.type" class="rhythm-mark"><circle :cx="mark.x" :cy="mark.y" r="3"><title>{{ clock(mark.time) }} {{ mark.label }}</title></circle></g>
      <g v-if="chart.cursor" class="rhythm-cursor"><line :x1="chart.cursor.x" :x2="chart.cursor.x" :y1="chart.top" :y2="chart.h-chart.bottom"/><circle :class="'rhythm-td-'+chart.tdState" :cx="chart.cursor.x" :cy="chart.cursor.y" r="3"/></g>
      <g v-if="chart.forming" class="rhythm-forming"><title>当前一分钟形成中，仅显示实时EMA3，不计算预估TD</title><path :d="chart.forming.d"/><circle :cx="chart.forming.x" :cy="chart.forming.y" r="3.5"/></g>
    </svg>
    <div class="rhythm-phases" :aria-label="ticker(track?.symbol)+'行情阶段'">
      <span v-for="(phase,i) in chart.phases" :key="i" :class="'rhythm-phase-'+phase.key" :style="{left:phase.offset+'%',width:phase.width+'%'}" :title="clock(phase.start)+'–'+clock(phase.end)+' '+phase.label">{{ phase.width * chart.plotWidth / 100 > 72 ? phase.label : '' }}</span>
    </div>
    <small v-if="track?.points?.length && (!track?.aligned || track?.issue)" class="rhythm-track-warning">{{ !track?.aligned ? '该标的数据滞后于共同时间轴' : track.issue === 'ema_warmup' ? '历史预热不足，暂不参与候选排名' : '数据不完整，轨道及排名暂停' }}</small>
  </section>`,
};

const MarketRhythm = {
  components:{RhythmTrack},props:{active:Boolean,tvSyncStatus:String,market:Object,directionSnapshot:{type:Object,default:()=>({version:0,rows:{}})},tdSnapshot:{type:Object,default:()=>({version:0,rows:{}})},now:Number,lots:Array,trader:String,
    positionsReady:{type:Boolean,default:true},rankingSnapshotId:{type:String,default:''}},emits:['select','open','manage','frame','grid'],
  data:()=>({frame:null,error:'',loading:false,selected:'',view:'price',rankingView:'mixed',windowMinutes:390,focus:null,locked:false,clockNow:Date.now()/1000,displayState:null}),
  mounted(){this.requestId=0;this.loadDisplay();if(this.active)this.start();},
  beforeUnmount(){this.stop();},
  watch:{active(value){if(value){this.start();this.publishGrid();}else this.stop();},
    rankingSnapshotId(value){
      if(this.active&&value&&value!==this.frame?.ranking_snapshot?.id)this.refresh(true);
    },
    gridKey(){this.publishGrid();},
    rankingView(){this.reconcileDisplay();},
    positionKey(){this.reconcileDisplay();},
    positionsReady(){this.reconcileDisplay();},
    trader(){this.loadDisplay();this.reconcileDisplay();}},
  computed:{
    sourceTracks(){return Object.fromEntries([...(this.frame?.tracks||[]),...(this.frame?.watch_tracks||[]),...(this.frame?.ranking_tracks||[])].filter(t=>t.symbol).map(t=>[t.symbol,t]));},
    positions(){return rhythmPositions(this.lots||[],[...(this.frame?.symbols||[]),...(this.frame?.ranks||[]).map(r=>r.symbol),...Object.keys(this.market||{})]);},
    positionKey(){return this.positions.map(p=>p.symbol).join(',');},
    positionOverflow(){return Math.max(0,this.positions.length-12);},
    rankingPending(){return !!this.frame&&(this.frame.batch_progress?.waiting||!rhythmRankingReady(this.frame));},
    rankingProgress(){return this.frame?.batch_progress?.coverage||this.frame?.coverage||{};},
    visibleRankingRows(){return this.rankingRows;},
    entryWindowOpen(){return rhythmEntryWindow(this.now??this.clockNow);},
    benchmarkRows(){
      return ['CAPITALCOM:NAS100','CAPITALCOM:SPX500'].map(symbol=>{
        const fact=this.liveTd(symbol),td=fact.count;
        return {symbol,tdCount:td,tdState:rhythmTdState(td),tdLabel:td==null?'TD —':'TD '+(td>0?'+':'')+td,
          confirmedAt:fact.known?fact.confirmed_at:null,
          tdTitle:this.tdTitle(fact)};
      });
    },
    rows(){if(this.positionsReady===false)return [];return (this.displayState?.order||[]).map((symbol,i)=>{
      const raw=(this.displayState?.candidateRows||this.frame?.ranks||[]).find(r=>r.symbol===symbol),position=this.positions.find(p=>p.symbol===symbol);
      const direction=position?(position.side==='long'?1:position.side==='short'?-1:0):raw?.direction_sign;
      const row={...raw,symbol,rank:i+1,candidateRank:raw?.rank??null,isHeld:!!position,position,
        direction_sign:direction};
      const track=this.sourceTracks[symbol],time=(this.displayState?.candidateAsOf||this.frame?.as_of||0)-60;
      const td=raw&&Object.hasOwn(raw,'tdCount')?raw.tdCount:
        track?.points?.some(p=>p[0]===time)?rhythmConfirmedTd(track,time):null;
      return {...row,tdCount:td,tdState:rhythmTdState(td),tdLabel:td==null?'TD —':'TD '+(td>0?'+':'')+td,
        tdTitle:td==null?'等待TD确认数据':rhythmTime(time)+' K线收盘确认 · TD '+(td>0?'+':'')+td};
    });},
    rankingRows(){
      if(this.positionsReady===false)return [];
      const candidates=[...(this.frame?((this.frame.rankings||{})[this.rankingView||'mixed']||this.displayState?.candidateRows||[]):[])].sort((a,b)=>a.rank-b.rank);
      const positions=this.positions||[],held=new Map(positions.map(p=>[p.symbol,p]));
      const bySymbol=new Map(candidates.map(r=>[r.symbol,r]));
      const view=this.rankingView||'mixed',byView=this.frame?.rank_positions||{};
      const actualRank=symbol=>bySymbol.get(symbol)?.rank??byView[view]?.[symbol]??null;
      const pinned=positions.map(p=>p.symbol).sort((a,b)=>
        Number(b===this.selected)-Number(a===this.selected)
        || (actualRank(a)??Number.MAX_SAFE_INTEGER)-(actualRank(b)??Number.MAX_SAFE_INTEGER));
      const order=[...pinned,...candidates.map(r=>r.symbol).filter(s=>!held.has(s))];
      return order.map(symbol=>{
        const raw=bySymbol.get(symbol),position=held.get(symbol),seat=this.rows.find(r=>r.symbol===symbol);
        const fact=this.liveTd(symbol),td=fact.count,direction=position?(position.side==='long'?1:position.side==='short'?-1:0):raw?.direction_sign;
        const referenceSlot=(this.displayState?.referenceSlots||[]).indexOf(symbol);
        const entry=this.liveDirection(symbol);
        const rank=actualRank(symbol), mixedRank=byView.mixed?.[symbol]??this.frame?.ranks?.find(r=>r.symbol===symbol)?.rank??null;
        return {...raw,symbol,rank,mixedRank,candidateRank:raw?.rank??null,gridSeat:seat?.rank??null,referenceSlot,
          isHeld:!!position,position,direction_sign:direction,entryDirection:entry.known?entry.direction:0,entryReason:entry.reason,tdCount:td,tdState:rhythmTdState(td),
          tdLabel:td==null?'TD —':'TD '+(td>0?'+':'')+td,tdTitle:this.tdTitle(fact)};
      });
    },
    // Visual row order on TV: display slots 9/7/5/3 above 10/8/6/4.
    tvSymbols(){return [9,7,5,3,10,8,6,4].map(rank=>this.rows.find(r=>r.rank===rank)?.symbol||'');},
    focusSymbols(){return [this.rows.find(r=>r.rank===1)?.symbol||'','CAPITALCOM:NAS100','CAPITALCOM:SPX500',
      this.rows.find(r=>r.rank===2)?.symbol||'',this.displayState?.referenceSlots?.[0]||'NASDAQ:QQQ',
      this.displayState?.referenceSlots?.[1]||'NASDAQ:NVDA'];},
    gridKey(){return [...this.tvSymbols,...this.focusSymbols].join(',');},
    tracks(){
      const beta=['CAPITALCOM:NAS100','CAPITALCOM:SPX500'].map(symbol=>this.sourceTracks[symbol]||{symbol,is_beta:true});
      const stocks=[1,2].map(rank=>{
        const symbol=this.rows.find(r=>r.rank===rank)?.symbol||'';
        const track=this.sourceTracks[symbol]||{symbol,points:[],events:[],issue:'missing_data'};
        return {...track,rank};
      });
      return [...beta,...stocks].map(track=>({...track,forming:this.active?rhythmFormingPoint(track,
        this.market?.[track.symbol]?.rhythm_price_1m,this.frame?.as_of,this.now??this.clockNow):null}));
    },
    end(){return Math.max(0,(this.frame?.as_of||60)-60,...this.tracks.map(t=>t.forming?.time||0));},
    startTime(){return Math.max(this.tracks.flatMap(t=>t.points||[]).map(p=>p[0]).reduce((a,b)=>Math.min(a,b),this.end),this.end-(this.windowMinutes-1)*60);},
    focusTime(){return this.focus==null?Math.max(this.startTime,(this.frame?.as_of||60)-60):Math.max(this.startTime,Math.min(this.end,this.focus));},
    zDomain(){const values=this.tracks.flatMap(t=>[...(t.points||[]).filter(p=>p[0]>=this.startTime&&p[0]<=this.end).flatMap(p=>t.is_beta&&p[3]>0?[p[8],(p[5]-p[2])/p[3],(p[6]-p[2])/p[3]]:[p[8]]),t.forming?.z]).filter(Number.isFinite);return [Math.min(-1.4,...values)-.1,Math.max(1.4,...values)+.1];},
    events(){return this.tracks.flatMap(t=>(t.events||[]).filter(e=>e.time>=this.startTime&&e.time<=this.end).map(e=>({...e,symbol:t.symbol}))).sort((a,b)=>b.time-a.time||a.symbol.localeCompare(b.symbol)).slice(0,7).reverse();},
    old(){return this.frame?.as_of&&this.clockNow-this.frame.as_of>120;},
    sessionState(){return rhythmSessionState(this.frame?.as_of||0,this.clockNow,this.frame?.ranking_session);},
    facts(){return this.tracks.filter(t=>t.is_beta).map(t=>{const p=t.points?.at(-1),z=p?.[8];return this.ticker(t.symbol)+' '+(!t.aligned?'数据待齐':!Number.isFinite(z)?'轨道未就绪':z>1?'上轨外':z<-1?'下轨外':z>=0?'上区间':'下区间');}).join(' · ');},
  },
  methods:{
    rankTitle(row){
      if(row.rank)return '本分钟榜单第 '+row.rank+' 名';
      if(row.mixedRank)return '本方向无名次 · 混合榜第 '+row.mixedRank+' 名';
      return '无确认名次';
    },
    issueLabel:reason=>RHYTHM_ISSUES[reason]||reason,eventLabel:type=>RHYTHM_EVENTS[type]||type,clock:rhythmTime,ticker:rhythmTicker,number:rhythmNumber,signed:rhythmSigned,phase:p=>RHYTHM_LABELS[p]||p,
    liveDirection(symbol){return rhythmVwapFact(this.directionSnapshot?.rows?.[symbol],this.now??this.clockNow);},
    liveTd(symbol){return rhythmTdFact(this.tdSnapshot?.rows?.[symbol],this.now??this.clockNow);},
    tdTitle(fact){return fact.known?'K线 '+rhythmTime(fact.bar_time)+' ET · '+rhythmTime(fact.confirmed_at)+' 收盘确认 · TD '+(fact.count>0?'+':'')+fact.count+(fact.ready?'':' · '+fact.reason):fact.reason;},
    loadDisplay(){
      this.displayState=null;
      try{const saved=JSON.parse(localStorage.getItem('rhythm_display_v2:'+(this.trader||'master')));
        if(saved?.version===2&&Array.isArray(saved.order)&&saved.order.every(s=>typeof s==='string')
          &&Array.isArray(saved.heldOrder)&&saved.heldOrder.every(s=>typeof s==='string')
          &&Array.isArray(saved.referenceSlots)&&saved.referenceSlots.length===2
          &&saved.referenceSlots.every(s=>typeof s==='string'))this.displayState=saved;
      }catch{} // Presentation preferences never restore positions or trading authority.
    },
    reconcileDisplay(){
      if(!this.frame||this.positionsReady===false)return;
      const next=rhythmDisplaySlots(this.displayState,this.frame,this.positions,this.rankingView);
      if(JSON.stringify(next)===JSON.stringify(this.displayState))return;
      this.displayState=next;
      try{localStorage.setItem('rhythm_display_v2:'+(this.trader||'master'),JSON.stringify(next));}catch{}
    },
    start(){this.stop();this.refresh();this.timer=setInterval(()=>this.refresh(),10000);},
    stop(){clearInterval(this.timer);this.requestId++;this.controller?.abort();this.loading=false;},
    async refresh(force=false){
      if(!this.active||this.loading&&!force)return;
      this.clockNow=Date.now()/1000;
      this.controller?.abort();this.controller=new AbortController();const id=++this.requestId;
      const controller=this.controller, timeout=setTimeout(()=>controller.abort(),8000);
      this.loading=true;
      try{
        const watch=[...new Set([...(this.positions||[]).map(p=>p.symbol),...(this.displayState?.order||[])])].slice(0,10).join(',');
        const response=await fetch('/api/market-rhythm?symbol='+encodeURIComponent(this.selected)+'&watch='+encodeURIComponent(watch),{signal:this.controller.signal,cache:'no-store'});
        if(!response.ok)throw new Error('联动行情读取失败（'+response.status+'）');
        const frame=await response.json();if(id!==this.requestId)return;
        this.clockNow=Date.now()/1000;
        if(frame.status!=='ready'){this.error=frame.message||'等待联动行情';return;}
        if(frame.scoring?.model!=='opening15_midpoint_v7'||frame.scoring.version!==7){
          this.error='榜单版本未更新，请重启控制台加载小区间排名';return;
        }
        const accepted=frame.ranking_snapshot;
        if(accepted?.version!==1||!accepted.id||accepted.as_of!==frame.as_of||accepted.day!==frame.day){
          this.error='等待服务端完整分钟榜单契约，请重启控制台加载新版';return;
        }
        if(this.frame&&frame.as_of<this.frame.as_of)return;
        this.error='';
        if(this.frame?.revision!==frame.revision||this.frame?.selected!==frame.selected
            ||JSON.stringify(this.frame?.batch_progress)!==JSON.stringify(frame.batch_progress)){
          this.frame=frame;
          if(!(this.positions||[]).some(p=>p.symbol===this.selected))this.selected=frame.selected;
          this.$emit('frame',frame);
          if(!this.locked)this.focus=null;
        }
        this.reconcileDisplay();
        this.publishGrid();
      }catch(error){if(id===this.requestId)this.error=error.name==='AbortError'?'联动行情读取超时':error.message;}
      finally{clearTimeout(timeout);if(id===this.requestId)this.loading=false;}
    },
    choose(symbol){
      if(!symbol)return;
      this.selected=symbol;this.$emit('select',symbol,{view:this.rankingView||'mixed'});this.refresh(true);
    },
    selectPanel(index,symbol){
      if(symbol){this.selected=symbol;this.$emit('select',symbol);}
    },
    rankLocation(rank){return rank<=2?'TV六格左'+(rank===1?'上':'下'):'TV第'+(6-Math.ceil(rank/2))+'列'+(rank%2?'上':'下');},
    gridLocation(row){const locations=[];
      if(row.referenceSlot===0||row.referenceSlot===1)locations.push('TV六格下'+(row.referenceSlot===0?'中':'右')+' · 持仓');
      if(row.gridSeat)locations.push(this.rankLocation(row.gridSeat));
      else if(row.gridSeat===undefined&&row.rank)locations.push(this.rankLocation(row.rank));
      return locations.join(' / ')||'未分配TV席位';},
    openRank(row){
      if(row?.rankingType==='td')return; // Retired client-side board source.
      if(row?.isHeld){this.manageRank(row);return;}
      if(!row?.symbol||![1,-1].includes(row.direction_sign))return;
      this.selected=row.symbol;this.$emit('select',row.symbol,{view:this.rankingView||'mixed'});
      this.$emit('open',{symbol:row.symbol,frame:this.frame,view:this.rankingView||'mixed'});
    },
    manageRank(row){if(row?.symbol){this.selected=row.symbol;this.$emit('select',row.symbol);this.$emit('manage',row.symbol);}},
    rowDescription(row){
      const rank=row.candidateRank?'强度#'+row.candidateRank:'未入候选';
      if(Number.isFinite(row.opening_position))return (row.isHeld?row.position.qty+'股 · ':'')+'位置 '+this.signed(row.opening_position)+' · '+(row.entryDirection===1?'VWAP方向多':row.entryDirection===-1?'VWAP方向空':'卡片中核对方向');
      return row.isHeld?row.position.qty+'股 · '+rank:rank+' · '+(row.score_parts?.position!=null?this.number(row.score)+'分 · ':'')+(Number.isFinite(row.z)?this.signed(row.z)+'σ':this.phase(row.phase));},
    scoreDescription(row){
      if(Number.isFinite(row.opening_position))return '归一位置 '+this.signed(row.opening_position)+'个小区间半宽\n'
        +'(1分钟close − 小区间中点) / 区间半宽\n'
        +'开盘 '+this.number(row.day_open)+' · 区间 '+this.number(row.opening_low)+'–'+this.number(row.opening_high)+'\n'
        +'位置=(close−小区间中点)/半宽 · '+this.signed(row.range_position)+'，上沿+1／下沿−1\n'
        +'按距中点的幅度排名；开仓仍由最新收盘价与VWAP决定\n'+this.gridLocation(row);
      if(row.score_parts?.position==null)return this.rowDescription(row)+'\n'+this.gridLocation(row);
      return '总分 '+this.number(row.score)+'/100 · 位置50% / VWAP推进30% / EMA5分类20%\n'
        +'Z = (1分钟close − VWAP) / σ · '+this.signed(row.z)+'σ · 位置 '+this.number(row.score_parts.position)+'/50\n'
        +'VWAP开盘 '+this.number(row.vwap_open)+' → 当前 '+this.number(row.direction_vwap)
        +' · 顺趋势推进 '+this.signed(row.vwap_aligned_progress_sigma)+'σ · '+this.number(row.score_parts.vwap_progress)+'/30\n'
        +'五分钟EMA5分类 '+row.classification_correct+'/'+row.classification_samples+' · '+this.number(row.classification_rate*100)+'%'
        +' · '+this.number(row.score_parts.classification)+'/20\n'
        +'VWAP上方只做多，下方只做空；分类是保持率，非交易胜率\n'+this.gridLocation(row);
    },
    publishGrid(force=false){
      const view=this.rankingView||'mixed',state=this.displayState;
      if(!this.active||!this.frame||this.positionsReady===false||!state||(state.rankingView||'mixed')!==view)return;
      const completeView=state.candidateSnapshotId&&Array.isArray(this.frame.rankings?.[view]);
      // TV always expects a full layout. Benchmark charts replace unused candidate seats.
      const fill=symbols=>completeView
        ?symbols.map((symbol,i)=>symbol||this.focusSymbols[1+(i%2)])
        :symbols.every(Boolean)?symbols:[];
      const symbols=fill(this.tvSymbols),focusSymbols=fill(this.focusSymbols);
      if(symbols.length||focusSymbols.length)this.$emit('grid',{symbols,focusSymbols,force});
    },
    hover(time){if(!this.locked)this.focus=time;},
    lock(time){this.locked=!this.locked;this.focus=this.locked?time:null;},
    eventFocus(time){this.locked=true;this.focus=time;},

  },
  template:`<div class="market-rhythm rhythm-native" aria-label="前十候选与订单批次工作台" :data-ranking-snapshot-id="displayState?.candidateSnapshotId || ''" :data-ranking-as-of="displayState?.candidateAsOf || ''">
    <div class="rhythm-board">
      <aside class="rhythm-ranking" aria-label="固定指数与每分钟候选排名">
        <header class="rhythm-ranking-tools">
        <strong :title="tvSyncStatus || '一分钟收盘更新 · 持仓置顶'">趋势榜</strong>
        <div class="rhythm-view-switch" role="group" aria-label="候选方向筛选"><button v-for="option in [{value:'mixed',label:'混合'},{value:'long',label:'只看多头'},{value:'short',label:'只看空头'}]" :key="option.value" type="button" :aria-pressed="rankingView===option.value" :class="{selected:rankingView===option.value}" @click="rankingView=option.value">{{ option.label }}</button></div>
        <button type="button" class="rhythm-refresh" title="刷新数据" aria-label="刷新数据" @click="refresh(true)">↻</button>
        </header>
        <div v-if="error" class="rhythm-message rhythm-compact-status" role="status" :title="error">{{ error }}</div>
        <div v-else-if="rankingPending" class="rhythm-message rhythm-compact-status" role="status" :title="frame?.batch_progress?.message || '保留上批榜单与宫格'">待齐 {{ rankingProgress.aligned || 0 }}/{{ rankingProgress.total || '—' }}</div>
        <div v-else-if="frame && ['premarket','history','stale'].includes(sessionState)" class="rhythm-message rhythm-compact-status" role="status">{{ sessionState==='premarket'?'盘前观察':sessionState==='history'?'历史榜单':'榜单滞后' }}</div>
        <div class="rhythm-benchmarks" role="list" aria-label="固定大盘指数TD"><div v-for="row in benchmarkRows" :key="row.symbol" class="rhythm-benchmark-row" role="listitem" :data-symbol="row.symbol" :title="row.tdTitle">
          <span class="rhythm-benchmark-kind">指</span><b>{{ ticker(row.symbol) }}</b><time>{{ row.confirmedAt ? clock(row.confirmedAt)+' ET' : '待确认' }}</time><span class="rhythm-rank-td" :class="'rhythm-td-'+row.tdState">{{ row.tdLabel }}</span>
        </div></div>
        <div v-if="positionOverflow" class="rhythm-message rhythm-compact-status" role="status">TV 未放入 {{ positionOverflow }} 只持仓</div>
        <div v-if="visibleRankingRows.length" class="rhythm-rank-list" role="list"><div v-for="row in visibleRankingRows" :key="row.symbol" class="rhythm-rank-row" role="listitem" :class="[row.direction_sign===1?'rhythm-long':row.direction_sign===-1?'rhythm-short':'',{'rhythm-rank-selected':selected===row.symbol,'rhythm-held':row.isHeld}]">
          <button type="button" class="rhythm-rank-pick" :aria-label="'选中'+ticker(row.symbol)" :aria-pressed="selected===row.symbol" @click="choose(row.symbol)" :title="scoreDescription(row)+' · '+rowDescription(row)"><span class="rhythm-rank" :class="{'rhythm-rank-out':row.isHeld&&(row.rank||row.mixedRank)>10}" :title="rankTitle(row)">{{ row.rank || (row.mixedRank ? '混'+row.mixedRank : '—') }}</span><span><b class="rhythm-rank-name"><span>{{ ticker(row.symbol) }} <i>{{ row.isHeld ? (row.position.side==='long'?'持多':row.position.side==='short'?'持空':'持仓') : row.direction_sign===1?'多':row.direction_sign===-1?'空':row.tdCount==null?'待确认':'中性' }}</i></span><small v-if="Number.isFinite(row.opening_position)" :title="scoreDescription(row)">{{ signed(row.opening_position) }}</small><span class="rhythm-rank-td" :class="'rhythm-td-'+row.tdState" :title="row.tdTitle">{{ row.tdLabel }}</span></b></span></button>
          <button type="button" class="rhythm-open-order" :class="!row.isHeld ? (row.entryDirection===1?'rhythm-long':row.entryDirection===-1?'rhythm-short':'') : ''" :title="row.isHeld?'管理现有批次':'打开开仓卡片；按VWAP趋势方向交易，TD仅供参考'" :aria-label="ticker(row.symbol)+(row.isHeld?'管理持仓':'开仓')" @click="openRank(row)">{{ row.isHeld?'管理':row.entryDirection===1?'开多':row.entryDirection===-1?'开空':'开仓' }}</button>
        </div></div>
        <div v-if="!visibleRankingRows.length" class="rhythm-track-empty">{{ positionsReady===false?'正在同步持仓':'等待持仓或有效趋势候选' }}</div>
      </aside>
      <section class="rhythm-orders" aria-label="订单批次">
        <header class="rhythm-order-header"><div class="rhythm-orders-controls"><slot name="order-controls"></slot></div><slot name="protection-controls"></slot></header>
        <slot><div class="rhythm-track-empty">等待订单批次</div></slot>
      </section>
    </div>
  </div>`,
};
if(typeof module!=='undefined')module.exports={MarketRhythm,RhythmTrack,rhythmChartModel,rhythmTime,rhythmSessionState,rhythmTradePreview,rhythmConfirmedTd,rhythmFormingPoint,rhythmPositions,rhythmHoldingRankChanges,rhythmDisplaySlots,rhythmRankingReady,rhythmTdFact,rhythmVwapFact,rhythmEntryWindow};
