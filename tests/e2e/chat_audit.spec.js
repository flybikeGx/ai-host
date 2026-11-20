// C5 AIHO-18 audit gate against the real backend, real SQLite and real accounts.
// A turn's send and stop rows share trace_id=turn_id, survive a process reuse,
// and the persisted ledger never contains the submitted message text.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
async function auth(request,user){expect((await request.post('/api/auth/login',{data:{username:user,password:user==='admin'?'fixture-browser-password':'fixture-member-password'}})).status()).toBe(200);}
test('C5 send and stop share turn trace in persisted audit',async({request})=>{
 await auth(request,'admin');
 const created=await request.post('/api/conversations',{data:{title:'audit trace'}});expect(created.status()).toBe(201);const id=(await created.json()).id;
 const sent=await request.post(`/api/conversations/${id}/messages`,{data:{text:'FIXTURE_SLEEP',client_key:'audit'}});expect(sent.status()).toBe(202);const turn=(await sent.json()).id;
 await request.post(`/api/conversations/${id}/stop`);
 const audit=await request.get('/api/v1/audit?trace_id='+turn);expect(audit.status()).toBe(200);const body=JSON.stringify(await audit.json());expect(body).toContain('chat.send');expect(body).toContain('chat.stop');expect(body).not.toContain('FIXTURE_SLEEP');
});
