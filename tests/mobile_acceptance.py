"""Phone-sized browser acceptance against tests/preview_server.py only."""
import json,urllib.request,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
BASE='http://127.0.0.1:8794/'
def wait_state(test,page):
 deadline=time.monotonic()+15
 while time.monotonic()<deadline:
  state=json.load(urllib.request.urlopen(BASE+'api/state'))
  if test(state):return
  page.wait_for_timeout(200)
 raise AssertionError('State condition not reached')
def wait_discovery(page):
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  jobs=json.load(urllib.request.urlopen(BASE+'api/discovery/history'))['jobs']
  if any(j['status']=='done' for j in jobs):return
  page.wait_for_timeout(200)
 raise AssertionError('Discovery did not finish')
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True)
 context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto(BASE);expect(page.locator('#add-download')).to_be_visible();assert page.locator('nav button').count()==3
 page.locator('nav [data-page=files]').click();expect(page.locator('.month-card')).to_have_count(2)
 page.locator('#select-mode').click();page.locator('#select-unpublished').click();expect(page.locator('#selected-expose')).to_have_text('公开 2 个')
 page.locator('#selected-expose').click();expect(page.locator('#modal')).to_contain_text('2 个文件');page.locator('#confirm').click()
 wait_state(lambda s:sum(f['zone']=='temporary' for f in s['files'])==2,page)
 page.wait_for_selector('#undo-batch');page.locator('#undo-batch').click()
 wait_state(lambda s:sum(f['zone']=='hidden' for f in s['files'])==2,page)
 page.locator('#select-mode').click();page.locator('[data-open-month="2026-10"]').click();page.locator('[data-file-detail]').filter(has_text='旅行').click();page.locator('#detail-tags').click()
 page.locator('#tag-editor .tag-input').fill('电影');page.locator('[data-new-tag]').click();page.locator('#save-tags').click()
 wait_state(lambda s:any('电影' in f['tags'] for f in s['files']),page)
 page.locator('#show-filters').click();page.locator('#file-tag-search').fill('电');expect(page.locator('[data-tag-choice]')).to_have_count(1);page.locator('[data-tag-choice]').click();page.locator('#filter-apply').click();expect(page.locator('.file')).to_have_count(1)
 page.locator('#search').fill('旅行');page.reload();expect(page.locator('#search')).to_have_value('旅行');expect(page.locator('.file')).to_have_count(1)
 page.locator('#select-mode').click();page.locator('#select-all').check();page.locator('#selection-more').click();page.locator('[data-selection-action=tags]').click();page.locator('#bulk-tags .tag-input').fill('视频');page.locator('[data-new-tag]').click();page.locator('#bulk-tag-save').click()
 wait_state(lambda s:any('电影' in f['tags'] and '视频' in f['tags'] for f in s['files']),page);expect(page.locator('#modal')).not_to_be_visible()
 page.locator('nav [data-page=download]').click();page.locator('#add-download').click();expect(page.locator('#url')).to_be_visible();page.locator('[data-sheet-close]').click()
 page.locator('#app-menu').click();page.locator('#menu-maintenance').click();expect(page.locator('#cleanup-residuals')).to_be_visible();page.locator('[data-restart=nas]').click();expect(page.locator('#restart-send')).to_be_disabled();page.locator('#restart-cancel').click()
 page.locator('nav [data-page=explore]').click();page.locator('#explore-url').fill('demo');page.locator('.explore-go').click();expect(page.locator('#discovery-keyword')).to_have_value('demo')
 page.locator('#discovery-site').fill('https://demo.example/');page.locator('#discovery-form > details.sub-options').evaluate('(e)=>e.open=true');page.locator('#discovery-depth').select_option('3');page.locator('#discovery-published-after').fill('2026-10-01');page.locator('#discovery-start').click()
 wait_discovery(page)
 page.wait_for_selector('[data-discovery-download]');expect(page.locator('#discovery-depth')).to_have_value('3')
 page.locator('#manual-explore').click();page.locator('#explore-url').fill('https://demo.example/play/demo');page.locator('.explore-go').click();page.wait_for_selector('#browser-downloads');expect(page.locator('#browser-downloads')).to_contain_text('视频')
 page.locator('#browser-downloads').click();expect(page.locator('#browser-results')).to_be_visible();expect(page.locator('#explore-file-type')).to_be_visible();page.wait_for_timeout(250);page.screenshot(path='/tmp/vault-mobile-resources.png')
 page.locator('#close-browser-results').click();expect(page.locator('#browser-results')).to_be_hidden()
 # Browse navigation/reload restores saved page and its result entry.
 page.reload();page.wait_for_selector('#browser-downloads');expect(page.locator('#browser-downloads')).to_contain_text('发现')
 assert not errors,errors
 print(json.dumps({'phone':'390×844','flows':['月份公开','撤回','标签编辑','实时标签筛选','筛选刷新恢复','批量标签','添加面板','重启确认','关键词探索与日期深度','浏览结果面板','浏览恢复'],'page_errors':errors},ensure_ascii=False))
 # Narrow phone layout and desktop overflow checks.
 for width,height in [(320,740),(430,932),(1280,900)]:
  page.set_viewport_size({'width':width,'height':height});page.locator('nav [data-page=files]').click();page.locator('#select-mode').click();page.wait_for_timeout(200)
  assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),width
  page.screenshot(path=f'/tmp/vault-mobile-{width}.png')
 browser.close()
