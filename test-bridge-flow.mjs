import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {CONFIG} from './dist/model.mjs';
import {base58Encode,fromBase64,toBase64} from './dist/trade.mjs';
import {createSettlementClient,SETTLEMENT_NETWORK,SETTLEMENT_DOMAIN,SETTLEMENT_AUDIENCE,SETTLEMENT_ROUTES,settlementSigningMessage} from './dist/settlement.mjs';
import {createBridgeFlow} from './dist/bridge-flow.mjs';
import {associatedUsdcAccount,expectedSourceMessage,reviewPreparedSource} from './dist/bridge-source.mjs';
import {mountBridge} from './dist/bridge-ui.mjs';
import {loadSolanaLibrary} from './dist/solana-loader.mjs';

// Synthetic public accounts/signatures only. No keys or external service calls.
const A=base58Encode(new Uint8Array(32).fill(7)),B=base58Encode(new Uint8Array(32).fill(8));
const signatureBytes=new Uint8Array(64).fill(11),signature=base58Encode(signatureBytes);
const sdk=vm.runInNewContext(readFileSync(new URL('./dist/assets/vendor/solana-web3-1.98.4.iife.min.js',import.meta.url),'utf8')+'\nsolanaWeb3;',{
  TextEncoder,TextDecoder,console,setTimeout,clearTimeout,crypto:{getRandomValues(){throw new Error('No keys in this fixture.');}},fetch(){throw new Error('No network.');}});
const memory=()=>{const values=new Map();return{values,getItem:k=>values.get(k)??null,setItem(k,v){values.set(k,v);}};};
const clone=value=>JSON.parse(JSON.stringify(value));
const hex=bytes=>Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');

