// C6 AIHO-19: the chat page wired to real accounts, /me permissions, domain
// selection and the frozen §6.4 error actions. Real backend, real cookie
// sessions, real SQLite and the real built frontend.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
import {execFileSync} from 'node:child_process';

const PASSWORD='fixture-member-password';
const ADMIN_PASSWORD='fixture-browser-password';

async function auth(request,user,password){
  const response=await request.post('/api/auth/login',{data:{username:user,password:password||PASSWORD}});
  expect(response.status()).toBe(200);
}

test('C6 AIHO-19 developer can open chat UI',async({page})=>{
  await auth(page.request,'developer');
  await page.goto('/chat');
  // The chat surface (not the account landing page) is what a developer gets;
  // the composer appears once an Agent from the catalog is chosen.
  await expect(page.getByRole('heading',{name:'选择一个 Agent，开始新对话'})).toBeVisible({timeout:3000});
  await expect(page.getByRole('button',{name:/与 Default Agent 新建对话/})).toBeVisible({timeout:3000});
  await page.getByRole('button',{name:/与 Default Agent 新建对话/}).click();
  await expect(page.locator('textarea')).toBeVisible({timeout:3000});
  const composer=page.getByRole('textbox',{name:'消息',exact:true});
  await expect(composer).toHaveJSProperty('readOnly',false);
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'重命名对话',exact:true})).toHaveCount(0);
  // D6: a developer never sees the Admin Agent, neither in the catalog nor in
  // any new-conversation entry.
  await expect(page.getByRole('button',{name:/与 Admin Agent 新建对话/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/与 Default Agent 新建对话/})).toBeVisible();
});

test('C6 developer cannot enter global admin pages and has no scope=all entry',async({page})=>{
  await auth(page.request,'developer');
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:'对话',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'仪表盘',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'共享服务',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'定时任务',exact:true})).toHaveCount(0);
  await expect(page.getByRole('link',{name:'工作台与 Repo 资源'})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/全部对话/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/本机引导/})).toHaveCount(0);
  expect((await page.request.get('/api/dashboard')).status()).toBe(403);
  // A direct navigation to a global page is redirected to the chat surface,
  // so the browser never lands on a page whose API the server denies.
  await page.goto('/dashboard');
  await expect(page).toHaveURL(/\/chat$/);
});

test('C6 viewer gets the read-only chat shell without create/write entries',async({page})=>{
  await auth(page.request,'viewer');
  await page.goto('/chat');
  await expect(page.getByRole('heading',{name:'暂无可发起的新对话'})).toBeVisible();
  await expect(page.getByRole('button',{name:/新建对话/})).toHaveCount(0);
  await expect(page.locator('.agent-list')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'仪表盘',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/全部对话/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toHaveCount(0);
});

test('C6 reader cannot write, rename, archive, cancel or see artifacts',async({page})=>{
  await auth(page.request,'reader');
  await page.goto('/chat#reader-chat');
  const composer=page.getByRole('textbox',{name:'消息',exact:true});
  await expect(composer).toBeVisible();
  await expect(composer).toHaveJSProperty('readOnly',true);
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'重命名对话',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'归档对话',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'恢复对话',exact:true})).toHaveCount(0);
  await expect(page.locator('.composer-area')).toContainText('当前角色没有发送消息的权限');
  await page.getByRole('button',{name:'对话信息'}).click();
  await expect(page.locator('.artifact-files')).toHaveCount(0);
});

