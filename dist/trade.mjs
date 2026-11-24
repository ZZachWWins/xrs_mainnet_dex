import { CONFIG, rpc, request, checkChain, getQuote, getBalances } from './model.mjs';

const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const MAX_U64 = (1n << 64n) - 1n;
const encoder = new TextEncoder();
const ACTIONS = new Set(['buy', 'sell', 'wrap', 'unwrap']);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const concat = (...parts) => Uint8Array.from(parts.flatMap(part => Array.from(part)));
const fail = message => { throw new Error(message); };

export function unsigned(value, label = 'Amount') {
  if (typeof value !== 'bigint' && typeof value !== 'string' && !(typeof value === 'number' && Number.isSafeInteger(value))) fail(`${label} must be an exact integer.`);
  if (!/^(0|[1-9][0-9]*)$/.test(String(value))) fail(`${label} must be an unsigned integer.`);
  const result = BigInt(value);
  if (result > MAX_U64) fail(`${label} exceeds the supported range.`);
  return result;
}

export function base58Encode(bytes) {
  let value = 0n;
  for (const byte of bytes) value = (value << 8n) | BigInt(byte);
  let result = '';
  while (value > 0n) { result = ALPHABET[Number(value % 58n)] + result; value /= 58n; }
  for (const byte of bytes) { if (byte !== 0) break; result = `1${result}`; }
  return result;
}

export function base58Decode(text, expectedLength = 32) {
  if (typeof text !== 'string' || text.length < 1 || text.length > 128) fail('Invalid base58 value.');
  let value = 0n;
  for (const char of text) {
    const digit = ALPHABET.indexOf(char);
    if (digit < 0) fail('Invalid base58 value.');
    value = value * 58n + BigInt(digit);
  }
  const bytes = [];
  while (value > 0n) { bytes.unshift(Number(value & 255n)); value >>= 8n; }
  for (const char of text) { if (char !== '1') break; bytes.unshift(0); }
  const result = Uint8Array.from(bytes);
  if (result.length !== expectedLength || base58Encode(result) !== text) fail('Unexpected public-key or signature length.');
  return result;
}

function u64(value) {
  let remaining = unsigned(value);
  const bytes = new Uint8Array(8);
  for (let index = 0; index < 8; index++) { bytes[index] = Number(remaining & 255n); remaining >>= 8n; }
  return bytes;
}
function u32(value) { return Uint8Array.of(value & 255, value >>> 8 & 255, value >>> 16 & 255, value >>> 24 & 255); }
function shortvec(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 65535) fail('Invalid compact length.');
  const bytes = [];
  do { let byte = value & 127; value >>>= 7; if (value > 0) byte |= 128; bytes.push(byte); } while (value > 0);
  return Uint8Array.from(bytes);
}
function bincodeBytes(bytes) { return concat(u64(bytes.length), bytes); }
function bincodeString(text) { return bincodeBytes(encoder.encode(text)); }
function hexBytes(text) {
  if (typeof text !== 'string' || !/^[0-9a-f]{64}$/i.test(text)) fail('The node did not return a valid blockhash.');
  return Uint8Array.from(text.match(/../g), value => parseInt(value, 16));
}
function sameBytes(a, b) { return a.length === b.length && a.every((value, index) => value === b[index]); }
export function toBase64(bytes) { return btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join('')); }
export function fromBase64(text) {
  if (typeof text !== 'string' || text.length > 8192 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text)) fail('The wallet returned an invalid transaction.');
  const bytes = Uint8Array.from(atob(text), char => char.charCodeAt(0));
  if (toBase64(bytes) !== text) fail('The wallet returned a noncanonical transaction.');
  return bytes;
}

export function instructionFor({ action, amountRaw, minOutputRaw }) {
  if (!ACTIONS.has(action)) fail('Unsupported action.');
  const amount = unsigned(amountRaw);
  if (amount === 0n) fail('Enter a positive amount.');
  if (action === 'wrap' || action === 'unwrap') return concat(u32(action === 'wrap' ? 13 : 14), u64(amount));
  const minimum = unsigned(minOutputRaw, 'Minimum output');
  if (minimum === 0n) fail('A positive minimum output is required.');
  const args = encoder.encode(`{"amount":${amount},"min_output":${minimum}}`);
  return concat(u32(4), bincodeString(CONFIG.poolId), bincodeString(action === 'buy' ? 'swap_b_to_a' : 'swap_a_to_b'), bincodeBytes(args));
}

