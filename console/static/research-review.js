'use strict';

const reviewActionNames = {entry:'开仓',entry_intent:'开仓计划',exit:'平仓',manual_breakeven:'手动保本',
  manual_stop_changed:'调整止损',manual_stop_failed:'止损调整失败',order_close_requested:'请求平仓',
  order_close_half:'减半仓',order_cancelled:'撤单',broker_order:'券商回报',
  candidate_order_protection:'候选持仓保护',candidate_order_protection_failed:'保护操作失败',
  auto_breakeven:'自动保本',pyramid_breakeven:'自动保本',execution_entry_stage:'执行阶段'};
const reviewActorNames = {human:'人工操作',automatic:'自动规则',protective_stop:'保护止损',
  external_guard:'外部订单守卫',broker_report:'券商确认',system_execution:'执行流程',notice:'系统提醒',unknown:'意图待核实'};
const reviewQuestionStates = {pending:'待填写',asked:'已问待答',answered:'已保存',confirmed:'已确认',revised:'已修订'};
const reviewDraftCache = new Map();
const reviewClock = new Intl.DateTimeFormat('zh-CN', {timeZone:'America/New_York',
  month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
const reviewTime = value => Number.isFinite(value)&&value>0 ? reviewClock.format(new Date(value*1000)) : '—';
const reviewNumber = value => Number.isFinite(value) ? value.toFixed(2) : '—';
const reviewSigned = value => Number.isFinite(value) ? (value>0?'+':'')+value.toFixed(2) : '—';
const reviewUnanswered = question => ['pending','asked'].includes(question.status);
const reviewDraftDirty = draft => draft.text!==draft.saved || draft.confirmed!==draft.savedConfirmed;
function reviewActionText(action) {
  const f=action.facts||{},parts=[];
  for(const [key,label] of [['avg_fill','成交均价'],['stop','初始止损'],['stop_px','止损价'],
    ['exit_px','平仓价'],['fill_px','成交价'],['fill_qty','成交股数'],['qty','股数']]) {
    if(Number.isFinite(f[key]))parts.push(label+' '+reviewNumber(f[key]));
  }
  if(f.reason||f.exit_reason)parts.push(({stop:'保护止损成交',manual:'人工平仓'})[f.reason||f.exit_reason]||f.reason||f.exit_reason);
  if(f.status)parts.push(f.status);
  if(f.action)parts.push(f.action);
  if(f.error)parts.push(f.error);
  return parts.join(' · ')||'具体价格未记录';
}

const ResearchReviewPanel = {
  props: {active:Boolean},
  data() { return {days:[],day:'',report:null,selected:'',detail:null,loading:false,detailLoading:false,
    error:'',detailError:'',questionFilter:'all',allActions:false,initialized:false,
    dayEpoch:0,detailEpoch:0,drafts:Object.fromEntries(reviewDraftCache),saving:{},saveErrors:{}}; },
  computed: {
    visibleQuestions() { return (this.detail?.questions||[]).filter(q=>this.questionFilter==='all'||reviewUnanswered(q)); },
    visibleActions() { return (this.detail?.actions||[]).filter(a=>this.allActions||
      !['broker_report','system_execution','notice'].includes(a.actor)); },
    dirtyCount() { return Object.values(this.drafts).filter(reviewDraftDirty).length; },
  },
  watch: {active(value){if(value&&!this.initialized)this.refresh();}},
  mounted() {
    this._guard=event=>{if(this.dirtyCount){event.preventDefault();event.returnValue='';}};
    window.addEventListener('beforeunload',this._guard);
    if(this.active)this.refresh();
  },
  beforeUnmount() {
    this.dayEpoch++;this.detailEpoch++;
    for(const [id,draft] of Object.entries(this.drafts))reviewDraftCache.set(id,{...draft});
    window.removeEventListener('beforeunload',this._guard);
  },
  methods: {
    time:reviewTime,number:reviewNumber,signed:reviewSigned,actionText:reviewActionText,
    actionName(kind){return reviewActionNames[kind]||kind||'操作';},
    actorName(actor){return reviewActorNames[actor]||'待核实';},
    ticker(symbol){return (symbol||'').split(':').pop();},
    sideName(side){return ({long:'做多',short:'做空',mixed:'多空混合'})[side]||'方向未知';},
    questionState(q){return reviewQuestionStates[q.status]||q.status;},
    stateName(state){return ({CLOSED:'已平仓',CANCELLED:'已撤单',ACTIVE:'持仓中',PENDING:'委托中',CLOSING:'退出中',FAILED:'失败'})[state]||state;},
    async request(path,body) {
      const response=await fetch('/api/research/workbench'+path, body===undefined
        ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const result=await response.json();
      if(!response.ok) {
        const error=new Error(typeof result.detail==='string'?result.detail:'研究请求失败，请检查输入后重试');
        error.status=response.status;throw error;
      }
      return result;
    },
    async refresh() {
      if(this.loading)return;
      this.loading=true;this.error='';
      try {
        const result=await this.request('/refresh',{});
        this.days=result.days||[];this.initialized=true;
        if(!this.day)this.day=this.days[0]?.day||'';
        if(this.day)await this.loadDay();
        else {this.report=null;this.detail=null;}
      } catch(error){this.error=error.message;}
      finally{this.loading=false;}
    },
    async loadDay() {
      const epoch=++this.dayEpoch;this.detailEpoch++;this.detail=null;this.detailLoading=false;this.detailError='';this.error='';
      const day=this.day;this.report=null;
      if(!day)return;
      try {
        const result=await this.request('/day?day='+encodeURIComponent(day),{});
        if(epoch!==this.dayEpoch)return;
        this.report=result;
        const first=result.trades.find(row=>row.trade_id===this.selected)||result.trades[0];
        this.selected='';
        if(first)await this.selectTrade(first.trade_id);
      } catch(error){if(epoch===this.dayEpoch)this.error=error.message;}
    },
    async selectTrade(identity) {
      const epoch=++this.detailEpoch;this.selected=identity;this.detailLoading=true;this.detailError='';this.detail=null;
      try {
        const detail=await this.request('/trades/'+encodeURIComponent(identity),{});
        if(epoch!==this.detailEpoch)return;
        this.detail=detail;
        for(const q of detail.questions) {
          const existing=this.drafts[q.question_id];
          if(!existing||!reviewDraftDirty(existing))
            this.drafts[q.question_id]={text:q.answer||'',saved:q.answer||'',revision:q.id,
              confirmed:q.status==='confirmed',savedConfirmed:q.status==='confirmed'};
        }
        const row=this.report?.trades.find(r=>r.trade_id===identity);
        if(row) {
          const unanswered=detail.questions.filter(reviewUnanswered).length;
          const answered=detail.questions.filter(q=>q.answer).length;
          this.report.summary.unanswered+=unanswered-row.unanswered;
          this.report.summary.answered+=answered-row.answered;
          Object.assign(row,{unanswered,answered});
        }
      } catch(error){if(epoch===this.detailEpoch)this.detailError=error.message;}
      finally{if(epoch===this.detailEpoch)this.detailLoading=false;}
    },
    dirty(q){const draft=this.drafts[q.question_id];return draft&&reviewDraftDirty(draft);},
    async save(q) {
      const id=q.question_id,draft=this.drafts[id],identity=this.detail.trade_id;
      if(this.saving[id]||!draft.text.trim())return;
      this.saving[id]=true;this.saveErrors[id]='';
      try {
        const result=await this.request('/trades/'+encodeURIComponent(identity)+'/questions/'+encodeURIComponent(id),
          {text:draft.text,revision:draft.revision,confirmed:draft.confirmed});
        draft.text=result.answer;draft.saved=result.answer;draft.revision=result.id;draft.savedConfirmed=draft.confirmed;
        reviewDraftCache.set(id,{...draft});
        if(this.detail?.trade_id===identity)Object.assign(q,result);
        const report=await this.request('/day?day='+encodeURIComponent(this.day));
        if(report.day===this.day)this.report=report;
      } catch(error) {this.saveErrors[id]=error.message;}
      finally{this.saving[id]=false;}
    },
    async reloadQuestion(q) {
      // A conflicting draft stays intact. Only its revision is rebased after explicit review.
      try {
        const detail=await this.request('/trades/'+encodeURIComponent(this.detail.trade_id),{});
        const fresh=detail.questions.find(row=>row.question_id===q.question_id);
        if(!fresh)return;
        Object.assign(q,fresh);
        this.drafts[q.question_id].revision=fresh.id;
        this.saveErrors[q.question_id]='';
      } catch(error){this.saveErrors[q.question_id]=error.message;}
    },
  },
  template:`
  <section class="review-page" aria-label="复盘研究">
    <header class="review-toolbar">
      <div><h2>复盘研究</h2><span>交易事实 · 操作过程 · 我的思考</span></div>
      <label>交易日 <select v-model="day" :disabled="loading" @change="loadDay" aria-label="复盘交易日">
        <option v-for="item in days" :key="item.day" :value="item.day">{{ item.day }} · {{ item.trades }}笔</option>
      </select></label>
      <button class="review-button" @click="refresh" :disabled="loading">{{ loading ? '同步研究索引中…' : '刷新订单' }}</button>
      <small>美东时间<span v-if="dirtyCount"> · {{ dirtyCount }}项草稿未保存</span></small>
    </header>
    <div v-if="error" class="review-error" role="alert">{{ error }} <button class="review-button" @click="refresh">重试</button></div>
    <div v-if="report" class="review-summary">
      <span><b>{{ report.summary.trades }}</b> 交易组</span>
      <span>组累计已实现 <b :class="report.summary.realized_pnl>=0?'review-positive':'review-negative'">{{ signed(report.summary.realized_pnl) }}</b> USD</span>
      <span><b>{{ report.summary.unanswered }}</b> 待解释</span><span><b>{{ report.summary.answered }}</b> 已填写</span>
      <small>费用未核对 · 跨日组按完整交易展示</small>
    </div>
    <div class="review-body">
      <aside class="review-trades" aria-label="当日交易">
        <p v-if="!report" class="review-empty">{{ loading ? '正在读取订单研究记录…' : '选择交易日查看订单' }}</p>
        <p v-else-if="!report.trades.length" class="review-empty">该日没有交易记录</p>
        <button v-for="row in report?.trades||[]" :key="row.trade_id" class="review-trade" :class="{selected:selected===row.trade_id}"
          :aria-pressed="selected===row.trade_id" @click="selectTrade(row.trade_id)">
          <div><strong>{{ row.symbols.map(ticker).join(' / ') }}</strong><span :class="row.side==='long'?'review-positive':'review-negative'">{{ sideName(row.side) }}</span>
            <b :class="row.metrics.realized_pnl>=0?'review-positive':'review-negative'">{{ signed(row.metrics.realized_pnl) }}</b></div>
          <div><span>{{ time(row.entry_at||row.created_at) }}</span><span>{{ row.states.map(stateName).join(' / ') }}</span></div>
          <div><small>{{ row.trade_id }}<span v-if="row.cross_day"> · 跨日</span></small><small class="review-question-count">{{ row.unanswered }}待解释</small></div>
        </button>
      </aside>
      <main class="review-detail" aria-label="交易复盘详情">
        <div v-if="detailLoading" class="review-empty" role="status">读取交易过程与待问问题…</div>
        <div v-else-if="detailError" class="review-error" role="alert">{{ detailError }} <button class="review-button" @click="selectTrade(selected)">重试</button></div>
        <template v-else-if="detail">
          <header class="review-detail-head">
            <h3>{{ detail.lots.map(row=>ticker(row.symbol)).filter((v,i,a)=>a.indexOf(v)===i).join(' / ') }} <span :class="detail.lots[0].side==='long'?'review-positive':'review-negative'">{{ sideName(detail.lots[0].side) }}</span></h3>
            <span>估算毛R <b>{{ signed(detail.metrics.gross_r) }}R</b></span>
            <small v-if="!detail.ranking_available">当时完整榜单未归档；保留订单原始证据</small>
          </header>
          <div class="review-lots">
            <div v-for="lot in detail.lots" :key="lot.lot_id">
              <b>{{ lot.lot_id }}</b><span>开仓 {{ time(lot.entry_broker_time||lot.entry_time) }}</span><span>平仓 {{ time(lot.closed_at) }}</span>
              <span>成交 <em class="review-entry">{{ number(lot.avg_fill) }}</em></span><span>初始止损 <em class="review-negative">{{ number(lot.init_stop_px) }}</em></span>
              <span>平仓价 {{ number(lot.exit_px) }}</span><span>股数 {{ lot.entry_cumulative_qty||'未记录' }}</span>
            </div>
          </div>
          <div class="review-columns">
            <section class="review-timeline">
              <header><h4>操作时间线</h4><label><input type="checkbox" v-model="allActions">包含券商与执行回报</label></header>
              <ol>
                <li v-for="action in visibleActions" :key="action.id" :class="'actor-'+action.actor">
                  <time>{{ time(action.ts) }}</time><div><b>{{ actionName(action.kind) }}</b><span class="review-actor">{{ actorName(action.actor) }}</span><p>{{ actionText(action) }}</p></div>
                </li>
              </ol>
            </section>
            <section class="review-thinking" aria-label="待问问题与交易思考">
              <header><h4>我的交易思考</h4><select v-model="questionFilter" aria-label="问题筛选"><option value="all">全部问题</option><option value="unanswered">只看待解释</option></select></header>
              <p class="review-help">按动作填写当时观察和操作理由。保存原文并保留修订，不自动生成已确认交易规则。</p>
              <article v-for="q in visibleQuestions" :key="q.question_id" class="review-question">
                <div class="review-question-head"><b>{{ actionName(q.action_kind) }} · {{ time(q.action_time) }}</b><span>{{ questionState(q) }}</span></div>
                <p>{{ q.action_kind==='entry' ? '为什么选择这只股票、这个方向和入场时刻？当时大盘与个股处于什么节奏？' : q.action_kind==='exit' ? '为什么在此时退出？看到了什么变化，与原计划是否一致？' : '当时观察到了什么，为什么采取这个动作？' }}</p>
                <textarea :aria-label="actionName(q.action_kind)+'的交易思考'" v-model="drafts[q.question_id].text" :disabled="saving[q.question_id]" maxlength="20000"
                  placeholder="记录你当时的判断、预期和操作理由…" rows="4"></textarea>
                <div v-if="q.answer && q.answer!==drafts[q.question_id].saved" class="review-conflict-current"><b>当前已保存版本</b><p>{{ q.answer }}</p></div>
                <div class="review-save-row">
                  <label><input type="checkbox" v-model="drafts[q.question_id].confirmed" :disabled="saving[q.question_id]">确认这条解释</label>
                  <small v-if="dirty(q)">未保存</small><small v-else-if="q.answer">已保存 · {{ time(q.recorded_at) }}</small>
                  <button class="review-button primary" @click="save(q)" :disabled="saving[q.question_id]||!drafts[q.question_id].text.trim()">{{ saving[q.question_id]?'保存中…':q.answer?'保存修订':'保存思考' }}</button>
                </div>
                <div v-if="saveErrors[q.question_id]" class="review-save-error" role="alert">{{ saveErrors[q.question_id] }} <button class="review-button" @click="reloadQuestion(q)">读取最新版本</button></div>
              </article>
              <p v-if="!visibleQuestions.length" class="review-empty">没有待解释的问题，可切换到全部问题查看已保存的思考。</p>
            </section>
          </div>
        </template>
        <div v-else class="review-empty">选择左侧交易，查看操作过程并填写思考。</div>
      </main>
    </div>
  </section>`,
};
if(typeof module!=='undefined')module.exports={ResearchReviewPanel,reviewActionText,reviewTime,reviewUnanswered};
