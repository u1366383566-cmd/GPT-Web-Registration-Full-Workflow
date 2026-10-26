const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const html = fs.readFileSync('webui/templates/index_legacy.html', 'utf8');
// 使用后端真实配置声明，确保旧 UI 能直接消费 API 返回的 choices。
const python = process.env.PYTHON || (fs.existsSync('.venv/bin/python') ? '.venv/bin/python' : 'python3');
const fields = JSON.parse(execFileSync(python, ['-c',
  'import json; from webui.config_editor import get_config; print(json.dumps([f for f in get_config() if f["key"] in ["PROTOCOL_HTTP_CLIENT", "CURL_CFFI_IMPERSONATE", "WREQ_EMULATION"]]))'
], {encoding: 'utf8'}));
const field = fields.find(f => f.key === 'PROTOCOL_HTTP_CLIENT');
field.value = 'curl_cffi';
function extract(name, end) {
  const start = html.indexOf(`function ${name}(`);
  assert.notEqual(start, -1);
  return html.slice(start, html.indexOf(end, start));
}
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    innerHTML: '', disabled: false, listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
  });
  return elements.get(selector);
}
const requests = [];
const select = {dataset: {key: field.key}, value: 'wreq'};
select.closest = () => select;
const profileSelects = ['CURL_CFFI_IMPERSONATE', 'WREQ_EMULATION'].map(key => {
  const el = {dataset: {key}, value: 'chrome136'};
  el.closest = () => el;
  return el;
});
const context = vm.createContext({
  CONFIG: fields, CONFIG_ACTIVE_GROUP: '功能开关', CONFIG_PENDING_UPDATES: {},
  $: element, $$: () => [select, ...profileSelects],
  esc: value => String(value ?? ''), attrEsc: value => String(value ?? ''),
  isPlaceholderEmpty: value => !value || value === '-',
  renderConfigRecommendedLinks: () => '', renderRoxyWorkspaceTools: () => '',
  bindRoxyWorkspaceTools() {}, showToast() {},
  async api(url, options) {
    requests.push({url, ...options});
    return {reloaded: true, updated: [field.key]};
  },
});
const run = code => vm.runInContext(code, context);
run(extract('renderConfigField', '\nasync function loadMailCodeCatalog'));
run(extract('renderConfigFields', '\nfunction renderConfigTabs'));
run(extract('configGroups', '\nfunction renderConfigField'));
run(extract('renderConfigPanel', '\nasync function loadConfig'));
run(extract('readConfigElementValue', "\n$('#configForm')"));
run(html.slice(html.indexOf("$('#configForm').addEventListener('input'"),
               html.indexOf('// ---------- 全局复制委托')));
(async () => {
  run('renderConfigPanel()');
  let markup = element('#configForm').innerHTML;
  assert.match(markup, /select data-key="PROTOCOL_HTTP_CLIENT"/);
  assert.match(markup, /value="curl_cffi" selected/);
  assert.match(markup, /value="wreq"/);
  assert.match(markup, /select data-key="CURL_CFFI_IMPERSONATE"/);
  assert.match(markup, /select data-key="WREQ_EMULATION"/);
  assert.match(markup, /不改变 Roxy 注册驱动/);
  element('#configForm').listeners.change({target: select});
  assert.equal(run('CONFIG_PENDING_UPDATES.PROTOCOL_HTTP_CLIENT'), 'wreq');
  run('renderConfigPanel()');
  assert.match(element('#configForm').innerHTML, /value="wreq" selected/);
  for (const el of profileSelects) element('#configForm').listeners.change({target: el});
  await element('#btnSaveConfig').listeners.click();
  assert.equal(requests[0].url, '/api/config');
  assert.equal(requests[0].method, 'POST');
  const saved = JSON.parse(requests[0].body).updates;
  assert.equal(saved.PROTOCOL_HTTP_CLIENT, 'wreq');
  assert.equal(saved.CURL_CFFI_IMPERSONATE, 'chrome136');
  assert.equal(saved.WREQ_EMULATION, 'chrome136');
  assert.equal(run("CONFIG.find(f => f.key === 'PROTOCOL_HTTP_CLIENT').value"), 'wreq');
  assert.equal(run('Object.keys(CONFIG_PENDING_UPDATES).length'), 0);
  assert.equal(element('#btnSaveConfig').disabled, false);
  const modern = fs.readFileSync('webui/templates/index.html', 'utf8');
  const start = modern.indexOf('function renderConfigSectionV2(');
  for (const f of fields) assert.equal(f.group, '功能开关');
  context.configGroupSlug = () => 'features';
  context.renderConfigSectionHead = () => '';
  context.renderConfigPlainFieldV2 = context.renderConfigField;
  context.renderRegistrationDriverField = () => '';
  context.renderFeatureSwitchField = () => '';
  run(modern.slice(start, modern.indexOf('function renderConfigLayoutV2(', start)));
  const modernMarkup = run("renderConfigSectionV2('功能开关', CONFIG)");
  assert.match(modernMarkup, /select data-key="CURL_CFFI_IMPERSONATE"/);
  assert.match(modernMarkup, /select data-key="WREQ_EMULATION"/);
  console.log('新旧 UI 协议后端及浏览器指纹：渲染、切换、保存测试通过');
})().catch(error => {console.error(error); process.exitCode = 1;});
