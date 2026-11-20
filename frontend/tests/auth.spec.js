import {test,expect} from '@playwright/test';

// C6: chat page identity gating. Cookie sessions, /me permissions, the domain
// selector and the frozen §6.4 error actions are exercised against the real
// fixture backend; per-role /me payloads are stubbed so all three roles are
// deterministic without creating governance users in every run.
const PASSWORD='fixture-browser-password';
const ALL=['agent.run','agent.read','agent.cancel','task.read','task.write','file.read'];

async function signIn(page,username='admin',password=PASSWORD){
  await page.getByLabel('账号',{exact:true}).fill(username);
  await page.getByLabel('密码',{exact:true}).fill(password);
  await page.getByRole('button',{name:'登录工作空间',exact:true}).click();
}

function identity({permissions=ALL,platformAdmin=false,domains=[{id:'d1',code:'default',name:'默认域'}],home='d1'}={}){
  return {user:{id:'u1',username:'tester',display_name:'测试用户',status:'active',home_domain_id:home},roles:[],
    platform_admin:platformAdmin,home_domain_id:home,
    domains:domains.map(domain=>({...domain,status:'active',joined_at:0,permissions:[...permissions].sort()})),
    effective_permissions:[...permissions]};
}

const targets=[
  {id:'finance-agent',name:'finance-agent',kind:'repo',available:true,skills:[],mcp:[]},
  {id:'admin',name:'Admin Agent',kind:'admin',available:true,skills:[],mcp:[]},
];
function snapshotFor(id,target='finance-agent'){
  return {conversation:{id,target,title:'测试对话',archived:false,owner_user_id:'u1',domain_id:'d1',spec:{runtime:{model:'fixture-model',effort:'high'}}},
    turns:[],events:[],cursor:0,files:[]};
}
function historyFor(id,extra={}){
  return {id,target:'finance-agent',title:'测试对话',archived:false,owner_user_id:'u1',domain_id:'d1',status:'idle',preview:'',spec:{},...extra};
}

async function stubWorkspace(page,{me,onTargets,onList,onMessage,snapshot}={}){
  await page.route('**/api/**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/auth/session')return route.continue();
    if(path==='/api/v1/me')return route.fulfill({json:typeof me==='function'?me():me});
    if(path==='/api/chat/targets')return onTargets?onTargets(route):route.fulfill({json:{targets,defaults:{model:'fixture-model',effort:'high'}}});
    if(path==='/api/conversations')return onList?onList(route):route.fulfill({json:[]});
    if(/^\/api\/conversations\/[^/]+\/messages$/.test(path)&&onMessage)return onMessage(route);
    if(path.endsWith('/events'))return route.fulfill({status:200,contentType:'text/event-stream',body:': keepalive\n\n'});
    if(path.endsWith('/files'))return route.fulfill({json:[]});
    const single=path.match(/^\/api\/conversations\/([^/]+)$/);
    if(single){
      if(Array.isArray(snapshot)){const item=snapshot[0];return route.fulfill({status:item.status,contentType:'application/json',body:JSON.stringify(item.body)});}
      return route.fulfill({json:snapshot||snapshotFor(single[1])});
    }
    return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({detail:'未模拟',code:'conversation_not_found'})});
  });
}

test.afterEach(async({request})=>{await request.post('/fixture/auth/reset');});

test('chat requires a session, keeps the requested path, and a 401 returns to login',async({page,request})=>{
  await request.post('/fixture/auth/setup');
  await page.goto('/chat');
  await expect(page).toHaveURL(/\/login\?next=%2Fchat$/);
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await signIn(page);
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole('button',{name:/与 Admin Agent 新建对话/})).toBeVisible();
  // §6.4: a lost session clears local state and returns to login, once.
  await request.post('/fixture/auth/expire');
  await page.getByRole('button',{name:'刷新 Agent 列表'}).click();
  await expect(page.getByRole('heading',{name:'欢迎回来'})).toBeVisible();
  await expect(page).toHaveURL(/\/login\?next=%2Fchat$/);
  await expect.poll(()=>page.evaluate(()=>sessionStorage.getItem('agent-base-domain'))).toBe(null);
  // The page surface rejects the anonymous request (the control-plane /me
  // endpoint keeps its documented loopback bootstrap identity in hybrid mode).
  expect((await page.request.get('/api/dashboard')).status()).toBe(401);
});

test('viewer /me permissions hide every create and write entry',async({page})=>{
  await stubWorkspace(page,{me:identity({permissions:['agent.read','task.read','file.read']})});
  await page.goto('/chat');
  await expect(page.getByRole('heading',{name:'暂无可发起的新对话'})).toBeVisible();
  await expect(page.getByRole('button',{name:/新建对话/})).toHaveCount(0);
  await expect(page.locator('.agent-list')).toHaveCount(0);
  await expect(page.getByRole('button',{name:/全部对话/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'仪表盘',exact:true})).toHaveCount(0);
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveCount(0);
});

test('developer /me permissions allow writing but never the Admin Agent or scope=all',async({page})=>{
  await stubWorkspace(page,{me:identity()});
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:/与 finance-agent 新建对话/})).toBeVisible();
  await expect(page.getByRole('button',{name:/与 Admin Agent 新建对话/})).toHaveCount(0);
  await expect(page.getByRole('button',{name:/全部对话/})).toHaveCount(0);
  await page.goto('/chat#dev-chat');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveJSProperty('readOnly',false);
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'重命名对话'})).toBeVisible();
  await expect(page.getByRole('button',{name:'归档对话',exact:true})).toBeVisible();
});

