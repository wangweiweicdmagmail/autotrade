'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const script=fs.readFileSync(require.resolve('../extensions/autotrade-tv/chart-grid.js'),'utf8');
const address='https://www.tradingview.com/chart/test/';
function fixture(count=6,focus=false){
 const symbols=['CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:AMD','NASDAQ:NVDA','NASDAQ:MSFT','NASDAQ:AAPL',...(count===8?['NASDAQ:TSLA','NASDAQ:COIN']:[])];
 if(count===4)symbols.length=4;
 const columns=focus?3:count===8?4:2;
 const writes=[],created=[],removed=[];
 const widgets=symbols.map((symbol,i)=>({current:symbol,
   _mainDiv:{getBoundingClientRect:()=>({top:Math.floor(i/columns)*200+(i%columns)*.3,left:i%columns*400,width:390,height:190})},
   symbol(){return{value:()=>this.current};},setSymbol(s){writes.push({i,s});this.current=s;}}));
 const shapes=widgets.map(()=>new Map([['user-drawing',{id:'user-drawing'}]]));
 const api=widgets.map((w,i)=>({_chartWidget:w,chartWidget:()=>w,getAllShapes:()=>[...shapes[i].values()].filter(s=>!s.symbol||s.symbol===w.current),
   resolution:()=> '1',
   async createMultipointShape(points,options){const id='mark-'+created.length;created.push({i,points,options,id});shapes[i].set(id,{id,symbol:w.current,points,raw:points.map(p=>({index:Math.floor(p.time/60),price:p.price}))});return id;},
   getShapeById(id){const shape=shapes[i].get(id);return {getRawPoints:()=>shape.raw,setRawPoint:(index,point)=>shape.raw[index]=point,setPoints:points=>{shape.points=points;shape.raw=points.map(p=>({index:Math.floor(p.time/60),price:p.price}));},setProperties(){}};},
   removeEntity(id){removed.push({i,id});shapes[i].delete(id);}}));
 const order=count===4?[3,0,2,1]:count===8?[7,1,5,0,6,2,4,3]:[5,2,0,4,1,3];
 const window={TradingViewApi:{_chartWidgetCollection:{getAll:()=>order.map(i=>widgets[i])},chartsCount:()=>count,chart:i=>api[order[i]]}};
 const context=vm.createContext({window,location:new URL(address)});vm.runInContext(script,context);
 const grid=(s=symbols,change=true,expected=null,layout=focus?'focus':null)=>{context.args=[s,address,change,expected,layout];return vm.runInContext('inspectTradingViewGrid(...args)',context);};
 const marks=entries=>{context.args=[entries,address];return vm.runInContext('markTradingViewEntries(...args)',context);};
 return {grid,marks,writes,created,removed,widgets,symbols,window,api,shapes};
}

test('six TV panes map by visual rows, preserve unchanged cells and verify exact position',()=>{
 const f=fixture();assert(f.grid().ok);assert.equal(f.writes.length,0);
 const next=[...f.symbols];next[5]='NYSE:UNH';const r=f.grid(next);
 assert(r.ok);assert.deepEqual(f.writes,[{i:5,s:'NYSE:UNH'}]);
 assert.equal(r.panes[0].shown,'CAPITALCOM:NAS100');assert.equal(r.panes[5].shown,'NYSE:UNH');
 const wrong=[...next];[wrong[2],wrong[3]]=[wrong[3],wrong[2]];
 assert.equal(f.grid(wrong,false).ok,false); // Membership alone is not verification.
});

test('eight panes verify ranks at their exact visual positions regardless of widget creation order',()=>{
 const f=fixture(8),ranked=[9,7,5,3,10,8,6,4].map(n=>'NASDAQ:S'+n);
 const result=f.grid(ranked);assert(result.ok);assert.equal(result.panes.length,8);
 assert.deepEqual(Array.from(result.panes,p=>p.shown),ranked);
 assert.equal(result.panes[3].shown,'NASDAQ:S3');assert.equal(result.panes[7].shown,'NASDAQ:S4');
 f.grid(ranked);assert.equal(f.writes.length,8);
 f.widgets.forEach((w,i)=>{w._mainDiv.getBoundingClientRect=()=>({top:Math.floor(i/2)*200,left:i%2*400,width:390,height:190});});
 assert.equal(f.grid(ranked).ok,false);assert.equal(f.writes.length,8);
});

