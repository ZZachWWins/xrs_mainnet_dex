import test from 'node:test';
import assert from 'node:assert/strict';
import { createPrivateKey, createPublicKey, sign, webcrypto } from 'node:crypto';
import { CONFIG } from './dist/model.mjs';
import { base58Encode, base58Decode, buildUnsignedTransaction, instructionFor, toBase64, fromBase64,
  verifySignedTransaction, execute, reconcile, getPending, getHistory, checkProvider, unsigned } from './dist/trade.mjs';

// Deterministic synthetic test keys. No user key files, live RPC or live writes.
const synthetic = createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), Buffer.alloc(32, 25)]), format: 'der', type: 'pkcs8' });
const publicBytes = createPublicKey(synthetic).export({ format: 'der', type: 'spki' }).subarray(-32);
const address = base58Encode(publicBytes);
const now = 1806020000000;
const blockhash = '11'.repeat(32);
const buyIntent = { action: 'buy', amountRaw: 500000n, minOutputRaw: 33216842938n, expectedOutputRaw: 33383761746n, quoteTime: now };
class MemoryStorage {
  data = new Map(); fail = false; discard = false;
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key, value) { if (this.fail) throw new Error('Storage failed'); if (!this.discard) this.data.set(key, value); }
}
function harness() {
  const storage = new MemoryStorage();
  const held = new Set();
  const state = { sends: 0, signs: 0, reads: 0, connects: 0, mode: 'success', clock: now, receipt: 'confirmed', native: 10000000n, wrapped: 50000000000n, usdc: 1000000n, latestSig: null };
  const provider = {
    isXerisWallet: true,
    async connect() { state.connects++; return { publicKey: { toString: () => state.mode === 'changed-wallet' ? '11111111111111111111111111111111' : address } }; },
    async getRpcUrl() { return state.mode === 'wrong-network' ? 'https://rpc.xerisweb.com' : CONFIG.rpc; },
    async signTransaction(encoded) {
      state.signs++;
      const bytes = fromBase64(encoded);
      if (state.mode === 'changed-message') bytes[bytes.length - 1] ^= 1;
      const signature = sign(null, bytes.subarray(65), synthetic);
      bytes.set(signature, 1);
      if (state.mode === 'bad-signature') bytes[1] ^= 1;
      if (state.mode === 'expired-during-approval') state.clock += 31000;
      if (state.mode === 'switch-during-approval') state.mode = 'changed-wallet';
      const id = base58Encode(bytes.subarray(1, 65));
      state.latestSig = id;
      return { signature: id, signedTransaction: toBase64(bytes) };
    },
  };
  const locks = { async request(key, options, callback) {
    assert.equal(options.ifAvailable, true);
    if (held.has(key)) return callback(null);
    held.add(key);
    try { return await callback({ name: key }); } finally { held.delete(key); }
  } };
  const options = {
    storage, locks, crypto: webcrypto, now: () => state.clock,
    async checkChain() { if (state.mode === 'wrong-chain') throw new Error('Wrong chain'); return true; },
    async getBalances() { return { nativeRaw: state.native, wrappedRaw: state.wrapped, usdcRaw: state.usdc }; },
    async getQuote(buying, amountRaw) { return { buying, inputRaw: amountRaw, outputRaw: state.mode === 'price-moved' ? 1n : buyIntent.expectedOutputRaw }; },
    async rpc(method, params) {
      state.reads++;
      if (method === 'getLatestBlockhash') return { value: { blockhash, lastValidBlockHeight: '250' } };
      if (method === 'getSlot') return state.mode === 'expired-blockhash' ? '251' : '101';
      assert.equal(method, 'getTransaction');
      assert.equal(params[0], state.latestSig);
      if (state.receipt === 'unavailable') throw new Error('Read failed');
      if (state.receipt === 'pending') return null;
      return { slot: '110', transaction: { signatures: [state.receipt === 'wrong-id' ? 'wrong' : state.latestSig] },
        meta: { err: state.receipt === 'failed' ? { InstructionError: ['0', 'ExecutionFailed'] } : state.receipt === 'unknown' ? 'OutcomeUnknown' : null,
          instructionOutcomes: state.receipt === 'missing-outcomes' ? [] : state.receipt === 'extra-outcomes' ? [{ index: '0', ok: true }, { index: '1', ok: true }] : [{ index: '0', ok: state.receipt !== 'failed' }] } };
    },
    async request(path, options) {
      assert.equal(path, '/submit'); assert.equal(options.method, 'POST');
      const pending = getPending(address, { storage });
      assert.equal(pending.signature, state.latestSig, 'The original ID must be durable before any write');
      const signature = base58Encode(fromBase64(options.body.tx_base64).subarray(1, 65));
      assert.equal(signature, pending.signature);
      state.sends++;
      if (state.mode === 'response-lost') throw new Error('Connection lost');
      if (state.mode === 'wrong-ack') return { status: 'queued', signature: 'wrong-id' };
      if (state.mode === 'rejected') return { error: 'Rejected' };
      return { status: 'queued', signature };
    },
  };
  return { state, options, provider, storage, held, run: intent => execute({ provider, address, ...buyIntent, ...intent }, options) };
}

