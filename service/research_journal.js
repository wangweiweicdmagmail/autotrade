'use strict';
// Side-channel only: never imports an indicator, broker or execution model.
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
function canonical(value){
  if(Array.isArray(value))return value.map(canonical);
  if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().filter(k=>value[k]!==undefined).map(k=>[k,canonical(value[k])]));
  return value;
}
const digest=value=>crypto.createHash('sha256').update(typeof value==='string'?value:JSON.stringify(canonical(value))).digest('hex');
function rankingRecord(frame){
  const c=frame?.coverage,s=frame?.scoring;
  if(!frame?.as_of||frame.as_of%60||!c||c.total<=0||c.aligned!==c.total||c.missing?.length||!s||!Array.isArray(s.pending)||s.pending.length)return null;
  const symbols=[...(frame.beta||[]),...(frame.ranks||[]).map(r=>r.symbol)];
  const context=Object.fromEntries(symbols.map(symbol=>{
    const series=frame.series?.[symbol],points=series?.points||[];
    return [symbol,{point:points.findLast(p=>p[0]+60<=frame.as_of)||null,source:series?.source||null}];
  }));
  const payload={day:frame.day,as_of:frame.as_of,scoring:s,coverage:c,ranks:frame.ranks,context};
  return {...payload,batch_id:digest(payload),published_at:frame.generated_at};
}
function createResearchJournal(directory,{maxPending=2048,maxPendingBytes=32*1024*1024,onError=()=>{},clock=()=>Date.now()/1000}={}){
  const producer=crypto.randomUUID(),seen=new Map(),rankSeen=new Map();
  let sequence=0,segment=0,queue=[],writing=null,error='',dropped=0,bytes=0,pendingBytes=0,retry=null,closed=false;
  const status=()=>({enabled:!!directory,producer,pending:queue.length,pending_bytes:pendingBytes,error,dropped,sequence});
  async function drain(){
    if(writing)return writing;
    writing=(async()=>{
      if(!directory)return;
      await fs.mkdir(directory,{recursive:true,mode:0o700});
      while(queue.length){
        const batch=queue.slice(0,32),file=path.join(directory,producer+'-'+String(segment).padStart(6,'0')+'.ndjson');
        const text=batch.map(r=>JSON.stringify(r)+'\n').join('');
        let handle;
        try{
          handle=await fs.open(file,'a',0o600);await handle.writeFile(text);await handle.sync();
          queue.splice(0,batch.length);pendingBytes-=batch.reduce((n,r)=>n+Buffer.byteLength(r.body),0);bytes+=Buffer.byteLength(text);error='';
          if(bytes>=8*1024*1024){await fs.writeFile(file+'.sealed',JSON.stringify({bytes}),{mode:0o600});segment++;bytes=0;}
        }catch(e){segment++;bytes=0;throw e;}finally{await handle?.close();}
      }
    })().catch(e=>{
      error=String(e.message||e);onError(error);
      if(!closed&&!retry)retry=setTimeout(()=>{retry=null;void drain();},2000).unref();
    }).finally(()=>{writing=null;});
    return writing;
  }
  function append(kind,payload){
    if(!directory||closed)return false;
    const body=JSON.stringify({kind,payload,observed_at:clock(),health:{dropped}});
    if(queue.length>=maxPending||pendingBytes+Buffer.byteLength(body)>maxPendingBytes){dropped++;error='research journal queue full';onError(error);return false;}
    const id=producer+':'+(++sequence);pendingBytes+=Buffer.byteLength(body);
    queue.push({id,body,sha256:digest(body)});void drain();return true;
  }
  return {
    append,status,
    observeBars(series,bars,observedAt=clock(),source='subscription'){
      if(!directory)return;
      const identity=digest(series),cache=seen.get(identity)||new Map(),changed=[];
      for(let i=0;i<bars.length;i++){
        const b=bars[i],end=b.close_time??(series.interval==='1d'?(bars[i+1]?.time??b.time+86400):b.time+(series.interval==='1m'?60:300));
        if(!Number.isFinite(b.time)||end>observedAt)continue;
        const hash=[end,b.open,b.high,b.low,b.close,b.volume].join(',');
        if(cache.get(b.time)!==hash)changed.push({row:{time:b.time,close_time:end,open:b.open,high:b.high,low:b.low,close:b.close,volume:b.volume},hash});
      }
      for(let i=0;i<changed.length;i+=256){
        const part=changed.slice(i,i+256);
        if(append('bars',{series,rows:part.map(r=>r.row),source,received_at:observedAt}))part.forEach(r=>cache.set(r.row.time,r.hash));
      }
      // Bound only the in-memory dedup index, never archived data.
      const oldest=bars[0]?.time;for(const stamp of cache.keys())if(stamp<oldest)cache.delete(stamp);
      seen.set(identity,cache);
    },
    ranking(frame){const record=rankingRecord(frame);if(!record)return null;
      if(rankSeen.get(record.as_of)!==record.batch_id&&append('ranking',record))rankSeen.set(record.as_of,record.batch_id);
      if(rankSeen.size>1440)rankSeen.delete(rankSeen.keys().next().value);
      return record.batch_id;
    },
    async flush(){await drain();},
    async close(){closed=true;clearTimeout(retry);await drain();},
  };
}
module.exports={createResearchJournal,rankingRecord,digest};
