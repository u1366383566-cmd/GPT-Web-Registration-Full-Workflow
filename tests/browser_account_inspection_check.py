"""巡检组件真实 Chrome 布局回归（mock API，不执行账号网络请求）。

运行：.venv/bin/python tests/browser_account_inspection_check.py
需要 playwright Python 包和本机 Chrome；截图输出到临时目录。
"""
import re
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
output=Path(tempfile.mkdtemp(prefix='inspection-ui-'))
component=(root/'webui/templates/account_inspections.html').read_text()
samples='''[
{id:128,archived:'only',created_at:'2026-10-17T17:20:10',status:'running',workers:8,total_count:120,checked_count:72,skipped_count:2,invalid_count:18,live_success_count:15,live_failed_count:2,plan_success_count:13,plan_failed_count:1},
{id:127,archived:'0',created_at:'2026-10-17T16:30:00',finished_at:'2026-10-17T16:34:28',status:'completed',workers:3,total_count:86,checked_count:86,invalid_count:12,live_success_count:10,live_failed_count:2,plan_success_count:9,plan_failed_count:1},
{id:126,archived:'all',created_at:'2026-10-17T15:10:00',finished_at:'2026-10-17T15:11:08',status:'interrupted',workers:5,total_count:200,checked_count:32,invalid_count:8,live_success_count:6,live_failed_count:1,plan_success_count:5,plan_failed_count:0,error:'服务重启，巡检已中断；统计保留已完成部分'},
{id:125,archived:'only',created_at:'2026-10-17T14:00:00',finished_at:'2026-10-17T14:01:08',status:'failed',workers:3,total_count:20,checked_count:2,invalid_count:1,live_success_count:0,live_failed_count:1,plan_success_count:0,plan_failed_count:0,error:'数据库暂时不可用，请稍后重试'}
]'''
bootstrap='''<script>
window.setInterval=()=>0; // 测试手动触发相同刷新函数，避免轮询时间干扰。
let SHOW_ARCHIVED_ACCOUNTS=true;
let fixtureItems=SAMPLES;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function showToast(){} function loadAccounts(){} function loadSummary(){}
async function api(url){if(url.includes('/failures'))return {task:{archived:'only'},details_available:true,total:2,items:[{account_id:12,email:'failed@example.test',status:'failed',error:'登录验证码等待超时：mailbox unavailable',failed_at:'2026-10-18T12:00:00'},{account_id:13,email:'<img src=x>',status:'rejected',error:'<svg/onload=alert(1)>',failed_at:'2026-10-18T12:01:00'}]};return {active:fixtureItems.find(x=>x.status==='running'),total:fixtureItems.length,items:fixtureItems};}
</script>'''.replace('SAMPLES',samples)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True, channel='chrome')
    for template in ('index.html','index_legacy.html'):
        css=''.join(re.findall(r'<style>([\s\S]*?)</style>',(root/'webui/templates'/template).read_text()))
        # 隔离固定侧栏外壳，仅验证工具栏本身在窄容器中换行，不测试全站侧栏适配。
        layout=browser.new_page(viewport={'width':390,'height':844})
        source=(root/'webui/templates'/template).read_text()
        selector='#accountsToolbarV2' if template=='index.html' else '.accounts-actionbar'
        toolbar=layout.evaluate("([source,selector])=>new DOMParser().parseFromString(source,'text/html').querySelector(selector).outerHTML", [source,selector])
        layout.set_content(f'<html><head><style>{css} body {{padding-left:0;}}</style></head><body><main><section class="accounts-panel-v2 card"><div class="accounts-toolbar">{toolbar}</div></section></main></body></html>')
        for width in (320,390,768,1328):
            layout.set_viewport_size({'width':width,'height':844})
            button=layout.locator('#btnDeleteDeactivatedAccounts')
            button.scroll_into_view_if_needed()
            box=button.bounding_box()
            assert box and box['width']>0 and box['x']>=0 and box['x']+box['width']<=width, (template,width,box)
        layout.screenshot(path=str(output/f'deactivated-toolbar-{template}.png'))
        layout.close()
        page=browser.new_page(viewport={'width':1328,'height':800},device_scale_factor=1)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_content(f'<html><head><style>{css}</style></head><body><main><section class="card"><div class="head"><h3>账号管理</h3></div><div class="body">账号列表 · 一键巡检</div></section></main><div id="tab-accounts"></div><button id="btnStartInspection">一键巡检</button>{bootstrap}{component}</body></html>')
        page.evaluate('openInspectionHistory()')
        page.wait_for_selector('.inspection-table')
        assert page.locator('#inspectionHistoryTitle').evaluate('(el)=>getComputedStyle(el).color')=='rgb(23, 32, 51)'
        assert page.locator('#inspectionHistoryDialog').evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(255, 255, 255)'
        assert page.locator('.inspection-table tbody > tr:not(.inspection-error)').count()==4
        page.screenshot(path=str(output/f'inspection-history-{template}.png'))
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output/f'inspection-history-mobile-{template}.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        box=page.locator('#inspectionHistoryDialog').bounding_box()
        assert box['x']>=0 and box['x']+box['width']<=390
        page.locator('.inspection-detail-link').first.click()
        page.wait_for_selector('#inspectionFailuresDialog .inspection-failure-table')
        assert page.locator('#inspectionFailuresDialog tbody tr').count()==2
        assert page.locator('#inspectionFailuresDialog img, #inspectionFailuresDialog .inspection-reason svg').count()==0
        page.screenshot(path=str(output/f'inspection-failures-mobile-{template}.png'))
        detail_box=page.locator('#inspectionFailuresDialog').bounding_box()
        assert detail_box['x']>=0 and detail_box['x']+detail_box['width']<=390
        history_wrap=page.locator('#inspectionHistoryContent .inspection-table-wrap')
        history_wrap.evaluate('(el)=>el.scrollLeft=180')
        page.evaluate('refreshInspectionHistory()')
        page.wait_for_function('!inspectionLoading')
        assert history_wrap.evaluate('(el)=>el.scrollLeft') == 180
        # 同页轮询不能把手机横向滚动位置重置到最左侧。
        page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el.scrollLeft=180')
        page.evaluate('refreshInspectionFailures()')
        page.wait_for_function('!inspectionFailuresLoading')
        assert page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el.scrollLeft') == 180
        # 真实长文本和满页，在窄屏/横屏/桌面均不得挤出弹窗或覆盖分页。
        page.evaluate("""api=async()=>({task:{archived:'only'},details_available:true,total:21,
            items:Array.from({length:20},(_,i)=>({account_id:i+1,email:'long'.repeat(70)+'@example.test',
            status:'failed',error:'失败详情\\n'+'timeout'.repeat(280),failed_at:'2026-10-18T12:00:00'}))});
            refreshInspectionFailures()""")
        page.wait_for_function('!inspectionFailuresLoading')
        for width,height in ((320,568),(360,740),(390,844),(640,360),(768,1024),(1328,800),(1920,1080)):
            page.set_viewport_size({'width':width,'height':height})
            box=page.locator('#inspectionFailuresDialog').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=width, (template,width,box)
            assert box['y']>=0 and box['y']+box['height']<=height, (template,width,box)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            footer=page.locator('#inspectionFailuresDialog .inspection-footer').bounding_box()
            button=page.locator('#inspectionFailuresNext').bounding_box()
            assert button['y']>=footer['y'] and button['y']+button['height']<=height
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#inspectionFailuresContent').evaluate('(el)=>el.scrollTop=600')
        page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el.scrollLeft=180')
        page.locator('#inspectionFailuresContent .inspection-table-wrap').focus()
        # 数据确有变化的轮询也应保留阅读位置和键盘焦点。
        page.evaluate("const previousApi=api; api=async()=>{const r=await previousApi();r.items[0].error+='new';return r};refreshInspectionFailures()")
        page.wait_for_function('!inspectionFailuresLoading')
        assert page.locator('#inspectionFailuresContent').evaluate('(el)=>el.scrollTop')==600
        assert page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el.scrollLeft')==180
        assert page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el===document.activeElement')
        page.screenshot(path=str(output/f'inspection-failures-long-mobile-{template}.png'))
        page.evaluate('changeInspectionFailuresPage(1)')
        page.wait_for_function('!inspectionFailuresLoading')
        assert page.locator('#inspectionFailuresContent').evaluate('(el)=>el.scrollTop')==0
        assert page.locator('#inspectionFailuresContent .inspection-table-wrap').evaluate('(el)=>el.scrollLeft')==0
        page.set_viewport_size({'width':1328,'height':800})
        page.screenshot(path=str(output/f'inspection-failures-desktop-{template}.png'))
        page.keyboard.press('Escape')
        assert not page.locator('#inspectionFailuresDialog').evaluate('(el)=>el.open')
        assert page.locator('#inspectionHistoryDialog').evaluate('(el)=>el.open')
        page.set_viewport_size({'width':390,'height':844})
        page.keyboard.press('Escape')
        assert not page.locator('#inspectionHistoryDialog').evaluate('(el)=>el.open')
        page.evaluate("api=async()=>({active:null,total:fixtureItems.length,items:fixtureItems});fixtureItems=[]; refreshInspectionHistory()")
        page.evaluate('inspectionActive=false; startAccountInspection()')
        page.screenshot(path=str(output/f'inspection-start-mobile-{template}.png'))
        assert page.locator('#inspectionScopeLabel').inner_text()=='已归档'
        page.keyboard.press('Escape')
        page.evaluate('openInspectionHistory()')
        page.get_by_text('暂无巡检记录',exact=True).wait_for()
        page.set_viewport_size({'width':1328,'height':800})
        page.screenshot(path=str(output/f'inspection-empty-{template}.png'))
        # 接口字段作为文本，未知状态和错误信息不能注入 HTML。
        page.evaluate('fixtureItems=[{id:1,status:"<img src=x onerror=alert(1)>",error:"<script>alert(1)</scr"+"ipt>",total_count:0}]; refreshInspectionHistory()')
        assert page.locator('#inspectionHistoryContent img, #inspectionHistoryContent script').count()==0
        assert not errors, errors
        page.close()
    browser.close()
print(f'浏览器检查通过：新旧 UI，320–1920px/横屏，长文本/满页、刷新滚动与焦点保持、翻页重置、嵌套 Escape、空态和转义。截图：{output}')
