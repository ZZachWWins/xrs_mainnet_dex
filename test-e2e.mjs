import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, verify, webcrypto } from 'node:crypto';
import { CONFIG, getQuote } from './dist/model.mjs';
import { execute, reconcile, getHistory, getPending } from './dist/trade.mjs';

// OFFLINE application/ledger integration. All fetches are intercepted and every
// unexpected URL or operation fails. The synthetic wallet key exists only in
// memory. No user keys/files, live RPC, issuance, burn or Solana bridge is used.
const FEE = 1_000_000n;
const INITIAL_A = 709_700_000_000n;
const INITIAL_B = 10_099_031n;
const INPUT = 500_000n;
const BOUGHT = 33_383_761_746n;
const RETURNED = 497_145n;
const MAX_U64 = (1n << 64n) - 1n;
const POOL_ID = 'xrs-xusdc-mainnet-v1';
const BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const b58 = bytes => {
  let value = BigInt(`0x${Buffer.from(bytes).toString('hex') || '0'}`), text = '';
  while (value) { text = BASE58[Number(value % 58n)] + text; value /= 58n; }
  for (const byte of bytes) { if (byte !== 0) break; text = '1' + text; }
  return text;
};
const exactJSON = value => {
  if (typeof value === 'bigint') return value.toString();
  if (Array.isArray(value)) return `[${value.map(exactJSON).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).map(([k, v]) => `${JSON.stringify(k)}:${exactJSON(v)}`).join(',')}}`;
  return JSON.stringify(value);
};
const reply = value => new Response(exactJSON(value), {status: 200, headers: {'Content-Type': 'application/json'}});

class MemoryStorage {
  constructor(entries = []) { this.data = new Map(entries); }
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key, value) { this.data.set(key, String(value)); }
}

// Independent decoder: do not use the application's instruction builder,
// amount parser or AMM math when deciding what the submitted transaction does.
function decodeSignedWire(encoded, publicBytes, publicKey) {
  const wire = Buffer.from(encoded, 'base64');
  assert.equal(wire.toString('base64'), encoded, 'canonical transaction base64');
  let position = 0;
  const take = count => { assert.ok(position + count <= wire.length, 'wire bounds'); const bytes = wire.subarray(position, position + count); position += count; return bytes; };
  const byte = () => take(1)[0];
  const compact = () => {
    let value = 0, shift = 0, count = 0, last;
    do { last = byte(); assert.ok(++count <= 3); value += (last & 127) * 2 ** shift; shift += 7; } while (last & 128);
    assert.ok(count === 1 || last !== 0, 'minimal compact vector');
    assert.ok(value <= 65535); return value;
  };
  assert.equal(compact(), 1, 'one signature');
  const signature = take(64), message = wire.subarray(position);
  assert.ok(verify(null, message, publicKey, signature), 'mock node independently verifies Ed25519');
  assert.deepEqual([...take(3)], [1, 0, 1], 'one writable signer and one readonly unsigned program');
  assert.equal(compact(), 2);
  assert.deepEqual(take(32), publicBytes);
  assert.deepEqual(take(32), Buffer.alloc(32), 'system program');
  const blockhash = take(32).toString('hex');
  assert.equal(compact(), 1, 'single instruction');
  assert.equal(byte(), 1, 'program index');
  assert.equal(compact(), 1);
  assert.equal(byte(), 0, 'only instruction account is the signer');
  const data = take(compact());
  assert.equal(position, wire.length, 'no extra wire data');
  let offset = 0;
  const dataTake = count => { assert.ok(offset + count <= data.length); const bytes = data.subarray(offset, offset + count); offset += count; return bytes; };
  const variant = dataTake(4).readUInt32LE();
  const u64 = () => dataTake(8).readBigUInt64LE();
  const string = () => { const length = u64(); assert.ok(length <= 512n); return dataTake(Number(length)).toString('utf8'); };
  let action, amount, minimum = null;
  if (variant === 13 || variant === 14) {
    action = variant === 13 ? 'wrap' : 'unwrap'; amount = u64();
  } else {
    assert.equal(variant, 4, 'only ContractCall, WrapXrs or UnwrapXrs');
    assert.equal(string(), POOL_ID);
    const method = string();
    assert.ok(['swap_b_to_a', 'swap_a_to_b'].includes(method));
    action = method === 'swap_b_to_a' ? 'buy' : 'sell';
    const args = string().match(/^\{"amount":([1-9][0-9]*),"min_output":([1-9][0-9]*)\}$/);
    assert.ok(args, 'exact integer swap arguments');
    amount = BigInt(args[1]); minimum = BigInt(args[2]);
    assert.ok(minimum > 0n && minimum <= MAX_U64);
  }
  assert.equal(offset, data.length, 'no extra instruction data');
  assert.ok(amount > 0n && amount <= MAX_U64);
  return {signature: b58(signature), message, blockhash, action, amount, minimum};
}

