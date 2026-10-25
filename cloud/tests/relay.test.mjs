import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker, { seal, open } from '../worker/index.js';

function environment() {
  const db = new DatabaseSync(':memory:');
  db.exec(readFileSync('drizzle/0000_relay.sql', 'utf8').replaceAll('--> statement-breakpoint', ''));
  return {
    RC_RELAY_TOKEN: 'test-bridge-token-'+'a'.repeat(40),
    RC_RELAY_KEY: Buffer.alloc(32, 17).toString('base64'),
    DB: { prepare(sql) {
      const statement = db.prepare(sql);
      const prepare = (args=[]) => ({bind: (...newArgs) => prepare(newArgs),
        first: async () => statement.get(...args) || null,
        run: async () => ({meta: statement.run(...args)}),
      });
      return prepare();
    }},
    db,
  };
}
function request(path, data, headers={}) {
  return new Request('https://private-site.example'+path, {method:'POST', headers:{'Content-Type':'application/json',...headers}, body:JSON.stringify(data)});
}
const rpc = (name, args={}) => ({jsonrpc:'2.0', id:1, method:'tools/call', params:{name, arguments:args}});

test('protocol discovery exposes typed tools; invocations require verified owner identity', async () => {
  const env=environment();
  const list=await (await worker.fetch(request('/mcp',{jsonrpc:'2.0',id:1,method:'tools/list'}),env)).json();
  assert.equal(list.result.tools.length,20);
  assert.equal(list.result.tools.find(t=>t.name==='read_file').annotations.readOnlyHint,true);
  const denied=await (await worker.fetch(request('/mcp',rpc('list_devices')),env)).json();
  assert.equal(denied.error.code,-32001);
  assert.equal(env.db.prepare('SELECT COUNT(*) AS n FROM relay_jobs').get().n,0);
});

test('bridge rejects missing credentials; offline calls are not enqueued', async()=>{
  const env=environment();
  assert.equal((await worker.fetch(request('/relay/poll',{capacity:1}),env)).status,401);
  const result=await (await worker.fetch(request('/mcp',rpc('start_process',{command:'x'}),{'oai-authenticated-user-id':'owner'}),env)).json();
  assert.equal(result.result.isError,true);
  assert.equal(env.db.prepare('SELECT COUNT(*) AS n FROM relay_jobs').get().n,0);
});

test('encrypted queue supports claim, single execution, result delivery and ownership isolation', async()=>{
  const env=environment();
  const headers={Authorization:'Bearer '+env.RC_RELAY_TOKEN};
  await worker.fetch(request('/relay/poll',{capacity:0},headers),env);
  const response=worker.fetch(request('/mcp',rpc('read_file',{path:'x'}),{'oai-authenticated-user-id':'owner'}),env);
  let row;
  for(let i=0;i<100;i++) { row=env.db.prepare('SELECT * FROM relay_jobs').get(); if(row)break; await new Promise(r=>setTimeout(r,10)); }
  assert.ok(row);
  assert.ok(!row.payload.includes('read_file'));
  assert.equal((await open(env,row.payload,row.id+':request')).name,'read_file');
  const claimed=await (await worker.fetch(request('/relay/poll',{capacity:1},headers),env)).json();
  assert.equal(claimed.jobs.length,1);
  assert.equal((await (await worker.fetch(request('/relay/poll',{capacity:1},headers),env)).json()).jobs.length,0);
  const result={content:[{type:'text',text:'done'}],isError:false};
  const payload=await seal(env,result,row.id+':response');
  for(let i=0;i<2;i++) await worker.fetch(request('/relay/complete',{id:row.id,claim:claimed.jobs[0].claim,payload},headers),env);
  assert.deepEqual((await (await response).json()).result,result);
  const wrongOwner=await (await worker.fetch(request('/mcp',rpc('get_operation_result',{operation_id:row.id}),{'oai-authenticated-user-id':'other'}),env)).json();
  assert.equal(wrongOwner.result.isError,true);
  const own=await (await worker.fetch(request('/mcp',rpc('get_operation_result',{operation_id:row.id}),{'oai-authenticated-user-id':'owner'}),env)).json();
  assert.deepEqual(own.result,result);
});

test('expired worker leases are never retried as new operations', async()=>{
  const env=environment(),now=Date.now();
  env.db.prepare("INSERT INTO relay_jobs(id,owner,status,payload,claim,lease_until,created,expires) VALUES ('op','owner','running','cipher','claim',?,?,?)").run(now-1,now-1000,now+100000);
  const result=await (await worker.fetch(request('/relay/poll',{capacity:4},{Authorization:'Bearer '+env.RC_RELAY_TOKEN}),env)).json();
  assert.equal(result.jobs.length,0);
  assert.equal(env.db.prepare("SELECT status FROM relay_jobs WHERE id='op'").get().status,'uncertain');
  await worker.fetch(request('/relay/complete',{id:'op',claim:'claim',payload:'stale'},{Authorization:'Bearer '+env.RC_RELAY_TOKEN}),env);
  assert.equal(env.db.prepare("SELECT status FROM relay_jobs WHERE id='op'").get().status,'uncertain');
});

test('ciphertext is authenticated and bound to its operation', async()=>{
  const env=environment();
  const data=await seal(env,{command:'secret'},'op:request');
  assert.deepEqual(await open(env,data,'op:request'),{command:'secret'});
  await assert.rejects(open(env,data,'different-operation:request'));
});