test('platform admin /me shows the Admin Agent and the scope=all entry',async({page})=>{
  await stubWorkspace(page,{me:identity({platformAdmin:true,domains:[{id:'d1',code:'default',name:'默认域'},{id:'d2',code:'other',name:'其它域'}],home:'d1'})});
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:/与 Admin Agent 新建对话/})).toBeVisible();
  await expect(page.getByRole('button',{name:'查看全部对话（只读）'})).toBeVisible();
  await expect(page.getByRole('combobox',{name:'切换域'}).locator('option')).toHaveCount(2);
});

test('domain switch stores the id, sends x-domain-id and recovers from domain_forbidden',async({page})=>{
  const seen=[];
  let revoked=false;
  const scopedDomains=()=>revoked?[{id:'d1',code:'default',name:'默认域'}]:[{id:'d1',code:'default',name:'默认域'},{id:'d2',code:'other',name:'其它域'}];
  const me=()=>identity({permissions:ALL,domains:scopedDomains(),home:'d1'});
  await stubWorkspace(page,{
    me,
    onList:route=>{
      seen.push(route.request().headers()['x-domain-id']||'');
      if(revoked&&seen.at(-1)==='d2')return route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({detail:'域不可用或未加入',code:'domain_forbidden'})});
      return route.fulfill({json:[]});
    },
    onTargets:route=>route.fulfill({json:{targets,defaults:{model:'fixture-model',effort:'high'}}}),
  });
  await page.goto('/chat');
  const selector=page.getByRole('combobox',{name:'切换域'});
  await expect(selector).toHaveValue('d1');
  expect(await page.evaluate(()=>sessionStorage.getItem('agent-base-domain'))).toBe('d1');
  await selector.selectOption('d2');
  await expect.poll(()=>seen.at(-1)).toBe('d2');
  expect(await page.evaluate(()=>sessionStorage.getItem('agent-base-domain'))).toBe('d2');
  // The server drops the selected domain: the frontend clears it, re-reads /me
  // and falls back to a domain it still belongs to.
  revoked=true;
  await page.getByRole('button',{name:'刷新 Agent 列表'}).click();
  await expect(page.locator('.notice-banner')).toContainText('当前域不可用');
  await expect(selector).toHaveValue('d1');
  await expect(selector.locator('option')).toHaveCount(1);
  expect(await page.evaluate(()=>sessionStorage.getItem('agent-base-domain'))).toBe('d1');
});

test('§6.4 errors are shown in place with their frozen actions',async({page})=>{
  let reply={status:409,body:{detail:'当前轮次正在进行中',code:'conversation_busy'}};
  let meCalls=0;
  await stubWorkspace(page,{
    me:()=>{meCalls+=1;return identity();},
    onMessage:route=>route.fulfill({status:reply.status,contentType:'application/json',body:JSON.stringify(reply.body)}),
    snapshot:snapshotFor('dev-chat'),
  });
  await page.goto('/chat#dev-chat');
  const composer=page.getByRole('textbox',{name:'消息',exact:true});
  await composer.fill('请保留这段输入');

  reply={status:409,body:{detail:'当前轮次正在进行中',code:'conversation_busy'}};
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.error-banner')).toContainText('当前轮次正在进行中');
  await expect(composer).toHaveValue('请保留这段输入');

  reply={status:400,body:{detail:'消息过长',code:'invalid_request'}};
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.error-banner')).toContainText('消息过长');

  reply={status:422,body:{detail:[{loc:['body','text'],msg:'field required'}]}};
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.error-banner')).toContainText('请求格式不正确');

  reply={status:403,body:{detail:'缺少权限：task.write',code:'permission_denied'}};
  const before=meCalls;
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.error-banner')).toContainText('无权限');
  // A 403 re-reads /me so a stale permission set cannot keep offering the entry.
  await expect.poll(()=>meCalls).toBeGreaterThan(before);

  reply={status:429,body:{detail:'活跃轮次已达上限',code:'user_turn_quota'}};
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(page.locator('.error-banner')).toContainText('已达到个人上限');
  await expect(page.getByRole('button',{name:'发送消息',exact:true})).toBeDisabled();
});

test('a 404 is treated as deleted: removed from the list and the selection cleared',async({page})=>{
  await stubWorkspace(page,{
    me:identity(),
    onList:route=>route.fulfill({json:[historyFor('gone-chat',{title:'已删除对话'})]}),
    snapshot:[{status:404,body:{detail:'对话不存在',code:'conversation_not_found'}}],
  });
  await page.goto('/chat');
  await expect(page.locator('.history-list')).toContainText('已删除对话');
  await page.locator('.history-item').filter({hasText:'已删除对话'}).click();
  await expect(page.locator('.error-banner')).toContainText('已从列表移除');
  await expect(page.locator('.history-list')).not.toContainText('已删除对话');
  await expect(page.getByRole('textbox',{name:'消息',exact:true})).toHaveCount(0);
});

test('no token or cookie ever reaches localStorage',async({page})=>{
  await stubWorkspace(page,{me:identity()});
  await page.goto('/chat');
  await expect(page.getByRole('button',{name:/与 finance-agent 新建对话/})).toBeVisible();
  const storage=await page.evaluate(()=>({local:{...localStorage},session:{...sessionStorage}}));
  expect(storage.local).toEqual({});
  expect(Object.keys(storage.session).every(key=>key==='agent-base-domain'||key.startsWith('agent-draft:'))).toBe(true);
  expect(JSON.stringify(storage)).not.toMatch(/agent_base_session|bearer|cookie/i);
});
