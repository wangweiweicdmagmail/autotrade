'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {createResearchJournal,rankingRecord,digest}=require('../service/research_journal');
const series={provider:'tradingview',symbol:'NASDAQ:TEST',resolved_symbol:'BATS:TEST',interval:'1m',session:'extended',adjustment:'splits'};
const bar=(time,close=100)=>({time,open:100,high:102,low:98,close,volume:1000});
async function records(root){const names=(await fs.readdir(root)).filter(s=>s.endsWith('.ndjson'));let rows=[];
 for(const name of names){const text=await fs.readFile(path.join(root,name),'utf8');rows.push(...text.trim().split('\n').filter(Boolean).map(JSON.parse));}return rows;}

test('preloaded history is archived once, open bars wait and deep revisions create small increments',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'research-journal-'));
 let now=190;const journal=createResearchJournal(root,{clock:()=>now});
 try{
  const bars=[bar(60),bar(120),bar(180)];journal.observeBars(series,bars,now);await journal.flush();
  const initial=await records(root);assert.equal(initial.length,1);
  assert.deepEqual(JSON.parse(initial[0].body).payload.rows.map(r=>r.time),[60,120]);
  journal.observeBars(series,bars,now);await journal.flush();assert.equal((await records(root)).length,1);
  now=250;journal.observeBars(series,bars,now);await journal.flush();
  bars[0]=bar(60,101);journal.observeBars(series,bars,now);await journal.flush();
  const rows=await records(root);assert.equal(rows.length,3);
  assert.equal(JSON.parse(rows[2].body).payload.rows.length,1);
  for(const row of rows)assert.equal(row.sha256,digest(row.body));
 }finally{await journal.close();await fs.rm(root,{recursive:true,force:true});}
});

test('bounded journal reports overload and filesystem failures without throwing into trading callbacks',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'research-failure-'));
 const errors=[],journal=createResearchJournal(root,{maxPending:1,onError:s=>errors.push(s)});
 assert(journal.append('configuration',{series:[]}));assert.equal(journal.append('configuration',{series:[]}),false);
 await journal.flush();assert.equal(journal.status().dropped,1);await journal.close();
 const file=path.join(root,'file');await fs.writeFile(file,'unchanged');
 const broken=createResearchJournal(file,{onError:s=>errors.push(s)});
 broken.append('configuration',{});await broken.flush();assert(broken.status().error);assert.equal(broken.status().pending,1);
 await broken.close();assert.equal(await fs.readFile(file,'utf8'),'unchanged');assert(errors.length>=2);
 await fs.rm(root,{recursive:true,force:true});
});

test('ranking batches are complete, timestamped, stable across heartbeats and keep meaningful revisions',()=>{
 const frame={day:'2026-11-20',as_of:180,generated_at:181,beta:[],ranks:[{symbol:'NASDAQ:TEST',rank:1}],
  coverage:{total:1,aligned:1,missing:[]},scoring:{pending:[]},series:{}};
 assert.equal(rankingRecord(frame).batch_id,rankingRecord({...frame,generated_at:185}).batch_id);
 assert.equal(rankingRecord({...frame,coverage:{total:2,aligned:1,missing:['LATE']}}),null);
 assert.notEqual(rankingRecord(frame).batch_id,rankingRecord({...frame,ranks:[]}).batch_id);
});

test('production journal setup archives all model definitions without enabling cloud studies or broker code',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'research-daemon-'));
 const credentials=path.join(root,'test-creds.json');await fs.writeFile(credentials,JSON.stringify({sessionid:'test',sessionid_sign:'test'}));
 const vm=require('node:vm'),syncFs=require('node:fs');
 const originalRequire=require('node:module').createRequire(path.join(__dirname,'../service/tv_cloud_daemon.js'));
 const charts=[],errors=[];
 class Chart{constructor(){charts.push(this);}onUpdate(){}onError(){}setMarket(){}get Study(){throw Error('Study forbidden');}}
 const client={Session:{Chart},onConnected(){},onDisconnected(){},onError(){}};
 const fakeProcess={argv:['node','daemon','--symbols','NASDAQ:QQQ','--creds',credentials,'--research-spool',root],env:{},stdout:{write(){}},exit:c=>{fakeProcess.code=c;}};
 const context=vm.createContext({process:fakeProcess,__dirname:path.join(__dirname,'../service'),Date,
  console:{error:s=>errors.push(s)},setTimeout:()=>({unref(){}}),setInterval:()=>({unref(){}}),clearTimeout(){},
  require:name=>name==='@mathieuc/tradingview'?{}:name==='./tv_market_proxy'?{createMarketClient:async()=>client}:
  name==='./pinets_live'?{BAR_LIMIT:601,candle:x=>x,createLocalIndicators:()=>({reset(){}})}:originalRequire(name)});
 const source=syncFs.readFileSync(path.join(__dirname,'../service/tv_cloud_daemon.js'),'utf8');
 try{
  vm.runInContext(source.replace('main().catch','globalThis.done=main().catch'),context);await context.done;
  assert.equal(fakeProcess.code,undefined,errors.join('\n'));
  let rows=[];for(let i=0;i<30;i++){try{rows=await records(root);}catch{}if(rows.length>=2)break;await new Promise(resolve=>setTimeout(resolve,5));}
  assert(rows.some(r=>JSON.parse(r.body).kind==='definition'));
  const config=rows.map(r=>JSON.parse(r.body)).find(r=>r.kind==='configuration');assert(config.payload.series.some(s=>s.interval==='1m'));
 }finally{await fs.rm(root,{recursive:true,force:true});}
});