test('C6 platform admin sees Admin Agent, the domain selector and the scope=all entry',async({page})=>{
  await auth(page.request,'admin',ADMIN_PASSWORD);
  const me=await (await page.request.get('/api/v1/me')).json();
  expect(me.platform_admin).toBe(true);
  const other=me.domains.find(domain=>domain.code==='other');
  expect(other).toBeTruthy();
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:/与 Admin Agent 新建对话/})).toBeVisible();
  const selector=page.getByRole('combobox',{name:'切换域'});
  await expect(selector).toBeVisible();
  await expect(selector.locator('option')).toHaveCount(me.domains.length);
  await expect(page.getByRole('button',{name:'查看全部对话（只读）'})).toBeVisible();
  // Selecting a domain writes sessionStorage and carries x-domain-id, which
  // the server uses to re-scope the Agent catalog.
  await selector.selectOption(other.id);
  await expect(page.getByRole('button',{name:/与 Other Agent 新建对话/})).toBeVisible();
  await expect(page.getByRole('button',{name:/与 Default Agent 新建对话/})).toHaveCount(0);
  expect(await page.evaluate(()=>sessionStorage.getItem('agent-base-domain'))).toBe(other.id);
  const scoped=await page.request.get('/api/chat/targets',{headers:{'x-domain-id':other.id}});
  expect((await scoped.json()).targets.map(target=>target.id)).toContain('other-agent');
});

test('C6 every workspace request carries the selected x-domain-id header',async({page})=>{
  await auth(page.request,'admin',ADMIN_PASSWORD);
  const me=await (await page.request.get('/api/v1/me')).json();
  const other=me.domains.find(domain=>domain.code==='other');
  const seen=[];
  page.on('request',request=>{
    const url=new URL(request.url());
    if(url.pathname==='/api/chat/targets'||url.pathname==='/api/conversations')seen.push(request.headers()["x-domain-id"]||"");
  });
  await page.goto('/chat');
  await expect(page.getByRole('combobox',{name:'切换域'})).toBeVisible();
  const home=me.domains.find(domain=>domain.code==='default');
  await expect.poll(()=>seen.length).toBeGreaterThan(0);
  expect(seen.every(value=>value===home.id)).toBe(true);
  await page.getByRole('combobox',{name:'切换域'}).selectOption(other.id);
  await expect.poll(()=>seen.at(-1)).toBe(other.id);
  await expect.poll(()=>seen.slice(-2).every(value=>value===other.id)).toBe(true);
});

test('C6 platform admin scope=all read-only view hides every write control',async({page})=>{
  await auth(page.request,'admin',ADMIN_PASSWORD);
  await page.goto('/chat');
  await page.getByRole('button',{name:'查看全部对话（只读）'}).click();
  await expect(page.getByRole('button',{name:'退出全部对话（只读）'})).toBeVisible();
  await expect(page.locator('.history-list')).toContainText('foreign private');
  await page.locator('.history-item').filter({hasText:'foreign private'}).click();
  await expect(page.locator('.composer-area')).toContainText('只读：平台管理员正在查看全部对话');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveJSProperty('readOnly',true);
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'停止回复',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'重命名对话',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'归档对话',exact:true})).toHaveCount(0);
});

test('C6 404 on a conversation removes it from the list and clears the selection',async({page})=>{
  await auth(page.request,'developer');
  await page.goto('/chat#peer-chat');
  await expect(page.locator('.error-banner')).toContainText('已从列表移除');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveCount(0);
  expect(new URL(page.url()).hash).toBe('');
  await expect(page.locator('.history-list')).not.toContainText('peer private');
});

test('C6 expired session returns to login and keeps no token in browser storage',async({page})=>{
  await auth(page.request,'developer');
  await page.goto('/chat');
  await page.getByRole('button',{name:/与 Default Agent 新建对话/}).click();
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toBeVisible();
  const storage=await page.evaluate(()=>({local:{...localStorage},session:{...sessionStorage}}));
  expect(Object.keys(storage.local)).toEqual([]);
  const serialized=JSON.stringify(storage.local)+JSON.stringify(storage.session);
  expect(serialized).not.toMatch(/agent_base_session|bearer|token|cookie/i);
  execFileSync(process.env.E2E_PYTHON,['-c',"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('UPDATE browser_sessions SET expires=0'); c.commit()",process.env.E2E_CONTROL]);
  await page.getByRole('button',{name:'刷新 Agent 列表'}).click();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await expect(page).toHaveURL(/\/login\?next=/);
  expect((await page.request.get('/api/conversations')).status()).toBe(401);
});
