'use strict';
const {createCalculator,getDefinition}=require('./pinets_runtime');
const {PINE_MODELS,tsFromPeriod,ssFromPeriod,fvgFromPeriod,attackFromPeriod}=require('./local_snapshot');
const {emaFromPeriods}=require('./ema');
const version=require('../package-lock.json').packages['node_modules/pinets'].version;
const keys=['e9','e20','e7v2','golden','trend','fvg','smart','attack'];

async function computeHistory(dataset) {
  const series={};
  for(const key of keys) {
    const rows=await createCalculator(getDefinition(key),{version})(dataset);
    series[key]=new Map(rows.map(row=>[row.time,row]));
  }
  const newest=[...dataset.bars].reverse().map(row=>({...row,max:row.high,min:row.low}));
  let lastDir=null;
  return dataset.bars.map((bar,index)=>{
    const period=key=>({$time:bar.time,...series[key].get(bar.time)});
    const fvg=period('fvg');
    if(fvg.KFVG110)lastDir=Math.sign(fvg.KFVG110);
    return {...bar,ts:{...tsFromPeriod(period('trend')),...ssFromPeriod(period('smart')),
      ema_9:emaFromPeriods(newest,dataset.bars.length-1-index,9)},
      fvg:fvgFromPeriod({...fvg,Last_Direction:lastDir}),attack:attackFromPeriod(period('attack')),
      ...Object.fromEntries(PINE_MODELS.filter(m=>(m.interval_seconds??300)===300)
        .map(model=>[model.realtime_key,series[model.realtime_key].get(bar.time)]))};
  });
}
module.exports={computeHistory};
