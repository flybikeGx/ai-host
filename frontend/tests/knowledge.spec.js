import {test,expect} from '@playwright/test';

test('home and shared navigation keep all pages reachable',async({page})=>{
 await page.goto('/');await expect(page).toHaveURL(/\/dashboard$/);
 await page.getByRole('button',{name:'知识库',exact:true}).click();await expect(page).toHaveURL(/\/knowledge$/);
 await expect(page.getByRole('heading',{name:'知识库',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'返回主页',exact:true}).click();await expect(page).toHaveURL(/\/dashboard$/);
 await expect(page.getByRole('link',{name:'工作台与 Repo 资源'})).toHaveAttribute('href','/workbench');
 await page.goto('/workbench');await expect(page.getByRole('heading',{name:'Agent Base',exact:true})).toBeVisible();
});

test('knowledge reading, human editing, history and namespace binding',async({page})=>{
 await page.goto('/knowledge');await expect(page.getByText('当前为只读浏览',{exact:true})).toBeVisible();
 expect((await page.request.post('/api/knowledge/namespaces',{data:{slug:'forbidden',name:'forbidden'}})).status()).toBe(401);
 await page.request.post('/fixture/knowledge-login');await page.reload();
 await page.getByRole('button',{name:'新建项目',exact:true}).click();
 await page.getByLabel('项目标识',{exact:true}).fill('browser-project');await page.getByLabel('项目名称',{exact:true}).fill('浏览器新项目');
 await page.getByRole('button',{name:'保存项目',exact:true}).click();
 await expect(page.getByLabel('知识项目',{exact:true})).toContainText('浏览器新项目');
 await page.getByRole('button',{name:'新建知识',exact:true}).click();
 await page.getByLabel('知识标题',{exact:true}).fill('结算流程');await page.getByLabel('知识摘要',{exact:true}).fill('结算时区与跨日补单规则');
 await page.getByLabel('知识正文',{exact:true}).fill('# 结算时区\n北京时间零点结算。\n# 补单\n进入下一周期。');
 await page.getByRole('button',{name:'发布',exact:true}).click();
 await expect(page.getByRole('heading',{name:'结算流程',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'编辑知识',exact:true}).click();await page.getByLabel('知识正文',{exact:true}).fill('# 结算时区\n北京时间一点结算。');
 await page.getByRole('button',{name:'定时任务',exact:true}).click();await expect(page).toHaveURL(/\/knowledge/);
 await expect(page.getByRole('alert')).toContainText('未保存');
 await page.getByRole('button',{name:'发布',exact:true}).click();await expect(page.locator('.kb-detail-heading')).toContainText('v2');
 await page.getByRole('button',{name:'历史版本',exact:true}).click();await expect(page.locator('.kb-history button')).toHaveCount(2);
 await page.getByRole('tab',{name:'项目与绑定',exact:true}).click();await page.getByLabel('绑定 Agent',{exact:true}).selectOption('admin');
 await page.getByLabel('主项目',{exact:true}).selectOption({label:'浏览器新项目'});
 await page.getByLabel('投稿 浏览器新项目',{exact:true}).check();await page.getByRole('button',{name:'保存绑定',exact:true}).click();
 await expect(page.getByRole('status').filter({hasText:'绑定已保存'})).toBeVisible();
});

test('agent proposal becomes searchable only after human review',async({page})=>{
 const fixture=await(await page.request.post('/fixture/knowledge-login')).json();await page.goto('/knowledge?namespace='+fixture.namespace);
 await page.getByRole('tab',{name:/投稿审批/}).click();await page.getByRole('button',{name:/待审批日切规则/}).click();
 await expect(page.locator('.kb-diff')).toContainText('每日北京时间零点结算');
 await page.getByRole('button',{name:'批准发布',exact:true}).click();await expect(page.locator('.kb-detail')).toContainText('已发布');
 await page.getByRole('tab',{name:'知识文档',exact:true}).click();await page.getByLabel('搜索知识',{exact:true}).fill('北京时间');
 await page.getByRole('button',{name:/待审批日切规则/}).click();await expect(page.locator('.kb-markdown')).toContainText('每日北京时间零点结算');
 await page.screenshot({path:'test-results/knowledge-desktop.png',animations:'disabled'});
 await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
 await page.screenshot({path:'test-results/knowledge-mobile.png',animations:'disabled'});
 await page.getByRole('button',{name:'打开侧栏',exact:true}).click();await page.getByRole('button',{name:'返回主页',exact:true}).click();await expect(page).toHaveURL(/\/dashboard$/);
});
