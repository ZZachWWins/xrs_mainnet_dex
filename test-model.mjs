import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {formatQuoteRatio} from './dist/currency.mjs';
import {
  CONFIG, integer, parseAmount, formatAmount, parseExactJSON, request, rpc,
  checkChain, parsePool, quoteFromPool, quoteValuation, getQuote, parseBalances,
  MAX_RESPONSE_BYTES,
} from './dist/model.mjs';

const U64 = (1n << 64n) - 1n;
const ADDRESS = 'BpPwVSjQB6PDmki3bx15REAifJpQRuxWmCwBzpZaQgZT';
const poolResponse = () => ({success: true, contract: {
  contract_id: CONFIG.poolId, contract_type: 'Swap', is_active: true,
  owner: 'HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX', created_slot: '27201',
  state: {Swap: {token_a: CONFIG.wrapped, token_b: CONFIG.usdc, reserve_a: '709700000000',
    reserve_b: '10099031', fee_bps: '30', total_shares: '2677178048'}},
}});
const balanceResponse = () => ({address: ADDRESS, native_xrs: '9007199254740993', token_accounts: [
  {token_id: CONFIG.wrapped, balance: '33383761746', balance_display: 0, decimals: '9'},
  {token_id: CONFIG.usdc, balance: '500000', balance_display: 999999999, decimals: '6'},
]});
const response = (body, status = 200) => ({ok: status >= 200 && status < 300, status,
  headers: new Headers({'Content-Type': 'application/json'}),
  text: async () => typeof body === 'string' ? body : JSON.stringify(body)});
async function withFetch(fetcher, callback) {
  const previous = globalThis.fetch;
  globalThis.fetch = fetcher;
  try { return await callback(); } finally { globalThis.fetch = previous; }
}

test('decimal input uses exact base units at both token precisions', () => {
  assert.equal(parseAmount('0.50', 6), 500000n);
  assert.equal(parseAmount(' 0,50 ', 6), 500000n);
  assert.equal(parseAmount('0.000001', 6), 1n);
  assert.equal(parseAmount('33.383761746', 9), 33383761746n);
  assert.equal(parseAmount('18446744073.709551615', 9), U64);
  assert.equal(parseAmount('9007199.254740993', 9), 9007199254740993n);
});

test('decimal input rejects zero, negatives, excess precision and u64 overflow', () => {
  for (const value of ['', '0', '0.000000', '-0.5', '+1', '1e3', 'Infinity', 'NaN', '.', '.5', '1.', '1,2,3', '0.0000001', '18446744073710']) {
    assert.throws(() => parseAmount(value, 6), undefined, value);
  }
  assert.throws(() => parseAmount('18446744073.709551616', 9));
});

test('integer gate rejects unsafe numeric coercion and invalid raw amounts', () => {
  assert.equal(integer(U64.toString()), U64);
  assert.equal(integer(9007199254740991), 9007199254740991n);
  for (const value of [9007199254740992, -1, 0.5, NaN, Infinity, true, null, undefined, {}, '-1', '1e3', '1.0', '']) {
    assert.throws(() => integer(value), undefined, String(value));
  }
  assert.throws(() => integer(U64 + 1n));
  assert.throws(() => integer(0, true));
});

test('formatting never rounds a large raw balance through Number', () => {
  assert.equal(formatAmount(9007199254740993n, 9), '9007199.254740993');
  assert.equal(formatAmount(U64, 9), '18446744073.709551615');
  assert.equal(formatAmount(1n, 9), '0.000000001');
  assert.equal(formatAmount(500000n, 6), '0.5');
  assert.equal(formatAmount(497145n, 6, 2), '0.49');
});

test('JSON parsing preserves exact u64 integers and numeric text inside strings', () => {
  const data = parseExactJSON('{"balance":18446744073709551615,"native":9007199254740993,"zero":0,"text":"123.45 + 9007199254740993","escaped":"say \\"42\\""}'.replace('\\\\"', '\\"'));
  assert.equal(integer(data.balance), U64);
  assert.equal(integer(data.native), 9007199254740993n);
  assert.equal(integer(data.zero), 0n);
  assert.equal(data.text, '123.45 + 9007199254740993');
  assert.equal(data.escaped, 'say "42"');
});