test('replacing one departed candidate touches only its pane and never resets a synchronized time viewport',()=>{
 const f=fixture(8),actions=[],autoscale=[];
 f.api.forEach((api,i)=>{
   api.executeActionById=action=>actions.push({i,action});
   api.getPanes=()=>[{getMainSourcePriceScale:()=>({setAutoScale:value=>autoscale.push({i,value})})}];
 });
 const next=[...f.symbols];next[4]='NYSE:UNH';
 const r=f.grid(next);assert(r.ok);
 assert.deepEqual(f.writes,[{i:4,s:'NYSE:UNH'}]);
 assert.deepEqual(autoscale,[{i:4,value:true}]);assert.deepEqual(actions,[]);
 assert.deepEqual(f.widgets.map(w=>w.current),next);
 f.grid(next);assert.equal(f.writes.length,1);assert.equal(autoscale.length,1);
});

test('single-symbol clicks retain cell six in an eight-pane layout and verify all seven other panes',()=>{
 const f=fixture(8),expected=[...f.symbols];expected[5]='NYSE:UNH';
 const result=f.grid('NYSE:UNH');assert(result.ok);assert.equal(result.cell,5);
 assert.deepEqual(f.writes,[{i:5,s:'NYSE:UNH'}]);assert.deepEqual(f.widgets.map(w=>w.current),expected);
 f.widgets[7].current='NYSE:UNH';const readback=f.grid('NYSE:UNH',false,expected);
 assert.equal(readback.ok,false);assert.match(readback.reason,/其他宫格/);
 assert.equal(f.writes.length,1);
});

test('wrong geometry and navigation refuse all mutations, index providers are not ticker aliases',()=>{
 const f=fixture();f.widgets[0].current='PEPPERSTONE:NAS100';assert(f.grid().ok);
 assert.deepEqual(f.writes,[{i:0,s:'CAPITALCOM:NAS100'}]);
 f.widgets[0]._mainDiv.getBoundingClientRect=()=>({top:0,left:0,width:0,height:0});
 assert.equal(f.grid().ok,false);assert.equal(f.writes.length,1);
});

test('single-symbol clicks always switch visual cell six even when another cell already shows the symbol',()=>{
 const f=fixture(),before=f.widgets.slice(0,5).map(w=>w.current);
 const result=f.grid('NASDAQ:AMD');
 assert(result.ok);assert.equal(result.cell,5);assert.equal(result.shown,'NASDAQ:AMD');
 assert.deepEqual(f.writes,[{i:5,s:'NASDAQ:AMD'}]);
 assert.deepEqual(f.widgets.slice(0,5).map(w=>w.current),before);
 assert(f.grid('NASDAQ:AMD').ok);assert.equal(f.writes.length,1);
 f.widgets[5].current='BATS:AMD';assert(f.grid('NASDAQ:AMD').ok);assert.equal(f.writes.length,1);
 assert(f.grid('AMD').ok);assert.equal(f.writes.length,1);
 assert(f.grid(null).ok);assert.equal(f.grid(null).cell,5);assert.equal(f.writes.length,1);
});

test('single-symbol switching refuses a hidden or non-six grid without falling back to the first chart',()=>{
 const f=fixture();f.widgets[5]._mainDiv.getBoundingClientRect=()=>({top:400,left:400,width:0,height:0});
 assert.equal(f.grid('NYSE:UNH').ok,false);assert.equal(f.writes.length,0);
 f.widgets.forEach((w,i)=>{w._mainDiv.getBoundingClientRect=()=>({top:0,left:i*200,width:190,height:500});});
 assert.equal(f.grid('NYSE:UNH').ok,false);assert.equal(f.writes.length,0);
});