function marketQuote(pool, buying, amount) {
  const reserveIn = buying ? pool.b : pool.a, reserveOut = buying ? pool.a : pool.b;
  const fee = amount * 30n / 10_000n;
  const output = reserveOut * (amount - fee) / (reserveIn + amount - fee);
  assert.ok(amount > 0n && output > 0n && output < reserveOut && reserveIn + amount <= MAX_U64);
  return {input_token: buying ? 'xeris_usdc' : 'xrs_native', output_token: buying ? 'xrs_native' : 'xeris_usdc',
    input_amount: amount, output_amount: output, fee, fee_bps: 30};
}

function harness() {
  const key = generateKeyPairSync('ed25519'); // Synthetic, ephemeral; never exported.
  const publicBytes = key.publicKey.export({format: 'der', type: 'spki'}).subarray(-32);
  const address = b58(publicBytes);
  const state = {
    pool: {a: INITIAL_A, b: INITIAL_B},
    user: {native: 10_000_000n, wrapped: 0n, usdc: INPUT},
    external: {native: 10_000_000n, wrapped: 0n, usdc: 5_000_000n},
    wrappedSupply: INITIAL_A, feeSink: 0n, slot: 28000n,
    signs: 0, submissions: [], receipts: new Map(), issuedHashes: new Set(), receiptReads: [],
    hiddenReceipts: new Set(), loseResponseFor: null, afterSign: null,
  };
  let storage = new MemoryStorage();
  const held = new Set();
  const locks = {async request(name, settings, callback) {
    assert.equal(settings.mode, 'exclusive'); assert.equal(settings.ifAvailable, true);
    if (held.has(name)) return callback(null);
    held.add(name); try { return await callback({name}); } finally { held.delete(name); }
  }};
  const provider = {
    isXerisWallet: true,
    async connect() { return {publicKey: {toString: () => address}}; },
    async getRpcUrl() { return CONFIG.rpc; },
    async signTransaction(encoded) {
      state.signs++;
      const wire = Buffer.from(encoded, 'base64');
      assert.equal(wire[0], 1); assert.ok(wire.subarray(1, 65).every(x => x === 0));
      const signature = sign(null, wire.subarray(65), key.privateKey);
      wire.set(signature, 1);
      if (state.afterSign) { const hook = state.afterSign; state.afterSign = null; await hook(); }
      return {signature: b58(signature), signedTransaction: wire.toString('base64')};
    },
  };
  const options = () => ({storage, locks, crypto: webcrypto});
  const snapshot = () => structuredClone({pool: state.pool, user: state.user,
    external: state.external, wrappedSupply: state.wrappedSupply, feeSink: state.feeSink});
  const assertConservation = () => {
    assert.equal(state.pool.b + state.user.usdc + state.external.usdc, INITIAL_B + INPUT + 5_000_000n, 'all xUSDC is conserved');
    assert.equal(state.pool.a + state.user.wrapped + state.external.wrapped, state.wrappedSupply, 'pool and wallet wXRS match supply');
    assert.equal(state.wrappedSupply + state.user.native + state.external.native + state.feeSink,
      INITIAL_A + 20_000_000n, 'native/wrapped backing plus fees is conserved');
  };
  function applySwap(wallet, buying, amount, minimum) {
    const q = marketQuote(state.pool, buying, amount);
    const input = buying ? 'usdc' : 'wrapped', output = buying ? 'wrapped' : 'usdc';
    if (wallet[input] < amount || q.output_amount < minimum) throw new Error('ExecutionFailed');
    wallet[input] -= amount; wallet[output] += q.output_amount;
    if (buying) { state.pool.b += amount; state.pool.a -= q.output_amount; }
    else { state.pool.a += amount; state.pool.b -= q.output_amount; }
    return q;
  }
  const externalBuy = () => {
    state.external.native -= FEE; state.feeSink += FEE;
    applySwap(state.external, true, 5_000_000n, 1n);
    state.slot++; assertConservation();
  };
  async function mockFetch(url, settings = {}) {
    const parsed = new URL(url);
    assert.equal(parsed.origin, CONFIG.rpc, 'no external network may be reached');
    if (settings.method === 'GET') {
      if (parsed.pathname === '/v2/block/slot/27201') return reply({success: true, data: {slot: 27201, hash: '0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3'}});
      if (parsed.pathname === `/contract/${POOL_ID}`) return reply({success: true, contract: {
        contract_id: POOL_ID, contract_type: 'Swap', is_active: true, created_slot: 27201,
        owner: 'HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX', state: {Swap: {
          token_a: 'xrs_native', token_b: 'xeris_usdc', reserve_a: state.pool.a, reserve_b: state.pool.b,
          fee_bps: 30, total_shares: 2_677_178_048n,
        }},
      }});
      if (parsed.pathname === `/contract/${POOL_ID}/quote`) {
        const input = parsed.searchParams.get('input_token');
        assert.ok(input === 'xeris_usdc' || input === 'xrs_native');
        return reply({success: true, quote: marketQuote(state.pool, input === 'xeris_usdc', BigInt(parsed.searchParams.get('amount')))});
      }
      if (parsed.pathname === `/token/accounts/${address}`) return reply({address, native_xrs: state.user.native,
        token_accounts: [
          {token_id: 'xrs_native', balance: state.user.wrapped, decimals: 9},
          {token_id: 'xeris_usdc', balance: state.user.usdc, decimals: 6},
        ].filter(token => token.balance > 0n)});
      assert.fail(`Unexpected mock GET ${parsed.pathname}`);
    }
    assert.equal(settings.method, 'POST');
    const body = JSON.parse(settings.body);
    if (parsed.pathname === '/') {
      if (body.method === 'getLatestBlockhash') {
        const hash = state.slot.toString(16).padStart(64, '0'); state.issuedHashes.add(hash);
        return reply({jsonrpc: '2.0', id: body.id, result: {value: {blockhash: hash, lastValidBlockHeight: state.slot + 150n}}});
      }
      if (body.method === 'getSlot') return reply({jsonrpc: '2.0', id: body.id, result: state.slot});
      if (body.method === 'getTransaction') {
        const id = body.params[0]; state.receiptReads.push(id);
        return reply({jsonrpc: '2.0', id: body.id, result: state.hiddenReceipts.has(id) ? null : state.receipts.get(id) ?? null});
      }
      assert.fail(`Unexpected mock RPC ${body.method}`);
    }
    assert.equal(parsed.pathname, '/submit', 'the only possible mock write');
    const tx = decodeSignedWire(body.tx_base64, publicBytes, key.publicKey);
    assert.ok(state.issuedHashes.has(tx.blockhash), 'a known recent hash was signed');
    assert.equal(getPending(address, options()).signature, tx.signature, 'the signed ID is persisted before sending');
    assert.ok(!state.submissions.some(item => item.signature === tx.signature), 'no transaction resubmission');
    state.submissions.push(tx);
    // Like the real node, a failed instruction does not refund its base fee.
    assert.ok(state.user.native >= FEE, 'node fee gate');
    state.user.native -= FEE; state.feeSink += FEE;
    let successful = true;
    try {
      if (tx.action === 'buy' || tx.action === 'sell') applySwap(state.user, tx.action === 'buy', tx.amount, tx.minimum);
      else if (tx.action === 'unwrap') {
        if (state.user.wrapped < tx.amount) throw new Error('ExecutionFailed');
        state.user.wrapped -= tx.amount; state.user.native += tx.amount; state.wrappedSupply -= tx.amount;
      } else {
        if (state.user.native < tx.amount) throw new Error('ExecutionFailed');
        state.user.native -= tx.amount; state.user.wrapped += tx.amount; state.wrappedSupply += tx.amount;
      }
    } catch (error) { assert.equal(error.message, 'ExecutionFailed'); successful = false; }
    state.slot++; assertConservation();
    state.receipts.set(tx.signature, {slot: state.slot, transaction: {signatures: [tx.signature]},
      meta: {fee: FEE, err: successful ? null : {InstructionError: [0, 'ExecutionFailed']}, instructionOutcomes: [{index: 0, ok: successful}]}});
    if (state.loseResponseFor === tx.action) { state.loseResponseFor = null; throw new Error('Simulated lost submission response after node execution'); }
    return reply({status: 'queued', signature: tx.signature});
  }
  async function intent(action, amountRaw) {
    if (action === 'wrap' || action === 'unwrap') return {action, amountRaw};
    const quote = await getQuote(action === 'buy', amountRaw);
    return {action, amountRaw, expectedOutputRaw: quote.outputRaw, minOutputRaw: quote.minimumOutputRaw, quoteTime: quote.quotedAt};
  }
  const run = async (action, amount) => execute({provider, address, ...await intent(action, amount)}, options());
  const reload = () => { storage = new MemoryStorage([...storage.data.entries()]); };
  return {state, address, options, provider, mockFetch, intent, run, reload, externalBuy, snapshot, assertConservation};
}

