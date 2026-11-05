import {test,expect} from '@playwright/test';

test('multi-turn admin, history, Markdown safety and reload',async({page})=>{
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:/Admin Agent 管理与调度/})).toBeVisible();
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('FIXTURE_MARKDOWN');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.getByRole('heading',{name:'测试回复',exact:true})).toBeVisible();
  await expect(page.locator('.markdown pre')).toContainText("print('hello')");
  expect(await page.evaluate(()=>window.__xss)).toBeUndefined();
  await expect(page.locator('.markdown a')).not.toHaveAttribute('href',/^javascript:/);
  const hash=new URL(page.url()).hash;
  await page.reload();
  await expect(page.getByRole('heading',{name:'测试回复',exact:true})).toBeVisible();
  expect(new URL(page.url()).hash).toBe(hash);
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('继续对话');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.markdown').last()).toContainText('fixture completed');
  await expect(page.locator('.user-message')).toHaveCount(2);
  await page.screenshot({path:'test-results/admin-chat.png',fullPage:true,animations:'disabled'});
  expect(errors).toEqual([]);
});

test('reload reconnects to active generation without sending it again',async({page,request})=>{
  await page.goto('/chat');
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('FIXTURE_STREAM');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.markdown')).toContainText('正在生成测试回复');
  const id=new URL(page.url()).hash.slice(1);
  await page.reload();
  await expect(page.locator('.markdown')).toContainText('fixture completed',{timeout:15000});
  const snapshot=await (await request.get('/api/conversations/'+id)).json();
  expect(snapshot.turns).toHaveLength(1);
  expect(snapshot.turns[0].status).toBe('succeeded');
});

test('repo tools, shared resources and switching while a chat runs',async({page})=>{
  await page.goto('/chat');
  await page.getByRole('button',{name:/finance-agent 仓库 Agent/}).click();
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('运行示例');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.markdown')).toContainText('fixture completed',{timeout:20000});
  await page.locator('.tool-group>summary').click();
  await expect(page.locator('.tool>summary code')).toHaveText(['read_skill','domain__analyze','write_report']);
  await page.locator('.tool').first().locator('summary').click();
  await expect(page.locator('.tool-body').first()).toContainText('FINANCE_SKILL_OK');
  await page.getByRole('button',{name:'对话信息'}).click();
  await expect(page.locator('.info-panel')).toContainText('analysis');
  await expect(page.locator('.artifact-files')).toContainText('artifacts/report.json');
  const downloadPromise=page.waitForEvent('download');
  await page.locator('.artifact-files button').click();
  expect((await downloadPromise).suggestedFilename()).toBe('report.json');
  await page.screenshot({path:'test-results/repo-chat.png',fullPage:true,animations:'disabled'});
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('FIXTURE_SLEEP');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toBeVisible();
  await page.getByRole('button',{name:/Admin Agent 管理与调度/}).click();
  await expect(page.getByRole('heading',{name:'和 Admin Agent 开始对话'})).toBeVisible();
  await page.getByRole('button',{name:/finance-agent 仓库 Agent/}).click();
  await page.locator('.history-list button').first().click();
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'停止回复',exact:true}).click();
  await expect(page.getByText('已停止',{exact:true})).toBeVisible();
});

test('mobile composer and sidebar remain usable',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto('/chat');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'打开侧栏'}).click();
  await expect(page.getByRole('button',{name:/finance-agent 仓库 Agent/})).toBeVisible();
  await page.getByRole('button',{name:/finance-agent 仓库 Agent/}).click();
  await expect(page.getByRole('heading',{name:'和 finance-agent 开始对话'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  const composer=page.getByRole('textbox',{name:'消息',exact:true});
  await composer.fill('中文输入法');
  await composer.dispatchEvent('keydown',{key:'Enter',isComposing:true});
  await expect(page.locator('.user-message')).toHaveCount(0);
  await composer.fill('');
  await page.screenshot({path:'test-results/mobile-chat.png',fullPage:true,animations:'disabled'});
});
