// C3 AIHO-16 read-path authorization against the real backend, real SQLite and
// real accounts. These are the must-pass gate for the frozen C0 contract.
import {test,expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
import {execFileSync} from 'node:child_process';

const PASSWORD='fixture-member-password';
const ADMIN_PASSWORD='fixture-browser-password';

async function auth(request,user,password){
  const response=await request.post('/api/auth/login',{data:{username:user,password:password||PASSWORD}});
  expect(response.status()).toBe(200);
}

test('C3 developer only reads own conversations',async({request})=>{
  await auth(request,'developer');
  const listed=await request.get('/api/conversations');
  expect(listed.status()).toBe(200);
  const titles=(await listed.json()).map(item=>item.title);
  expect(titles).toEqual(['developer private']);
  for(const suffix of ['','/events?once=true','/files','/download?path=artifacts/report.txt']){
    expect((await request.get('/api/conversations/peer-chat'+suffix)).status()).toBe(404);
    expect((await request.get('/api/conversations/foreign-chat'+suffix)).status()).toBe(404);
    expect((await request.get('/api/conversations/reader-chat'+suffix)).status()).toBe(404);
  }
  expect((await request.get('/api/conversations/developer-chat')).status()).toBe(200);
  const artifact=await request.get('/api/conversations/developer-chat/download?path=artifacts/report.txt');
  expect(await artifact.text()).toBe('developer isolated artifact\n');
});

test('C3 same-domain peer stays private and missing objects use the same 404',async({request})=>{
  await auth(request,'developer');
  const missing=await request.get('/api/conversations/nonexistent');
  expect(missing.status()).toBe(404);
  const missingBody=await missing.json();
  expect(missingBody.code).toBe('conversation_not_found');
  const peer=await request.get('/api/conversations/peer-chat');
  expect(peer.status()).toBe(404);
  expect(await peer.json()).toEqual(missingBody);
  for(const suffix of ['/events?once=true','/files','/download?path=artifacts/report.txt']){
    expect(await (await request.get('/api/conversations/peer-chat'+suffix)).json()).toEqual(missingBody);
  }
});

test('C3 Agent catalog is domain-scoped and Admin Agent is platform-admin only',async({request})=>{
  await auth(request,'developer');
  let targets=await (await request.get('/api/chat/targets')).json();
  let ids=targets.targets.map(item=>item.id);
  expect(ids).toContain('default-agent');
  expect(ids).not.toContain('other-agent');
  expect(ids).not.toContain('admin');
  expect(targets.targets.map(item=>item.id)).toEqual(['default-agent']);

  await auth(request,'viewer');
  targets=await (await request.get('/api/chat/targets')).json();
  expect(targets.targets.map(item=>item.id)).toContain('default-agent');
  expect(targets.targets.map(item=>item.id)).not.toContain('admin');

  await auth(request,'foreign');
  ids=(await (await request.get('/api/chat/targets')).json()).targets.map(item=>item.id);
  expect(ids).toContain('other-agent');
  expect(ids).not.toContain('default-agent');

  await auth(request,'admin',ADMIN_PASSWORD);
  ids=(await (await request.get('/api/chat/targets')).json()).targets.map(item=>item.id);
  expect(ids[0]).toBe('admin');
  expect(ids).toContain('default-agent');
  expect(ids).not.toContain('other-agent');
});

test('C3 platform admin scope=all reads with audit and no scope leak',async({request})=>{
  await auth(request,'admin',ADMIN_PASSWORD);
  const me=await (await request.get('/api/v1/me')).json();
  const listed=await request.get('/api/conversations');
  expect(listed.status()).toBe(200);
  expect((await listed.json()).map(item=>item.title)).not.toContain('foreign-chat');
  expect((await request.get('/api/conversations/foreign-chat')).status()).toBe(404);

  expect((await request.get('/api/conversations/foreign-chat?scope=all')).status()).toBe(200);
  const artifact=await request.get('/api/conversations/foreign-chat/download?scope=all&path=artifacts/report.txt');
  expect(artifact.status()).toBe(200);
  expect(await artifact.text()).toBe('foreign isolated artifact\n');
  const scoped=await request.get('/api/conversations?scope=all');
  expect(scoped.status()).toBe(200);
  expect((await scoped.json()).map(item=>item.title)).toContain('foreign private');

  const audit=await request.get('/api/v1/audit?user_id='+me.id);
  expect(audit.status()).toBe(200);
  const rows=(await audit.json()).filter(row=>row.action==='chat.scope_all');
  expect(rows.length).toBeGreaterThan(0);
  expect(rows.map(row=>row.detail.path)).toEqual(expect.arrayContaining([
    '/api/conversations','/api/conversations/{id}','/api/conversations/{id}/download']));
  expect(rows.every(row=>row.level==='chat'&&row.user_id===me.id)).toBe(true);
});

test('C3 non-admin scope=all and foreign domain are denied',async({request})=>{
  await auth(request,'developer');
  expect((await request.get('/api/conversations?scope=all')).status()).toBe(403);
  expect((await request.get('/api/conversations/developer-chat?scope=all')).status()).toBe(403);
  expect((await request.get('/api/conversations?scope=everything')).status()).toBe(400);
});

function insertOrphan(){
  execFileSync(process.env.E2E_PYTHON,['-c',
    "import os,sqlite3,sys,time; d=sqlite3.connect(sys.argv[1]); c=sqlite3.connect(sys.argv[2]);"
    +"default=c.execute(\"SELECT id FROM domains WHERE code='default'\").fetchone()[0];"
    +"d.execute(\"INSERT OR REPLACE INTO conversations(id,title,target,spec,created,updated,owner_user_id,domain_id) VALUES('orphan-chat','orphan private','admin','{}',?,?,NULL,?)\",(time.time(),time.time(),default));"
    +"d.commit(); ws=os.path.join(os.path.dirname(sys.argv[1]),'conversations','orphan-chat','workspace','artifacts'); os.makedirs(ws,exist_ok=True); open(os.path.join(ws,'report.txt'),'w').write('orphan isolated artifact\\n')",
    process.env.E2E_DATA, process.env.E2E_CONTROL]);
}

test('C3 NULL owner is hidden from normal users and default admin reads',async({request})=>{
  insertOrphan();
  await auth(request,'developer');
  expect((await request.get('/api/conversations/orphan-chat')).status()).toBe(404);
  const own=await (await request.get('/api/conversations')).json();
  expect(own.map(item=>item.title)).not.toContain('orphan-chat');
  await auth(request,'admin',ADMIN_PASSWORD);
  expect((await request.get('/api/conversations/orphan-chat')).status()).toBe(404);
  // §5.3 orders the NULL-owner check before the explicit scope=all bypass.
  expect((await request.get('/api/conversations/orphan-chat?scope=all')).status()).toBe(404);
});

test('C3 snapshot files field collapses without file.read',async({request})=>{
  await auth(request,'reader');
  const snapshot=await request.get('/api/conversations/reader-chat');
  expect(snapshot.status()).toBe(200);
  expect((await snapshot.json()).files).toEqual([]);
  const denied=await request.get('/api/conversations/reader-chat/files');
  expect(denied.status()).toBe(403);
  expect((await denied.json()).code).toBe('permission_denied');
  expect((await request.get('/api/conversations/reader-chat/download?path=artifacts/report.txt')).status()).toBe(403);
});

test('C3 service account is rejected by the page read surface',async({request})=>{
  await auth(request,'service');
  for(const path of ['/api/chat/targets','/api/conversations']){
    const response=await request.get(path);
    expect(response.status()).toBe(403);
    expect((await response.json()).code).toBe('service_account_forbidden');
  }
});

test('C3 open SSE stream ends within 30 seconds after privilege revocation',async({request})=>{
  await auth(request,'revoked');
  const started=Date.now();
  const pending=request.get('/api/conversations/revoked-chat/events');
  await new Promise(resolve=>setTimeout(resolve,800));
  execFileSync(process.env.E2E_PYTHON,['-c',
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE users SET status='disabled' WHERE username='revoked'\"); c.commit()",
    process.env.E2E_CONTROL]);
  const response=await pending;
  const body=await response.text();
  expect(response.status()).toBe(200);
  expect(Date.now()-started).toBeLessThan(30000);
  expect(body).toContain('event: error');
  expect(body).toContain('"code": "unauthorized"');
  expect(body).not.toContain('Traceback');
});
