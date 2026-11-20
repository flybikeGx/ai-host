// Not-yet-implemented C4-C6 contracts. Kept separate from the must-pass gate:
// `python3 scripts/ci.py contracts` reports them without masking the gap.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
async function auth(request,user){expect((await request.post('/api/auth/login',{data:{username:user,password:user==='admin'?'fixture-browser-password':'fixture-member-password'}})).status()).toBe(200);}
test('C6 AIHO-19 developer can open chat UI',async({page})=>{await auth(page.request,'developer');await page.goto('/chat');await expect(page.locator('textarea')).toBeVisible({timeout:3000});});
for(const [method,suffix,data] of [['post','/messages',{text:'test',client_key:'cross-domain'}],['post','/stop',{}],['patch','',{title:'intrusion'}],['patch','',{archived:true}]]){
 test(`C4 AIHO-17 cross-domain ${method} ${suffix} ${JSON.stringify(data)}`,async({request})=>{await auth(request,'developer');expect((await request[method]('/api/conversations/foreign-chat'+suffix,{data})).status()).toBe(404);});
}
test('C4 AIHO-17 scope all never permits admin writes',async({request})=>{await auth(request,'admin');expect((await request.patch('/api/conversations/foreign-chat?scope=all',{data:{title:'no bypass'}})).status()).toBe(403);});
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