test('malformed JSON numbers are not repaired into valid raw amounts', () => {
  for (const text of ['{"balance":01}', '{"balance":-01}', '{"balance":+1}', '{"balance":1.}', '{"balance":1e}', '{"balance":NaN}']) {
    assert.throws(() => parseExactJSON(text), undefined, text);
  }
});

test('fractional or exponent JSON raw amounts cannot become rounded safe integers', () => {
  for (const lexeme of ['1.0000000000000001', '9007199254740990.1', '1e0', '1.0']) {
    const data = parseExactJSON(`{"balance":${lexeme}}`);
    assert.throws(() => integer(data.balance), undefined, lexeme);
  }
});

test('pool parsing pins the contract, both token IDs and the chosen fee', () => {
  const pool = parsePool(poolResponse());
  assert.equal(pool.reserveA, 709700000000n);
  assert.equal(pool.reserveB, 10099031n);
  assert.equal(pool.totalShares, 2677178048n);
  for (const mutate of [
    d => { d.success = false; }, d => { d.success = 'true'; },
    d => { d.contract.contract_id = 'xrs-xusdc-testnet'; },
    d => { d.contract.contract_type = 'Launchpad'; },
    d => { d.contract.is_active = false; },
    d => { d.contract.owner = CONFIG.treasury; },
    d => { delete d.contract.owner; },
    d => { d.contract.created_slot = '27202'; },
    d => { delete d.contract.created_slot; },
    d => { d.contract.state.Swap.token_a = CONFIG.usdc; },
    d => { d.contract.state.Swap.token_b = CONFIG.wrapped; },
    d => { d.contract.state.Swap.fee_bps = '77'; },
    d => { delete d.contract.state.Swap.fee_bps; },
    d => { d.contract.state.Swap.reserve_a = '0'; },
    d => { d.contract.state.Swap.reserve_b = '0'; },
    d => { d.contract.state.Swap.reserve_a = Number(U64); },
    d => { d.contract.state.Swap.total_shares = '0'; },
  ]) {
    const d = poolResponse(); mutate(d); assert.throws(() => parsePool(d));
  }
});

test('AMM quote reproduces the real half-dollar pilot integer outputs', () => {
  const pool = parsePool(poolResponse());
  const buy = quoteFromPool(pool, true, 500000n);
  assert.equal(buy.feeRaw, 1500n);
  assert.equal(buy.outputRaw, 33383761746n);
  assert.equal(buy.minimumOutputRaw, 33216842938n);
  const after = {...pool, reserveA: pool.reserveA - buy.outputRaw, reserveB: pool.reserveB + buy.inputRaw};
  const sell = quoteFromPool(after, false, buy.outputRaw);
  assert.equal(sell.outputRaw, 497145n);
  assert.equal(sell.minimumOutputRaw, 494660n);
  assert.equal(after.reserveB - sell.outputRaw, 10101886n);
});

test('quote enforces execution reserve range and rejects dust zero output', () => {
  assert.throws(() => quoteFromPool({reserveA: U64, reserveB: 10n}, false, 1n));
  assert.throws(() => quoteFromPool({reserveA: 10n, reserveB: U64}, true, 1n));
  assert.throws(() => quoteFromPool(parsePool(poolResponse()), false, 1n));
});

test('token balances use raw amounts and ignore display-number approximations', () => {
  const result = parseBalances(balanceResponse(), ADDRESS);
  assert.deepEqual(result, {nativeRaw: 9007199254740993n, wrappedRaw: 33383761746n, usdcRaw: 500000n});
  assert.deepEqual(parseBalances({address: ADDRESS, native_xrs: '0', token_accounts: []}, ADDRESS),
    {nativeRaw: 0n, wrappedRaw: 0n, usdcRaw: 0n});
});

test('balance metadata changes, duplicates, missing fields and wrong owner fail closed', () => {
  for (const mutate of [
    d => { d.address = CONFIG.treasury; }, d => { delete d.native_xrs; },
    d => { d.native_xrs = 9007199254740992; }, d => { d.token_accounts = null; },
    d => { d.token_accounts[0].decimals = '6'; },
    d => { d.token_accounts[1].decimals = '9'; },
    d => { delete d.token_accounts[1].decimals; },
    d => { d.token_accounts[1].balance = '0.5'; },
    d => { d.token_accounts.push({...d.token_accounts[0]}); },
  ]) {
    const d = balanceResponse(); mutate(d); assert.throws(() => parseBalances(d, ADDRESS));
  }
});