async function offline(callback) {
  const original = globalThis.fetch, h = harness();
  globalThis.fetch = h.mockFetch;
  try { await callback(h); } finally { globalThis.fetch = original; }
}

function assertGoldenEnd(h) {
  assert.deepEqual(h.state.user, {native: 6_000_000n, wrapped: 0n, usdc: RETURNED});
  assert.deepEqual(h.state.pool, {a: INITIAL_A, b: 10_101_886n});
  assert.equal(h.state.wrappedSupply, INITIAL_A);
  assert.equal(h.state.feeSink, 4_000_000n, 'four separate Xeris base fees');
  assert.equal(INPUT - RETURNED, 2_855n, 'LP fee/rounding retained in the pool');
  assert.equal(h.state.signs, 4); assert.equal(h.state.submissions.length, 4);
  assert.deepEqual(h.state.submissions.map(tx => tx.action), ['buy', 'unwrap', 'wrap', 'sell']);
  assert.deepEqual(h.state.submissions.map(tx => tx.amount), [INPUT, BOUGHT, BOUGHT, BOUGHT]);
  assert.deepEqual(getHistory(h.address, h.options()).map(tx => tx.status), ['confirmed', 'confirmed', 'confirmed', 'confirmed']);
  assert.equal(getPending(h.address, h.options()), null);
  h.assertConservation();
}

