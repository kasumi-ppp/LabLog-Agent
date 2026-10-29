const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
  try {
    const context = await browser.newContext({viewport:{width:1536,height:1024}});
    const page = await context.newPage(), errors = [];
    page.on('pageerror',e => errors.push(e.message));
    const records = Array.from({length:425},(_,index) => ({
      key:`codex:bulk-${index}`, title:`Bulk conversation ${index}`, source:'codex',
      updated_at:'2026-10-15T00:00:00Z', selected:false, revision:1,
      deleted:index === 422, excluded:index === 423, available:index !== 424, archived:false
    }));
    const listRequests = [], patchSizes = [];
    await context.route('**/api/v1/workspace/conversations**',async route => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() === 'GET' && url.pathname.endsWith('/conversations')) {
        const offset = Number(url.searchParams.get('offset') || 0);
        const limit = Number(url.searchParams.get('limit') || 50);
        listRequests.push({offset,limit});
        const items = records.slice(offset,offset + limit);
        await route.fulfill({json:{data:{items,total:records.length,selected_count:records.filter(item => item.selected).length,
          counts:{codex:records.length,workbuddy:0}}}});
        return;
      }
      if (request.method() === 'PATCH' && url.pathname.endsWith('/conversations/selection')) {
        const {changes} = request.postDataJSON();
        patchSizes.push(changes.length);
        for (const change of changes) {
          const item = records.find(record => record.key === change.key);
          assert.ok(item,`unexpected key ${change.key}`);
          item.selected = change.selected;
          item.revision++;
        }
        await route.fulfill({json:{data:changes.map(change => records.find(item => item.key === change.key))}});
        return;
      }
      await route.continue();
    });
    await page.goto(process.env.LABLOG_TEST_URL || 'http://127.0.0.1:8016');
    await page.waitForFunction(() => document.querySelectorAll('.conversation-row').length === 50);
    assert.equal(await page.locator('#select-all').innerText(),'选择当前项目');
    await page.locator('#select-all').click();
    await page.waitForFunction(() => document.querySelector('#selected-count').textContent === '422');
    assert.deepEqual(listRequests.filter(request => request.limit === 200).map(request => request.offset),[0,200,400]);
    assert.deepEqual(patchSizes,[200,200,22]);
    assert.equal(records.filter(item => item.selected).length,422);
    assert.equal(records.slice(422).filter(item => item.selected).length,0);
    assert.equal(errors.length,0,errors.join('\n'));
    await context.close();
    console.log(JSON.stringify({passed:true,checks:['all filtered pages fetched','selection writes batched at 200','deleted/excluded/unavailable omitted']}));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