test('network reader sets no-store, refuses redirects and sends JSON content type', async () => {
  const calls = [];
  await request('/', {method: 'POST', body: {method: 'getHealth'}, fetcher: async (...args) => {
    calls.push(args); return response({result: {ok: true}});
  }});
  assert.equal(calls[0][0], CONFIG.rpc + '/');
  assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(calls[0][1].redirect, 'error');
  assert.equal(calls[0][1].headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0][1].body), {method: 'getHealth'});
});

test('429, HTTP errors and JSON error envelopes stop the read without retrying', async () => {
  for (const value of [response({}, 429), response({}, 502), response({error: 'bad'}), response({success: false})]) {
    let count = 0;
    await assert.rejects(request('/health', {fetcher: async () => { count++; return value; }}));
    assert.equal(count, 1);
  }
});

test('read timeout produces a bounded actionable error', async () => {
  await assert.rejects(request('/health', {timeout: 5, fetcher: async (_, options) => new Promise((_, reject) => {
    options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
  })}), /timed out/);
});

test('request deadline covers ignored aborts and stalled body cancellation, then permits recovery', {timeout:1000}, async () => {
  const never = new Promise(() => {});
  let lateFetch, lateBodyReads = 0;
  await assert.rejects(request('/health', {timeout:5, fetcher:() => new Promise(resolve => {lateFetch = resolve;})}), error => error.code === 'TIMEOUT');
  lateFetch({ok:true,text:async () => {lateBodyReads++; return '{}';}});
  await new Promise(resolve => setTimeout(resolve,0));
  assert.equal(lateBodyReads,0,'late transport results are discarded before reading/parsing');
  let cancellations = 0;
  const stalled = {ok:true,body:{getReader:() => ({read:() => never,cancel:() => {cancellations++;return never;},releaseLock(){}})}};
  await assert.rejects(request('/health', {timeout:5,fetcher:async () => stalled}), error => error.code === 'TIMEOUT');
  assert.equal(cancellations,1,'best-effort body cancellation must not delay the deadline');
  assert.deepEqual(await request('/health',{fetcher:async () => response({ok:true})}),{ok:true});
});

test('network response bytes are bounded before parsing despite absent or false length headers', {timeout:1000}, async () => {
  let reads=0,cancellations=0;
  await assert.rejects(request('/health',{fetcher:async () => ({ok:true,headers:new Headers({'Content-Length':String(MAX_RESPONSE_BYTES+1)}),
    body:{cancel:() => {cancellations++;return new Promise(() => {});}},text:async () => {reads++;return '{}';}})}),/supported size/);
  assert.equal(reads,0);assert.equal(cancellations,1);
  for(const headers of [{},{'Content-Length':'1'}]) {
    await assert.rejects(request('/health',{fetcher:async () => new Response('x'.repeat(MAX_RESPONSE_BYTES+1),{headers})}),/supported size/);
  }
  await assert.rejects(request('/health',{fetcher:async () => response('"'+'é'.repeat(MAX_RESPONSE_BYTES/2)+'"')}),/supported size/);
  const exact='{"balance":9007199254740993}';
  assert.deepEqual(await request('/health',{fetcher:async () => new Response(exact)}),{balance:'9007199254740993'});
});

test('JSON-RPC rejects result.error rather than treating HTTP 200 as success', async () => {
  await withFetch(async () => response({result: {error: 'missing'}}), async () => {
    await assert.rejects(rpc('getBalance', [ADDRESS]), /could not complete/);
  });
});

test('chain identity check requires the pinned historical slot and exact hash', async () => {
  await withFetch(async () => response({success: true, data: {slot: CONFIG.checkpointSlot, hash: CONFIG.checkpointHash}}), async () => {
    assert.equal(await checkChain(), true);
  });
  for (const data of [
    {success: true, data: {slot: CONFIG.checkpointSlot + 1, hash: CONFIG.checkpointHash}},
    {success: true, data: {slot: CONFIG.checkpointSlot, hash: '0'.repeat(64)}},
    {success: true, data: {slot: CONFIG.checkpointSlot}},
  ]) {
    await withFetch(async () => response(data), async () => { await assert.rejects(checkChain(), /identity/); });
  }
});