test('single-cell delayed readback retains the other five symbols and never re-sends a switch',()=>{
 const f=fixture();let requested;
 f.widgets[5].setSymbol=s=>{f.writes.push({i:5,s});requested=s;};
 const pending=f.grid('NYSE:UNH');assert(pending.pending);
 const expected=Array.from(pending.panes,p=>p.symbol);
 f.widgets[5].current=requested;
 assert(f.grid('NYSE:UNH',false,expected).ok);
 assert.deepEqual(f.writes,[{i:5,s:'NYSE:UNH'}]);
 // A TV symbol-sync group must not silently replace a beta or another stock.
 f.widgets[0].current='NYSE:UNH';
 const invalid=f.grid('NYSE:UNH',false,expected);
 assert.equal(invalid.ok,false);assert.match(invalid.reason,/同步改变了其他宫格/);
 assert.equal(f.writes.length,1);
});

test('a requested ticker is not confirmed before the chart data series changes',()=>{
 const f=fixture(8),widget=f.widgets[5];let loaded=widget.current;
 widget.model=()=>({mainSeries:()=>({symbol:()=>loaded})});
 const pending=f.grid('NYSE:UNH');
 assert.equal(widget.current,'NYSE:UNH');
 assert.equal(pending.ok,false);assert.equal(pending.pending,true);
 const expected=Array.from(pending.panes,p=>p.symbol);
 loaded='BATS:UNH';
 assert.equal(f.grid('NYSE:UNH',false,expected).ok,true);
 assert.deepEqual(f.writes,[{i:5,s:'NYSE:UNH'}]);
});

test('four panes map beta and top two without permitting single-cell overwrites',()=>{
 const f=fixture(4),wanted=['CAPITALCOM:NAS100','CAPITALCOM:SPX500','NYSE:UNH','NASDAQ:CRWD'];
 const r=f.grid(wanted);assert(r.ok);assert.deepEqual(Array.from(r.panes,p=>p.shown),wanted);
 assert(f.grid(null).ok);const count=f.writes.length;
 assert.equal(f.grid('NASDAQ:AMD').ok,false);assert.equal(f.writes.length,count);
});

test('auto_trade2 six panes put candidates at left and the four beta charts at their exact visual cells',()=>{
 const f=fixture(6,true),wanted=['NYSE:UNH','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:NVDA','NASDAQ:QQQ','AMEX:SPY'];
 const r=f.grid(wanted);assert(r.ok);assert.equal(r.panes.length,6);
 assert.deepEqual(Array.from(r.panes,p=>p.shown),wanted);
 assert(f.grid(null).ok);assert.equal(f.grid(null).pane_count,6);
 f.writes.length=0;
 const replacement=[...wanted];replacement[3]='NASDAQ:AMD';
 assert(f.grid(replacement).ok);assert.deepEqual(f.writes,[{i:3,s:'NASDAQ:AMD'}]);
 const count=f.writes.length;
 assert.equal(f.grid('NASDAQ:TSLA').ok,false);assert.equal(f.writes.length,count);
});

test('focus rejects the legacy transposed six-grid and old four-grid before any symbol changes',()=>{
 const wanted=['NYSE:UNH','CAPITALCOM:NAS100','CAPITALCOM:SPX500','NASDAQ:NVDA','NASDAQ:QQQ','AMEX:SPY'];
 const transposed=fixture(6),old=fixture(4);
 assert.equal(transposed.grid(wanted,true,null,'focus').ok,false);assert.equal(transposed.writes.length,0);
 assert.equal(old.grid(old.symbols,true,null,'focus').ok,false);assert.equal(old.writes.length,0);
});

