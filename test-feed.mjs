import test from 'node:test';
import assert from 'node:assert/strict';
import {parseExactJSON} from './dist/model.mjs';
import {parseMarketSnapshot, formatQuantity, formatMarketCap, formatCompactMarketCap, createMarketFeed} from './dist/feed.mjs';
const NOW = 1795489537647;
const ADDRESS = '4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL';
const HASH = '0d3915c71138518d96fe9e9b77b944fc75ca43306dd347aa96c893d296ac6b05';
function fixture(at = NOW) {
  return {schema_version:'2',chain_id:'xeris-mainnet-v4',status:'ready',observed_at_ms:String(at),expires_at_ms:String(at+20000),
    as_of:{slot:'29284',block_hash:HASH},coverage:{complete:true},
    supply:{status:'available',basis:'unlocked_on_chain_v2',chain_id:'xeris-mainnet-v4',slot:'29284',block_hash:HASH,decimals:9,
      circulating_raw:'200289840767123232',total_emitted_raw:'292840767123232',components_raw:{native_wallets:'0',company_native:'200289131067123232',wrapped_wallets:'0',company_wrapped:'0',amm_inventory:'709700000000'}},
    market:{token_id:'native_xrs',symbol:'XRS',circulating_raw:'200289840767123232',decimals:'9',quote_token_id:'xeris_usdc',quote_symbol:'xUSDC',quote_decimals:'6',
      market_cap_quote_raw:'2850930165404',price_quote_raw_numerator:'10101886',price_xrs_raw_denominator:'709700000000',price_pool_id:'xrs-xusdc-mainnet-v1',valuation_basis:'circulating_supply',price_basis:'pool_spot'},
    mining:{total_mined_raw:'292840767123232',total_staked_raw:'3000000000000',current_block_reward_xrs:'10.0',last_block_timestamp_ms:String(at-960)},
    blocks:[{slot:'29284',hash:HASH,proposer:ADDRESS,timestamp_ms:String(at-960),transaction_count:'0'}],
    policy:{id:'xeris-mainnet-circulation-v2',sha256:'a'.repeat(64),description:'All unlocked on-chain XRS, including company holdings and active AMM inventory.',company_wallets:[{address:ADDRESS,role:'Treasury'}],included_pool_ids:['xrs-xusdc-mainnet-v1']},
    launchpad_tokens:[]};
}
function launch(overrides={}) {
  return {token_id:'lp_example',name:'Example token',symbol:'EXAMPLE',status:'ready',decimals:'9',quote_decimals:'6',quote_token_id:'xeris_usdc',quote_symbol:'xUSDC',valuation_basis:'circulating_supply',
    circulating_raw:'1000000000',market_cap_quote_raw:'14234',price_xrs_raw_numerator:'1000000000',price_token_raw_denominator:'1000000000',
    supply:{status:'available',basis:'unlocked_on_chain_v2',token_id:'lp_example',decimals:9,circulating_raw:'1000000000',issued_supply_raw:'5000000000',components_raw:{wallets:'800000000',company_wallets:'0',amm_inventory:'200000000'}},...overrides};
}

test('public feed preserves exact circulation and derives cap from xUSDC pricing',()=>{
  const row=parseMarketSnapshot(JSON.stringify(fixture()),NOW);
  assert.equal(row.circulatingRaw,200289840767123232n); assert.equal(row.marketCapRaw,2850930165404n);
  assert.equal(row.minedRaw,292840767123232n); assert.equal(row.slot,29284n);
  assert.equal(formatQuantity(row.marketCapRaw,6),'2,850,930.165404');
  assert.equal(formatQuantity(row.circulatingRaw,9),'200,289,840.767123232');
});

test('large raw amounts survive lossless JSON parsing and never become floating values',()=>{
  const data=fixture(); data.supply.total_emitted_raw=data.mining.total_mined_raw='9007199254740993';
  const raw=JSON.stringify(data).replace('"decimals":9','"decimals":9');
  assert.equal(parseMarketSnapshot(parseExactJSON(raw),NOW).minedRaw,9007199254740993n);
  assert.equal(formatQuantity(9007199254740993n,9),'9,007,199.254740993');
  data.mining.total_mined_raw=9007199254740992;
  assert.throws(()=>parseMarketSnapshot(data,NOW));
});

test('expired, missing, unbounded or future snapshots never produce a live valuation',()=>{
  assert.equal(parseMarketSnapshot(fixture(),NOW+20000).status,'stale');
  assert.equal(parseMarketSnapshot({schema_version:'2',chain_id:'xeris-mainnet-v4',status:'unavailable'},NOW).status,'unavailable');
  for(const mutate of [d=>delete d.expires_at_ms,d=>d.expires_at_ms=String(NOW+61000),d=>d.observed_at_ms=String(NOW+6000)]) {
    const d=fixture(); mutate(d);assert.throws(()=>parseMarketSnapshot(d,NOW));
  }
});

