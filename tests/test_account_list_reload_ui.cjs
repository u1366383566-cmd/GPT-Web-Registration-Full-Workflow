const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function flush(){for(let i=0;i<16;i++)await Promise.resolve();}
function harness(template) {
  const source=fs.readFileSync(`webui/templates/${template}`, 'utf8');
  const loader=source.match(/async function loadAccounts\(\) \{[\s\S]*?\n\}\nasync function pollAccountPlanStatuses/)[0].replace(/\nasync function pollAccountPlanStatuses$/, '');
  const requests=[],renders=[],timers=new Map(),toasts=[];
  let nextTimer=0;
  const context=vm.createContext({
    SHOW_ARCHIVED_ACCOUNTS:false,ACCOUNTS:[],ACCOUNTS_TOTAL:0,PAGERS:{accounts:{page:1,size:20}},AbortController,
    getAccountsPlanFilter:()=>'',getAccountsCodexFilter:()=>'',getAccountsTotpFilter:()=>'',getAccountsQuery:()=>'',
    document:{getElementById:()=>null},$:()=>null,
    setTimeout(fn){timers.set(++nextTimer,fn);return nextTimer;},clearTimeout(id){timers.delete(id);},
    api(url,options){return new Promise((resolve,reject)=>{
      requests.push({url,resolve,reject});options?.signal?.addEventListener('abort',()=>reject(new Error('aborted')));
    });},
    renderAccounts(){renders.push(vm.runInContext('ACCOUNTS.map(a=>a.id).join(",")',context));},
    showToast(message){toasts.push(message);},
  });
  const run=code=>vm.runInContext(code,context);
  run('let accountsLoading=false;let accountsReloadPending=false;'+loader);
  return {run,requests,renders,timers,toasts,
    async resolve(i,value){requests[i].resolve(value);await flush();},
    async reject(i){requests[i].reject(new Error('offline'));await flush();},
    async expire(){for(const fn of [...timers.values()])fn();await flush();},
  };
}
(async()=>{
  for(const template of ['index.html','index_legacy.html']) {
    // 真实 api() 在响应头已到达、读取 body 时取消，也必须抛错，不能吞掉变成成功空对象。
    const source=fs.readFileSync(`webui/templates/${template}`, 'utf8');
    const apiSource=source.match(/async function api\(url, opts\) \{[\s\S]*?\n\}/)[0];
    const controller=new AbortController();
    const apiContext=vm.createContext({fetch:async()=>({ok:true,json:async()=>{controller.abort();throw new Error('body aborted');}})});
    vm.runInContext(apiSource,apiContext);
    await assert.rejects(apiContext.api('/api/accounts',{signal:controller.signal}),/body aborted/);
    // 删除后的刷新不能被正在加载的旧列表请求吞掉，更不能把已删除账号重新渲染出来。
    const h=harness(template);h.run('loadAccounts();loadAccounts();loadAccounts()');
    assert.equal(h.requests.length,1);
    await h.resolve(0,{items:[{id:2}],total:1});
    assert.equal(h.renders.length,0,`${template}: stale pre-delete list rendered`);
    assert.equal(h.requests.length,2,`${template}: post-delete refresh lost`);
    assert.equal(h.run('accountsLoading'),true);
    await h.resolve(1,{items:[],total:0});
    assert.deepEqual(h.renders,['']);assert.equal(h.run('accountsLoading'),false);
    // 页数缩小后自动补读最后一页，补读期间锁仍有效。
    h.run('PAGERS.accounts.page=9;loadAccounts()');
    await h.resolve(2,{items:[],total:21});
    assert.match(h.requests[3].url,/page=2&/);assert.equal(h.run('accountsLoading'),true);
    h.run('loadAccounts()');assert.equal(h.requests.length,4);
    await h.resolve(3,{items:[{id:21}],total:21});
    assert.equal(h.requests.length,5);assert.equal(h.run('accountsLoading'),true);
    await h.resolve(4,{items:[{id:21}],total:21});
    assert.equal(h.run('accountsLoading'),false);
    // 旧请求失败或超时也要释放锁，并执行已合并的刷新请求。
    h.run('loadAccounts();loadAccounts()');await h.reject(5);
    assert.equal(h.requests.length,7);await h.resolve(6,{items:[],total:0});
    h.run('loadAccounts();loadAccounts()');await h.expire();
    assert.equal(h.requests.length,9);assert.equal(h.run('accountsLoading'),true);
    await h.resolve(8,{items:[],total:0});
    assert.equal(h.timers.size,0);assert.equal(h.run('accountsLoading'),false);
  }
  console.log('新旧账号列表回归通过：删除后刷新合并、旧响应丢弃、缩页补读锁、失败/超时恢复。');
})().catch(error=>{console.error(error);process.exitCode=1;});