test('remote quote must exactly agree on direction, input, output and fee', async () => {
  const valid = {success: true, quote: {input_token: CONFIG.usdc, output_token: CONFIG.wrapped,
    input_amount: '500000', output_amount: '33383761746', fee: '1500', fee_bps: '30'}};
  for (const mutate of [null, d => { d.quote.output_amount = '33383761745'; },
    d => { d.quote.input_token = CONFIG.wrapped; }, d => { d.quote.output_token = CONFIG.usdc; },
    d => { d.quote.fee = '0'; }, d => { d.quote.fee_bps = '77'; },
    d => { d.quote.input_amount = '500001'; }, d => { delete d.quote.output_amount; }]) {
    const data = structuredClone(valid); if (mutate) mutate(data);
    await withFetch(async url => response(url.includes('/quote?') ? data : poolResponse()), async () => {
      if (mutate) await assert.rejects(getQuote(true, 500000n));
      else assert.equal((await getQuote(true, 500000n)).outputRaw, 33383761746n);
    });
  }
});


test('sell valuation separates spot market value, executable proceeds and protected minimum', () => {
  const q = quoteFromPool(parsePool(poolResponse()), false, 1000000000000n);
  const value = quoteValuation(q, {buying:false, amountText:'1000', now:q.quotedAt});
  assert.equal(value.marketValue.numerator / value.marketValue.denominator, 14230000n);
  assert.deepEqual(value.estimatedProceeds, {numerator:5899533n,denominator:1n});
  assert.deepEqual(value.minimumReceived, {numerator:5870036n,denominator:1n});
  assert.equal(value.feeRaw, 3000000000n);
  assert.equal(value.priceImpactPercent, '58.42');
  assert.equal(value.slippageBps, 50);
  // Slippage protects the lower bound. It is not deducted a second time from
  // the actual 5.899533 xUSDC AMM quote, which already includes the LP fee.
  assert.equal(value.outputRaw, 5899533n);
  assert.equal(value.minimumOutputRaw, (value.outputRaw * 9950n + 9999n) / 10000n);
  assert.ok(value.outputRaw > value.minimumOutputRaw);
});

test('valuation is tied to the quoted reserve snapshot, not a later display pool', () => {
  const pool = parsePool(poolResponse());
  const q = quoteFromPool(pool, false, 1000000000n);
  pool.reserveB *= 2n;
  const value = quoteValuation(q, {buying:false, amountText:'1', now:q.quotedAt});
  assert.equal(value.marketValue.numerator, 1000000000n * 10099031n);
  assert.equal(value.marketValue.denominator, 709700000000n);
  assert.ok(Object.isFrozen(q) && Object.isFrozen(q.poolSnapshot));
});

test('mismatched input, direction, pool identity, fee, output and minimum suppress all valuations', () => {
  const q = quoteFromPool(parsePool(poolResponse()), false, 1000000000000n);
  const options = {buying:false,amountText:'1000',now:q.quotedAt};
  for (const changed of [null, {...q,buying:true}, {...q,inputRaw:q.inputRaw+1n},
    {...q,feeRaw:q.feeRaw+1n}, {...q,outputRaw:q.outputRaw+1n}, {...q,minimumOutputRaw:q.minimumOutputRaw-1n},
    {...q,slippageBps:100}, {...q,priceImpactPercent:'0.00'}, {...q,poolSnapshot:null},
    ...[{poolId:'other'},{chainId:'testnet'},{feeBps:77},{reserveA:0n},{reserveB:10099032n}].map(change=>({...q,poolSnapshot:{...q.poolSnapshot,...change}}))]) {
    assert.equal(quoteValuation(changed,options),null);
  }
  assert.equal(quoteValuation(q,{...options,buying:true}),null);
  for(const amountText of ['1001','invalid','0','1000.0000000001']) assert.equal(quoteValuation(q,{...options,amountText}),null);
});

test('quote and source observation must both be current and never future dated', () => {
  const q = quoteFromPool(parsePool(poolResponse()), false, 1000000000n);
  const options = {buying:false,amountText:'1'};
  assert.ok(quoteValuation(q,{...options,now:q.quotedAt+29999}));
  assert.equal(quoteValuation(q,{...options,now:q.quotedAt+30000}),null);
  assert.equal(quoteValuation(q,{...options,now:q.quotedAt-1}),null);
  assert.equal(quoteValuation({...q,poolSnapshot:{...q.poolSnapshot,observedAt:q.quotedAt+1}}, {...options,now:q.quotedAt+2}),null);
  assert.equal(quoteValuation({...q,poolSnapshot:{...q.poolSnapshot,observedAt:q.quotedAt-30000}}, {...options,now:q.quotedAt}),null);
});