/** One legacy transaction, one signer, one instruction, fixed system program. */
export function buildUnsignedTransaction({ address, blockhash, ...intent }) {
  const publicKey = base58Decode(address);
  if (publicKey.every(value => value === 0)) fail('The system program cannot be a wallet.');
  const data = instructionFor(intent);
  const message = concat(Uint8Array.of(1, 0, 1), shortvec(2), publicKey, new Uint8Array(32), hexBytes(blockhash),
    shortvec(1), Uint8Array.of(1), shortvec(1), Uint8Array.of(0), shortvec(data.length), data);
  const wire = concat(Uint8Array.of(1), new Uint8Array(64), message);
  return { message, wire, base64: toBase64(wire) };
}

export async function verifySignedTransaction(result, unsignedTransaction, address, cryptoImpl = globalThis.crypto) {
  const wire = fromBase64(result?.signedTransaction);
  if (wire.length !== unsignedTransaction.wire.length || wire[0] !== 1 || !sameBytes(wire.slice(65), unsignedTransaction.message)) {
    fail('The wallet changed the reviewed transaction. Nothing was submitted.');
  }
  const signatureBytes = wire.slice(1, 65);
  if (signatureBytes.every(value => value === 0)) fail('The wallet did not sign the transaction.');
  const signature = base58Encode(signatureBytes);
  if (result.signature !== signature) fail('The wallet signature does not match its signed transaction.');
  if (!cryptoImpl?.subtle) fail('This browser cannot verify the wallet signature.');
  let verified;
  try {
    const publicKey = await cryptoImpl.subtle.importKey('raw', base58Decode(address), { name: 'Ed25519' }, false, ['verify']);
    verified = await cryptoImpl.subtle.verify('Ed25519', publicKey, signatureBytes, unsignedTransaction.message);
  } catch { fail('This browser does not support Ed25519 signature verification.'); }
  if (!verified) fail('The wallet signature is invalid. Nothing was submitted.');
  return { signature, signedTransaction: result.signedTransaction, wire };
}

