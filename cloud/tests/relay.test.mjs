import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker, { seal, open, presentToolResult } from '../worker/index.js';

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

const toolResult = (data) => ({content:[{type:'text',text:JSON.stringify(data)}], structuredContent:data, isError:false});

test('results carry data once without changing file, binary, process or search semantics', () => {
  const samples = [
    ['read_file',{content:'\n  原样保留空格\r\n{"x":null,"flag":false}\n',total_lines:8,next_offset:3}],
    ['read_file',{content:'AAH/AA==',encoding:'base64',size:4}],
    ['read_process_output',{output:'\r\n  exact\toutput\n',cursor:123,truncated:true,state:'running',exit_code:null,pid:42}],
    ['read_process_output',{output:'',cursor:123,truncated:false,state:'finished',exit_code:0,pid:42}],
    ['get_more_search_results',{results:[{path:'/a b/c',line:2,text:'  match  '}],next_offset:1,complete:false,truncated:false,scanned:3,skipped:1,error:null,canceled:false}],
    ['get_more_search_results',{results:[],next_offset:1,complete:true,truncated:true,scanned:4,skipped:2,error:'permission denied',canceled:true}],
    ['list_directory',{entries:[{name:'link',is_directory:false,is_symlink:true}],next_offset:null}],
    ['write_file',{path:'/absolute/file',bytes_written:0}],
    ['force_terminate',{session_id:'s',termination_requested:true}],
  ];
  for (const [name,data] of samples) {
    const original=toolResult(data), snapshot=structuredClone(original);
    const compact=presentToolResult(original,name);
    assert.deepEqual(compact.structuredContent,data);
    assert.deepEqual(compact.content,[]);
    assert.deepEqual(original,snapshot,'must not mutate the saved result');
    assert.ok(JSON.stringify(compact).length < JSON.stringify(original).length);
  }
});

test('device lists are compact by default and full identities remain retrievable', () => {
  const data={devices:[
    {id:'mac',online:true,name:'mac',hostname:'my-mac.local',user:'owner',system:'Darwin',python:'3.14',python_executable:'/Users/owner/.venv/bin/python'},
    {id:'linux',online:true,name:'Lab computer',hostname:'lab',system:'Linux'},
    {id:'offline',online:false,error:'Connection failed; check URL, token and agent service'},
  ]};
  const original=toolResult(data);
  const compact=presentToolResult(original,'list_devices');
  assert.deepEqual(compact.structuredContent,{devices:[
    {id:'mac',online:true,system:'Darwin'},
    {id:'linux',online:true,name:'Lab computer',system:'Linux'},
    {id:'offline',online:false,error:data.devices[2].error},
  ]});
  assert.deepEqual(compact.content,[]);
  const full=presentToolResult(original,'list_devices','full');
  assert.deepEqual(full.structuredContent,data);
  assert.deepEqual(full.content,[]);
  // An unrecognized future shape must not be silently stripped.
  const other=toolResult({devices:[null,{future:'identity'}],extra:'keep'});
  assert.deepEqual(presentToolResult(other,'list_devices').structuredContent,other.structuredContent);
});

test('presentation preserves errors, distinct text, media and result metadata', () => {
  const data={content:'exact body',next_offset:null};
  const original=toolResult(data);
  original.content.push({type:'text',text:'warning: inspect this separately'}, {type:'image',data:'AA==',mimeType:'image/png'});
  original._meta={private:'component only'};
  const compact=presentToolResult(original,'read_file');
  assert.deepEqual(compact.content,original.content.slice(1));
  assert.deepEqual(compact._meta,original._meta);
  assert.deepEqual(presentToolResult({...original,isError:true},'read_file'),{...original,isError:true});
  for(const result of [{content:[{type:'text',text:'legacy text'}]}, {content:[],structuredContent:null}]) {
    assert.deepEqual(presentToolResult(result,'read_file'),result);
  }
});

test('only exact JSON duplicates are removed, including pretty-printed text', () => {
  const original=toolResult({content:'body',next_offset:null});
  original.content[0].text=JSON.stringify(original.structuredContent,null,2);
  assert.deepEqual(presentToolResult(original,'read_file').content,[]);
  original.content[0].text=JSON.stringify({content:'different body',next_offset:null});
  assert.deepEqual(presentToolResult(original,'read_file').content,original.content);
});

test('saved queue results support compact/full retrieval without forwarding detail or rerunning', async () => {
  const env=environment(), headers={Authorization:'Bearer '+env.RC_RELAY_TOKEN};
  const owner={'oai-authenticated-user-id':'owner'};
  await worker.fetch(request('/relay/poll',{capacity:0},headers),env);
  const response=worker.fetch(request('/mcp',rpc('list_devices',{detail:'full'}),owner),env);
  let row;
  for(let i=0;i<100;i++) {row=env.db.prepare('SELECT * FROM relay_jobs').get();if(row)break;await new Promise(r=>setTimeout(r,10));}
  assert.ok(row);
  const decoded=await open(env,row.payload,row.id+':request');
  assert.deepEqual(decoded,{name:'list_devices',arguments:{}});
  const claimed=await (await worker.fetch(request('/relay/poll',{capacity:1},headers),env)).json();
  const data={devices:[{id:'mac',online:true,name:'mac',hostname:'mac.local',user:'owner',system:'Darwin',python:'3.14'}]};
  const original=toolResult(data);
  const payload=await seal(env,original,row.id+':response');
  await worker.fetch(request('/relay/complete',{id:row.id,claim:claimed.jobs[0].claim,payload},headers),env);
  const full=(await (await response).json()).result;
  assert.deepEqual(full.structuredContent,data);
  assert.deepEqual(full.content,[]);
  const compact=(await (await worker.fetch(request('/mcp',rpc('get_operation_result',{operation_id:row.id}),owner),env)).json()).result;
  assert.deepEqual(compact.structuredContent,{devices:[{id:'mac',online:true,system:'Darwin'}]});
  const again=(await (await worker.fetch(request('/mcp',rpc('get_operation_result',{operation_id:row.id,detail:'full'}),owner),env)).json()).result;
  assert.deepEqual(again,full);
  assert.equal(env.db.prepare('SELECT COUNT(*) AS n FROM relay_jobs').get().n,1);
  assert.equal((await (await worker.fetch(request('/relay/poll',{capacity:1},headers),env)).json()).jobs.length,0);
  const denied=(await (await worker.fetch(request('/mcp',rpc('get_operation_result',{operation_id:row.id,detail:'full'}),{'oai-authenticated-user-id':'other'}),env)).json()).result;
  assert.equal(denied.isError,true);
});

test('detail is discoverable and invalid detail never queues an operation', async () => {
  const env=environment();
  const list=await (await worker.fetch(request('/mcp',{jsonrpc:'2.0',id:1,method:'tools/list'}),env)).json();
  for(const name of ['list_devices','get_operation_result']) {
    assert.deepEqual(list.result.tools.find(t=>t.name===name).inputSchema.properties.detail.enum,['compact','full']);
    const rejected=await (await worker.fetch(request('/mcp',rpc(name,{detail:'invalid'}),{'oai-authenticated-user-id':'owner'}),env)).json();
    assert.equal(rejected.error.code,-32602);
  }
  assert.equal(env.db.prepare('SELECT COUNT(*) AS n FROM relay_jobs').get().n,0);
});