test('buy valuation retains exact input value and output units without applying slippage to proceeds', () => {
  const q=quoteFromPool(parsePool(poolResponse()),true,500000n);
  const value=quoteValuation(q,{buying:true,amountText:'0.50',now:q.quotedAt});
  assert.deepEqual(value.marketValue,{numerator:500000n,denominator:1n});
  assert.deepEqual(value.estimatedProceeds,{numerator:33383761746n*10099031n,denominator:709700000000n});
  assert.equal(value.minimumOutputRaw,33216842938n);
});

test('headline market-value rational preserves amounts above JavaScript safe integer range', () => {
  const pool={reserveA:9007199254740993n,reserveB:9007199254740991n,at:Date.now()};
  const q=quoteFromPool(pool,false,9007199254740993n);
  const value=quoteValuation(q,{buying:false,amountText:'9007199.254740993',now:q.quotedAt});
  assert.equal(value.marketValue.numerator,9007199254740993n*9007199254740991n);
  assert.equal(value.marketValue.denominator,9007199254740993n);
  assert.equal(value.marketValue.numerator/value.marketValue.denominator,9007199254740991n);
});


function valuationDOM(q, amountText, snapshot = {currency:'USD',status:'ready'}) {
  const html=readFileSync(new URL('./dist/index.html',import.meta.url),'utf8');
  const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([,id])=>[id,{textContent:'',value:'',dataset:{},
    classList:{values:new Set(),toggle(name,on){on?this.values.add(name):this.values.delete(name);}}}]));
  nodes.get('pay-amount').value=amountText;
  let time=q.quotedAt, currency=snapshot;
  const context={quote:q,buying:q.buying,swapReviewQuote:null,balances:null,formatAmount,
    $:id=>nodes.get(id)||null,quoteValuation:(value,options)=>quoteValuation(value,{...options,now:time}),
    ratioMoney:(n,d,options={})=>formatQuoteRatio(n,d,currency,{...options,now:time})};
  const app=readFileSync(new URL('./dist/app.mjs',import.meta.url),'utf8');
  const source=app.slice(app.indexOf('function activeTradeValuation()'),app.indexOf('function renderValuations()'));
  runInNewContext(source,context);
  return {nodes,context,render(){context.renderFiatEstimates();},setTime(value){time=value;},setCurrency(value){currency=value;}};
}

test('actual sell renderer makes spot value the headline and labels smaller pool proceeds/minimum', () => {
  const q=quoteFromPool(parsePool(poolResponse()),false,1000000000000n);
  const dom=valuationDOM(q,'1000');dom.render();
  assert.equal(dom.nodes.get('sell-market-value').textContent,'≈ $14.23');
  assert.equal(dom.nodes.get('sell-proceeds-fiat').textContent,'≈ $5.90');
  assert.equal(dom.nodes.get('sell-proceeds-token').textContent,'5.899533 xUSDC');
  assert.equal(dom.nodes.get('sell-minimum-token').textContent,'5.870036 xUSDC');
  assert.equal(dom.nodes.get('sell-value-summary').classList.values.has('hidden'),false);
  assert.equal(dom.nodes.get('receive-box').classList.values.has('hidden'),true);
  const html=readFileSync(new URL('./dist/index.html',import.meta.url),'utf8');
  assert.match(html,/>Market value</);assert.match(html,/>Estimated proceeds</);assert.match(html,/>Minimum received</);
  assert.match(html,/slippage limit, not an additional fee/);
});

test('actual renderer clears expired/failed/mismatched quote amounts instead of retaining old proceeds', () => {
  const q=quoteFromPool(parsePool(poolResponse()),false,1000000000000n);
  for(const invalidate of [dom=>dom.setTime(q.quotedAt+30000),dom=>{dom.context.quote=null;},dom=>{dom.nodes.get('pay-amount').value='1001';}]) {
    const dom=valuationDOM(q,'1000');dom.render();invalidate(dom);dom.render();
    assert.equal(dom.nodes.get('sell-market-value').textContent,'Unavailable');
    assert.equal(dom.nodes.get('sell-proceeds-fiat').textContent,'Unavailable');
    assert.equal(dom.nodes.get('sell-minimum-fiat').textContent,'Unavailable');
    assert.equal(dom.nodes.get('sell-proceeds-token').textContent,'— xUSDC');
    assert.equal(dom.nodes.get('minimum-output').textContent,'—');
  }
});