const entry={id:'lot-one',symbol:'NASDAQ:AMD',side:'long',time:1795014007,price:100,stop:98,qty:10,session_end:1795035600};
test('a position creates yellow cost and red stop native segments, deduplicates, and clears only owned shapes',async()=>{
 const f=fixture();let result=await f.marks([entry]);assert(result.ok);assert.equal(result.marked,1);
 assert.equal(f.created.length,2);assert.equal(f.created[0].i,2);
 assert.equal(f.created[0].options.shape,'trend_line');assert.equal(f.created[0].options.overrides.linecolor,'#f1c40f');
 assert.equal(f.created[1].options.overrides.linecolor,'#ef4444');
 assert.equal(f.created[0].points[0].time,Math.floor(entry.time/60)*60);
 assert.equal(f.created[0].points[1].time,entry.session_end);
 assert.equal(f.created[0].points[0].price,f.created[0].points[1].price);
 assert(f.created[0].options.disableSave);await f.marks([entry]);assert.equal(f.created.length,2);
 await f.marks([{...entry,stop:100}]);assert.equal(f.created.length,2);assert.equal(f.removed.length,0);
 assert.equal(f.shapes[2].get('mark-1').raw[1].index,entry.session_end/60);
 f.widgets[2].current='BATS:NVDA';await f.marks([]);assert.equal(f.removed.length,0);
 f.widgets[2].current='NASDAQ:AMD';await f.marks([]);assert.equal(f.removed.length,2);
 assert(!f.removed.some(x=>x.id==='user-drawing'));
});

test('BATS aliases receive both position segments but another index provider does not',async()=>{
 const f=fixture(4);f.widgets[3].current='BATS:NVDA';f.widgets[0].current='PEPPERSTONE:NAS100';
 const r=await f.marks([{...entry,symbol:'NASDAQ:NVDA'}, {...entry,id:'beta',symbol:'CAPITALCOM:NAS100'}]);
 assert.equal(r.marked,1);assert.equal(f.created.length,2);assert.equal(f.created[0].i,3);
});

test('pending segment creation is shared across retries and a failed pair is removed before retry',async()=>{
 const f=fixture();let finish,calls=0;
 f.api[2].createMultipointShape=async()=>{
   calls++;if(calls===1)await new Promise(resolve=>{finish=resolve;});
   if(calls===2)throw Error('second segment failed');
   const id='pending-'+calls;f.shapes[2].set(id,{id,raw:[{index:0,price:100},{index:1,price:100}]});return id;
 };
 const first=f.marks([entry]),second=f.marks([entry]);assert.equal(calls,1);finish();
 assert.equal((await first).ok,false);assert((await second).ok);assert.equal(calls,4);
 assert(f.removed.some(e=>e.id==='pending-1'));
});

test('breakeven updates existing native segments without duplicating drawings',async()=>{
 const f=fixture(),updates=[];await f.marks([entry]);
 f.api[2].getShapeById=id=>({setPoints:points=>updates.push({id,points}),setProperties(){},getRawPoints:()=>[{index:0}],setRawPoint(){}});
 await f.marks([{...entry,stop:100}]);assert.equal(f.created.length,2);assert.equal(updates.length,2);
 assert.equal(updates[1].points[0].price,100);
});

test('future close uses a logical offset even when TV snaps timestamps to the last available bar',async()=>{
 const f=fixture(4),e={...entry,time:1795098607,session_end:1795122000};
 const create=f.api[2].createMultipointShape;
 f.api[2].createMultipointShape=async(points,options)=>{
   const id=await create(points,options);f.shapes[2].get(id).raw=[{index:100,price:points[0].price},{index:101,price:points[1].price}];return id;
 };
 assert((await f.marks([e])).ok);
 for(const shape of f.shapes[2].values())if(shape.raw)assert.equal(shape.raw[1].index,490);
});

test('native widget recreation preserves position ownership by stable chart id',async()=>{
 const f=fixture(4);f.widgets[2].id=()=> 'stable-pane';
 await f.marks([entry]);assert.equal(f.created.length,2);
 f.api[2].chartWidget=()=>({...f.widgets[2]});
 await f.marks([entry]);assert.equal(f.created.length,2);
 await f.marks([]);assert.equal(f.removed.length,2);
});
