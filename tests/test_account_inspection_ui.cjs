const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webui/templates/account_inspections.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const empty = {active:null, total:0, items:[]};
const active = {active:{id:1}, total:1, items:[{id:1, archived:'only', status:'running', workers:4}]};
async function flush() {for (let i = 0; i < 12; i++) await Promise.resolve();}
function harness() {
  const elements = new Map(), requests = [], timers = new Map(), toasts = [];
  let nextTimer = 0, accountsReloaded = 0;
  const confirmations = [], selected = new Set([1,2]);
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'4', open:false, disabled:false, textContent:'', innerHTML:'',
      scrollTop:0, querySelector(){return null;}, closest(){return this;},
      classList:{contains:()=>false}, showModal(){this.open=true;}, close(){this.open=false;},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    document:{hidden:false, getElementById:element}, SHOW_ARCHIVED_ACCOUNTS:true, AbortController,
    ACCOUNT_SELECTED:selected, confirm(message){confirmations.push(message);return true;},
    setTimeout(callback){const id=++nextTimer;timers.set(id,callback);return id;},
    clearTimeout(id){timers.delete(id);}, setInterval(){},
    showToast(message){toasts.push(message);}, loadAccounts(){accountsReloaded++;}, loadSummary(){},
    esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));},
    api(url,options){return new Promise((resolve,reject)=>{
      const request={url,options,settled:false};
      request.resolve=value=>{request.settled=true;resolve(value);};
      request.reject=error=>{request.settled=true;reject(error);};
      requests.push(request);
      options?.signal?.addEventListener('abort',()=>request.reject(new Error('aborted')));
    });},
  });
  const run=source=>vm.runInContext(source,context);
  run(script);
  return {element,requests,timers,toasts,run,confirmations,selected,get accountsReloaded(){return accountsReloaded;},
    async resolve(index,value){requests[index].resolve(value);await flush();},
    async reject(index,error){requests[index].reject(error);await flush();},
    async expire(){for(const callback of [...timers.values()])callback();await flush();},
  };
}
(async()=>{
  // 范围以打开弹窗时为准；启动前/期间旧 GET 不能解锁，丢弃后立即补读。
  const h=harness();
  h.run('startAccountInspection()');
  assert.equal(h.element('inspectionScopeLabel').textContent,'已归档');
  h.run('SHOW_ARCHIVED_ACCOUNTS=false');
  const submit=h.run('submitAccountInspection({preventDefault(){}})');
  assert.deepEqual(JSON.parse(h.requests[1].options.body),{workers:4,archived:'only'});
  await h.resolve(0,empty);
  assert.equal(h.element('btnStartInspection').disabled,true);
  h.run('refreshInspectionHistory()');
  await h.resolve(1,{ok:true}); await submit;
  assert.equal(h.run('inspectionActive'),true);
  await h.resolve(2,empty);
  assert.equal(h.element('btnStartInspection').disabled,true);
  assert.equal(h.requests.length,4); // 不丢失启动完成时的刷新请求。
  await h.resolve(3,active);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/已归档/);
  h.run('refreshInspectionHistory()');
  await h.reject(4,new Error('network failure'));
  assert.equal(h.run('inspectionLoading'),false);
  h.run('refreshInspectionHistory()'); await h.expire();
  assert.equal(h.run('inspectionLoading'),false);
  assert.equal(h.timers.size,0);
  h.run('refreshInspectionHistory()'); await h.resolve(6,empty);
  assert.equal(h.element('btnStartInspection').disabled,false);
  assert.equal(h.accountsReloaded,1);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/暂无巡检记录/);
  h.run('refreshInspectionHistory()');
  await h.resolve(7,{active:null,total:1,items:[{
    id:8,archived:'only',status:'<img src=x>',workers:8,created_at:'2026-10-17T17:20:10',
    checked_count:44,total_count:40,skipped_count:2,invalid_count:5,
    live_success_count:3,live_failed_count:2,plan_success_count:2,plan_failed_count:1,
    error:'<script>alert(1)</script>',
  }]});
  const markup=h.element('inspectionHistoryContent').innerHTML;
  for(const pattern of [/44 \/ 40/,/跳过 2 个/,/width:100%/,/inspection-pill--unknown/,/&lt;script&gt;/])assert.match(markup,pattern);
  assert.doesNotMatch(markup,/<script>|<img/);
  assert.equal(h.element('inspectionRefresh').disabled,false);

  // POST 超时不能永久卡住，也不能假定服务端未创建任务而允许盲目重提。
  const timeout=harness();await timeout.resolve(0,empty);
  timeout.run('startAccountInspection()');
  const pending=timeout.run('submitAccountInspection({preventDefault(){}})');
  await timeout.expire();await pending;
  assert.equal(timeout.run('inspectionStarting'),false);
  assert.equal(timeout.run('inspectionUncertain'),true);
  assert.equal(timeout.element('btnStartInspection').disabled,true);
  assert.match(timeout.element('btnStartInspection').textContent,/待确认/);
  timeout.run('submitAccountInspection({preventDefault(){}})');
  assert.equal(timeout.requests.length,3);
  await timeout.resolve(2,active);
  assert.equal(timeout.run('inspectionUncertain'),false);
  assert.equal(timeout.run('inspectionActive'),true);

  // 切换页码时遇到正在加载的请求，完成后应立即读取目标页。
  const pages=harness();await pages.resolve(0,empty);
  pages.run('inspectionPage=2;refreshInspectionHistory()');
  pages.run('openInspectionHistory()');
  await pages.resolve(1,empty);
  assert.equal(pages.requests[2].url,'/api/accounts/inspections?page=1');
  await pages.resolve(2,empty);
  assert.equal(pages.run('inspectionLoading'),false);

  // POST 断连后的旧 GET 不得错误解除“状态待确认”。
  const uncertain=harness();await uncertain.resolve(0,empty);
  uncertain.run('startAccountInspection()');
  const failure=uncertain.run('submitAccountInspection({preventDefault(){}})');
  uncertain.run('refreshInspectionHistory()');
  await uncertain.reject(1,new Error('connection lost'));await failure;
  await uncertain.resolve(2,empty);
  assert.equal(uncertain.run('inspectionUncertain'),true);
  await uncertain.resolve(3,empty);
  assert.equal(uncertain.run('inspectionUncertain'),false);
  assert.equal(uncertain.element('btnStartInspection').disabled,false);
  assert.equal(uncertain.timers.size,0);

  // 失败详情入口、按任务隔离、分页、旧响应隔离和转义。
  assert.match(markup,/openInspectionFailures\(8\)/);
  const details=harness();await details.resolve(0,empty);
  details.run('openInspectionFailures(8)');
  assert.equal(details.requests[1].url,'/api/accounts/inspections/8/failures?page=1');
  details.run('openInspectionFailures(9)');
  await details.resolve(1,{task:{archived:'0'},total:1,details_available:true,items:[{account_id:1,email:'stale@example.test',error:'stale'}]});
  assert.doesNotMatch(details.element('inspectionFailuresContent').innerHTML,/stale@example/);
  assert.equal(details.requests[2].url,'/api/accounts/inspections/9/failures?page=1');
  await details.resolve(2,{task:{archived:'only'},total:21,details_available:true,items:[{
    account_id:2,email:'<img src=x>',status:'deactivated',error:'<script>alert(1)</script>',failed_at:'2026-10-18T12:00:00',
  }]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/账号已废/);
  assert.match(details.element('inspectionFailuresContent').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(details.element('inspectionFailuresContent').innerHTML,/<script>|<img/);
  assert.equal(details.element('inspectionFailuresNext').disabled,false);
  details.run('changeInspectionFailuresPage(1)');
  assert.equal(details.requests[3].url,'/api/accounts/inspections/9/failures?page=2');
  await details.resolve(3,{task:{archived:'only'},total:21,details_available:true,items:[{account_id:25,email:'last@example.test',error:'mailbox unavailable'}]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/last@example/);
  assert.equal(details.element('inspectionFailuresNext').disabled,true);
  details.run('openInspectionFailures(10)');
  assert.equal(details.element('inspectionFailuresNext').disabled,true);
  assert.doesNotMatch(details.element('inspectionFailuresSubtitle').textContent,/已归档/);
  await details.resolve(4,{task:{archived:'0'},total:0,details_available:false,items:[]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/旧任务仅保留汇总/);
  details.run('openInspectionFailures(11)');
  await details.expire();
  assert.match(details.element('inspectionFailuresContent').innerHTML,/暂时无法读取/);
  assert.equal(details.element('inspectionFailuresRefresh').disabled,false);
  details.run('refreshInspectionFailures()');
  await details.resolve(6,{task:{archived:'0'},total:0,details_available:true,items:[]});
  assert.match(details.element('inspectionFailuresContent').innerHTML,/暂无查活失败或已废跳过记录/);
  assert.equal(details.timers.size,0);

  // 同页 DOM 不变时跳过重建；有变化保留阅读位置，翻页重置。
  details.run(`(() => {
    let markup = 'old', writes = 0;
    let wrap = {scrollLeft:0};
    const body = {scrollTop:0};
    const content = {
      closest:()=>body, querySelector:()=>wrap,
      get innerHTML(){return markup;},
      set innerHTML(value){markup=value;writes++;wrap={scrollLeft:0};body.scrollTop=0;},
    };
    renderInspectionContent(content, 'first', 'page:1');
    wrap.scrollLeft=180;body.scrollTop=600;
    renderInspectionContent(content, 'first', 'page:1');
    if(writes!==1 || wrap.scrollLeft!==180 || body.scrollTop!==600)throw Error('unchanged render reset');
    renderInspectionContent(content, 'changed', 'page:1');
    if(writes!==2 || wrap.scrollLeft!==180 || body.scrollTop!==600)throw Error('refresh scroll reset');
    renderInspectionContent(content, 'second', 'page:2');
    if(wrap.scrollLeft!==0 || body.scrollTop!==0)throw Error('page scroll not reset');
  })()`);

  // 一键删除已废：确认范围、启动时快照、防重复提交和选择清理。
  const deletion=harness();await deletion.resolve(0,empty);
  const deleting=deletion.run('deleteDeactivatedAccounts()');
  assert.match(deletion.confirmations[0],/已归档/);
  assert.match(deletion.confirmations[0],/不限当前页/);
  assert.deepEqual(JSON.parse(deletion.requests[1].options.body),{archived:'only'});
  deletion.run('SHOW_ARCHIVED_ACCOUNTS=false;deleteDeactivatedAccounts()');
  assert.equal(deletion.requests.length,2);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,true);
  await deletion.resolve(1,{ok:true,deleted_count:2});await deleting;
  assert.equal(deletion.selected.size,0);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,false);
  const retried=deletion.run('deleteDeactivatedAccounts()');
  assert.deepEqual(JSON.parse(deletion.requests[2].options.body),{archived:'0'});
  await deletion.reject(2,new Error('offline'));await retried;
  assert.match(deletion.toasts.at(-1),/核实/);
  assert.equal(deletion.element('btnDeleteDeactivatedAccounts').disabled,false);
  deletion.run('confirm=()=>false;deleteDeactivatedAccounts()');
  assert.equal(deletion.requests.length,3);
  // 已废跳过能独立进入详情，但不冒充查活失败。
  deletion.run('refreshInspectionHistory()');
  await deletion.resolve(3,{active:null,total:1,items:[{id:6,status:'completed',total_count:2,
    checked_count:0,skipped_count:2,deactivated_skipped_count:2,live_failed_count:0}]});
  assert.match(deletion.element('inspectionHistoryContent').innerHTML,/已废 2/);
  assert.match(deletion.element('inspectionHistoryContent').innerHTML,/失败详情（2）/);
  deletion.run('openInspectionFailures(6)');
  await deletion.resolve(4,{task:{archived:'0'},details_available:true,total:2,
    items:[{account_id:1,email:'dead@example.test',status:'deactivated_skipped',error:'已废跳过'}]});
  assert.match(deletion.element('inspectionFailuresContent').innerHTML,/已废跳过/);

  const initial=harness();await initial.reject(0,new Error('offline'));
  assert.match(initial.element('inspectionHistoryContent').innerHTML,/暂时无法读取/);
  assert.equal(initial.element('inspectionRefresh').disabled,false);
  console.log('巡检 UI 验收通过：范围、旧响应隔离、GET/POST 超时、未知启动结果防重、分页补读、统计与转义。');
})().catch(error=>{console.error(error);process.exitCode=1;});