test('FX expiry clears both headline and original review valuation while exact token quote remains', () => {
  const q=quoteFromPool(parsePool(poolResponse()),false,1000000000000n),now=q.quotedAt;
  const dom=valuationDOM(q,'1000',{currency:'EUR',status:'ready',asOf:now-59000,expiresAt:now+1000,rate:{numerator:9n,denominator:10n}});
  dom.nodes.set('review-market-value',{textContent:''});
  dom.nodes.get('info-dialog').dataset.swapReview='true';dom.context.swapReviewQuote=q;
  dom.render();assert.equal(dom.nodes.get('sell-market-value').textContent,'≈ €12.81');
  assert.equal(dom.nodes.get('review-market-value').textContent,'≈ €12.81');
  dom.setTime(now+1000);dom.render();
  assert.equal(dom.nodes.get('sell-market-value').textContent,'Unavailable');
  assert.equal(dom.nodes.get('review-market-value').textContent,'Unavailable');
  assert.equal(dom.nodes.get('sell-proceeds-token').textContent,'5.899533 xUSDC');
});

test('buy renderer keeps the original token-output view and hides the sell-only summary', () => {
  const q=quoteFromPool(parsePool(poolResponse()),true,500000n),dom=valuationDOM(q,'0.5');dom.render();
  assert.equal(dom.nodes.get('receive-amount').textContent,'33.383761746');
  assert.equal(dom.nodes.get('minimum-output').textContent,'33.216842938 wXRS');
  assert.equal(dom.nodes.get('receive-box').classList.values.has('hidden'),false);
  assert.equal(dom.nodes.get('sell-value-summary').classList.values.has('hidden'),true);
});


test('sub-dollar quote money retains four fractional digits so small differences remain visible', () => {
  const q=quoteFromPool(parsePool(poolResponse()),false,10000000000n),dom=valuationDOM(q,'10');dom.render();
  assert.equal(dom.nodes.get('sell-market-value').textContent,'≈ $0.1423');
  assert.notEqual(dom.nodes.get('sell-market-value').textContent,dom.nodes.get('sell-proceeds-fiat').textContent);
  assert.notEqual(dom.nodes.get('sell-proceeds-fiat').textContent,dom.nodes.get('sell-minimum-fiat').textContent);
});

function loadingDOM() {
  const html=readFileSync(new URL('./dist/index.html',import.meta.url),'utf8');
  return new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([,id])=>[id,{textContent:'',hidden:false,disabled:false,
    attrs:{},setAttribute(name,value){this.attrs[name]=value;},classList:{values:new Set(),toggle(name,on){on?this.values.add(name):this.values.delete(name);}}}]));
}
function sourceSpan(start,end) {
  const app=readFileSync(new URL('./dist/app.mjs',import.meta.url),'utf8');
  return app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
}

test('pool refresh keeps observed reserves visible and ends with named error/retry when the read fails', async () => {
  const nodes=loadingDOM();let reject;
  nodes.get('reserve-a').textContent='709.7';nodes.get('reserve-b').textContent='10.099031';
  const pool={reserveA:709700000000n,reserveB:10099031n};
  const context={$:id=>nodes.get(id),poolBusy:false,currentPool:pool,lastObserved:1,startupPending:true,
    checkChain:async()=>true,getPool:()=>new Promise((_,r)=>{reject=r;}),renderValuations(){},schedulePoolValuationExpiry(){},formatAmount,observations:[]};
  runInNewContext(sourceSpan('function setLoadState(', 'let provider')+sourceSpan('async function refreshPool()', "$('refresh-pool').onclick"),context);
  const pending=context.refreshPool();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(nodes.get('pool-refresh-status').attrs['aria-busy'],'true');
  assert.equal(nodes.get('reserve-a').textContent,'709.7');
  assert.equal(nodes.get('reserve-b').textContent,'10.099031');
  assert.equal(nodes.get('refresh-pool').disabled,true);
  reject(new Error('Synthetic read failure'));await pending;
  assert.equal(nodes.get('pool-refresh-status').attrs['aria-busy'],'false');
  assert.match(nodes.get('pool-refresh-status').textContent,/Last observed pool.*Refresh to retry/);
  assert.equal(nodes.get('refresh-pool').disabled,false);
  assert.equal(nodes.get('dex-startup').hidden,true);
  assert.equal(nodes.get('dex-shell').hidden,false);
});

