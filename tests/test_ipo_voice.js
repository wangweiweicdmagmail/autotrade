'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
const NOW=Date.parse('2026-11-19T15:12:17Z')/1000;
const record={id:'ipo:NASDAQ:ANT:2026-11-19',symbol:'NASDAQ:ANT',listing_date:'2026-11-19',price:101.25,observed_at:NOW,detected_at:NOW,announce_until:NOW+120};
const update=event=>({watches:[{symbol:'NASDAQ:ANT',listing_date:'2026-11-19'}],events:[event],error:''});
function setup(storage=new Map()) {let options;const spoken=[],notices=[];
 vm.runInNewContext(fs.readFileSync(require.resolve('../console/static/app.js'),'utf8'),{
  Vue:{createApp(o){options=o;return{use(){return this;},mount(){}};}},ElementPlus:{},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
  rhythmHoldingRankChanges:require('../console/static/market-rhythm').rhythmHoldingRankChanges,
 });
 const ui={...options.data(),...options.methods,speak:(text,flags)=>spoken.push({text,flags}),$notify:n=>notices.push(n),$message:{success(){}}};
 return{ui,storage,spoken,notices};
}

test('first valid price speaks with priority once across quote changes, reconnect and page refresh',()=>{
 const a=setup();a.ui.checkIpoOpenings(update(record),NOW);a.ui.checkIpoOpenings(update({...record,price:120}),NOW+1);
 assert.equal(a.spoken.length,1);assert.match(a.spoken[0].text,/A N T.*101.25/);
 assert(a.spoken[0].flags.queue&&a.spoken[0].flags.priority);assert.equal(a.notices.length,1);
 const b=setup(a.storage);b.ui.checkIpoOpenings(update(record),NOW+2);assert.equal(b.spoken.length,0);
});

test('old, future, cross-day and demo data do not produce IPO speech',()=>{
 const a=setup();a.ui.checkIpoOpenings(update(record),NOW+121);
 a.ui.checkIpoOpenings(update({...record,detected_at:NOW+1}),NOW);
 a.ui.checkIpoOpenings(update({...record,listing_date:'2026-11-18'}),NOW);
 a.ui.demoMode=true;a.ui.checkIpoOpenings(update(record),NOW);
 assert.equal(a.spoken.length,0);
});

test('voice test is clearly a readiness test and cannot mark ANT as trading',()=>{
 const a=setup();a.ui.testIpoVoice();assert.match(a.spoken[0].text,/已就绪.*等待真实成交/);
 assert.equal(a.ui.ipoOpenings.events.length,0);assert.equal(Object.keys(a.ui.notifiedIpoOpenings).length,0);
});

test('an already-open second console tab sees the persisted announcement before speaking',()=>{
 const storage=new Map(),a=setup(storage),b=setup(storage);
 a.ui.checkIpoOpenings(update(record),NOW);b.ui.checkIpoOpenings(update(record),NOW);
 assert.equal(a.spoken.length,1);assert.equal(b.spoken.length,0);
});

test('holding rank warns every minute with one popup until the holding recovers',()=>{
 const {ui,spoken,notices}=setup();
 const symbol='NASDAQ:NVDA',closed=[];
 ui.lots=[{symbol,state:'ACTIVE',filled_qty:5,side:'long'}];
 ui.bottomTab='review';
 ui.$notify=notice=>{notices.push(notice);return{close:()=>closed.push(notice)};};
 const at=minute=>Math.floor(NOW/60)*60+minute*60;
 const board=(minute,rank)=>({
  snapshot:{version:1,id:'holding-'+minute,day:'2026-11-19',as_of:at(minute)},
  top10:rank<=10?[symbol]:[],positions:{[symbol]:rank},
 });
 ui.checkHoldingRankChanges(board(0,10),at(0));
 ui.checkHoldingRankChanges(board(1,17),at(1));
 ui.checkHoldingRankChanges(board(1,17),at(1)+15);
 ui.checkHoldingRankChanges(board(2,18),at(2));
 ui.checkHoldingRankChanges(undefined,at(3));
 assert.equal(spoken.length,3);
 assert.match(spoken[0].text,/N V D A 第 17 名/);
 assert.match(spoken[1].text,/N V D A 第 18 名/);
 assert.match(spoken[2].text,/沿用上次确认榜单/);
 assert.equal(notices[0].duration,0);
 assert.match(notices[0].message,/NVDA 第 17 名/);
 assert.equal(closed.length,2);
 ui.checkHoldingRankChanges(board(4,9),at(4));
 assert.equal(closed.length,3);
 assert.equal(spoken.length,3);
 assert.equal(ui.holdingRankNotice,null);
});
