// Offline DOM contract checks: no browser, network or application server.
const fs = require('fs'); const vm = require('vm'); const assert = require('assert');
const nodes = new Map();
function node(key) {
  if (!nodes.has(key)) nodes.set(key, {innerHTML: '', textContent: '', value: '', hidden: false, disabled: false,
    dataset: {}, events: {}, classList: {add(){}, remove(){}},
    addEventListener(name, handler){this.events[name] = handler;},
    setAttribute(name,value){this[name]=value;}, removeAttribute(name){delete this[name];},
    querySelector(selector){return node(selector);}, querySelectorAll(){return [];},
    showModal(){this.open=true;}, close(){this.open=false;}, reset(){}, scrollHeight: 500, scrollTop: 0});
  return nodes.get(key);
}
const storage = new Map(); const calls=[];
const fixture = JSON.parse(fs.readFileSync(require('path').join(process.argv[2], '../tests/fixtures/demo.json')));
const item = {id:'demo-event', title:'<script>bad()</script>长标题', source_label:'活动记录', state_label:'需你处理', status:'attention', revision:'v1', background:'事实', reason:'本人确认', evidence_summary:'证据', completion_rule:'真实回执', question:'发生了吗？', recommendation:'先记录意愿', answer_options:[{value:'local:synthetic',label:'先保留',full:'先保留，仅限合成材料，核对后再决定。'}]};
let messages = []; let failNextDraft = false;
let closed = false; const actions = [];
const scopes = ['records','automatic','waiting','closed'].map(scope => {const button=node('scope-'+scope);button.dataset.issueScope=scope;return button;});
const document = {querySelector: node, querySelectorAll(selector){
  if(selector==='[data-issue-scope]') return scopes;
  if(selector==='[data-answer-index]') return [...node('#needs-detail').innerHTML.matchAll(/data-answer-index="(\d+)"/g)].map(match=>{const button=node('answer-'+match[1]);button.dataset.answerIndex=match[1];return button;});
  return [];
}, addEventListener(){}, documentElement: node('html'), body: node('body')};
node('meta[name="csrf-token"]').content='test';
const context=vm.createContext({document, localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
  console, Intl, Date, Map, setTimeout, clearTimeout, crypto:{randomUUID:()=> 'test-'+calls.length},
  fetch:async (path, opts) => {
    calls.push({path,opts}); let body={};
    if(path.endsWith('/draft') && failNextDraft){failNextDraft=false;throw new Error('synthetic autosave failure');}
    if(path==='/api/state') body={status:'ok',metrics:{needs:closed?0:1,automatic:0},sources:[],systems:[],recent_resolved:[],automatic:[],history:[],events:[]};
    else if(path.endsWith('/codex-continue')) body={phase:'delivered',message:'Codex 已收到接手内容'};
    else if(path.endsWith('/action')) {actions.push(JSON.parse(opts.body));closed=true;body={message:'已在本机记录，待同步'};}
    else if(path.endsWith('/conversation')) body={messages:[...messages],draft:'',notice:'未连接任何模型'};
    else if(path.endsWith('/message')) {messages.push({author:'user',content:JSON.parse(opts.body).content}); body={saved:true};}
    else if(path.includes('?scope=')) {const scope=path.split('=')[1];body={items:closed?(scope==='closed'?[{...item,status:'closed'}]:[]):[item],counts:{attention:closed?0:1,closed:closed?1:0,all:1},status:'ok'};}
    else body=closed?{...item,status:'closed'}:item;
    return {ok:true,json:async()=>body};
  }});