test('legacy buy bytes exactly match the separately verified live pilot public fixture', () => {
  const tx = buildUnsignedTransaction({ address: 'BpPwVSjQB6PDmki3bx15REAifJpQRuxWmCwBzpZaQgZT',
    blockhash: '0e5b6cbc7efc6443ae36104527d8dea017c7b0fada260a807c7291b769eae186', ...buyIntent });
  assert.equal(toBase64(tx.message), 'AQABAqC5GuTPABG44gypjuDin/KHPbI9OUxAQrEarGXyraK+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOW2y8fvxkQ642EEUn2N6gF8ew+tomCoB8cpG3aerhhgEBAQBlBAAAABQAAAAAAAAAeHJzLXh1c2RjLW1haW5uZXQtdjELAAAAAAAAAHN3YXBfYl90b19hKgAAAAAAAAB7ImFtb3VudCI6NTAwMDAwLCJtaW5fb3V0cHV0IjozMzIxNjg0MjkzOH0=');
  assert.equal(tx.wire[0], 1); assert.ok(tx.wire.subarray(1, 65).every(value => value === 0));
});
test('wrap and unwrap encode u64 max exactly and distinct variants', () => {
  for (const [action, variant] of [['wrap', 13], ['unwrap', 14]]) {
    const bytes = instructionFor({ action, amountRaw: (1n << 64n) - 1n });
    assert.deepEqual([...bytes], [variant, 0, 0, 0, ...Array(8).fill(255)]);
  }
  assert.match(new TextDecoder().decode(instructionFor({ action: 'sell', amountRaw: 33383761746n, minOutputRaw: 494660n })), /swap_a_to_a|swap_a_to_b/);
});
test('base58 preserves leading zeros and rejects noncanonical or wrong-length input', () => {
  assert.equal(base58Encode(base58Decode('1'.repeat(32))), '1'.repeat(32));
  assert.equal(base58Encode(base58Decode(address)), address);
  for (const value of ['O', '0'.repeat(32), '1'.repeat(33), '1'.repeat(31)]) assert.throws(() => base58Decode(value));
  for (const value of [1.5, 9007199254740992, -1n, '01', '1e6']) assert.throws(() => unsigned(value));
});
test('wallet signature is verified over the exact reviewed message', async () => {
  const h = harness();
  const tx = buildUnsignedTransaction({ address, blockhash, ...buyIntent });
  const signed = await h.provider.signTransaction(tx.base64);
  assert.equal((await verifySignedTransaction(signed, tx, address, webcrypto)).signature, signed.signature);
  await assert.rejects(verifySignedTransaction({ ...signed, signature: 'other' }, tx, address, webcrypto));
});
test('successful action writes once, requires exact receipt, and retains public history', async () => {
  const h = harness(); const result = await h.run();
  assert.equal(result.status, 'confirmed'); assert.equal(h.state.sends, 1); assert.equal(h.state.signs, 1);
  assert.equal(getPending(address, h.options), null);
  assert.equal(getHistory(address, h.options)[0].signature, result.signature);
});
for (const mode of ['response-lost', 'wrong-ack', 'rejected']) test(`${mode} preserves a pending ID across reload and never resubmits`, async () => {
  const h = harness(); h.state.mode = mode;
  const result = await h.run(); assert.equal(result.status, 'pending');
  assert.equal(getPending(address, { storage: h.storage }).signature, result.signature);
  await assert.rejects(h.run(), /unresolved/); assert.equal(h.state.sends, 1); assert.equal(h.state.signs, 1);
  h.state.mode = 'success';
  const checked = await reconcile(address, h.options);
  assert.equal(checked.status, 'confirmed'); assert.equal(checked.signature, result.signature); assert.equal(h.state.sends, 1);
});
for (const receipt of ['pending', 'unknown', 'missing-outcomes', 'extra-outcomes', 'wrong-id', 'unavailable']) test(`${receipt} receipt never becomes a successful action`, async () => {
  const h = harness(); h.state.receipt = receipt;
  const result = await h.run(); assert.equal(result.status, 'pending'); assert.equal(h.state.sends, 1);
  assert.equal((await reconcile(address, h.options)).status, 'pending'); assert.equal(h.state.sends, 1);
});
test('confirmed execution failure is distinct from pending and success', async () => {
  const h = harness(); h.state.receipt = 'failed';
  assert.equal((await h.run()).status, 'failed');
});
for (const failure of ['fail', 'discard']) test(`storage ${failure} stops before signing or sending`, async () => {
  const h = harness(); h.storage[failure] = true;
  await assert.rejects(h.run()); assert.equal(h.state.signs, 0); assert.equal(h.state.sends, 0);
});
test('storage failure after wallet approval still blocks submission', async () => {
  const h = harness(); const original = h.provider.signTransaction;
  h.provider.signTransaction = async value => { const signed = await original(value); h.storage.fail = true; return signed; };
  await assert.rejects(h.run()); assert.equal(h.state.signs, 1); assert.equal(h.state.sends, 0);
});
test('unavailable Web Locks and another locked tab stop before wallet prompts', async () => {
  const h = harness();
  await assert.rejects(execute({ provider: h.provider, address, ...buyIntent }, { ...h.options, locks: null }), /lock transactions/);
  h.options.locks = { async request(_name, _opts, callback) { return callback(null); } };
  await assert.rejects(h.run(), /Another tab/); assert.equal(h.state.signs, 0);
});
for (const mode of ['wrong-network', 'changed-wallet', 'wrong-chain', 'price-moved']) test(`${mode} stops before signing`, async () => {
  const h = harness(); h.state.mode = mode;
  await assert.rejects(h.run()); assert.equal(h.state.signs, 0); assert.equal(h.state.sends, 0);
});
for (const mode of ['changed-message', 'bad-signature', 'switch-during-approval', 'expired-during-approval', 'expired-blockhash']) test(`${mode} stops before network submission`, async () => {
  const h = harness(); h.state.mode = mode;
  await assert.rejects(h.run()); assert.equal(h.state.signs, 1); assert.equal(h.state.sends, 0);
});
test('future/stale quotes, zero, and weakened slippage bounds fail before signing', async () => {
  const h = harness();
  for (const invalid of [{ quoteTime: now + 1 }, { quoteTime: now - 30001 }, { amountRaw: 0n }, { minOutputRaw: 33216842937n }]) await assert.rejects(h.run(invalid));
  assert.equal(h.state.signs, 0);
});
test('native gas, spendable wrapped tokens, and xUSDC are checked separately', async () => {
  const h = harness(); h.state.native = 999999n;
  await assert.rejects(h.run(), /native XRS/);
  h.state.native = 10000000000n; h.state.usdc = 1n;
  await assert.rejects(h.run(), /xUSDC balance/);
  h.state.wrapped = 1n;
  await assert.rejects(h.run({ action: 'sell' }), /wrapped XRS/);
  assert.equal(h.state.signs, 0);
});
test('wrap leaves enough native XRS for its separate transaction fee', async () => {
  const h = harness();
  const intent = { action: 'wrap', amountRaw: 10000000n, minOutputRaw: null, expectedOutputRaw: null, quoteTime: null };
  await assert.rejects(h.run(intent), /native XRS/);
  h.state.native = 11000000n;
  assert.equal((await h.run(intent)).status, 'confirmed');
});
test('corrupt or mismatched public journal blocks new actions', async () => {
  const h = harness(); await h.run();
  const key = [...h.storage.data.keys()][0]; const raw = h.storage.getItem(key);
  h.storage.setItem(key, '{broken'); await assert.rejects(h.run(), /journal/);
  const body = JSON.parse(raw); body.attempts[0].inputRaw = '7'; h.storage.setItem(key, JSON.stringify(body));
  await assert.rejects(h.run(), /intent does not match/); assert.equal(h.state.sends, 1);
});
test('ordinary Solana providers are rejected even when a public key is supplied', async () => {
  const h = harness(); delete h.provider.isXerisWallet;
  await assert.rejects(checkProvider(h.provider, address), /Xeris wallet/);
});
test('UI status listener errors cannot lose a durable pending ID or change send count', async () => {
  const h = harness(); h.options.onStatus = () => { throw new Error('UI gone'); };
  assert.equal((await h.run()).status, 'confirmed'); assert.equal(h.state.sends, 1);
});