function fixture(){
  let milliseconds=1800000000000,next=0,quote,intent,source;
  const storage=memory(),credentials=memory(),calls=[],state={enabled:true,status:'awaiting_funding',owner:A,revision:1,ownership:0,signs:0,broadcasts:0,sourcePosts:0,ambiguous:false,badSource:false,badFee:false,valid:true,token:'b'.repeat(64)};
  const now=()=>milliseconds,seconds=()=>Math.floor(milliseconds/1000);
  const crypto={getRandomValues(bytes){bytes.fill(++next);return bytes;},subtle:{digest:(...args)=>webcrypto.subtle.digest(...args),async importKey(){return 'public-fixture';},async verify(){return state.valid;}}};
  const status=()=>({...Object.fromEntries(['client_request_id','quote_id','action','route','network','source_owner','destination_owner','solana_token_account','amount_raw','min_output_raw'].map(k=>[k,intent[k]])),status:state.status,updated_at:seconds(),source_txid:['processing','completed'].includes(state.status)?state.sourceIdOverride||signature:null,destination_txid:state.status==='completed'?signature:null,actual_output_raw:state.status==='completed'?(intent.action==='buy'?'998000001':intent.amount_raw):null,refund_amount_raw:null,refund_asset:null});
  const client=createSettlementClient({storage,credentialStorage:credentials,crypto,now,locks:{request:async(_key,_options,fn)=>fn({})},async fetcher(url,init){
    assert.ok(url.startsWith(SETTLEMENT_AUDIENCE+'/'));const path=url.slice(SETTLEMENT_AUDIENCE.length),body=init.body?JSON.parse(init.body):null;calls.push({path,body});let reply;
    if(path==='/capabilities')reply={schema_version:1,network:SETTLEMENT_NETWORK,can_accept_funds:state.enabled,routes:Object.fromEntries(Object.keys(SETTLEMENT_ROUTES).map(k=>[k,{enabled:state.enabled&&k!=='native_xrs_to_solana_usdc'}])),issued_at:seconds(),expires_at:seconds()+20};
    else if(path==='/quotes'){const {slippage_bps,...rest}=body;assert.equal(slippage_bps,50);quote={schema_version:1,...rest,quote_id:'q-'+next,network:SETTLEMENT_NETWORK,min_output_raw:body.action==='buy'?'995000000':body.amount_raw,expected_output_raw:body.action==='buy'?'1000000000':body.amount_raw,created_at:seconds(),expires_at:seconds()+60};reply=quote;}
    else if(path==='/challenges'){intent={schema_version:2,domain:SETTLEMENT_DOMAIN,audience:SETTLEMENT_AUDIENCE,...Object.fromEntries(['action','route','amount_raw','min_output_raw','source_owner','destination_owner','solana_token_account','quote_id','client_request_id','network','created_at','expires_at'].map(k=>[k,quote[k]])),nonce:'a'.repeat(64)};reply={schema_version:1,intent,signing_message_utf8:settlementSigningMessage(intent,{now:seconds()})};}
    else if(path==='/requests'){assert.equal(init.headers['Idempotency-Key'],intent.client_request_id);assert.equal(body.signature,signature);reply={schema_version:1,request:status(),status_token:state.token};}
    else if(path.endsWith('/access'))reply={schema_version:1,request:status(),status_token:state.token};
    else if(path.endsWith('/prepare-source')){
      assert.equal(init.headers.Authorization,'Bearer '+state.token);
      const message=await expectedSourceMessage(intent,new Uint8Array(32).fill(3),settlementSigningMessage(intent,{now:seconds()}),{crypto});
      const wire=new Uint8Array(65+message.length);wire[0]=1;wire.set(message,65);
      source={client_request_id:intent.client_request_id,intent_digest:hex(await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(settlementSigningMessage(intent,{now:seconds()})))),chain:intent.action==='buy'?'solana:mainnet':CONFIG.chainId,stage:intent.action==='buy'?'deposit':'burn',signer:A,transaction_base64:toBase64(wire),message_base64:toBase64(message),message_sha256:hex(await webcrypto.subtle.digest('SHA-256',message)),expires_at:seconds()+30,fee_raw:intent.action==='buy'?'5000':state.badFee?'999999':'1000000',fee_asset:intent.action==='buy'?'SOL':'XRS'};
      if(state.badSource){wire[wire.length-1]^=1;source.transaction_base64=toBase64(wire);}
      reply={schema_version:1,source};
    }else if(path.endsWith('/submit-source')){
      assert.equal(init.headers.Authorization,'Bearer '+state.token);state.sourcePosts++;
      const local=client.getSource(intent.client_request_id);assert.equal(body.transaction_base64,local.signed_transaction);assert.equal(local.signature,signature);
      if(seconds()>=source.expires_at){state.status='manual_review';}
      else {assert.equal(local.state,'submitting');state.broadcasts++;state.status='processing';}
      if(state.ambiguous)throw new Error('Synthetic lost acknowledgment');reply={schema_version:1,request:status()};
    }else{assert.equal(init.method,'GET');assert.equal(init.headers.Authorization,'Bearer '+state.token);reply={schema_version:1,request:status()};}
    return new Response(JSON.stringify(reply),{status:200,headers:{'Content-Type':'application/json'}});
  }});
  function signed(encoded){const wire=fromBase64(encoded);wire.set(signatureBytes,1);return{signature,signedTransaction:toBase64(wire)};}
  const solana={getState:()=>({status:'connected',address:state.owner,revision:state.revision,providerId:'synthetic',canSignOwnership:true,canSignTransaction:true}),async signOwnership(){state.ownership++;return{signature};},async signSourceTransaction(encoded){state.signs++;return signed(encoded);}};
  const provider={isXeris:true,async connect(){return{publicKey:state.owner};},async getRpcUrl(){return CONFIG.rpc;},async signMessage(){state.ownership++;return{signature:signatureBytes};},async signTransaction(encoded){state.signs++;return signed(encoded);}};
  const flow=createBridgeFlow({client,solana,getXeris:()=>({provider,address:state.owner}),crypto,now});
  const input=action=>({action,amount_raw:'1000000',source_owner:A,destination_owner:B,solana_token_account:associatedUsdcAccount(action==='buy'?A:B,sdk)});
  return {state,flow,client,calls,storage,credentials,crypto,now,input,solana,provider,advance:s=>{milliseconds+=s*1000;},async start(action='buy'){const q=await flow.quote(input(action));await flow.authorize(q.client_request_id);return q.client_request_id;}};
}

