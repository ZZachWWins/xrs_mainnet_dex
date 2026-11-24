import test from 'node:test';
import assert from 'node:assert/strict';
import { base58Encode } from './dist/trade.mjs';
import { SETTLEMENT_ORIGIN, SETTLEMENT_AUDIENCE, SETTLEMENT_DOMAIN, SETTLEMENT_NETWORK, SETTLEMENT_ROUTES,
  parseSettlementAmount, formatSettlementAmount, settlementAddress, validateSettlementBounds,
  canonicalSettlementJSON, settlementSigningMessage, verifySettlementProof, createSettlementClient } from './dist/settlement.mjs';

// Synthetic public accounts only. No keypair, wallet, RPC or funded operation.
const A = base58Encode(new Uint8Array(32).fill(7));
const B = base58Encode(new Uint8Array(32).fill(8));
const C = base58Encode(new Uint8Array(32).fill(9));
const SIGNATURE = base58Encode(new Uint8Array(64).fill(10));
const TOKEN = 'b'.repeat(64);
const copy = value => JSON.parse(JSON.stringify(value));

function memory() {
  const data = new Map();
  return { data, failWrites: false, getItem(key) { return data.get(key) ?? null; },
    setItem(key, value) { if (this.failOnce) {this.failOnce=false;throw new Error('one synthetic storage failure');} if (this.failWrites) throw new Error('private storage details'); data.set(key, value); } };
}
function setup(options = {}) {
  let milliseconds = 1800000000000, nextId = 0;
  const storage = memory(), credentialStorage = memory(), calls = [], proofs = [];
  const state = { enabled: true, routeEnabled: true, badCapabilities: false, badQuote: false, badChallenge: false,
    submitError: false, status: 'awaiting_funding', badStatusIdentity: false, signatureValid: true, saved: null };
  const now = () => milliseconds;
  const seconds = () => Math.floor(milliseconds / 1000);
  const input = action => ({ action, amount_raw: '1000000', source_owner: A, destination_owner: B, solana_token_account: C });
  const routes = () => Object.fromEntries(Object.keys(SETTLEMENT_ROUTES).map(route => [route, { enabled: state.routeEnabled }]));
  let quoted;
  function status() {
    const intent = state.saved;
    const result = Object.fromEntries(['client_request_id', 'quote_id', 'action', 'route', 'network', 'source_owner',
      'destination_owner', 'solana_token_account', 'amount_raw', 'min_output_raw'].map(key => [key, intent[key]]));
    return { ...result, destination_owner: state.badStatusIdentity ? C : result.destination_owner, status: state.status,
      updated_at: seconds(), source_txid: ['completed','refunded'].includes(state.status) ? SIGNATURE : null,
      destination_txid: ['completed','refunded'].includes(state.status) ? SIGNATURE : null,
      actual_output_raw:state.status==='completed'?(Object.hasOwn(state,'actualOutput')?state.actualOutput:'1000000'):null,
      refund_amount_raw:state.status==='refunded'?'500000':null,refund_asset:state.status==='refunded'?(state.badRefund?'xusdc':'solana_usdc'):null };
  }
  const dependencies = {
    storage, credentialStorage, now,
    locks: { request: async (_key, _options, fn) => fn({ name: 'mock-lock' }) },
    crypto: { getRandomValues(bytes) { bytes.fill(++nextId); return bytes; }, subtle: {
      async importKey(kind, bytes, algorithm, extractable, usage) {
        assert.equal(kind, 'raw'); assert.deepEqual([...bytes], new Array(32).fill(7));
        assert.deepEqual(algorithm, { name: 'Ed25519' }); assert.equal(extractable, false); assert.deepEqual(usage, ['verify']);
        return 'mock-public-key';
      },
      async verify(algorithm, key, signature, message) {
        assert.equal(algorithm, 'Ed25519'); assert.equal(key, 'mock-public-key'); assert.equal(signature.length, 64);
        proofs.push(new TextDecoder().decode(message)); return state.signatureValid;
      },
    } },
    async fetcher(url, init) {
      assert.ok(url.startsWith(SETTLEMENT_AUDIENCE + '/'));
      assert.equal(init.redirect, 'error'); assert.equal(init.credentials, 'omit'); assert.equal(init.cache, 'no-store');
      const path = url.slice(SETTLEMENT_AUDIENCE.length), body = init.body === undefined ? undefined : JSON.parse(init.body);
      calls.push({ path, method: init.method, body: copy(body ?? null), headers: { ...init.headers } });
      if (options.response) { const custom = await options.response(path, init, body); if (custom) return custom; }
      let reply;
      if (path === '/capabilities') reply = { schema_version: 1, network: state.badCapabilities ? { ...SETTLEMENT_NETWORK, xeris_chain_id: 'foreign' } : SETTLEMENT_NETWORK,
        can_accept_funds: state.enabled, routes: routes(), issued_at: seconds(), expires_at: seconds() + 20 };
      else if (path === '/quotes') {
        const { slippage_bps, ...bindings } = body;
        assert.equal(slippage_bps, 50);
        quoted = { schema_version: 1, ...bindings, quote_id: 'quote-1', network: SETTLEMENT_NETWORK, min_output_raw: body.action==='redeem'?body.amount_raw:'995000',
          expected_output_raw: '1000000', created_at: seconds(), expires_at: seconds() + 60 };
        reply = state.badQuote ? { ...quoted, destination_owner: C } : quoted;
      } else if (path === '/challenges') {
        assert.equal(body.quote_id, quoted.quote_id); assert.equal(body.client_request_id, quoted.client_request_id);
        const intent = { schema_version: 2, domain: SETTLEMENT_DOMAIN, audience: SETTLEMENT_AUDIENCE,
          action: quoted.action, route: quoted.route, nonce: 'a'.repeat(64), created_at: seconds(), expires_at: quoted.expires_at,
          network: SETTLEMENT_NETWORK, amount_raw: quoted.amount_raw, min_output_raw: quoted.min_output_raw,
          source_owner: quoted.source_owner, destination_owner: quoted.destination_owner, solana_token_account: quoted.solana_token_account,
          quote_id: quoted.quote_id, client_request_id: quoted.client_request_id };
        reply = { schema_version: 1, intent, signing_message_utf8: settlementSigningMessage(intent, { now: seconds() }) + (state.badChallenge ? '\n' : '') };
      } else if (path === '/requests') {
        assert.equal(init.headers['Idempotency-Key'], body.intent.client_request_id);
        state.saved = copy(body.intent);
        if (state.submitError) throw new Error('Do not expose server/wallet/token error details.');
        reply = { schema_version: 1, request: status(), status_token: TOKEN };
        if(state.failJournalAfterRequest)storage.failOnce=true;
      } else if (path.endsWith('/access')) {
        assert.deepEqual(body.intent, state.saved); assert.equal(body.signature, SIGNATURE);
        reply = { schema_version: 1, request: status(), status_token: TOKEN };
      } else {
        assert.equal(init.method, 'GET'); assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
        reply = { schema_version: 1, request: status() };
      }
      return new Response(JSON.stringify(reply), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  };
  const client = createSettlementClient(dependencies);
  return { client, dependencies, input, calls, state, storage, credentialStorage, proofs,
    advance(seconds) { milliseconds += seconds * 1000; },
    async reviewed(action = 'buy') { const q = await client.quote(input(action)); return client.challenge(q.client_request_id); } };
}

test('fixed settlement origin and all three route directions are explicit', () => {
  assert.equal(SETTLEMENT_ORIGIN, 'https://mainnetrpc.xerisweb.com');
  assert.deepEqual(Object.keys(SETTLEMENT_ROUTES), ['solana_usdc_to_native_xrs', 'native_xrs_to_solana_usdc', 'xusdc_to_solana_usdc']);
  assert.equal(SETTLEMENT_ROUTES.solana_usdc_to_native_xrs.outputAsset, 'native_xrs');
  assert.ok(Object.isFrozen(SETTLEMENT_ROUTES));
});

test('completion requires confirmed actual raw output meeting the bound minimum; refund asset is explicit',async()=>{
  for(const actual of [null,'994999']){const f=setup(),proof=await f.reviewed();await f.client.submit(proof.intent,SIGNATURE);f.state.status='completed';f.state.actualOutput=actual;assert.equal((await f.client.recover(proof.intent.client_request_id)).status,'unresolved');}
  const f=setup(),proof=await f.reviewed();await f.client.submit(proof.intent,SIGNATURE);f.state.status='refunded';const refund=await f.client.recover(proof.intent.client_request_id);assert.equal(refund.refund_asset,'solana_usdc');assert.equal(refund.refund_amount_raw,'500000');assert.equal(refund.actual_output_raw,null);
  const bad=setup(),badProof=await bad.reviewed();await bad.client.submit(badProof.intent,SIGNATURE);bad.state.status='refunded';bad.state.badRefund=true;await assert.rejects(bad.client.recover(badProof.intent.client_request_id),/refund asset/);
});

test('deadline bounds a fetch/body stall even when cancellation never resolves',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  for(const stall of ['fetch','body']){
    const f=setup({response:async path=>path==='/capabilities'?(stall==='fetch'?new Promise(()=>{}):{ok:true,headers:{get:()=>null},body:{getReader:()=>({read:()=>new Promise(()=>{}),cancel:()=>new Promise(()=>{})})}}):null});
    const rejected=assert.rejects(f.client.getCapabilities(),/unavailable or invalid/);await Promise.resolve();await Promise.resolve();t.mock.timers.tick(8001);await rejected;
  }
});

test('one post-ack journal write failure preserves a conservative recoverable original ID',async()=>{
  const f=setup(),proof=await f.reviewed();f.state.failJournalAfterRequest=true;
  await assert.rejects(f.client.submit(proof.intent,SIGNATURE),/unresolved/);
  const saved=f.client.getHistory()[0];assert.equal(saved.status,'submission_unknown');assert.equal(saved.request,null);
  assert.equal((await f.client.recover(proof.intent.client_request_id)).status,'awaiting_funding');
  assert.equal(f.calls.filter(c=>c.path==='/requests').length,1);
});

test('settlement decimals preserve raw integers above 2^53 and exact u64 boundaries', () => {
  assert.equal(parseSettlementAmount('1.000001', 'solana_usdc'), '1000001');
  assert.equal(parseSettlementAmount('0.000000001', 'native_xrs'), '1');
  assert.equal(parseSettlementAmount('9007199254.740993', 'xusdc'), '9007199254740993');
  assert.equal(parseSettlementAmount('18446744073.709551615', 'native_xrs'), '18446744073709551615');
  assert.equal(formatSettlementAmount('9007199254740993', 'solana_usdc'), '9007199254.740993');
  assert.equal(formatSettlementAmount('0', 'native_xrs'), '0');
});

test('ambiguous, rounded, excess-precision and overflowing amounts fail', () => {
  for (const value of ['', '0', '-1', '+1', '01', '.1', '1.', '1e3', '1,2', '1_000', 'NaN', 1, {}, null,
    '0.0000001', '18446744073709.551616']) assert.throws(() => parseSettlementAmount(value, 'solana_usdc'));
  assert.throws(() => parseSettlementAmount('1', '__proto__'));
  for (const value of [9007199254740992, '01', '1e3', '-1', '18446744073709551616']) {
    assert.throws(() => formatSettlementAmount(value, 'native_xrs'));
  }
});

test('public addresses remain canonical and quote bounds use integers only', () => {
  assert.equal(settlementAddress(A), A);
  for (const value of [` ${A}`, `${A}\n`, '1'.repeat(32), 'bad', {}, null]) assert.throws(() => settlementAddress(value));
  const bounds = { inputRaw: '9007199254740993', expectedOutputRaw: '200', minimumOutputRaw: '199', maximumInputRaw: '9007199254740993' };
  assert.deepEqual(validateSettlementBounds(bounds), bounds);
  assert.throws(() => validateSettlementBounds({ ...bounds, minimumOutputRaw: '201' }));
  assert.throws(() => validateSettlementBounds({ ...bounds, maximumInputRaw: '9007199254740992' }));
  assert.throws(() => validateSettlementBounds({ ...bounds, inputRaw: 9007199254740992 }));
});

test('paused global/route availability and changed network never create quotes, requests or proofs', async () => {
  for (const key of ['enabled', 'routeEnabled', 'badCapabilities']) {
    const fixture = setup(); fixture.state[key] = key === 'badCapabilities';
    await assert.rejects(fixture.client.quote(fixture.input('buy')));
    assert.deepEqual(fixture.calls.map(value => value.path), ['/capabilities']);
    assert.equal(fixture.proofs.length, 0);
    assert.deepEqual(fixture.client.getHistory(), []);
  }
  assert.throws(() => createSettlementClient({ origin: 'https://attacker.invalid' }));
});

test('challenge bytes are canonical and source, destination, raw bounds and domains cannot be changed', async () => {
  const fixture = setup(), reviewed = await fixture.reviewed();
  const expected = `Xeris settlement ownership proof\n${SETTLEMENT_DOMAIN}\n${canonicalSettlementJSON(reviewed.intent)}`;
  assert.equal(reviewed.signing_message_utf8, expected);
  assert.equal(expected.endsWith('\n'), false);
  assert.ok(expected.includes('"amount_raw":"1000000"'));
  for (const change of [{ destination_owner: C }, { amount_raw: '1000001' }, { audience: 'https://attacker.invalid' },
    { action: 'sell' }, { nonce: 'A'.repeat(64) }, { min_output_raw: '0' }]) {
    await assert.rejects(fixture.client.submit({ ...reviewed.intent, ...change }, SIGNATURE));
  }
  assert.equal(fixture.calls.filter(call => call.path === '/requests').length, 0);
  assert.throws(() => canonicalSettlementJSON({ unicode: 'é' }));
  assert.throws(() => canonicalSettlementJSON({ amount: 9007199254740992 }));
});

test('server challenge messages and quote recipients are checked rather than signed blindly', async () => {
  const badQuote = setup(); badQuote.state.badQuote = true;
  await assert.rejects(badQuote.client.quote(badQuote.input('buy')), /reviewed identity/);
  const badMessage = setup(), quote = await badMessage.client.quote(badMessage.input('buy'));
  badMessage.state.badChallenge = true;
  await assert.rejects(badMessage.client.challenge(quote.client_request_id), /ownership message/);
  assert.equal(badMessage.proofs.length, 0);
});

test('fresh availability is checked before challenge and submit; proof verification never calls a wallet', async () => {
  const fixture = setup(), q = await fixture.client.quote(fixture.input('buy'));
  fixture.state.enabled = false;
  await assert.rejects(fixture.client.challenge(q.client_request_id), /unavailable/);
  assert.equal(fixture.calls.filter(call => call.path === '/challenges').length, 0);
  fixture.state.enabled = true;
  const reviewed = await fixture.client.challenge(q.client_request_id);
  fixture.state.enabled = false;
  await assert.rejects(fixture.client.submit(reviewed.intent, SIGNATURE), /unavailable/);
  assert.equal(fixture.proofs.length, 0);
  fixture.state.enabled = true; fixture.state.signatureValid = false;
  await assert.rejects(fixture.client.submit(reviewed.intent, SIGNATURE), /ownership proof/);
  assert.equal(fixture.calls.filter(call => call.path === '/requests').length, 0);
});

test('all routes bind owners and tokens, keep public recovery IDs durable, and credentials separate', async () => {
  for (const action of ['buy', 'redeem', 'sell']) {
    const fixture = setup(), reviewed = await fixture.reviewed(action);
    const result = await fixture.client.submit(reviewed.intent, SIGNATURE);
    assert.equal(result.status, 'awaiting_funding');
    assert.equal(result.route, ({ buy: 'solana_usdc_to_native_xrs', redeem: 'xusdc_to_solana_usdc', sell: 'native_xrs_to_solana_usdc' })[action]);
    assert.equal(fixture.proofs[0], reviewed.signing_message_utf8);
    const publicText = [...fixture.storage.data.values()].join('');
    assert.ok(publicText.includes(result.client_request_id)); assert.equal(publicText.includes(TOKEN), false); assert.equal(publicText.includes(SIGNATURE), false);
    assert.ok([...fixture.credentialStorage.data.values()].join('').includes(TOKEN));
    const reloaded = createSettlementClient(fixture.dependencies);
    fixture.state.enabled = false;
    assert.equal((await reloaded.recover(result.client_request_id)).status, 'awaiting_funding');
    assert.equal(fixture.calls.at(-1).method, 'GET');
    await assert.rejects(reloaded.submit(reviewed.intent, SIGNATURE), /already pending/);
    await assert.rejects(reloaded.quote(fixture.input(action)), /unresolved/);
    assert.equal(fixture.calls.filter(call => call.path === '/requests').length, 1);
  }
});

test('ambiguous submission never automatically retries and lost tokens can reauthorize only the existing ID', async () => {
  const fixture = setup(), reviewed = await fixture.reviewed();
  fixture.state.submitError = true;
  await assert.rejects(fixture.client.submit(reviewed.intent, SIGNATURE), /unresolved/);
  assert.equal(fixture.client.getHistory()[0].status, 'submission_unknown');
  const reloaded = createSettlementClient(fixture.dependencies);
  await assert.rejects(reloaded.submit(reviewed.intent, SIGNATURE), /already pending/);
  await assert.rejects(reloaded.recover(reviewed.intent.client_request_id), /Restore read-only/);
  fixture.advance(200); fixture.state.enabled = false;
  const restored = await reloaded.restoreAccess(reviewed.intent, SIGNATURE);
  assert.equal(restored.client_request_id, reviewed.intent.client_request_id);
  assert.ok(fixture.calls.at(-1).path.endsWith('/access'));
  assert.equal((await reloaded.recover(restored.client_request_id)).status, 'awaiting_funding');
  assert.equal(fixture.calls.filter(call => call.path === '/requests').length, 1);
  assert.equal(fixture.calls.filter(call => call.path.endsWith('/access')).length, 1);
});

test('unknown status stays unresolved and destination changes never overwrite the saved request', async () => {
  const fixture = setup(), reviewed = await fixture.reviewed();
  fixture.state.status = 'unexpected_future_state';
  assert.equal((await fixture.client.submit(reviewed.intent, SIGNATURE)).status, 'unresolved');
  fixture.state.badStatusIdentity = true;
  await assert.rejects(fixture.client.recover(reviewed.intent.client_request_id), /reviewed identity/);
  assert.equal(fixture.client.getHistory()[0].destination_owner, B);
  await assert.rejects(fixture.client.quote(fixture.input('buy')), /unresolved/);
});

test('failed storage, cross-tab locks, stale quotes and oversize responses fail before any request submission', async () => {
  const fixture = setup(); fixture.storage.failWrites = true;
  await assert.rejects(fixture.client.quote(fixture.input('buy')), /could not be saved/);
  assert.equal(fixture.calls.some(value => value.method === 'POST'), false);
  const busy = setup(); busy.dependencies.locks = { request: async (_key, _options, callback) => callback(null) };
  await assert.rejects(createSettlementClient(busy.dependencies).quote(busy.input('buy')), /Another tab/);
  assert.equal(busy.calls.length, 0);
  const stale = setup(), q = await stale.client.quote(stale.input('buy')); stale.advance(121);
  await assert.rejects(stale.client.challenge(q.client_request_id), /expired/);
  const huge = setup({ response: () => new Response('x', { headers: { 'Content-Length': '65537' } }) });
  await assert.rejects(huge.client.getCapabilities(), /unavailable or invalid/);
  const hugeBody = setup({ response: () => new Response('x'.repeat(65537)) });
  await assert.rejects(hugeBody.client.getCapabilities(), /unavailable or invalid/);
});