for (const [name, error, outcomes] of [
  ['unknown string', 'AnUnknownFutureError', [{ index: '0', ok: false }]],
  ['unknown object', { NetworkError: 'unavailable' }, [{ index: '0', ok: false }]],
  ['known error without outcomes', 'ExecutionFailed', []],
  ['known error with successful outcome', 'ExecutionFailed', [{ index: '0', ok: true }]],
  ['wrong instruction index', { InstructionError: ['1', 'ExecutionFailed'] }, [{ index: '0', ok: false }]],
  ['extra error fields', { InstructionError: ['0', 'ExecutionFailed'], extra: true }, [{ index: '0', ok: false }]],
  ['null error with failed outcome', null, [{ index: '0', ok: false }]],
]) test(`${name} remains pending and blocks new sends`, async () => {
  const h = harness(); const original = h.options.rpc;
  h.options.rpc = async (method, params) => method === 'getTransaction'
    ? { slot: '110', transaction: { signatures: [h.state.latestSig] }, meta: { err: error, instructionOutcomes: outcomes } }
    : original(method, params);
  assert.equal((await h.run()).status, 'pending');
  await assert.rejects(h.run(), /unresolved/);
  assert.equal(h.state.sends, 1);
});

test('known ExecutionFailed plus the single explicit failed outcome resolves failure', async () => {
  const h = harness(); const original = h.options.rpc;
  h.options.rpc = async (method, params) => method === 'getTransaction'
    ? { slot: '110', transaction: { signatures: [h.state.latestSig] }, meta: { err: 'ExecutionFailed', instructionOutcomes: [{ index: '0', ok: false }] } }
    : original(method, params);
  assert.equal((await h.run()).status, 'failed');
});
