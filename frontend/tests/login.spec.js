import {test,expect} from '@playwright/test';

test.beforeEach(async({request})=>{await request.post('/fixture/auth/setup');});
test.afterEach(async({request})=>{await request.post('/fixture/auth/reset');});

async function signIn(page,username='admin',password='fixture-browser-password'){
  await page.getByLabel('账号',{exact:true}).fill(username);
  await page.getByLabel('密码',{exact:true}).fill(password);
  await page.getByRole('button',{name:'登录工作空间',exact:true}).click();
}

test('account login restores the workspace across navigation and revokes logout',async({page,context})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/services');
  await expect(page).toHaveURL(/\/login\?next=/);
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await expect(page.getByRole('link',{name:/注册/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/注册/})).toHaveCount(0);
  await signIn(page,'admin','wrong-password');
  await expect(page.getByRole('alert')).toContainText('用户名或密码错误');
  await expect(page.getByLabel('密码',{exact:true})).toHaveValue('');
  await signIn(page);
  await expect(page).toHaveURL(/\/services$/);
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  const cookie=(await context.cookies()).find(c=>c.name==='agent_base_session');
  expect(cookie.httpOnly).toBe(true);
  expect(await page.evaluate(()=>document.cookie)).not.toContain('agent_base_session');
  await page.reload();
  await expect(page.getByRole('heading',{name:'共享服务',exact:true})).toBeVisible();
  await page.goto('/login?next=/workbench');
  await expect(page).toHaveURL(/\/workbench$/);
  await expect(page.getByRole('heading',{name:'Agent Base',exact:true})).toBeVisible();
  await page.goto('/dashboard');
  await page.getByRole('button',{name:'仪表盘',exact:true}).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.getByRole('button',{name:'退出登录',exact:true}).click();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  expect((await page.request.get('/api/dashboard')).status()).toBe(401);
  await page.goto('/chat');
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  expect(errors).toEqual([]);
});

test('member signs in without receiving global admin access',async({page})=>{
  await page.goto('/login');
  await signIn(page,'member','fixture-member-password');
  await expect(page.getByRole('heading',{name:'你好，测试成员'})).toBeVisible();
  await expect(page.getByText('你已成功登录 Agent Base。')).toBeVisible();
  expect((await page.request.get('/api/dashboard')).status()).toBe(403);
  await expect(page.getByRole('button',{name:'共享服务',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'退出登录',exact:true}).click();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
});

test('expired sessions return to login and unsafe return URLs stay local',async({page,request})=>{
  await page.goto('/login?next=https://outside.invalid');
  await signIn(page);
  await expect(page).toHaveURL(/\/dashboard$/);
  await request.post('/fixture/auth/expire');
  await page.reload();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  expect((await page.request.get('/api/dashboard')).status()).toBe(401);
});

test('login stays usable on desktop and phone',async({page})=>{
  await page.goto('/login');
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await page.screenshot({path:'test-results/login-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByLabel('账号',{exact:true})).toBeVisible();
  await page.getByLabel('密码',{exact:true}).fill('fixture-value');
  await page.getByRole('button',{name:'显示密码'}).click();
  await expect(page.getByLabel('密码',{exact:true})).toHaveAttribute('type','text');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({path:'test-results/login-mobile.png',fullPage:true});
});