test('chain, denomination, valuation basis and circulation arithmetic are bound',()=>{
  const changes=[d=>d.chain_id='testnet',d=>d.schema_version='1',d=>d.market.quote_symbol='USD',d=>d.market.quote_decimals='9',
    d=>d.market.valuation_basis='total_supply',d=>d.market.market_cap_quote_raw='10101887',d=>d.supply.components_raw.native_wallets='1',
    d=>d.supply.slot='29283',d=>d.market.circulating_raw='709700000001',d=>d.coverage.complete=false,
    d=>d.market.price_pool_id='another-pool',d=>d.supply.basis='maximum_supply',d=>d.mining.total_mined_raw='1'];
  for(const change of changes) {const d=fixture();change(d);assert.throws(()=>parseMarketSnapshot(d,NOW));}
});

test('block stream rejects forged top identity, duplicate blocks, stale tips and invalid producers',()=>{
  const changes=[d=>d.blocks[0].hash='b'.repeat(64),d=>d.blocks.push({...d.blocks[0]}),d=>d.blocks[0].proposer='not-an-address',
    d=>d.blocks[0].timestamp_ms=String(NOW+31000),d=>{d.blocks[0].timestamp_ms=d.mining.last_block_timestamp_ms=String(NOW-120001);},
    d=>d.blocks[0].transaction_count='-1'];
  for(const change of changes) {const d=fixture();change(d);assert.throws(()=>parseMarketSnapshot(d,NOW));}
});

test('launch valuations require circulating provenance and exact two-pool pricing',()=>{
  const d=fixture();d.launchpad_tokens=[launch()];
  assert.equal(parseMarketSnapshot(d,NOW).launchpad[0].marketCapRaw,14234n);
  for(const change of [r=>r.market_cap_quote_raw='14235',r=>r.quote_symbol='USD',r=>r.supply.issued_supply_raw='1',r=>r.supply.components_raw.wallets='799999999']) {
    const invalid=fixture(),row=launch();change(row);invalid.launchpad_tokens=[row];assert.throws(()=>parseMarketSnapshot(invalid,NOW));
  }
  d.launchpad_tokens=[{token_id:'lp_pending',name:'Pending',symbol:'PENDING',status:'pricing_unavailable',market_cap_quote_raw:null}];
  assert.equal(parseMarketSnapshot(d,NOW).launchpad[0].marketCapRaw,null);
  d.launchpad_tokens[0].market_cap_quote_raw='0';assert.throws(()=>parseMarketSnapshot(d,NOW));
});

function harness({read}={}) {
  let now=NOW,id=0,reads=0;const timers=new Map(),states=[],sources=[];
  const feed=createMarketFeed({onState:s=>states.push(s),now:()=>now,
    read:async()=>{reads++;return read?read():fixture(now);},
    schedule:(fn,ms)=>{const key=++id;timers.set(key,{fn,at:now+ms,ms});return key;},cancel:key=>timers.delete(key),
    makeSource:()=>{const source={closed:false,addEventListener(name,fn){this[name]=fn;},close(){this.closed=true;}};sources.push(source);return source;}});
  return {feed,timers,states,sources,get reads(){return reads;},async flush(){await Promise.resolve();await Promise.resolve();await Promise.resolve();},
    async advance(ms){now+=ms;const due=[...timers].filter(([,t])=>t.at<=now);for(const [key,t]of due){if(timers.delete(key))t.fn();}await this.flush();},get now(){return now;}};
}

test('streaming feed removes values at the deadline and closes while hidden',async()=>{
  const h=harness();h.feed.start();await h.flush();
  h.sources[0].snapshot({data:JSON.stringify(fixture())});assert.equal(h.states.at(-1).transport,'stream');
  await h.advance(20000);assert.equal(h.states.at(-1).status,'stale');
  h.feed.stop();assert.equal(h.sources[0].closed,true);assert.equal(h.timers.size,0);assert.equal(h.states.at(-1).status,'paused');
  h.sources[0].snapshot({data:JSON.stringify(fixture(h.now))});assert.equal(h.states.at(-1).status,'paused');
});

test('SSE errors use bounded GET fallback and a recovered snapshot stops polling',async()=>{
  const h=harness();h.feed.start();await h.flush();const source=h.sources[0];
  source.snapshot({data:JSON.stringify(fixture())});source.onerror();source.onerror();
  assert.equal(h.states.at(-1).status,'unavailable');assert.equal([...h.timers.values()].filter(t=>t.ms===8000).length,1);
  await h.advance(7999);assert.equal(h.reads,1);await h.advance(1);assert.equal(h.reads,2);assert.equal(h.states.at(-1).transport,'polling');
  source.snapshot({data:JSON.stringify(fixture(h.now))});assert.equal(h.states.at(-1).transport,'stream');
  assert.equal([...h.timers.values()].filter(t=>t.ms===8000).length,0);
  h.feed.stop();
});

test('late initial GET cannot overwrite or invalidate a newer stream snapshot',async()=>{
  let reject;const h=harness({read:()=>new Promise((_,bad)=>{reject=bad;})});h.feed.start();
  h.sources[0].snapshot({data:JSON.stringify(fixture())});reject(new Error('offline'));await h.flush();
  assert.equal(h.states.at(-1).status,'ready');assert.equal(h.states.at(-1).transport,'stream');h.feed.stop();
});