test('first successful pool read ends startup and both pool loading indicators without an arbitrary delay', async () => {
  const nodes=loadingDOM();
  const context={$:id=>nodes.get(id),poolBusy:false,currentPool:null,lastObserved:0,startupPending:true,
    checkChain:async()=>true,getPool:async()=>({reserveA:709700000000n,reserveB:10099031n}),renderValuations(){},schedulePoolValuationExpiry(){},formatAmount,observations:[]};
  runInNewContext(sourceSpan('function setLoadState(', 'let provider')+sourceSpan('async function refreshPool()', "$('refresh-pool').onclick"),context);
  await context.refreshPool();
  assert.equal(nodes.get('reserve-a').textContent,'709.7');
  for(const id of ['pool-refresh-status','pool-list-status']) {
    assert.equal(nodes.get(id).attrs['aria-busy'],'false');assert.match(nodes.get(id).textContent,/Pool updated/);
  }
  assert.equal(nodes.get('dex-startup').hidden,true);assert.equal(nodes.get('dex-shell').hidden,false);
});

test('pool-derived fiat expires at its deadline even while refresh/feed work never completes',()=>{
  let now=100000,sequence=0;const timers=new Map(),renders=[];
  const context={currentPool:{reserveA:2n,reserveB:1n},lastObserved:now,poolValuationExpiryTimer:undefined,
    Date:{now:()=>now},setTimeout(fn,delay){const id=++sequence;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
    ratioMoney:(n,d)=>`${n}/${d}`,renderValuations(){renders.push(context.nativeFiat(100n));}};
  runInNewContext(sourceSpan('function nativeFiat(', 'function activeTradeValuation()')+
    sourceSpan('function schedulePoolValuationExpiry()', 'async function refreshPool()'),context);
  context.schedulePoolValuationExpiry();
  assert.equal(timers.get(1).delay,45000);assert.equal(context.nativeFiat(100n),'100/2');
  now+=44999;assert.equal(context.nativeFiat(100n),'100/2');
  now++;timers.get(1).fn();assert.deepEqual(renders,[null]);
  assert.notEqual(context.currentPool,null,'observed reserves remain available as historical data');
  context.lastObserved=now;context.schedulePoolValuationExpiry();
  assert.equal(timers.has(1),false);assert.equal(context.nativeFiat(100n),'100/2');
});

test('balance refresh retains existing data while pending and prevents an old wallet result from replacing the next wallet', async () => {
  let resolve;const events=[],old={nativeRaw:10n};
  const context={address:'owner-a',connectionGeneration:1,balanceStatus:'ready',balances:old,balanceReads:new Map(),
    getBalances:()=>new Promise(r=>{resolve=r;}),renderBalances(){events.push({status:context.balanceStatus,value:context.balances});},say(){}};
  runInNewContext(sourceSpan('async function refreshBalances()', "$('refresh-balances').onclick"),context);
  const pending=context.refreshBalances();
  assert.equal(events[0].status,'loading');assert.equal(events[0].value,old);
  context.address='owner-b';context.connectionGeneration=2;context.balances=null;context.balanceStatus='idle';
  resolve({nativeRaw:99n});await pending;
  assert.equal(context.balances,null);assert.equal(context.balanceStatus,'idle');
});

test('repeated feed start does not erase ready content or launch duplicate streams', () => {
  const nodes=loadingDOM(),states=[];let starts=0,stops=0;
  const context={$:id=>nodes.get(id),marketFeed:{start(){starts++;},stop(){stops++;}},renderNetwork:state=>states.push(state.status)};
  runInNewContext(sourceSpan('let feedRunning = false;', "document.addEventListener('visibilitychange'"),context);
  context.startNetworkFeed();context.startNetworkFeed();
  assert.equal(starts,1);assert.deepEqual(states,['loading']);
  context.stopNetworkFeed();context.startNetworkFeed();
  assert.equal(stops,1);assert.equal(starts,2);assert.deepEqual(states,['loading','loading']);
});