test('offline Web4 round trip executes buy → unwrap → wrap → sell with pilot-exact balances', async () => offline(async h => {
  assert.equal((await h.run('buy', INPUT)).status, 'confirmed');
  assert.deepEqual(h.state.user, {native: 9_000_000n, wrapped: BOUGHT, usdc: 0n});
  assert.equal((await h.run('unwrap', BOUGHT)).status, 'confirmed');
  assert.deepEqual(h.state.user, {native: BOUGHT + 8_000_000n, wrapped: 0n, usdc: 0n});
  assert.equal((await h.run('wrap', BOUGHT)).status, 'confirmed');
  assert.deepEqual(h.state.user, {native: 7_000_000n, wrapped: BOUGHT, usdc: 0n});
  assert.equal((await h.run('sell', BOUGHT)).status, 'confirmed');
  assertGoldenEnd(h);
}));

test('lost unwrap response survives reload, blocks dependent wrap, then reconciles the same ID without resending', async () => offline(async h => {
  await h.run('buy', INPUT);
  h.state.loseResponseFor = 'unwrap';
  const unknown = await h.run('unwrap', BOUGHT);
  assert.equal(unknown.status, 'pending');
  assert.equal(h.state.user.wrapped, 0n, 'node already unwrapped despite lost acknowledgement');
  assert.equal(h.state.user.native, BOUGHT + 8_000_000n);
  h.reload();
  assert.equal(getPending(h.address, h.options()).signature, unknown.signature);
  await assert.rejects(h.run('wrap', BOUGHT), /unresolved/);
  h.state.hiddenReceipts.add(unknown.signature);
  assert.equal((await reconcile(h.address, h.options())).status, 'pending');
  await assert.rejects(h.run('wrap', BOUGHT), /unresolved/);
  assert.equal(h.state.signs, 2); assert.equal(h.state.submissions.length, 2);
  h.state.hiddenReceipts.delete(unknown.signature);
  const recovered = await reconcile(h.address, h.options());
  assert.equal(recovered.status, 'confirmed'); assert.equal(recovered.signature, unknown.signature);
  assert.equal(h.state.signs, 2); assert.equal(h.state.submissions.length, 2);
  assert.deepEqual(h.state.receiptReads.slice(-2), [unknown.signature, unknown.signature]);
  await h.run('wrap', BOUGHT); await h.run('sell', BOUGHT);
  assertGoldenEnd(h);
}));