test('invalid SSE snapshots remove values without terminating reconnect or spamming reads',async()=>{
  const h=harness();h.feed.start();await h.flush();h.sources[0].snapshot({data:'{"status":"ready"}'});
  assert.equal(h.states.at(-1).status,'unavailable');assert.equal(h.sources[0].closed,false);assert.equal(h.reads,1);
  h.feed.stop();
});

test('an in-flight read from a hidden tab cannot populate a restarted view',async()=>{
  let resolve;let first=true;const h=harness({read:()=>first?(first=false,new Promise(ok=>{resolve=ok;})):fixture(h.now)});
  h.feed.start();h.feed.stop();h.feed.start();resolve(fixture());await h.flush();
  assert.notEqual(h.states.at(-1).status,'ready');await h.advance(8000);assert.equal(h.reads,2);assert.equal(h.states.at(-1).status,'ready');h.feed.stop();
});

test('market caps display cents without disguising positive sub-cent values as zero',()=>{
  assert.equal(formatMarketCap(10101886n),'10.10');
  assert.equal(formatMarketCap(0n),'0.00');
  assert.equal(formatMarketCap(1n),'<0.01');
  assert.equal(formatMarketCap(9999n),'<0.01');
  assert.equal(formatMarketCap(10000n),'0.01');
  assert.equal(formatMarketCap(1234567890123n),'1,234,567.89');
  assert.equal(formatMarketCap(1995000n),'2.00');
  assert.throws(()=>formatMarketCap(-1n));
});

for (const late of ['expired','unavailable']) {
  test(`a late ${late} GET cannot overwrite a fresh stream snapshot`,async()=>{
    let resolve;const h=harness({read:()=>new Promise(ok=>{resolve=ok;})});h.feed.start();
    h.sources[0].snapshot({data:JSON.stringify(fixture())});
    resolve(late==='expired'?fixture(NOW-30000):{schema_version:'2',chain_id:'xeris-mainnet-v4',status:'unavailable'});
    await h.flush();assert.equal(h.states.at(-1).status,'ready');assert.equal(h.states.at(-1).transport,'stream');
    assert.equal([...h.timers.values()].filter(t=>t.ms===8000).length,0);h.feed.stop();
  });
}

test('a pending GET cannot revive values after a newer unavailable stream event',async()=>{
  let resolve;const h=harness({read:()=>new Promise(ok=>{resolve=ok;})});h.feed.start();
  h.sources[0].snapshot({data:JSON.stringify({schema_version:'2',chain_id:'xeris-mainnet-v4',status:'unavailable'})});
  resolve(fixture());await h.flush();assert.equal(h.states.at(-1).status,'unavailable');
  assert.equal([...h.timers.values()].filter(t=>t.ms===8000).length,1);h.feed.stop();
});

test('schema 2 counts unlocked company holdings and rejects the old ownership exclusion policy',()=>{
  const data=fixture();
  data.supply.components_raw.company_native='200289130067123232';
  data.supply.components_raw.company_wrapped='1000000000';
  const result=parseMarketSnapshot(data,NOW);
  assert.equal(result.circulatingRaw,200289840767123232n);
  assert.equal(result.policy.companyWallets[0].address,ADDRESS);
  for (const change of [d=>d.schema_version='1',d=>d.supply.basis='liquid_outside_company_v1',
    d=>d.policy.id='xeris-mainnet-circulation-v1',d=>delete d.supply.components_raw.company_native,
    d=>delete d.supply.components_raw.company_wrapped,
    d=>{d.policy.excluded_wallets=d.policy.company_wallets;delete d.policy.company_wallets;},
    d=>{d.supply.circulating_raw=d.market.circulating_raw='709700000000';d.market.market_cap_quote_raw='10101886';}]) {
    const invalid=fixture();change(invalid);assert.throws(()=>parseMarketSnapshot(invalid,NOW));
  }
});

test('launch circulation includes unlocked company balances instead of dropping them by owner',()=>{
  const data=fixture(),row=launch();row.supply.components_raw.wallets='300000000';row.supply.components_raw.company_wallets='500000000';
  data.launchpad_tokens=[row];assert.equal(parseMarketSnapshot(data,NOW).launchpad[0].circulatingRaw,1000000000n);
  row.circulating_raw=row.supply.circulating_raw='500000000';row.market_cap_quote_raw='7117';
  assert.throws(()=>parseMarketSnapshot(data,NOW));
});

test('compact header capitalization preserves full network precision and explicit scale',()=>{
  assert.equal(formatCompactMarketCap(2850930165404n),'2.85M');
  assert.equal(formatMarketCap(2850930165404n),'2,850,930.17');
  assert.equal(formatCompactMarketCap(1000000000000000n),'1.00B');
  assert.equal(formatCompactMarketCap(1000000000000000000n),'1.00T');
  assert.equal(formatCompactMarketCap(10101886n),'10.10');
  assert.equal(formatCompactMarketCap(1n),'<0.01');
  assert.throws(()=>formatCompactMarketCap(-1n));
});