function deps(options = {}) {
  const result = {
    rpc, request, checkChain, getQuote, getBalances,
    storage: globalThis.localStorage, locks: globalThis.navigator?.locks,
    crypto: globalThis.crypto, now: Date.now, onStatus: () => {}, ...options,
  };
  const listener = result.onStatus;
  result.onStatus = text => { try { listener(text); } catch {} };
  return result;
}
function journalKey(address) {
  base58Decode(address);
  return `xeris-dex:mainnet-v4:trade-journal:v1:${address}`;
}
function validateAttempt(attempt, address) {
  if (!attempt || attempt.address !== address || attempt.chainId !== CONFIG.chainId || attempt.poolId !== CONFIG.poolId ||
      !ACTIONS.has(attempt.action) || !['pending', 'confirmed', 'failed'].includes(attempt.status) ||
      !Number.isSafeInteger(attempt.time) || typeof attempt.messageBase64 !== 'string') fail('The saved trade journal is invalid. Trading is blocked.');
  base58Decode(attempt.signature, 64);
  if (unsigned(attempt.inputRaw) === 0n) fail('The saved trade amount is invalid.');
  if (attempt.action === 'buy' || attempt.action === 'sell') {
    if (unsigned(attempt.minOutputRaw) === 0n || unsigned(attempt.minOutputRaw) > unsigned(attempt.expectedOutputRaw)) fail('The saved swap bounds are invalid.');
  }
  const message = fromBase64(attempt.messageBase64);
  if (message.length < 104) fail('The saved transaction message is invalid.');
  const blockhash = Array.from(message.slice(68, 100), byte => byte.toString(16).padStart(2, '0')).join('');
  const expected = buildUnsignedTransaction({ address, blockhash, action: attempt.action, amountRaw: attempt.inputRaw, minOutputRaw: attempt.minOutputRaw });
  if (!sameBytes(message, expected.message)) fail('The saved transaction intent does not match its original message.');
}
function readJournal(address, storage) {
  if (!storage || typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') fail('Local transaction storage is unavailable. Trading is blocked.');
  const raw = storage.getItem(journalKey(address));
  if (raw === null) return [];
  let body;
  try { body = JSON.parse(raw); } catch { fail('The saved trade journal cannot be read. Trading is blocked.'); }
  if (body?.version !== 1 || body.address !== address || body.chainId !== CONFIG.chainId || !Array.isArray(body.attempts)) fail('The saved trade journal binding is invalid. Trading is blocked.');
  const seen = new Set();
  for (const attempt of body.attempts) {
    validateAttempt(attempt, address);
    if (seen.has(attempt.signature)) fail('Duplicate transaction IDs in the saved journal.');
    seen.add(attempt.signature);
  }
  const pending = body.attempts.filter(value => value.status === 'pending');
  if (pending.length > 1 || (pending.length === 1 && pending[0] !== body.attempts.at(-1))) fail('The saved journal needs manual reconciliation.');
  return body.attempts;
}
function saveJournal(address, attempts, storage) {
  const key = journalKey(address);
  const value = JSON.stringify({ version: 1, chainId: CONFIG.chainId, address, attempts });
  storage.setItem(key, value);
  if (storage.getItem(key) !== value) fail('The transaction ID could not be saved. Nothing was submitted.');
}
export function getHistory(address, options = {}) { const d = deps(options); return readJournal(address, d.storage); }
export function getPending(address, options = {}) { return getHistory(address, options).find(value => value.status === 'pending') ?? null; }
async function locked(address, d, action) {
  if (typeof d.locks?.request !== 'function') fail('This browser cannot lock transactions across tabs. Trading is unavailable here.');
  return d.locks.request(journalKey(address), { mode: 'exclusive', ifAvailable: true }, async lock => {
    if (!lock) fail('Another tab is processing this wallet. Wait for it to finish.');
    return action();
  });
}

export async function checkProvider(provider, address) {
  if (!provider || (provider.isXerisWallet !== true && provider.isXeris !== true) ||
      typeof provider.connect !== 'function' || typeof provider.signTransaction !== 'function' || typeof provider.getRpcUrl !== 'function') {
    fail('Open this DEX in the Xeris wallet browser and connect your Xeris wallet.');
  }
  const connected = await provider.connect({ onlyIfTrusted: true });
  const currentAddress = connected?.publicKey?.toString?.() ?? connected?.toString?.();
  if (currentAddress !== address) fail('The active wallet changed. Connect and review this action again.');
  if (await provider.getRpcUrl() !== CONFIG.rpc) fail('Select Xeris mainnet in your wallet and reopen the DEX.');
  base58Decode(address);
}
function validateIntent(intent, now) {
  if (!ACTIONS.has(intent.action)) fail('Unsupported action.');
  if (unsigned(intent.amountRaw) === 0n) fail('Enter a positive amount.');
  if (intent.action === 'buy' || intent.action === 'sell') {
    const output = unsigned(intent.expectedOutputRaw, 'Quoted output');
    const min = unsigned(intent.minOutputRaw, 'Minimum output');
    if (!output || !min || min > output || min < (output * 9950n + 9999n) / 10000n) fail('Review a quote with the 0.5% minimum output before trading.');
    if (!Number.isSafeInteger(intent.quoteTime) || intent.quoteTime > now || now - intent.quoteTime > 30000) fail('This quote expired. Refresh it and review the new amount.');
  } else if (intent.minOutputRaw != null || intent.expectedOutputRaw != null || intent.quoteTime != null) fail('Wrap and unwrap require an exact 1:1 amount without a swap quote.');
}
function checkFunds(intent, balances) {
  const amount = unsigned(intent.amountRaw);
  const nativeNeeded = CONFIG.feeRaw + (intent.action === 'wrap' ? amount : 0n);
  if (unsigned(balances.nativeRaw) < nativeNeeded) fail('Keep 0.001 native XRS for this network fee, in addition to any XRS being wrapped.');
  if (intent.action === 'buy' && unsigned(balances.usdcRaw) < amount) fail('Insufficient xUSDC balance.');
  if (['sell', 'unwrap'].includes(intent.action) && unsigned(balances.wrappedRaw) < amount) fail('Insufficient wrapped XRS balance.');
}
function classifyReceipt(raw, signature) {
  if (!raw || typeof raw !== 'object') return null;
  const signatures = raw.transaction?.signatures;
  if (!Array.isArray(signatures) || signatures.length !== 1 || signatures[0] !== signature || !raw.meta || !own(raw.meta, 'err')) return null;
  let slot;
  try { slot = unsigned(raw.slot, 'Receipt slot'); } catch { return null; }
  if (slot === 0n) return null;
  const outcomes = raw.meta.instructionOutcomes;
  if (!Array.isArray(outcomes) || outcomes.length !== 1 || String(outcomes[0]?.index) !== '0') return null;
  if (raw.meta.err !== null) {
    const error = raw.meta.err;
    const instructionError = error && typeof error === 'object' && !Array.isArray(error) &&
      Object.keys(error).length === 1 && own(error, 'InstructionError') ? error.InstructionError : null;
    const knownFailure = error === 'ExecutionFailed' || (Array.isArray(instructionError) &&
      instructionError.length === 2 && String(instructionError[0]) === '0' && instructionError[1] === 'ExecutionFailed');
    // Only the node's known failure shape AND its matching explicit false
    // instruction outcome resolve a pending action as failed. Missing receipts,
    // unknown errors and contradictory outcomes never permit another send.
    if (!knownFailure || outcomes[0]?.ok !== false) return null;
    return { status: 'failed', slot: String(slot), detail: 'The node reports execution failure. Check balances before another action.' };
  }
  if (outcomes[0]?.ok !== true) return null;
  return { status: 'confirmed', slot: String(slot), detail: 'This action has a successful execution receipt.' };
}
async function reconcileLocked(address, attempts, d) {
  const pending = attempts.find(value => value.status === 'pending');
  if (!pending) return attempts.at(-1) ?? null;
  try {
    await d.checkChain();
    const receipt = await d.rpc('getTransaction', [pending.signature]);
    const result = classifyReceipt(receipt, pending.signature);
    if (!result) return pending;
    const checked = { ...pending, ...result };
    saveJournal(address, attempts.map(value => value.signature === pending.signature ? checked : value), d.storage);
    return checked;
  } catch { return pending; }
}
/** Reconciles one saved ID without connecting, signing, replacing or sending. */
export async function reconcile(address, options = {}) {
  const d = deps(options);
  return locked(address, d, () => reconcileLocked(address, readJournal(address, d.storage), d));
}

/** User-confirmed single action. The only write is a single POST /submit. */
export async function execute({ provider, address, ...intent }, options = {}) {
  const d = deps(options);
  return locked(address, d, async () => {
    validateIntent(intent, d.now());
    const attempts = readJournal(address, d.storage);
    if (attempts.some(value => value.status === 'pending')) fail('An earlier transaction is unresolved. Check its original ID before another action.');
    // Check that the exact public namespace is writable before a wallet prompt.
    saveJournal(address, attempts, d.storage);
    if (!d.crypto?.subtle) fail('This browser cannot verify wallet signatures.');
    d.onStatus('Checking wallet, mainnet and balances…');
    await checkProvider(provider, address);
    await d.checkChain();
    checkFunds(intent, await d.getBalances(address));
    if (intent.action === 'buy' || intent.action === 'sell') {
      const fresh = await d.getQuote(intent.action === 'buy', unsigned(intent.amountRaw));
      if (fresh.buying !== (intent.action === 'buy') || unsigned(fresh.inputRaw) !== unsigned(intent.amountRaw) || unsigned(fresh.outputRaw) < unsigned(intent.minOutputRaw)) {
        fail('The price moved outside your minimum output. Refresh and review the quote.');
      }
    }
    validateIntent(intent, d.now());
    const latest = await d.rpc('getLatestBlockhash', []);
    const blockhash = latest?.value?.blockhash;
    const lastValidSlot = unsigned(latest?.value?.lastValidBlockHeight, 'Blockhash expiry');
    const unsignedTransaction = buildUnsignedTransaction({ address, blockhash, ...intent });
    await checkProvider(provider, address);
    d.onStatus('Review this single action in your wallet…');
    const signed = await verifySignedTransaction(await provider.signTransaction(unsignedTransaction.base64), unsignedTransaction, address, d.crypto);
    await checkProvider(provider, address);
    validateIntent(intent, d.now());
    const currentSlot = unsigned(await d.rpc('getSlot', []), 'Current slot');
    if (currentSlot > lastValidSlot) fail('The transaction blockhash expired before submission. Review a fresh action.');
    if (attempts.some(value => value.signature === signed.signature)) fail('This signed transaction already exists in your history.');
    const pending = {
      signature: signed.signature, address, chainId: CONFIG.chainId, poolId: CONFIG.poolId,
      action: intent.action, inputRaw: String(intent.amountRaw), status: 'pending', time: d.now(),
      minOutputRaw: intent.minOutputRaw == null ? null : String(intent.minOutputRaw),
      expectedOutputRaw: intent.expectedOutputRaw == null ? null : String(intent.expectedOutputRaw),
      messageBase64: toBase64(unsignedTransaction.message),
      detail: 'Signed transaction saved. Check this original ID before another action.',
    };
    // This durable write + read-back MUST finish before the network write.
    saveJournal(address, [...attempts, pending], d.storage);
    d.onStatus('Submitting once. Your transaction ID is saved…');
    try {
      const result = await d.request('/submit', { method: 'POST', body: { tx_base64: signed.signedTransaction } });
      if (!result || !['ok', 'queued'].includes(result.status) ||
          (result.signature != null && result.signature !== signed.signature) ||
          (result.hash != null && result.hash !== signed.signature)) return pending;
    } catch { return pending; }
    d.onStatus('Checking the original transaction ID…');
    return reconcileLocked(address, [...attempts, pending], d);
  });
}
