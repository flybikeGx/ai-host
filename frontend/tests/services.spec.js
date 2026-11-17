import {test,expect} from '@playwright/test';

test('shared services page shows persisted state, bindings and details',async({page,request})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const before=await(await request.get('/api/runs')).json();
  await page.goto('/chat');
  await page.getByRole('button',{name:'共享服务',exact:true}).click();
  await expect(page).toHaveURL(/\/services$/);
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  const card=page.locator('.svc-card').filter({hasText:'ticket-ui'});
  await expect(card).toBeVisible();
  await expect(card).toContainText('已发布');
  await expect(card).toContainText('业务已验证');
  await expect(card).toContainText('ui-triage-agent');
  await expect(card).toContainText('1 个 Agent');
  await expect(page.locator('.svc-stats')).toContainText('已发布');
  await card.getByRole('button',{name:'详情',exact:true}).click();
  const detail=page.locator('.svc-detail');
  await expect(detail).toBeVisible();
  await expect(detail).toContainText('健康层次');
  await expect(detail).toContainText('存活');
  await expect(detail).toContainText('可用');
  await expect(detail).toContainText('ui-triage-agent');
  await expect(detail).toContainText('ticket_search');
  // A probe executes a real local MCP check and records the layered result.
  await detail.getByRole('button',{name:'探测',exact:true}).click();
  await expect(page.locator('.error-banner')).toHaveCount(0);
  await expect(detail).toContainText('业务已验证');
  // Maintenance is visible and blocks restart, without stopping the process.
  await detail.getByRole('button',{name:'进入维护',exact:true}).click();
  await expect(detail).toContainText('维护中');
  await detail.getByRole('button',{name:'重启',exact:true}).click();
  await expect(detail.locator('.svc-action-error')).toContainText('维护中');
  await detail.getByRole('button',{name:'关闭操作错误',exact:true}).click();
  await detail.getByRole('button',{name:'结束维护',exact:true}).click();
  await page.getByRole('button',{name:'关闭详情',exact:true}).click();
  await page.getByRole('button',{name:'暂停自动刷新'}).click();
  await expect(page.locator('.svc-health')).toContainText('监督器');
  await page.getByRole('button',{name:'刷新共享服务'}).click();
  await page.reload();
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  await expect(page.locator('.svc-card').filter({hasText:'ticket-ui'})).toBeVisible();
  const after=await(await request.get('/api/runs')).json();
  expect(after.length).toBe(before.length);
  await page.screenshot({path:'test-results/services-desktop.png',fullPage:true,animations:'disabled'});
  expect(errors).toEqual([]);
});

test('shared services page handles empty and error states',async({page})=>{
  await page.route('**/api/services',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({services:[],supervisor:{state:'never_run'},guardian:{enabled:false,state:'disabled',interval_seconds:300},agents:[],sampled_at:1700000000,limits:{note:'fixture'}})}));
  await page.goto('/services');
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  await expect(page.getByText('还没有共享服务部署')).toBeVisible();
  await expect(page.locator('.svc-health')).toContainText('看护 未启用');
  await page.unroute('**/api/services');
  await page.route('**/api/services',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({detail:'fixture failure'})}));
  await page.reload();
  await expect(page.locator('.error-banner')).toContainText('fixture failure');
});

test('shared services page stays usable on a phone',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto('/services');
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  await expect(page.locator('.svc-card').first()).toBeVisible();
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.locator('.svc-card').first().getByRole('button',{name:'详情',exact:true}).click();
  await expect(page.locator('.svc-detail')).toBeVisible();
  const detailOverflow=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
  expect(detailOverflow).toBeLessThanOrEqual(1);
  await page.getByRole('button',{name:'关闭详情',exact:true}).click();
  await page.screenshot({path:'test-results/services-mobile.png',fullPage:true,animations:'disabled'});
});

test('a late detail response cannot reopen a closed overlay',async({page})=>{
  let delayDetail=false;
  await page.route('**/api/services/ticket-ui',async route=>{
    if(delayDetail&&route.request().method()==='GET')await new Promise(resolve=>setTimeout(resolve,1200));
    await route.continue();
  });
  await page.goto('/services');
  await page.locator('.svc-card').first().getByRole('button',{name:'详情',exact:true}).click();
  await expect(page.locator('.svc-detail')).toBeVisible();
  delayDetail=true;
  await page.locator('.svc-detail').getByRole('button',{name:'探测',exact:true}).click();
  await page.getByRole('button',{name:'关闭详情',exact:true}).click();
  await expect(page.locator('.svc-detail-overlay')).toHaveCount(0);
  await page.waitForTimeout(1800);
  await expect(page.locator('.svc-detail-overlay')).toHaveCount(0);
  delayDetail=false;
});
