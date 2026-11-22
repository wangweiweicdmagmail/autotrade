const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {ResearchReviewPanel:panel,reviewActionText}=require('../console/static/research-review');
const question={question_id:'q',id:'r1',trade_id:'t',action_kind:'entry',action_time:100,status:'pending'};
const detail=id=>({trade_id:id,lots:[{symbol:'NASDAQ:TEST',side:'long'}],questions:[{...question}],actions:[]});
const report=()=>({day:'2026-11-20',trades:[{trade_id:'t',unanswered:0,answered:0}],summary:{unanswered:0,answered:0}});
function ui() {
  const instance={...panel.data(),...panel.methods,report:report(),day:'2026-11-20'};
  return instance;
}

test('refresh loads the latest day and creates a bounded, editable per-trade view',async()=>{
  const view=ui(),requests=[];
  view.day='';
  view.request=async(url,body)=>{
    requests.push(url);
    if(url==='/refresh')return {days:[{day:'2026-11-20',trades:1}]};
    if(url.startsWith('/day'))return report();
    return detail('t');
  };
  await view.refresh();
  assert.equal(view.day,'2026-11-20');assert.equal(view.selected,'t');
  assert.equal(view.drafts.q.revision,'r1');assert.equal(view.report.summary.unanswered,1);
  assert.deepEqual(requests,['/refresh','/day?day=2026-11-20','/trades/t']);
});

test('answers use exact group, question and revision; refresh does not erase edits',async()=>{
  const view=ui();view.request=async()=>detail('t');
  await view.selectTrade('t');
  view.drafts.q.text='大盘支撑，个股转强';
  await view.selectTrade('t');
  assert.equal(view.drafts.q.text,'大盘支撑，个股转强');
  const requests=[];
  view.request=async(url,body)=>{
    requests.push({url,body});
    return body?{...question,id:'r2',answer:body.text,status:'answered'}:report();
  };
  await view.save(view.detail.questions[0]);
  assert.equal(requests[0].url,'/trades/t/questions/q');
  assert.equal(requests[0].body.revision,'r1');
  assert.equal(view.drafts.q.revision,'r2');assert.equal(view.dirty(view.detail.questions[0]),false);
  assert.equal(view.detail.questions[0].status,'answered');
});

test('conflicts retain the draft and explicit reload exposes the newer answer',async()=>{
  const view=ui();view.request=async()=>detail('t');await view.selectTrade('t');
  view.drafts.q.text='我的草稿';
  view.request=async()=>{throw new Error('已被其他会话更新');};
  await view.save(view.detail.questions[0]);
  assert.equal(view.drafts.q.text,'我的草稿');assert.match(view.saveErrors.q,/其他会话/);
  view.request=async()=>({...detail('t'),questions:[{...question,id:'r3',answer:'新保存的解释',status:'answered'}]});
  await view.reloadQuestion(view.detail.questions[0]);
  assert.equal(view.drafts.q.revision,'r3');assert.equal(view.drafts.q.text,'我的草稿');
  assert.equal(view.detail.questions[0].answer,'新保存的解释');
});

test('late responses cannot replace a newly selected trade or date',async()=>{
  const view=ui();let finish;
  view.request=url=>url==='/trades/old'?new Promise(resolve=>finish=resolve):Promise.resolve(detail('new'));
  const old=view.selectTrade('old');await view.selectTrade('new');finish(detail('old'));await old;
  assert.equal(view.detail.trade_id,'new');assert.equal(view.detailLoading,false);
  let oldDay;
  view.request=url=>url.includes('2026-11-19')?new Promise(resolve=>oldDay=resolve):
    Promise.resolve(url.startsWith('/day')?report():detail('t'));
  view.day='2026-11-19';const loading=view.loadDay();view.day='2026-11-20';await view.loadDay();
  oldDay({...report(),day:'2026-11-19'});await loading;
  assert.equal(view.report.day,'2026-11-20');
});

test('technical callbacks are optional and user text stays escaped',()=>{
  const view=ui();view.detail={actions:[{actor:'human'},{actor:'broker_report'},{actor:'system_execution'}],
    questions:[{status:'pending'},{status:'answered'}]};
  assert.equal(panel.computed.visibleActions.call(view).length,1);
  view.allActions=true;assert.equal(panel.computed.visibleActions.call(view).length,3);
  view.questionFilter='unanswered';assert.equal(panel.computed.visibleQuestions.call(view).length,1);
  assert.equal(reviewActionText({facts:{avg_fill:100,stop:99,reason:'manual'}}),'成交均价 100.00 · 初始止损 99.00 · 人工平仓');
  assert.doesNotMatch(panel.template,/v-html/);
  assert.match(panel.template,/v-model="drafts\[q.question_id\].text"/);
  const html=fs.readFileSync(path.join(__dirname,'../console/static/index.html'),'utf8');
  assert.match(html,/selectWorkspace\('research', 'review'\)/);
  assert.match(html,/<research-review-panel/);
});

test('research requests never use trading APIs and failures are visible',async()=>{
  const view=ui();let captured;
  const original=global.fetch;
  global.fetch=async(url,options)=>{captured={url,options};return {ok:false,status:403,json:async()=>({detail:'研究数据只允许本机读取'})};};
  try {
    await assert.rejects(view.request('/refresh',{}),/只允许本机/);
    assert.equal(captured.url,'/api/research/workbench/refresh');
    assert.equal(captured.options.headers['Content-Type'],'application/json');
  } finally{global.fetch=original;}
});
