// Imports through frontend so npm's locked dependency resolves from this directory.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
import {execFileSync} from 'node:child_process';
const password='fixture-browser-password';
async function login(page,user='admin',pass=password){
  await page.goto('/login');
  await page.getByLabel('账号',{exact:true}).fill(user);
  await page.getByLabel('密码',{exact:true}).fill(pass);
  await page.getByRole('button',{name:'登录工作空间',exact:true}).click();
}
test('AIHO-12: real cookie login, restore, domain metadata and logout revocation',async({page,context})=>{
  await page.goto('/chat');
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await login(page);
  await expect(page).toHaveURL(/dashboard$/);
  const cookie=(await context.cookies()).find(c=>c.name==='agent_base_session');
  expect(cookie.httpOnly).toBe(true);expect(cookie.sameSite).toBe('Lax');
  expect(await page.evaluate(()=>document.cookie)).not.toContain('agent_base_session');
  await page.reload();await expect(page).toHaveURL(/dashboard$/);
  const me=await page.request.get('/api/v1/me');expect(me.status()).toBe(200);
  const identity=await me.json();expect(identity.username).toBe('admin');expect(identity.domains.length).toBeGreaterThan(0);
  await page.getByRole('button',{name:'退出登录',exact:true}).click();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  expect((await page.request.get('/api/conversations')).status()).toBe(401);
  // Replay stolen old cookie: server-side revocation, not merely client deletion.
  expect((await page.request.get('/api/v1/me',{headers:{Cookie:`agent_base_session=${cookie.value}`}})).status()).toBe(401);
});
test('AIHO-12: viewer cannot enter global workspace',async({page})=>{
  await login(page,'viewer','fixture-member-password');
  await expect(page.getByRole('heading',{name:'你好，测试成员'})).toBeVisible();
  expect((await page.request.get('/api/dashboard')).status()).toBe(403);
});
test('AIHO-12: persisted expiration rejects refresh',async({page})=>{
  await login(page);await expect(page).toHaveURL(/dashboard$/);
  execFileSync(process.env.E2E_PYTHON,['-c',"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('UPDATE browser_sessions SET expires=0'); c.commit()",process.env.E2E_CONTROL]);
  await page.reload();await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  expect((await page.request.get('/api/v1/me')).status()).toBe(401);
});
test('C1: explicit invalid bearer never falls back to valid cookie',async({page})=>{
  await login(page);await expect(page).toHaveURL(/dashboard$/);
  expect((await page.request.get('/api/v1/me',{headers:{Authorization:'Bearer invalid'}})).status()).toBe(401);
});
test('real scheduler, SSE, artifact bytes, conflict, stop, rename and archive',async({page})=>{
 await login(page);await expect(page).toHaveURL(/dashboard$/);
 const r=page.request;
 const created=await r.post('/api/conversations',{data:{title:'E2E native execution'}});expect(created.status()).toBe(201);
 const chat=(await created.json()).id;
 const sent=await r.post(`/api/conversations/${chat}/messages`,{data:{text:'E2E deterministic message',client_key:'e2e-1'}});expect(sent.status()).toBe(202);
 await expect.poll(async()=>{const x=await (await r.get(`/api/conversations/${chat}`)).json();return x.turns[0].status;},{timeout:25000}).toBe('succeeded');
 const events=await r.get(`/api/conversations/${chat}/events?once=true`);
 expect(events.headers()['content-type']).toContain('text/event-stream');expect(await events.text()).toContain('turn.');
 const file=await r.get(`/api/conversations/${chat}/download?path=artifacts/report.txt`);expect(file.status()).toBe(200);expect(await file.text()).toContain('E2E deterministic message');
 expect((await r.patch(`/api/conversations/${chat}`,{data:{title:'renamed'}})).status()).toBe(200);
 expect((await r.post(`/api/conversations/${chat}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'e2e-sleep'}})).status()).toBe(202);
 expect((await r.post(`/api/conversations/${chat}/messages`,{data:{text:'conflict',client_key:'e2e-conflict'}})).status()).toBe(409);
 expect((await r.post(`/api/conversations/${chat}/stop`)).status()).toBe(200);
 await expect.poll(async()=>{const x=await (await r.get(`/api/conversations/${chat}`)).json();return x.turns.at(-1).status;}).toBe('cancelled');
 expect((await r.patch(`/api/conversations/${chat}`,{data:{archived:true}})).status()).toBe(200);
});
test('browser chat sends through real SSE and restores persisted history',async({page})=>{
 await login(page);await expect(page).toHaveURL(/dashboard$/);await page.goto('/chat');
 await page.getByRole('button',{name:/与 Admin Agent 新建对话/}).click();
 await page.getByRole('textbox',{name:'消息',exact:true}).fill('E2E browser message');
 await page.getByRole('button',{name:'发送消息',exact:true}).click();
 await expect(page.locator('.markdown').last()).toContainText('fixture completed',{timeout:25000});
 await page.reload();await expect(page.locator('.user-message')).toContainText('E2E browser message');
 await expect(page.locator('.markdown').last()).toContainText('fixture completed');
});