test('a price move beyond the reviewed minimum is rejected before signing or debiting the user', async () => offline(async h => {
  const reviewed = await h.intent('buy', INPUT);
  h.externalBuy();
  const before = h.snapshot();
  await assert.rejects(execute({provider: h.provider, address: h.address, ...reviewed}, h.options()), /price moved/);
  assert.deepEqual(h.snapshot(), before);
  assert.equal(h.state.signs, 0); assert.equal(h.state.submissions.length, 0);
  assert.equal(getHistory(h.address, h.options()).length, 0);
}));

test('price movement during wallet approval fails the signed minimum on the node: only the network fee is charged', async () => offline(async h => {
  let beforeExecution;
  h.state.afterSign = () => { h.externalBuy(); beforeExecution = h.snapshot(); };
  const result = await h.run('buy', INPUT);
  assert.equal(result.status, 'failed');
  assert.deepEqual(h.state.pool, beforeExecution.pool, 'failed swap does not mutate either pool reserve');
  assert.equal(h.state.user.usdc, INPUT, 'no xUSDC spend on failed instruction');
  assert.equal(h.state.user.wrapped, 0n, 'no output credited');
  assert.equal(h.state.user.native, beforeExecution.user.native - FEE);
  assert.equal(h.state.feeSink, beforeExecution.feeSink + FEE);
  assert.equal(h.state.signs, 1); assert.equal(h.state.submissions.length, 1);
  assert.equal(getPending(h.address, h.options()), null);
  assert.equal(getHistory(h.address, h.options())[0].signature, result.signature);
  h.assertConservation();
}));

test('after unwrapping, attempting to wrap the entire native balance cannot consume the required gas reserve', async () => offline(async h => {
  await h.run('buy', INPUT); await h.run('unwrap', BOUGHT);
  const before = h.snapshot();
  await assert.rejects(h.run('wrap', h.state.user.native), /native XRS/);
  assert.deepEqual(h.snapshot(), before);
  assert.equal(h.state.signs, 2); assert.equal(h.state.submissions.length, 2);
  await h.run('wrap', BOUGHT); await h.run('sell', BOUGHT);
  assertGoldenEnd(h);
}));