test('complete synthetic purchase and redemption require distinct ownership, source signature and one submission',async()=>{
  for(const action of ['buy','redeem']){const f=fixture(),id=await f.start(action);assert.equal(f.state.ownership,1);assert.equal(f.state.signs,0);assert.equal(f.state.broadcasts,0);
    const prepared=await f.flow.prepare(id);assert.equal(prepared.fee_asset,action==='buy'?'SOL':'XRS');assert.equal(f.state.signs,0);
    await f.flow.sign(id);assert.equal(f.state.signs,1);assert.equal(f.state.broadcasts,0);assert.equal(f.client.getSource(id).state,'signed');
    const result=await f.flow.submit(id);assert.equal(result.status,'processing');assert.equal(f.state.broadcasts,1);assert.equal(f.state.sourcePosts,1);
    await assert.rejects(f.flow.submit(id));assert.equal(f.state.sourcePosts,1);
    f.state.status='completed';assert.equal((await f.flow.recover(id)).status,'completed');assert.equal(f.state.signs,1);
  }
});
test('paused availability never requests ownership approval, source approval or a payment',async()=>{
  const f=fixture();f.state.enabled=false;await assert.rejects(f.flow.quote(f.input('buy')));assert.equal(f.calls.length,1);assert.equal(f.state.ownership,0);
  f.state.enabled=true;const id=await f.start();await f.flow.prepare(id);f.state.enabled=false;await assert.rejects(f.flow.sign(id));assert.equal(f.state.signs,0);assert.equal(f.state.broadcasts,0);
});
test('changed source accounts reject ownership/source steps and never broadcast',async()=>{
  const f=fixture(),q=await f.flow.quote(f.input('buy'));f.state.owner=B;f.state.revision++;await assert.rejects(f.flow.authorize(q.client_request_id),/original Solana/);assert.equal(f.state.ownership,0);
  f.state.owner=A;const id=q.client_request_id;await f.flow.authorize(id);await f.flow.prepare(id);f.state.owner=B;await assert.rejects(f.flow.sign(id));assert.equal(f.state.signs,0);
});
test('prepared source mutations and a changed Xeris burn fee fail before any wallet approval',async()=>{
  for(const action of ['buy','redeem']){const f=fixture(),id=await f.start(action);f.state.badSource=true;await assert.rejects(f.flow.prepare(id));assert.equal(f.state.signs,0);}
  const f=fixture(),id=await f.start('redeem');f.state.badFee=true;await assert.rejects(f.flow.prepare(id),/fee changed/);assert.equal(f.state.signs,0);
});
test('a lost source acknowledgment is recoverable with the original ID without signing or broadcasting again',async()=>{
  const f=fixture(),id=await f.start();await f.flow.prepare(id);await f.flow.sign(id);f.state.ambiguous=true;await assert.rejects(f.flow.submit(id),/unresolved/);
  assert.equal(f.client.getSource(id).state,'submission_unknown');await assert.rejects(f.flow.submit(id));await assert.rejects(f.flow.quote(f.input('buy')),/unresolved/);
  assert.equal((await f.flow.recover(id)).status,'processing');assert.equal(f.state.sourcePosts,1);assert.equal(f.state.broadcasts,1);assert.equal(f.state.signs,1);
});
test('expired original source recovery is observation-only and cannot free a locally signed cancelled request for replacement',async()=>{
  const f=fixture(),id=await f.start();await f.flow.prepare(id);await f.flow.sign(id);f.advance(61);f.state.status='cancelled';
  const result=await f.flow.recover(id);assert.ok(['manual_review','unresolved'].includes(result.status));assert.equal(f.state.broadcasts,0);assert.equal(f.state.sourcePosts,1);assert.equal(f.state.signs,1);
  await assert.rejects(f.flow.quote(f.input('buy')),/unresolved/);assert.equal(f.client.getSource(id).state,'recovery_only');
});
test('tampering with durable signed bytes is rejected before source HTTP submission',async()=>{
  const f=fixture(),id=await f.start();await f.flow.prepare(id);await f.flow.sign(id);const key=[...f.storage.values.keys()].find(k=>k.includes(':source:'));
  const saved=JSON.parse(f.storage.getItem(key)),wire=fromBase64(saved.signed_transaction);wire[wire.length-1]^=1;saved.signed_transaction=toBase64(wire);f.storage.setItem(key,JSON.stringify(saved));
  await assert.rejects(f.flow.submit(id),/changed/);assert.equal(f.state.sourcePosts,0);
});
test('a different source signature in a final response never completes or replaces our original payment',async()=>{
  const f=fixture(),id=await f.start();await f.flow.prepare(id);await f.flow.sign(id);await f.flow.submit(id);
  const before=f.client.getHistory()[0];f.state.status='completed';f.state.sourceIdOverride=base58Encode(new Uint8Array(64).fill(12));
  await assert.rejects(f.client.recover(id),/original saved source transaction/);assert.deepEqual(f.client.getHistory()[0],before);assert.equal(f.client.getSource(id).signature,signature);await assert.rejects(f.flow.quote(f.input('buy')),/unresolved/);
});
test('signed source bytes survive reload and proof/access are separate from the public request journal',async()=>{
  const f=fixture(),id=await f.start();await f.flow.prepare(id);await f.flow.sign(id);const publicJournal=[...f.storage.values].find(([key])=>key.endsWith(':requests'))[1];
  assert.equal(publicJournal.includes(signature),false);assert.equal(publicJournal.includes(f.state.token),false);assert.ok([...f.credentials.values.values()].join('').includes(f.state.token));
  assert.ok(f.client.getSource(id).signed_transaction);assert.equal(f.client.getHistory()[0].destination_owner,B);
});
test('source template binds mint/reserve, full amount and signed intent memo; extra instructions are rejected',async()=>{
  const f=fixture(),id=await f.start(),source=await f.flow.prepare(id),r=f.client.getHistory()[0];const reviewed=await reviewPreparedSource(source,r.intent,settlementSigningMessage(r.intent,{allowExpired:true}),{crypto:f.crypto,now:f.now});
  const transaction=sdk.Transaction.from(reviewed.wire);assert.equal(transaction.instructions.length,2);assert.equal(transaction.instructions[0].programId.toBase58(),'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
  assert.equal(transaction.instructions[0].keys[1].pubkey.toBase58(),CONFIG.solanaMint);assert.equal(transaction.instructions[0].keys[2].pubkey.toBase58(),CONFIG.reserve);
  assert.equal(new TextDecoder().decode(transaction.instructions[1].data),'xeris:settlement:v2:'+source.intent_digest);
  for(const changed of [{...r.intent,amount_raw:'1000001'},{...r.intent,destination_owner:A}])await assert.rejects(reviewPreparedSource(source,changed,settlementSigningMessage(changed,{allowExpired:true}),{crypto:f.crypto,now:f.now}));
});

function mockDocument(){
  class Element{
    constructor(tag='div'){this.tagName=tag;this.children=[];this.attrs={};this.value='';this.textContent='';this.disabled=false;this.hidden=false;this.events={};this.classList={toggle(){}};}
    append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;this.textContent='';}setAttribute(k,v){this.attrs[k]=v;}addEventListener(k,v){this.events[k]=v;}
    querySelectorAll(selector){return this.children.flatMap(c=>[...(selector==='button'&&c.tagName==='button'?[c]:[]),...c.querySelectorAll(selector)]);}
  }
  const map=new Map();return{map,hidden:false,createElement:tag=>new Element(tag),getElementById(id){if(!map.has(id))map.set(id,new Element());return map.get(id);}};
}
const text=element=>element.textContent+element.children.map(text).join(' ');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('rendered purchase UI advances only on separate deliberate buttons and shows confirmed actual output',async t=>{
  const f=fixture(),document=mockDocument();t.mock.method(Date,'now',f.now);
  const ui=mountBridge({document,solana:f.solana,getXeris:()=>({provider:f.provider,address:A,balances:{usdcRaw:1000000n,nativeRaw:10000000n}}),sdk:()=>sdk,storage:memory(),flow:f.flow});t.after(()=>ui.dispose());
  await tick();assert.equal(f.state.ownership,0);assert.equal(f.state.signs,0);
  document.getElementById('deposit-recipient').value=B;document.getElementById('deposit-amount').value='1';ui.render();
  assert.equal(document.getElementById('review-deposit-addresses').disabled,false);await document.getElementById('review-deposit-addresses').onclick();
  assert.match(document.getElementById('deposit-submit').textContent,/Authorize request/);assert.ok(text(document.getElementById('deposit-summary')).includes(B));
  await document.getElementById('deposit-submit').onclick();assert.equal(f.state.ownership,1);assert.equal(f.state.signs,0);assert.match(document.getElementById('deposit-submit').textContent,/Prepare source/);
  await document.getElementById('deposit-submit').onclick();assert.equal(f.state.signs,0);assert.match(text(document.getElementById('deposit-summary')),/0.000005 SOL/);
  await document.getElementById('deposit-submit').onclick();assert.equal(f.state.signs,1);assert.equal(f.state.broadcasts,0);assert.match(document.getElementById('deposit-submit').textContent,/Submit signed/);
  await document.getElementById('deposit-submit').onclick();assert.equal(f.state.broadcasts,1);assert.equal(document.getElementById('deposit-submit').disabled,true);assert.equal(document.getElementById('bridge-new').disabled,true);
  f.state.status='completed';await document.getElementById('bridge-recover').onclick();
  const completed=text(document.getElementById('bridge-current'));assert.match(completed,/Actually delivered/);assert.match(completed,/0.998000001 native XRS/);assert.doesNotMatch(completed,/Actually delivered 1 native XRS/);
});
test('offline/paused UI has no enabled payment, burn or quote action and no unsolicited wallet call',async t=>{
  const f=fixture();f.state.enabled=false;const document=mockDocument();t.mock.method(Date,'now',f.now);
  const ui=mountBridge({document,solana:f.solana,getXeris:()=>({provider:f.provider,address:A}),sdk:()=>sdk,storage:memory(),flow:f.flow});t.after(()=>ui.dispose());await tick();
  document.getElementById('deposit-recipient').value=B;document.getElementById('deposit-amount').value='1';document.getElementById('redeem-recipient').value=B;document.getElementById('redeem-amount').value='1';ui.render();
  for(const id of ['deposit-submit','redeem-submit','review-deposit-addresses','review-redemption'])assert.equal(document.getElementById(id).disabled,true);
  assert.match(document.getElementById('bridge-availability').textContent,/paused/);assert.equal(f.state.ownership,0);assert.equal(f.state.signs,0);assert.equal(f.calls.filter(c=>c.path!=='/capabilities').length,0);
});
test('local SDK loader accepts only pinned same-origin asset and fails if it does not initialize',async()=>{
  const scripts=[],document={head:{append(script){scripts.push(script);}},createElement:()=>({})};
  const good=loadSolanaLibrary(document,{solanaWeb3:sdk});assert.equal(scripts[0].src,'/assets/vendor/solana-web3-1.98.4.iife.min.js');scripts[0].onload();await good;
  const bad=loadSolanaLibrary(document,{});scripts[1].onload();await assert.rejects(bad,/did not initialize/);
});
