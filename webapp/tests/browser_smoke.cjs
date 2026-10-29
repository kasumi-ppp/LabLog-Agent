// Run against an isolated server/config. Never invokes DingTalk or submit.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
  const page = await browser.newPage({viewport:{width:1536,height:1024}, deviceScaleFactor:1});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  const base = process.env.LABLOG_TEST_URL || 'http://127.0.0.1:8016';
  await page.goto(base);
  await page.waitForSelector('textarea[data-field="completed"]');
  const day = await page.locator('#date').inputValue();
  const work = [
    {title:'操作面板布局开发',summary:'完成面板基础布局与状态显示，等待运行时联调验证。',project:'UE 盾构机项目',status:'implemented_unverified'},
    {title:'状态控件联调',summary:'正在补充连接异常时的状态切换处理。',project:'UE 盾构机项目',status:'in_progress'},
    {title:'日志持久化方案讨论',summary:'梳理断电恢复与重复发送的处理规则，计划补充验证。',project:'钉钉日志',status:'planned'}
  ];
  for (const item of work) {
    const response = await page.request.post(base + '/api/v1/workspace/manual',{data:{date:day,...item}});
    assert.equal(response.status(),200);
  }
  await page.reload();
  await page.waitForSelector('.task-row');
  const oldValue = await page.locator('textarea[data-field="completed"]').inputValue();
  const text = '1. 推进操作面板布局开发\n完成基础布局和状态显示，等待运行时验证。\n\n2. 梳理日志持久化方案\n明确断电恢复与防重复提交规则。';
  await page.locator('textarea[data-field="completed"]').fill(text);
  await page.locator('textarea[data-field="progress"]').fill('面板主体功能已实现，正在补充异常状态处理。');
  await page.locator('textarea[data-field="problems"]').fill('异常状态切换需要进一步联调。');
  await page.locator('textarea[data-field="solutions"]').fill('补充状态事件处理与回归验证。');
  await page.locator('textarea[data-field="plan"]').fill('完成运行时联调与异常恢复测试。');
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#save-state').textContent.startsWith('已保存'));
  const firstCheckbox = page.locator('.task-row input').first();
  await firstCheckbox.uncheck();
  await page.waitForFunction(() => document.querySelector('#selected-count').textContent !== document.querySelector('#count-all').textContent);
  await page.reload();
  await page.waitForSelector('textarea[data-field="completed"]');
  assert.equal(await page.locator('textarea[data-field="completed"]').inputValue(),text);
  assert.equal(await page.locator('.task-row input').first().isChecked(),false);
  await page.locator('.task-title').first().click();
  await page.waitForSelector('.message-text');
  await page.getByRole('tab',{name:'任务摘要',exact:true}).click();
  assert.ok(await page.locator('.summary-card').isVisible());
  await page.getByRole('tab',{name:'原始对话',exact:true}).click();
  await page.locator('#task-search').fill('持久化');
  assert.ok(await page.locator('.task-row').count() >= 1);
  await page.locator('#task-search').fill('');
  await page.locator('#preview-btn').click();
  await page.waitForSelector('#modal[open]');
  assert.ok((await page.locator('#modal-body').innerText()).includes('防重复提交规则'));
  await page.locator('#modal-close').click();
  // A failed date navigation must keep the previous period and editor paired.
  const oldDay = await page.locator('#date').inputValue();
  const targetDate = new Date(oldDay + 'T12:00:00Z'); targetDate.setUTCDate(targetDate.getUTCDate()+1);
  const targetDay = targetDate.toISOString().slice(0,10);
  await page.route('**/api/v1/workspace?*',route => {
    if (new URL(route.request().url()).searchParams.get('day') === targetDay) return route.abort('failed');
    return route.continue();
  });
  await page.locator('#date').fill(targetDay);
  await page.locator('#date').dispatchEvent('change');
  await page.waitForFunction(day => document.querySelector('#date').value === day,oldDay);
  assert.equal(await page.locator('textarea[data-field="completed"]').inputValue(),text);
  await page.unroute('**/api/v1/workspace?*');
  await page.locator('#toast').waitFor({state:'hidden'});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await page.screenshot({path:'.tmp/workspace-desktop.png',fullPage:true});
  await page.locator('#theme-btn').click();
  assert.ok(await page.locator('body.dark').count());
  await page.screenshot({path:'.tmp/workspace-dark.png',fullPage:true});
  await page.locator('#theme-btn').click();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await page.locator('#open-tasks').click();
  assert.ok(await page.locator('#tasks-panel.open').isVisible());
  await page.locator('#close-tasks').click();
  await page.screenshot({path:'.tmp/workspace-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:['manual task','autosave','reload persistence','selection persistence','evidence tabs','search','preview','failed date navigation','desktop layout','dark theme','mobile layout'],pageErrors:errors}));
  await browser.close();
})().catch(error => {console.error(error); process.exitCode = 1;});
