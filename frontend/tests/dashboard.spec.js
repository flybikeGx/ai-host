import {test,expect} from '@playwright/test';

test('dashboard shows live MCP and resources without creating runs',async({page,request})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const before=await(await request.get('/api/runs')).json();
  await page.goto('/chat');
  await page.getByRole('button',{name:'仪表盘',exact:true}).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId('running-instances')).toHaveText('0');
  await expect(page.getByTestId('healthy-mcp')).toHaveText('1');
  await expect(page.locator('.dash-table')).toContainText('finance-agent');
  await expect(page.locator('.dash-mcp')).toContainText('domain');
  await expect(page.locator('.dash-mcp .dash-status')).toHaveText('健康');
  await page.getByRole('button',{name:'内存',exact:true}).click();
  await expect(page.getByRole('img',{name:'整机内存使用率历史趋势'})).toBeVisible();
  await page.getByRole('button',{name:'暂停自动刷新'}).click();
  await expect(page.locator('.dash-live')).toContainText('已暂停刷新');
  await page.getByRole('button',{name:'刷新仪表盘'}).click();
  await page.getByRole('button',{name:'恢复自动刷新'}).click();
  await page.reload();
  await expect(page.getByRole('heading',{name:'仪表盘',exact:true})).toBeVisible();
  await expect(page.getByTestId('healthy-mcp')).toHaveText('1');
  const after=await(await request.get('/api/runs')).json();
  expect(after.length).toBe(before.length);
  await page.screenshot({path:'test-results/dashboard-desktop.png',fullPage:true,animations:'disabled'});
  expect(errors).toEqual([]);
});

test('dashboard counts a running conversation once and survives navigation',async({page})=>{
  await page.goto('/chat');
  await page.getByRole('button',{name:'与 finance-agent 新建对话',exact:true}).click();
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('FIXTURE_SLEEP');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'仪表盘',exact:true}).click();
  await expect(page.getByTestId('running-instances')).toHaveText('1',{timeout:10000});
  await expect(page.locator('.dash-table tbody tr').filter({hasText:'finance-agent'})).toContainText('1');
  await page.getByRole('button',{name:'对话',exact:true}).click();
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'停止回复',exact:true}).click();
  await expect(page.getByText('已停止',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'仪表盘',exact:true}).click();
  await expect(page.getByTestId('running-instances')).toHaveText('0',{timeout:10000});
});

test('dashboard mobile and authenticated navigation',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.route('**/api/**',route=>route.request().headers().authorization==='Bearer dashboard-ui-fixture'?route.continue():route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({detail:'需要 Bearer token'})}));
  await page.goto('/dashboard');
  await expect(page.getByRole('heading',{name:'连接本地服务'})).toBeVisible();
  await page.getByLabel('管理 Token',{exact:true}).fill('dashboard-ui-fixture');
  await page.getByRole('button',{name:'连接',exact:true}).click();
  await expect(page.getByTestId('healthy-mcp')).toHaveText('1');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  await page.screenshot({path:'test-results/dashboard-mobile.png',animations:'disabled'});
  await page.getByRole('button',{name:'打开侧栏',exact:true}).click();
  await page.getByRole('button',{name:'定时任务',exact:true}).click();
  await expect(page.getByRole('heading',{name:'定时任务',exact:true})).toBeVisible();
  await expect(page.locator('.connection-modal')).toHaveCount(0);
  await page.getByRole('button',{name:'打开侧栏',exact:true}).click();
  await page.getByRole('button',{name:'仪表盘',exact:true}).click();
  await expect(page.getByTestId('healthy-mcp')).toHaveText('1');
});

test('stale or missing metrics are explicit, not fake zeroes',async({page,request})=>{
  const snapshot=await(await request.get('/api/dashboard')).json();
  snapshot.stale=true;snapshot.age_seconds=35;snapshot.sampling_error=true;
  snapshot.host.cpu_percent=null;snapshot.managed.cpu_percent=null;snapshot.managed.rss_bytes=null;
  await page.route('**/api/dashboard',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(snapshot)}));
  await page.goto('/dashboard');
  await expect(page.locator('.dash-stale')).toContainText('上次快照');
  await expect(page.locator('.dash-live')).toContainText('采样延迟');
  await expect(page.locator('.dash-stats article').nth(2).locator(':scope>strong')).toHaveText('采样中');
  await expect(page.locator('.dash-stats article').nth(3)).toContainText('RSS —');
});