for(const file of ['app.js','workbench.js']) vm.runInContext(fs.readFileSync(process.argv[2]+'/'+file,'utf8'),context);
async function main(){
  // Refresh feedback must survive failures and never submit twice while busy.
  await vm.runInContext('loadAll()',context);
  vm.runInContext('renderState({...ui.core,status:"unavailable"})',context);
  assert.equal(node('#maintenance-banner').hidden,false);
  vm.runInContext('renderState({...ui.core,status:"ok"})',context);
  assert.equal(node('#maintenance-banner').hidden,true);
  const originalFetch=context.fetch; let releaseRefresh; let refreshCalls=0;
  context.fetch=async()=>{refreshCalls++;await new Promise(resolve=>releaseRefresh=resolve);throw new Error('合成刷新失败');};
  const pending=vm.runInContext('loadAll(true)',context);
  assert.equal(node('#sync-all').disabled,true);
  await vm.runInContext('loadAll(true)',context);
  assert.equal(refreshCalls,1); releaseRefresh(); await pending;
  assert.equal(node('#sync-all').disabled,false);
  assert.equal(node('#maintenance-banner').textContent,'合成刷新失败');
  context.fetch=originalFetch; await vm.runInContext('loadAll()',context);
  assert.equal(node('#maintenance-banner').hidden,true);
  calls.length=0;

  assert.equal(vm.runInContext('count(null)',context),'—');
  assert.equal(vm.runInContext('count(undefined)',context),'—');
  assert.equal(vm.runInContext('count(0)',context),'0');
  assert.equal(vm.runInContext('formatTime("2026-11-09T00:00:00Z")',context),'08:00');
  assert.equal(vm.runInContext('formatTime("2026-11-09T00:00:00")',context),'—');
  vm.runInContext('setAppearance("dark")',context);
  assert.equal(node('html').dataset.theme,'dark');
  assert.equal(storage.get('psi-sync-center:appearance'),'dark');
  await vm.runInContext('workbench.open("demo-event")',context);
  assert(node('#needs-detail').innerHTML.includes('&lt;script&gt;'));
  assert(!node('#needs-detail').innerHTML.includes('<script>'));
  assert(node('#case-conversation').innerHTML.includes('和 AI 一起处理'));
  assert(node('#case-conversation').innerHTML.includes('事项和你的回复会一起交给 Codex'));
  assert(!node('#case-conversation').innerHTML.includes('保存笔记'));
  assert(node('#case-conversation').innerHTML.includes('写下你的想法'));
  assert(node('#case-conversation').innerHTML.includes('本问题的对话'));
  assert(node('#case-conversation').innerHTML.includes('一起判断下一步'));
  assert(!node('#case-conversation').innerHTML.includes('系统提示'));
  assert(node('#needs-detail').innerHTML.indexOf('data-answer-index') < node('#needs-detail').innerHTML.indexOf('具体是什么'));
  messages.push({author:'user', content:'原本机笔记', created_at:new Date().toISOString()});
  await vm.runInContext('workbench.open("demo-event")',context);
  assert(node('#case-conversation').innerHTML.includes('原本机笔记'));
  assert(!node('#case-conversation').innerHTML.includes('conversation-onboarding'));
  assert(!node('#case-conversation').innerHTML.includes('系统提示'));
  node('#case-input').value='仍会自动保存';
  node('#case-input').events.input();
  await new Promise(resolve=>setTimeout(resolve,400));
  assert.equal(JSON.parse(calls.filter(c=>c.path.endsWith('/draft')).at(-1).opts.body).content,'仍会自动保存');
  assert.equal(calls.filter(c=>c.path.endsWith('/message')).length,0);
  assert(calls.every(c=>c.path.startsWith('/api/workbench/')));
  assert(calls.every(c=>c.opts.headers['X-PSI-Session']==='test'));
  vm.runInContext('workbench.invalidate()',context);
  assert.equal(node('#case-conversation').innerHTML,'');
  assert(!node('#needs-detail').innerHTML.includes('&lt;script&gt;'));
  const detailReads = calls.filter(c=>c.path==='/api/workbench/demo-event').length;
  await vm.runInContext('showPage("records", "demo-event")',context);
  assert.equal(calls.filter(c=>c.path==='/api/workbench/demo-event').length-detailReads,1, 'history navigation reads detail only once');
  // Round 3: verify the actual detail and list markup, using synthetic API data.
  const original = {...item};
  item.question_subject = '合成资产「甲，乙」<主题>';
  item.question = '签名如何安排？';
  item.recommendation = '先完成只读核验，之后由本人完成签名';
  item.answer_options = [];
  await vm.runInContext('workbench.open("demo-event")',context);
  let brief = node('#needs-detail').innerHTML.match(/<section class="decision-brief">(.*?)<\/section>/s)[1];
  assert(brief.includes('<span class="eyebrow">合成资产「甲，乙」&lt;主题&gt;</span><h3>签名如何安排？</h3>'));
  assert(!node('#needs-detail').innerHTML.includes('data-answer-index'));
  assert(node('#needs-detail').innerHTML.includes('来源提供的待确认方案</span><p>'+item.recommendation+'</p>'));
  item.question_subject = original.title;
  item.question = '这件事需要你怎么定？';
  await vm.runInContext('workbench.open("demo-event")',context);
  brief = node('#needs-detail').innerHTML.match(/<section class="decision-brief">(.*?)<\/section>/s)[1];
  assert(brief.includes('&lt;script&gt;bad()&lt;/script&gt;长标题'));
  assert(brief.includes('<h3>这件事需要你怎么定？</h3>'));
  assert(!brief.includes('签名如何安排'));
  item.list_summary = '由本人确认合成用途';
  await vm.runInContext('workbench.load()',context);
  assert(node('#needs-list').innerHTML.includes('<span>由本人确认合成用途</span>'));
  item.list_summary = '';
  await vm.runInContext('workbench.load()',context);
  assert(node('#needs-list').innerHTML.includes('<span></span>'), 'empty replacement must not revive a template or reason');
  delete item.question_subject; delete item.list_summary; Object.assign(item, original);
  await vm.runInContext('workbench.open("demo-event")',context);
  vm.runInContext('workbench.bind()',context);
  node('answer-0').events.click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(actions.length,0,'preview never records a Core/local completion');
  assert.equal(calls.filter(c=>c.path.endsWith('/codex-continue')).length,1);
  assert.equal(JSON.parse(calls.filter(c=>c.path.endsWith('/codex-continue')).at(-1).opts.body).content,'我的选择：'+item.answer_options[0].full);
  assert(!node('#codex-dialog').open);
  assert(!closed,'handoff never closes the underlying issue');
  node('#case-input').value='我的合成补充';
  await node('#case-codex').events.click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(JSON.parse(calls.filter(c=>c.path.endsWith('/codex-continue')).at(-1).opts.body).content,'我的合成补充');
  assert.equal(calls.filter(c=>c.path.endsWith('/codex-continue')).length,2);
  assert(!node('#codex-dialog').open);
  assert(calls.every(c=>c.path.startsWith('/api/')),'no external requests');
  node('#case-input').value='自动保存失败后仍保留';failNextDraft=true;
  node('#case-input').events.input();
  await new Promise(resolve=>setTimeout(resolve,400));
  await node('#case-codex').events.click();
  const last=calls.filter(c=>c.path.endsWith('/codex-continue')).at(-1);
  assert.equal(JSON.parse(last.opts.body).draft,'自动保存失败后仍保留');
  assert.equal(calls.filter(c=>c.path.endsWith('/codex-continue')).length,3);
  item.responsibility='ai'; item.responsibility_label='AI 负责'; item.progress='blocked';
  item.state_label='受阻'; item.basis='技术原因已核清'; item.next_step='AI继续检查'; item.question='';
  await vm.runInContext('workbench.open("demo-event")',context);
  assert(!node('#needs-detail').innerHTML.includes('data-case-action'));
  assert(!node('#needs-detail').innerHTML.includes('data-answer-index'));
  assert(node('#needs-detail').innerHTML.includes('AI继续检查'));
  assert(node('#needs-detail').innerHTML.includes('查看依据与完成条件'));
  item.responsibility='user_decision';item.question='保留哪个个人输入码？';
  await vm.runInContext('workbench.open("demo-event")',context);
  assert(node('#needs-detail').innerHTML.includes('保留哪个个人输入码？'));
  assert(!node('#needs-detail').innerHTML.includes('确认方案'));
  // Sidebar routing must reset hidden filters and never retain an AI detail under Problems.
  const routingFetch=context.fetch;
  context.fetch=async(path,opts)=>{
    if(path.includes('?scope=')) {
      calls.push({path,opts});
      const scope=path.split('=')[1];
      const rows=scope==='attention' ? [] : [{...item,responsibility:'ai',status:'automatic'}];
      return {ok:true,json:async()=>({items:rows,counts:{attention:0,records:1,automatic:1,waiting:0,closed:0},status:'ok'})};
    }
    return routingFetch(path,opts);
  };
  await vm.runInContext('showPage("records")',context);
  assert.equal(node('#workbench-filters').hidden,false);
  assert(calls.some(c=>c.path==='/api/workbench?scope=records'));
  await vm.runInContext('showPage("needs-user")',context);
  assert.equal(node('#workbench-filters').hidden,true);
  assert.equal(node('#page-title').textContent,'问题');
  assert.equal(calls.at(-1).path,'/api/workbench?scope=records');
  assert(!node('#problem-summary').hidden);
  assert(node('#problem-summary').innerHTML.includes('当前没有需要你处理的问题'));
  assert(node('#problem-summary').innerHTML.includes('查看全部处理记录'));
  assert(node('#case-workbench').hidden);
  assert(!node('#needs-detail').innerHTML.includes('保留哪个个人输入码'));
  assert.equal(node('#case-conversation').innerHTML,'');
  await vm.runInContext('showPage("records")',context);
  await vm.runInContext('loadAll()',context);
  assert.equal(calls.filter(c=>c.path.includes('?scope=')).at(-1).path,'/api/workbench?scope=records');
  context.fetch=async(path)=>({ok:true,json:async()=>({
    items:path.endsWith("=attention") ? [] : [{...item,review_valid:false}],
    counts:{attention:null,records:null},status:"partial"
  })});
  await vm.runInContext('showPage("needs-user")',context);
  assert(node('#problem-summary').innerHTML.includes('其中 1 条需要重新核查'));
  assert(!node('#problem-summary').innerHTML.includes('当前没有需要你处理的问题'));
  assert(!node('#problem-summary').innerHTML.includes('data-open-record'));
  assert(!node('#problem-summary').innerHTML.includes('长标题'));
  context.fetch=routingFetch;
  await vm.runInContext('workbench.open("demo-event")',context);
  const edit = value => { node('#case-input').value = value; node('#case-input').events.input(); };
  const leave = () => vm.runInContext('workbench.prepareToClose()',context);
  const resume = () => vm.runInContext('workbench.resumeEditing()',context);
  const drafts = () => calls.filter(call => call.path.endsWith('/draft'));
  edit('immediate close');
  failNextDraft = true;
  assert.equal(await leave(),false);
  assert.equal(document.body.inert,false);
  assert.equal(node('#case-input').value,'immediate close');
  assert.equal(await leave(),true);
  assert.equal(JSON.parse(drafts().at(-1).opts.body).content,'immediate close');
  const countAfterSave = drafts().length;
  assert.equal(await leave(),true);
  assert.equal(drafts().length,countAfterSave,'viewing a clean record does not write');
  resume();
  edit('');
  await vm.runInContext('workbench.open("demo-choice")',context);
  edit('second item');
  assert.equal(await leave(),true);
  const saved = drafts().slice(countAfterSave).map(c=>[c.path,JSON.parse(c.opts.body).content]);
  assert.deepEqual(saved,[['/api/workbench/demo-event/draft',''],['/api/workbench/demo-choice/draft','second item']]);
  resume();
  let finishHandoff;
  let handoffs = 0;
  context.fetch=async (path,opts)=>{
    if (!path.endsWith('/codex-continue')) return routingFetch(path,opts);
    handoffs++;
    await new Promise(resolve=>{finishHandoff=resolve;});
    return {ok:true,json:async()=>({phase:'uncertain',message:'fixture pending receipt'})};
  };
  const sending=node('#case-codex').events.click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(await leave(),false,'in-flight delivery keeps the workbench open');
  assert.equal(handoffs,1,'closing must never re-submit');
  finishHandoff(); await sending;
  assert.equal(await leave(),true);resume();
  context.fetch=routingFetch;
  context.fetch = originalFetch;
  item.synthetic = true;
  await vm.runInContext('workbench.open("demo-event")', context);
  assert(node('#case-conversation').innerHTML.includes('草稿保存在本机。'));
  assert(!node('#needs-detail').innerHTML.includes('点击后直接进入 Codex'));
  assert.equal(node('#case-codex').disabled, true);
  const beforeDemoClick = calls.length;
  await node('#case-codex').events.click();
  assert.equal(calls.length, beforeDemoClick, 'example opens no external handoff');
  console.log('frontend checks passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
