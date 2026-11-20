// C4 AIHO-17 write-path authorization against the real backend, real SQLite and
// real accounts. Must-pass gate: create/send/stop/rename/archive are owner-only,
// administrators have no write bypass, and per-user turn/SSE quotas are enforced.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';

const PASSWORD='fixture-member-password';
const ADMIN_PASSWORD='fixture-browser-password';

async function auth(request,user,password){
  const response=await request.post('/api/auth/login',{data:{username:user,password:password||PASSWORD}});
  expect(response.status()).toBe(200);
}

const WRITES=[
  ['post','/messages',{text:'intrusion',client_key:'cross-domain'}],
  ['post','/stop',{}],
  ['patch','',{title:'intrusion'}],
  ['patch','',{archived:true}],
];

for(const [method,suffix,data] of WRITES){
  test(`C4 cross-domain ${method} ${suffix} ${JSON.stringify(data)} is 404`,async({request})=>{
    await auth(request,'developer');
    const response=await request[method]('/api/conversations/foreign-chat'+suffix,{data});
    expect(response.status()).toBe(404);
    expect((await response.json()).code).toBe('conversation_not_found');
  });
}
test('C4 missing conversation writes share the same 404',async({request})=>{
  await auth(request,'developer');
  for(const [method,suffix,data] of WRITES){
    const response=await request[method]('/api/conversations/does-not-exist'+suffix,{data});
    expect(response.status()).toBe(404);
    expect(await response.json()).toEqual({detail:'对话不存在',code:'conversation_not_found'});
  }
});
test('C4 platform admin has no write bypass and scope=all stays read-only',async({request})=>{
  await auth(request,'admin',ADMIN_PASSWORD);
  for(const [method,suffix,data] of WRITES){
    const response=await request[method]('/api/conversations/foreign-chat'+suffix,{data});
    expect(response.status()).toBe(404);
    expect((await response.json()).code).toBe('conversation_not_found');
  }
  const denied=await request.patch('/api/conversations/foreign-chat?scope=all',{data:{title:'no bypass'}});
  expect(denied.status()).toBe(403);
  expect((await denied.json()).code).toBe('scope_denied');
  await auth(request,'foreign');
  expect((await (await request.get('/api/conversations/foreign-chat')).json()).conversation.title).toBe('foreign private');
});
test('C4 create stamps owner/domain, filters targets and keeps the owner lifecycle',async({request})=>{
  await auth(request,'developer');
  const identity=await (await request.get('/api/v1/me')).json();
  const created=await request.post('/api/conversations',{data:{target:'default-agent',title:'C4 created'}});
  expect(created.status()).toBe(201);
  const chat=await created.json();
  expect(chat.owner_user_id).toBe(identity.id);
  expect(chat.domain_id).toBe(identity.home_domain_id);
  expect((await request.post('/api/conversations',{data:{target:'other-agent'}})).status()).toBe(404);
  expect((await request.post('/api/conversations',{data:{target:'admin'}})).status()).toBe(404);

  expect((await request.patch(`/api/conversations/${chat.id}`,{data:{title:'C4 renamed'}})).status()).toBe(200);
  expect((await request.patch(`/api/conversations/${chat.id}`,{data:{archived:true}})).status()).toBe(200);
  const archived=await request.post(`/api/conversations/${chat.id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'c4-archived'}});
  expect(archived.status()).toBe(400);
  expect((await archived.json()).code).toBe('conversation_archived');
  expect((await request.patch(`/api/conversations/${chat.id}`,{data:{archived:false}})).status()).toBe(200);
  const sent=await request.post(`/api/conversations/${chat.id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'c4-owner'}});
  expect(sent.status()).toBe(202);
  expect((await request.post(`/api/conversations/${chat.id}/stop`)).status()).toBe(200);
});
test('C4 viewer and service accounts are rejected on every write route',async({request})=>{
  await auth(request,'viewer');
  for(const [method,suffix,data] of WRITES){
    const response=await request[method]('/api/conversations/foreign-chat'+suffix,{data});
    expect(response.status()).toBe(403);
    expect((await response.json()).code).toBe('permission_denied');
  }
  expect((await request.post('/api/conversations',{data:{target:'default-agent'}})).status()).toBe(403);

  await auth(request,'service');
  for(const [method,suffix,data] of WRITES){
    const response=await request[method]('/api/conversations/foreign-chat'+suffix,{data});
    expect(response.status()).toBe(403);
    expect((await response.json()).code).toBe('service_account_forbidden');
  }
  expect((await request.post('/api/conversations',{data:{target:'default-agent'}})).status()).toBe(403);
});
test('C4 ninth active turn hits the per-user 429 quota',async({request})=>{
  await auth(request,'admin',ADMIN_PASSWORD);
  const chats=[];
  try{
    for(let n=0;n<9;n++){
      const created=await request.post('/api/conversations',{data:{title:'quota '+n}});
      expect(created.status()).toBe(201);
      const id=(await created.json()).id;
      chats.push(id);
      const sent=await request.post(`/api/conversations/${id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'quota'}});
      expect(sent.status()).toBe(n<8?202:429);
      if(n===8)expect((await sent.json()).code).toBe('user_turn_quota');
    }
  }finally{
    for(const id of chats)await request.post(`/api/conversations/${id}/stop`);
  }
});
test('C4 fifth simultaneous SSE stream hits the per-user 429 quota',async({request})=>{
  await auth(request,'admin',ADMIN_PASSWORD);
  const cookies=(await request.storageState()).cookies;
  const cookie=cookies.map(c=>`${c.name}=${c.value}`).join('; ');
  const controllers=[];const open=[];
  try{
    for(let n=0;n<5;n++){
      const controller=new AbortController();
      controllers.push(controller);
      const response=await fetch(process.env.E2E_URL+'/api/conversations/legacy/events',{headers:{Cookie:cookie,Accept:'text/event-stream'},signal:controller.signal});
      open.push(response); // keep the first four bodies referenced so their sockets stay open
      expect(response.status).toBe(n<4?200:429);
      if(n===4)expect((await response.json()).code).toBe('user_stream_quota');
    }
  }finally{
    controllers.forEach(controller=>controller.abort());
    for(const response of open)await response.body?.cancel().catch(()=>{});
  }
});
