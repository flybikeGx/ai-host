import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
async function auth(request,user){expect((await request.post('/api/auth/login',{data:{username:user,password:user==='admin'?'fixture-browser-password':'fixture-member-password'}})).status()).toBe(200);}
test('C3 AIHO-16 developer may read own chat list',async({request})=>{await auth(request,'developer');expect((await request.get('/api/conversations')).status()).toBe(200);});
test('C3 AIHO-16 viewer sees domain Agent catalog',async({request})=>{await auth(request,'viewer');expect((await request.get('/api/chat/targets')).status()).toBe(200);});
test('C4 AIHO-17 service identity rejected by chat API',async({request})=>{await auth(request,'service');expect((await request.get('/api/chat/targets')).status()).toBe(403);});
test('C4 AIHO-17 missing conversation uses 404',async({request})=>{await auth(request,'admin');expect((await request.get('/api/conversations/nonexistent')).status()).toBe(404);});
test('C6 AIHO-19 developer can open chat UI',async({page})=>{await auth(page.request,'developer');await page.goto('/chat');await expect(page.locator('textarea')).toBeVisible({timeout:3000});});
for(const suffix of ['', '/events?once=true','/files','/download?path=artifacts/report.txt']){
 test(`C3 AIHO-16 cross-domain object hidden ${suffix}`,async({request})=>{await auth(request,'developer');expect((await request.get('/api/conversations/foreign-chat'+suffix)).status()).toBe(404);});
}
for(const [method,suffix,data] of [['post','/messages',{text:'test',client_key:'cross-domain'}],['post','/stop',{}],['patch','',{title:'intrusion'}],['patch','',{archived:true}]]){
 test(`C4 AIHO-17 cross-domain ${method} ${suffix} ${JSON.stringify(data)}`,async({request})=>{await auth(request,'developer');expect((await request[method]('/api/conversations/foreign-chat'+suffix,{data})).status()).toBe(404);});
}
test('C3 AIHO-16 admin default list excludes foreign ownership',async({request})=>{await auth(request,'admin');const r=await request.get('/api/conversations');expect(r.status()).toBe(200);expect(JSON.stringify(await r.json())).not.toContain('foreign-chat');});
test('C3 AIHO-16 admin scope all reads real artifact',async({request})=>{await auth(request,'admin');const r=await request.get('/api/conversations/foreign-chat/download?scope=all&path=artifacts/report.txt');expect(r.status()).toBe(200);expect(await r.text()).toBe('foreign isolated artifact\n');});
test('C4 AIHO-17 scope all never permits admin writes',async({request})=>{await auth(request,'admin');expect((await request.patch('/api/conversations/foreign-chat?scope=all',{data:{title:'no bypass'}})).status()).toBe(403);});
test('C5 AIHO-18 scope all emits attributed audit',async({request})=>{await auth(request,'admin');await request.get('/api/conversations?scope=all');const r=await request.get('/api/v1/audit');expect(r.status()).toBe(200);expect(JSON.stringify(await r.json())).toContain('chat.scope_all');});
test('C4 AIHO-17 ninth active turn hits per-user 429 quota',async({request})=>{
 await auth(request,'admin');const chats=[];
 try{
  for(let n=0;n<9;n++){
   const c=await request.post('/api/conversations',{data:{title:'quota '+n}});expect(c.status()).toBe(201);
   const id=(await c.json()).id;chats.push(id);
   const sent=await request.post(`/api/conversations/${id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'quota'}});
   expect(sent.status()).toBe(n<8?202:429);
  }
 }finally{for(const id of chats)await request.post(`/api/conversations/${id}/stop`);}
});
test('C5 AIHO-18 send and stop share turn trace in persisted audit',async({request})=>{
 await auth(request,'admin');
 const created=await request.post('/api/conversations',{data:{title:'audit trace'}});expect(created.status()).toBe(201);const id=(await created.json()).id;
 const sent=await request.post(`/api/conversations/${id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'audit'}});expect(sent.status()).toBe(202);const turn=(await sent.json()).id;
 await request.post(`/api/conversations/${id}/stop`);
 const audit=await request.get('/api/v1/audit?trace_id='+turn);expect(audit.status()).toBe(200);const body=JSON.stringify(await audit.json());expect(body).toContain('chat.send');expect(body).toContain('chat.stop');expect(body).not.toContain('FIXTURE_SLEEP');
});
test('C3 AIHO-16 same-domain peer conversation stays private',async({request})=>{await auth(request,'developer');expect((await request.get('/api/conversations/peer-chat')).status()).toBe(404);});
test('C3 AIHO-16 developer Agent catalog hides Admin Agent',async({request})=>{await auth(request,'developer');const r=await request.get('/api/chat/targets');expect(r.status()).toBe(200);expect((await r.json()).targets.map(t=>t.id)).not.toContain('admin');});
test('C4 AIHO-17 fifth simultaneous SSE stream hits 429',async({request})=>{
 await auth(request,'admin');const cookies=(await request.storageState()).cookies;const cookie=cookies.map(c=>`${c.name}=${c.value}`).join('; ');
 const controllers=[];
 try{
  for(let n=0;n<5;n++){
   const controller=new AbortController();controllers.push(controller);
   const r=await fetch(process.env.E2E_URL+'/api/conversations/legacy/events',{headers:{Cookie:cookie},signal:controller.signal});
   expect(r.status).toBe(n<4?200:429);
  }
 }finally{controllers.forEach(c=>c.abort());}
});
