import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CONFIG, integer, parseAmount, formatAmount, parseExactJSON, request, rpc,
  checkChain, parsePool, quoteFromPool, getQuote, parseBalances,
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
