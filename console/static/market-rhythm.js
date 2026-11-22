'use strict';

const RHYTHM_LABELS = {warming:'指标预热',waiting:'等待突破',new_breakout:'新突破',
  outside_progress:'外轨推进',pullback_inside:'回踩轨道内',touch_vwap:'触及VWAP',data_gap:'数据缺口'};
const RHYTHM_EVENTS = {breakout:'突破外轨',first_outside:'首次有效值在外轨',pullback:'回踩轨道内',
  reexpansion:'恢复外轨推进',crossed_vwap:'越过VWAP，本段结束',
  prior_low_touch:'触及昨低',prior_low_near:'昨低附近',prior_high_touch:'触及昨高',prior_high_near:'昨高附近'};
const RHYTHM_ISSUES = {minute_gap:'历史分钟缺口',missing_session_anchor:'缺少默认交易日锚点',missing_open:'缺少开盘分钟',invalid_bar:'K线无效',ema_warmup:'预热不足',missing_data:'数据待齐'};
const rhythmTimeFormat = new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const rhythmSessionClock = new Intl.DateTimeFormat('en-GB',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
function rhythmSessionState(asOf, now) {
  const p=Object.fromEntries(rhythmSessionClock.formatToParts(new Date(now*1000)).map(p=>[p.type,p.value]));
  const minute=+p.hour*60 + +p.minute;
  if(['Sat','Sun'].includes(p.weekday)||minute<570||minute>=960)return 'history';
  return now-asOf>120?'stale':'confirmed';
}
const rhythmTime = t => Number.isFinite(t)&&t>0 ? rhythmTimeFormat.format(new Date(t*1000)) : '—';
const rhythmTicker = s => (s||'').split(':').pop();
const rhythmNumber = n => Number.isFinite(n) ? n.toFixed(2) : '—';
const rhythmSigned = n => Number.isFinite(n) ? (n>0?'+':'')+n.toFixed(2) : '—';
const rhythmTdState = count => count==null?'unknown':count>=3?'up':count<=-3?'down':'range';

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

// A partial minute is not evidence that a stock has left the cross-section.
function rhythmRankingReady(frame) {
  const c=frame?.coverage;
  return Number.isFinite(frame?.as_of)&&frame.as_of>0&&frame.as_of%60===0
    &&!!c&&Number.isSafeInteger(c.total)&&c.total>0&&c.aligned===c.total
    &&Array.isArray(c.missing)&&c.missing.length===0;
}

// Presentation slots only. Raw ranks and execution admission remain independent.
function rhythmDisplaySlots(previous, frame, positions=[]) {
  const ranked=[...(frame?.ranks||[])].filter(r=>r?.symbol).sort((a,b)=>a.rank-b.rank).slice(0,10);
  const held=positions.map(p=>p.symbol);
  const day=frame?.day||'',nextBucket=Number.isFinite(frame?.as_of)?Math.floor(frame.as_of/600):0;
  const valid=previous?.version===2&&previous.day===day&&Array.isArray(previous.order)&&Array.isArray(previous.heldOrder)
    &&Array.isArray(previous.referenceSlots)&&previous.referenceSlots.length===2;
  // One accepted, complete, closed-minute snapshot. Later revisions of that minute
  // cannot reshuffle membership while the next candle is forming.
  const accept=rhythmRankingReady(frame)&&(!valid||!Number.isFinite(previous.candidateAsOf)||frame.as_of>previous.candidateAsOf);
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
  // overflow holding before falling back to QQQ / SPY. Only overflow consumes
  // the ten candidate display seats.
  const referenceSlots=['',''];
  if(valid)previous.referenceSlots.forEach((s,i)=>{if(held.includes(s)&&!referenceSlots.includes(s))referenceSlots[i]=s;});
  const waiting=heldOrder.filter(s=>!referenceSlots.includes(s));
  referenceSlots.forEach((s,i)=>{if(!s)referenceSlots[i]=waiting.shift()||'';});
  const pinned=heldOrder.filter(s=>!referenceSlots.includes(s)).slice(0,10),rest=eligible.filter(s=>!pinned.includes(s));
  let order;
  if(!valid||previous.bucket!==bucket)order=[...pinned,...rest].slice(0,10);
  else {
    const slots=Array(10).fill('');pinned.forEach((s,i)=>{slots[i]=s;});
    previous.order.slice(0,10).forEach((s,i)=>{if(i>=pinned.length&&rest.includes(s)&&!slots.includes(s))slots[i]=s;});
    const incoming=rest.filter(s=>!slots.includes(s));
    for(let i=pinned.length;i<10;i++)if(!slots[i])slots[i]=incoming.shift()||'';
    order=slots.filter(Boolean);
  }
  return {version:2,day,bucket,order,heldOrder,referenceSlots,candidates:eligible,candidateAsOf,candidateRows};
}

function rhythmConfirmedTd(track,time) {
  const td=track?.td;
  if(td?.version!==1||td.comparison_bars!==4||td.trend_threshold!==3||!Array.isArray(td.counts))return null;
  const row=td.counts.findLast(p=>Array.isArray(p)&&p.length===2&&p[0]===time&&Number.isSafeInteger(p[1]));
  return row?row[1]:null;
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
  return {time:bar.time,ema3,z:Number.isFinite(p[2])&&p[3]>0?(ema3-p[2])/p[3]:null};
}

// Stop pricing is delegated to the ordinary market-order policy in app.js.
function rhythmTradePreview(frame,symbol,data,now,stopForSide) {
  const row=[...(frame?.tracks||[]),...(frame?.watch_tracks||[])].find(t=>t.symbol===symbol&&!t.is_beta);
  const p=row?.points?.at(-1),close=data?.bar?.close,observed=data?.price_observed_at,time=data?.bar?.time;
  const admission=Array.isArray(frame?.rows),candidate=frame?.rows?.find(r=>r.symbol===symbol);
  const pointTime=admission?candidate?.bar_time:p?.[0];
  const direction=admission?candidate?.direction:p?.[10],sigma=admission?candidate?.sigma:p?.[3],side=direction===1?'long':direction===-1?'short':null;
  const aligned=admission?frame.ready&&!!candidate:row?.aligned&&!row.issue;
  const rawAtr=(data?.ts_c||data?.ts||{}).atr;
  const atr=Number.isFinite(rawAtr)&&rawAtr>0?rawAtr:null;
  const stop=side&&typeof stopForSide==='function'?stopForSide(side,atr):null;
  const ready=!!(aligned&&side&&Number.isFinite(pointTime)&&Number.isFinite(stop)&&Number.isFinite(close)&&close>0
    &&now>=pointTime+60&&now-pointTime-60<=120&&Number.isFinite(observed)&&now>=observed&&now-observed<=45
    &&Number.isFinite(time)&&time<=now&&now<time+300&&stop>0&&(side==='long'?stop<close:stop>close)
    &&rhythmSessionState(pointTime+60,now)!=='history');
  const blockReason=ready?'':!Number.isFinite(pointTime)?'等待候选轨道':admission&&!frame.ready?frame.reason:rhythmSessionState(pointTime+60,now)==='history'
    ?'当前为常规盘历史，仅供查看；等待当日常规盘轨道':now-pointTime-60>120
      ?'轨道确认数据已过期':!side?'当前没有有效趋势方向':!aligned
        ?'轨道数据不完整或未对齐':'等待新鲜价格与有效止损';
  return {side,close,limitPx:close??null,stopPx:stop,atr,sigma,ready,blockReason};
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
  components:{RhythmTrack},props:{active:Boolean,tvSyncStatus:String,market:Object,now:Number,lots:Array,trader:String,
    positionsReady:{type:Boolean,default:true}},emits:['select','open','manage','frame','grid'],
  data:()=>({frame:null,error:'',loading:false,selected:'',view:'price',windowMinutes:390,focus:null,locked:false,clockNow:Date.now()/1000,displayState:null}),
  mounted(){this.requestId=0;this.loadDisplay();if(this.active)this.start();},
  beforeUnmount(){this.stop();},
  watch:{active(value){if(value){this.start();this.publishGrid();}else this.stop();},
    gridKey(){this.publishGrid();},
    positionKey(){this.reconcileDisplay();},
    positionsReady(){this.reconcileDisplay();},
    trader(){this.loadDisplay();this.reconcileDisplay();}},
  computed:{
    sourceTracks(){return Object.fromEntries([...(this.frame?.tracks||[]),...(this.frame?.watch_tracks||[])].filter(t=>t.symbol).map(t=>[t.symbol,t]));},
    positions(){return rhythmPositions(this.lots||[],[...(this.frame?.symbols||[]),...(this.frame?.ranks||[]).map(r=>r.symbol),...Object.keys(this.market||{})]);},
    positionKey(){return this.positions.map(p=>p.symbol).join(',');},
    positionOverflow(){return Math.max(0,this.positions.length-12);},
    rankingPending(){return !!this.frame&&!rhythmRankingReady(this.frame);},
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
      const candidates=[...(this.displayState?.candidateRows||[])].sort((a,b)=>a.rank-b.rank);
      const positions=this.positions||[],held=new Map(positions.map(p=>[p.symbol,p]));
      const bySymbol=new Map(candidates.map(r=>[r.symbol,r]));
      const order=[...positions.map(p=>p.symbol),...candidates.map(r=>r.symbol).filter(s=>!held.has(s))];
      return order.map(symbol=>{
        const raw=bySymbol.get(symbol),position=held.get(symbol),seat=this.rows.find(r=>r.symbol===symbol);
        const td=raw?.tdCount??null,direction=position?(position.side==='long'?1:position.side==='short'?-1:0):raw?.direction_sign;
        const referenceSlot=(this.displayState?.referenceSlots||[]).indexOf(symbol);
        return {...raw,symbol,rank:raw?.rank??null,candidateRank:raw?.rank??null,gridSeat:seat?.rank??null,referenceSlot,
          isHeld:!!position,position,direction_sign:direction,tdCount:td,tdState:rhythmTdState(td),
          tdLabel:td==null?'TD —':'TD '+(td>0?'+':'')+td,tdTitle:td==null?'等待TD确认数据':rhythmTime((this.displayState?.candidateAsOf||0)-60)+' K线收盘确认 · TD '+(td>0?'+':'')+td};
      });
    },
    // Visual row order on TV: display slots 9/7/5/3 above 10/8/6/4.
    tvSymbols(){return [9,7,5,3,10,8,6,4].map(rank=>this.rows.find(r=>r.rank===rank)?.symbol||'');},
    focusSymbols(){return [this.rows.find(r=>r.rank===1)?.symbol||'','CAPITALCOM:NAS100','CAPITALCOM:SPX500',
      this.rows.find(r=>r.rank===2)?.symbol||'',this.displayState?.referenceSlots?.[0]||'NASDAQ:QQQ',
      this.displayState?.referenceSlots?.[1]||'AMEX:SPY'];},
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
    sessionState(){return rhythmSessionState(this.frame?.as_of||0,this.clockNow);},
    facts(){return this.tracks.filter(t=>t.is_beta).map(t=>{const p=t.points?.at(-1),z=p?.[8];return this.ticker(t.symbol)+' '+(!t.aligned?'数据待齐':!Number.isFinite(z)?'轨道未就绪':z>1?'上轨外':z<-1?'下轨外':z>=0?'上区间':'下区间');}).join(' · ');},
  },
  methods:{issueLabel:reason=>RHYTHM_ISSUES[reason]||reason,eventLabel:type=>RHYTHM_EVENTS[type]||type,clock:rhythmTime,ticker:rhythmTicker,number:rhythmNumber,signed:rhythmSigned,phase:p=>RHYTHM_LABELS[p]||p,
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
      const next=rhythmDisplaySlots(this.displayState,this.frame,this.positions);
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
        if(this.frame&&frame.as_of<this.frame.as_of)return;
        this.error='';
        if(this.frame?.revision!==frame.revision||this.frame?.selected!==frame.selected){
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
      this.selected=symbol;this.$emit('select',symbol);this.refresh(true);
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
      if(row?.isHeld){this.manageRank(row);return;}
      if(!row?.symbol||![1,-1].includes(row.direction_sign))return;
      this.selected=row.symbol;this.$emit('select',row.symbol);
      this.$emit('open',{symbol:row.symbol,frame:this.frame});
    },
    manageRank(row){if(row?.symbol){this.selected=row.symbol;this.$emit('select',row.symbol);this.$emit('manage',row.symbol);}},
    rowDescription(row){const rank=row.candidateRank?'强度#'+row.candidateRank:'未入候选';
      return row.isHeld?row.position.qty+'股 · '+rank:rank+' · '+this.phase(row.phase);},
    scoreDescription(row){return Number.isFinite(row.score)?'强度#'+row.candidateRank+' · '+this.number(row.score)+'分\n'
      +'结构 '+this.number(row.score_parts?.vwap)+' + EMA9斜率 '+this.number(row.score_parts?.slope)+' + 分类 '+this.number(row.score_parts?.classification)+'\n'
      +'5分钟EMA9斜率 '+this.signed(row.ema9_slope_atr)+' ATR/根\n'
      +'近15分钟同侧 '+this.number(row.classification_rate*100)+'%（'+row.classification_correct+'/'+row.classification_samples+'根；样本'+row.classification_samples+'/15）\n'
      +'乖离 '+this.signed(row.ema9_bias_atr)+' ATR，仅位置参考\n'+this.gridLocation(row):this.rowDescription(row)+'\n'+this.gridLocation(row);},
    publishGrid(force=false){
      if(!this.active||!this.frame||this.positionsReady===false||!this.displayState)return;
      const symbols=this.tvSymbols.every(Boolean)?this.tvSymbols:[];
      const focusSymbols=this.focusSymbols.every(Boolean)?this.focusSymbols:[];
      if(symbols.length||focusSymbols.length)this.$emit('grid',{symbols,focusSymbols,force});
    },
    hover(time){if(!this.locked)this.focus=time;},
    lock(time){this.locked=!this.locked;this.focus=this.locked?time:null;},
    eventFocus(time){this.locked=true;this.focus=time;},

  },
  template:`<div class="market-rhythm rhythm-native" aria-label="前十候选与订单批次工作台">
    <header class="rhythm-toolbar"><div class="rhythm-protection-controls"><slot name="protection-controls"></slot></div><div class="rhythm-orders-controls"><slot name="order-controls"></slot></div></header>
    <div v-if="error" class="rhythm-message" role="status">{{ error }}<span v-if="frame"> · 保留上次榜单</span></div>
    <div v-if="rankingPending" class="rhythm-message" role="status">收盘数据 {{ frame?.coverage?.aligned || 0 }}/{{ frame?.coverage?.total || '—' }} · 候选未收齐，保留上批宫格</div>
    <div class="rhythm-board">
      <aside class="rhythm-ranking" aria-label="持仓与每分钟候选排名"><header><strong>持仓与候选</strong><span>榜单每分钟更新</span></header><p>收盘排名 · 持仓置顶<small>TV席位每10分钟重排 · 进出榜局部替换</small></p>
        <div v-if="positionOverflow" class="rhythm-message">另有 {{ positionOverflow }} 只持仓未放入TV，请在订单批次查看</div>
        <div v-if="rankingRows.length" class="rhythm-rank-list" role="list"><div v-for="row in rankingRows" :key="row.symbol" class="rhythm-rank-row" role="listitem" :class="[row.direction_sign===1?'rhythm-long':row.direction_sign===-1?'rhythm-short':'',{'rhythm-rank-selected':selected===row.symbol,'rhythm-held':row.isHeld}]">
          <button type="button" class="rhythm-rank-pick" :aria-label="'选中'+ticker(row.symbol)" :aria-pressed="selected===row.symbol" @click="choose(row.symbol)"><span class="rhythm-rank" :title="row.rank?'本分钟候选排名 '+row.rank:'榜外持仓'">{{ row.rank || '持' }}</span><span><b class="rhythm-rank-name"><span>{{ ticker(row.symbol) }} <i>{{ row.isHeld ? (row.position.side==='long'?'持多':row.position.side==='short'?'持空':'持仓') : row.direction_sign===1?'多':'空' }}</i></span><span class="rhythm-rank-td" :class="'rhythm-td-'+row.tdState" :title="row.tdTitle">{{ row.tdLabel }}</span></b><small :title="scoreDescription(row)">{{ rowDescription(row) }}</small></span></button>
          <button type="button" class="rhythm-open-order" :aria-label="ticker(row.symbol)+(row.isHeld?'管理持仓':'开仓')" :disabled="!row.isHeld&&![1,-1].includes(row.direction_sign)" @click="openRank(row)">{{ row.isHeld?'管理':'开仓' }}</button>
        </div></div>
        <div v-if="!rankingRows.length" class="rhythm-track-empty">{{ positionsReady===false?'正在同步持仓':'等待持仓或有效候选' }}</div><footer><button type="button" @click="refresh(true)">刷新数据</button><small title="榜单每分钟收盘更新；TV席位每10分钟重排，进出榜只局部替换">持仓即时更新</small></footer>
      </aside>
      <section class="rhythm-orders" aria-label="订单批次"><slot><div class="rhythm-track-empty">等待订单批次</div></slot></section>
    </div>
    <footer class="rhythm-bottom"><span>前十候选同向开仓 · 卡片确认后下单</span><span>{{ sessionState==='history'?'历史榜单 · ':sessionState==='stale'?'确认数据滞后 · ':'' }}auto_trade 八格 / auto_trade2 六格 / 控制台独立窗口</span></footer>
  </div>`,
};
if(typeof module!=='undefined')module.exports={MarketRhythm,RhythmTrack,rhythmChartModel,rhythmTime,rhythmSessionState,rhythmTradePreview,rhythmConfirmedTd,rhythmFormingPoint,rhythmPositions,rhythmDisplaySlots,rhythmRankingReady};
