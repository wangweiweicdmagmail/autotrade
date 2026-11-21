/* Shared by the local host CDP route and the client extension. Chart display only. */
// Arrays mirror the whole grid; a symbol retains the explicit cell-6 click policy.
// expected preserves every other cell across asynchronous readbacks.
function inspectTradingViewGrid(symbols, address, changeSymbols, expected = null, layout = null) {
  if (location.origin + location.pathname !== address) return {ok:false,reason:'TradingView 图表地址已变化，请重新绑定。'};
  const single=!Array.isArray(symbols);
  if (single ? symbols!==null&&(typeof symbols!=='string'||!/^(?:[A-Z0-9_.-]{1,24}:)?[A-Z0-9_.!+/-]{1,32}$/.test(symbols))
      : ![4,6,8].includes(symbols.length)||symbols.some(s=>typeof s!=='string'||!/^[A-Z0-9_.-]+:[A-Z0-9_.!+/-]+$/.test(s))) {
    return {ok:false,reason:'需要与宫格数量一致的完整标的代码。'};
  }
  const primary=window.TradingViewApi?._chartWidgetCollection?.getAll?.();
  const widgets=primary?.length?primary:window._exposed_chartWidgetCollection?._chartWidgetsDefs?.map(d=>d.chartWidget)||[];
  const visible=[...new Set(widgets)].map(chart=>({chart,rect:chart?._mainDiv?.getBoundingClientRect?.()}))
    .filter(p=>p.rect&&p.rect.width>50&&p.rect.height>50);
  // Bucket near-identical y positions; collection order is not visual order.
  const rows=[];
  for(const p of visible.sort((a,b)=>a.rect.top-b.rect.top||a.rect.left-b.rect.left)) {
    let row=rows.find(r=>Math.abs(r.top-p.rect.top)<Math.min(r.height,p.rect.height)*.2);
    if(!row){row={top:p.rect.top,height:p.rect.height,panes:[]};rows.push(row);}row.panes.push(p);
  }
  const count=single?visible.length:symbols.length;
  const focus=layout==='focus';
  const rowCount=focus?2:count===6?3:2,columns=focus?3:count===8?4:2;
  if((focus?count!==6:![4,6,8].includes(count))||rows.length!==rowCount||rows.some(r=>r.panes.length!==columns)) {
    return {ok:false,reason:focus?'请将 auto_trade2 布局设为两行三列六宫格，关闭单图最大化后重试。':count===4?'请将旧四宫格布局更新为 auto_trade2 两行三列六宫格。':count===6?'请将当前 TradingView 布局设为两列三行六宫格，关闭单图最大化后重试。'
      :'请将当前 TradingView 布局设为两行四列八宫格，关闭单图最大化后重试。',pane_count:visible.length};
  }
  const ordered=rows.sort((a,b)=>a.top-b.top).flatMap(r=>r.panes.sort((a,b)=>a.rect.left-b.rect.left));
  const requested=chart=>{const s=chart.symbol?.();return String(typeof s?.value==='function'?s.value():typeof s==='string'?s:'');};
  const read=chart=>{const s=chart.model?.()?.mainSeries?.()?.symbol?.();return typeof s==='string'&&s?s:requested(chart);};
  const matches=(shown,want)=>shown.toUpperCase()===want||(!want.includes(':')&&shown.toUpperCase().split(':').pop()===want)
    ||(['NASDAQ','NYSE','AMEX'].includes(want.split(':')[0])&&shown.toUpperCase()==='BATS:'+want.split(':')[1]);
  const setters=ordered.map(p=>typeof p.chart.setSymbol==='function'?s=>p.chart.setSymbol(s):null);
  const current=ordered.map(p=>read(p.chart));
  if(current.some(s=>!s))return {ok:false,reason:'TradingView 图格接口尚未就绪。'};
  if(symbols===null)return {ok:true,shown:current[focus||count===4?0:5],cell:focus||count===4?0:5,pane_count:count};
  if(single&&(focus||count===4))return {ok:false,reason:'单标的切换仅操作 auto_trade 八宫格第六格，不修改 auto_trade2。'};
  if(single? !setters[5]:setters.some(fn=>!fn))return {ok:false,reason:'TradingView 图格接口尚未就绪。'};
  if(single) {
    if(expected!==null&&(!Array.isArray(expected)||expected.length!==count||expected.some(s=>typeof s!=='string'||!s)))
      return {ok:false,reason:'第六格回读状态无效，请重新点击标的。'};
    const target=symbols;
    symbols=expected?[...expected]:current.map((s,i)=>i===5?target:s);
    if(symbols[5]!==target)return{ok:false,reason:'第六格目标已变化，请重新点击标的。'};
  }
  const panes=ordered.map((p,i)=>({cell:i,symbol:symbols[i],shown:read(p.chart)}));
  const changed=[];
  if(changeSymbols)for(let i=single?5:0;i<count;i++)if(!matches(panes[i].shown,symbols[i])||!matches(requested(ordered[i].chart),symbols[i])) {
    if(single&&i!==5)continue;
    try{
      setters[i](symbols[i]);changed.push(i);
      const tv=window.TradingViewApi;
      for(let n=0;n<(tv?.chartsCount?.()||0);n++) {
        const api=tv.chart(n);
        if((api.chartWidget?.()||api._chartWidget)!==ordered[i].chart)continue;
        for(const pane of api.getPanes?.()||[])pane.getMainSourcePriceScale?.()?.setAutoScale?.(true);
        // Keep the existing time viewport. timeScaleReset can propagate to every
        // pane through TV's layout synchronization and flash otherwise unchanged charts.
      }
    }catch(error){return{ok:false,reason:'第'+(i+1)+'格切换失败：'+String(error),panes,changed};}
  }
  const verified=ordered.map((p,i)=>({...panes[i],shown:read(p.chart),ok:matches(read(p.chart),symbols[i])&&matches(requested(p.chart),symbols[i])}));
  if(single&&verified.some(p=>p.cell!==5&&!p.ok))return {ok:false,reason:'TV 标的同步改变了其他宫格，请关闭标的同步后重试。',panes:verified,changed};
  return {ok:verified.every(p=>p.ok),pending:verified.some(p=>!p.ok),panes:verified,changed,
    ...(single?{cell:5,shown:verified[5].shown,before:panes[5].shown}:{} )};
}
if(typeof module!=='undefined')module.exports={inspectTradingViewGrid};

