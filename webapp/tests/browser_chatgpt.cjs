const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
  try {
    const context = await browser.newContext({viewport:{width:1100,height:900}});
    const page = await context.newPage(), errors = [], sourceQueries = [];
    page.on('pageerror',error => errors.push(error.message));
    page.on('console',message => {if (message.type() === 'error') errors.push(message.text());});
    const conversations = [
      {key:'codex:local',title:'Codex 工作会话',source:'codex',project_id:'project-1',project_name:'钉钉',updated_at:'2026-10-16T01:00:00Z',selected:false,revision:1,available:true},
      {key:'chatgpt:chat-1',title:'日报流程梳理',source:'chatgpt',project_id:'project-1',project_name:'钉钉',updated_at:'2026-10-16T02:00:00Z',selected:false,revision:1,available:true},
      {key:'chatgpt:chat-2',title:'前端交互检查',source:'chatgpt',project_id:'project-1',project_name:'钉钉',updated_at:'2026-10-16T03:00:00Z',selected:false,revision:1,available:true}
    ];
    await context.route('**/api/v1/workspace**',async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1/workspace','');
      const reply = data => route.fulfill({json:{data}});
      if (path === '' && request.method() === 'GET') return reply({
        conversations_enabled:true,tasks:[],
        report:{status:'empty',sections:{completed:'',progress:'',problems:'',solutions:'',plan:''},readonly:false,revision:1},
        automation:{enabled:false,timezone:'Asia/Shanghai'},
        sync:{status:'warning',warnings:['ChatGPT 会话索引需要检查']},sources:[]
      });
      if (path === '/projects') return reply([{id:'project-1',name:'钉钉',conversation_count:3}]);
      if (path === '/conversations' && request.method() === 'GET') {
        const source = url.searchParams.get('source');
        if (source) sourceQueries.push(source);
        const rows = conversations.filter(item => !source || item.source === source);
        return reply({items:rows,total:rows.length,selected_count:rows.filter(item => item.selected).length,
          counts:{codex:source && source !== 'codex' ? 0 : 1,workbuddy:0,chatgpt:source && source !== 'chatgpt' ? 0 : 2}});
      }
      if (path === '/conversations/selection' && request.method() === 'PATCH') {
        const {changes} = request.postDataJSON();
        for (const change of changes) {
          const item = conversations.find(row => row.key === change.key);
          item.selected = change.selected; item.revision++;
        }
        return reply(changes.map(change => conversations.find(item => item.key === change.key)));
      }
      if (path.startsWith('/conversations/') && path.endsWith('/messages')) {
        const key = decodeURIComponent(path.split('/')[2]);
        return reply({messages:[{role:'user',text:`用户提问：${key} 的会话内容`,time:'2026-10-16T03:00:00Z'}],tools:[],total:1,next_offset:null,summary:{message_count:1,tool_count:0}});
      }
      return route.fulfill({status:404,json:{error:{message:`Unhandled ${request.method()} ${path}`}}});
    });
    await page.goto(process.env.LABLOG_TEST_URL || 'http://127.0.0.1:8016');
    await page.waitForSelector('.conversation-row');
    assert.equal(await page.locator('#count-chatgpt').innerText(),'2');
    assert.equal(await page.locator('#count-codex').innerText(),'1');
    assert.equal(await page.locator('#count-all').innerText(),'3');
    assert.ok((await page.locator('#sync-state').getAttribute('title') || '').includes('ChatGPT'));

    await page.locator('[data-source="chatgpt"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.conversation-row').length === 2);
    assert.ok(sourceQueries.includes('chatgpt'));
    const first = page.locator('.conversation-row').first();
    assert.ok((await first.innerText()).includes('ChatGPT'));
    await first.locator('.conversation-title').click();
    await page.waitForFunction(() => document.querySelector('#evidence-content').innerText.includes('用户提问：chatgpt:chat-1'));
    assert.ok((await page.locator('#task-info').innerText()).includes('日报流程梳理'));
    assert.equal(await page.locator('.message').count(),1);
    assert.equal(await page.locator('.message.assistant').count(),0);
    assert.equal(await page.locator('.message .badge').count(),0);

    await first.locator('input[type="checkbox"]').check();
    await page.waitForFunction(() => document.querySelector('#selected-count').textContent === '1');
    assert.equal(conversations.find(item => item.key === 'chatgpt:chat-1').selected,true);
    assert.ok((await page.locator('.conversation-row').first().innerText()).includes('日报流程梳理'));
    assert.deepEqual(errors,[],errors.join('\n'));

    await page.setViewportSize({width:390,height:844});
    await page.locator('#open-tasks').click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.equal(await page.locator('.source-tabs').evaluate(el => el.scrollWidth > el.clientWidth),false);
    assert.deepEqual(errors,[],errors.join('\n'));
    await context.close();

    const legacyContext = await browser.newContext({viewport:{width:1280,height:900}}), legacyPage = await legacyContext.newPage(), legacyErrors = [];
    legacyPage.on('pageerror',error => legacyErrors.push(error.message));
    legacyPage.on('console',message => {if (message.type() === 'error') legacyErrors.push(message.text());});
    await legacyContext.route('**/api/v1/workspace**',route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1/workspace','');
      if (path === '' && request.method() === 'GET') return route.fulfill({json:{data:{
        tasks:[{id:'legacy-codex',source:'codex',title:'旧版 Codex 任务',project:'钉钉',session_id:'session-1',summary:'摘要',date:'2026-10-16',time:'10:00',status:'unknown',selected:false}],
        report:{status:'empty',sections:{completed:'',progress:'',problems:'',solutions:'',plan:''},readonly:false,revision:1},
        automation:{enabled:false,timezone:'Asia/Shanghai'},sync:{status:'success',warnings:[]},sources:[]
      }}});
      if (path.endsWith('/evidence')) return route.fulfill({json:{data:{error:'测试中不读取旧证据'}}});
      return route.fulfill({status:404,json:{error:{message:`Unhandled ${request.method()} ${path}`}}});
    });
    await legacyPage.goto(process.env.LABLOG_TEST_URL || 'http://127.0.0.1:8016');
    await legacyPage.waitForFunction(() => document.querySelector('#count-codex').textContent === '1');
    assert.equal(await legacyPage.locator('#count-chatgpt').innerText(),'0');
    assert.equal(await legacyPage.locator('#count-all').innerText(),'1');
    assert.deepEqual(legacyErrors,[],legacyErrors.join('\n'));
    await legacyContext.close();
    console.log(JSON.stringify({passed:true,pageErrors:[...errors,...legacyErrors],checks:['ChatGPT source counts and labels','warning display','source-filtered listing','user-message preview without invented assistant completion','durable checkbox selection','legacy ChatGPT count remains zero','compact desktop and mobile tabs']}));
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