async function markTradingViewEntries(entries,address) {
  if(location.origin+location.pathname!==address)return{ok:false,reason:'TradingView 图表地址已变化'};
  if(!Array.isArray(entries)||entries.length>200)return{ok:false,reason:'仓位线段数量无效'};
  const tv=window.TradingViewApi;
  if(!tv?.chart||!tv?.chartsCount)return{ok:false,reason:'TV原生图表线段接口尚未就绪'};
  const valid=entries.filter(e=>e&&typeof e.id==='string'&&e.id.length<=100&&typeof e.symbol==='string'
    &&['long','short'].includes(e.side)&&Number.isFinite(e.time)&&e.time>0
    &&[e.price,e.stop,e.qty].every(v=>Number.isFinite(v)&&v>0)&&Number.isFinite(e.session_end)&&e.session_end>e.time);
  const matches=(shown,want)=>shown.toUpperCase()===want||(['NASDAQ','NYSE','AMEX'].includes(want.split(':')[0])&&shown.toUpperCase()==='BATS:'+want.split(':')[1]);
  const previousOwners=window.__autotradePositionSegments;
  const owners=previousOwners instanceof Map?previousOwners:new Map();
  window.__autotradePositionSegments=owners;
  let marked=0;const failures=[];
  for(let i=0;i<tv.chartsCount();i++) {
    const api=tv.chart(i),widget=api.chartWidget?.()||api._chartWidget;
    const rect=widget?._mainDiv?.getBoundingClientRect?.();
    if(!rect||rect.width<50||rect.height<50)continue;
    const raw=widget.symbol?.(),shown=typeof raw?.value==='function'?raw.value():raw;
    if(typeof shown!=='string')continue;
    const minutes=Number(api.resolution?.()||widget.resolution?.()?.value?.());
    const chartKey=typeof widget.id==='function'?widget.id():widget.id||widget._id||widget;
    let stored=owners.get(chartKey)||previousOwners?.get(widget);
    if(!stored)stored=new Map();owners.set(chartKey,stored);
    const wanted=valid.filter(e=>matches(shown,e.symbol)),ids=new Set(wanted.map(e=>e.id));
    const shapes=new Set((api.getAllShapes?.()||[]).map(s=>s.id));
    // Clean up only arrows created by the previous AutoTrade display version.
    const legacy=window.__autotradeEntryMarks?.get(widget);
    if(legacy)for(const [id,old] of legacy)if(!old.pending&&matches(shown,old.symbol||'')) {
      if(shapes.has(old.entity))api.removeEntity(old.entity);legacy.delete(id);
    }
    for(const [id,old] of stored) {
      if(!matches(shown,old.symbol))continue;
      if(!old.pending&&(!ids.has(id)||old.entities.some(entity=>!shapes.has(entity)))) {
        old.entities.forEach(entity=>{if(shapes.has(entity))api.removeEntity(entity);});stored.delete(id);
      }
    }
    for(const e of wanted) {
      try {
        if(!Number.isFinite(minutes)||minutes<=0)throw Error('仓位线段需要分钟图表');
        const start=Math.floor(e.time/(minutes*60))*minutes*60;
        const points=price=>[{time:start,price},{time:e.session_end,price}];
        // Time-based drawings snap future/missing timestamps to the last bar.
        // Anchor the end by a logical bar offset from the entry bar instead.
        const alignEnd=(shape,price)=>{
          const first=shape?.getRawPoints?.()?.[0];
          if(!Number.isFinite(first?.index)||typeof shape?.setRawPoint!=='function')
            throw Error('TV原生绘图暂不支持收盘端点定位');
          shape.setRawPoint(1,{index:first.index+(e.session_end-start)/(minutes*60),price});
        };
        const signature=JSON.stringify([e,start,minutes]);let old=stored.get(e.id);
        if(old?.pending){try{await old.pending;}catch{}old=stored.get(e.id);}
        if(old?.signature===signature){marked++;continue;}
        if(old&&typeof api.getShapeById==='function') {
          const pair=old.entities.map(entity=>api.getShapeById(entity));
          if(pair.every(shape=>typeof shape?.setPoints==='function')) {
            pair[0].setPoints(points(e.price));pair[1].setPoints(points(e.stop));
            alignEnd(pair[0],e.price);alignEnd(pair[1],e.stop);
            pair[0].setProperties?.({text:'成本 '+e.price.toFixed(2)+' · '+e.qty+'股'});
            pair[1].setProperties?.({text:'止损 '+e.stop.toFixed(2)});
            old.signature=signature;marked++;continue;
          }
        }
        if(old){old.entities.forEach(entity=>api.removeEntity(entity));stored.delete(e.id);}
        const state={entities:[],signature,pending:null,symbol:e.symbol};stored.set(e.id,state);
        state.pending=(async()=>{
          try {
            for(const [price,color,label] of [[e.price,'#f1c40f','成本 '+e.price.toFixed(2)+' · '+e.qty+'股'],[e.stop,'#ef4444','止损 '+e.stop.toFixed(2)]]) {
              const entity=await api.createMultipointShape(points(price),{shape:'trend_line',text:label,lock:true,
                disableSelection:true,disableSave:true,disableUndo:true,zOrder:'top',
                overrides:{linecolor:color,linewidth:2,linestyle:0,extendLeft:false,extendRight:false,
                  showLabel:true,textcolor:color,fontsize:12,bold:true,vertLabelsAlign:label.startsWith('成本')?'top':'bottom'}});
              if(entity==null)throw Error('TV未返回原生线段ID');state.entities.push(entity);
              alignEnd(api.getShapeById(entity),price);
              const current=widget.symbol?.(),symbol=typeof current?.value==='function'?current.value():current;
              if(typeof symbol!=='string'||!matches(symbol,e.symbol))throw Error('图格标的已变化，跳过仓位线段');
            }
            state.pending=null;
          }catch(error){state.entities.forEach(entity=>api.removeEntity(entity));stored.delete(e.id);throw error;}
        })();
        await state.pending;marked++;
      }catch(error){failures.push({id:e.id,reason:String(error)});}
    }
  }
  return {ok:failures.length===0,marked,failures};
}
if(typeof module!=='undefined')module.exports.markTradingViewEntries=markTradingViewEntries;
