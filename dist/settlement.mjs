import { CONFIG } from './model.mjs';
import { base58Decode, verifySignedTransaction } from './trade.mjs';
import {reviewPreparedSource} from './bridge-source.mjs';

// No wallet, signing, transaction construction or payment APIs are used here.
export const SETTLEMENT_ORIGIN = 'https://mainnetrpc.xerisweb.com';
export const SETTLEMENT_AUDIENCE = `${SETTLEMENT_ORIGIN}/settlement/v1`;
export const SETTLEMENT_DOMAIN = 'xeris-mainnet-settlement-intent-v2';
export const SETTLEMENT_NETWORK = Object.freeze({
  solana_genesis: '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d',
  xeris_chain_id: CONFIG.chainId, usdc_mint: CONFIG.solanaMint,
  reserve_token_account: CONFIG.reserve, xeris_token_id: CONFIG.usdc,
  wrapped_token_id: CONFIG.wrapped, pool_id: CONFIG.poolId,
});
const U64 = (1n << 64n) - 1n;
const ASSETS = Object.freeze({ solana_usdc: 6, native_xrs: 9, xusdc: 6 });
export const SETTLEMENT_ROUTES = Object.freeze({
  solana_usdc_to_native_xrs: Object.freeze({ sourceChain: 'solana-mainnet', destinationChain: CONFIG.chainId,
    inputAsset: 'solana_usdc', outputAsset: 'native_xrs' }),
  native_xrs_to_solana_usdc: Object.freeze({ sourceChain: CONFIG.chainId, destinationChain: 'solana-mainnet',
    inputAsset: 'native_xrs', outputAsset: 'solana_usdc' }),
  xusdc_to_solana_usdc: Object.freeze({ sourceChain: CONFIG.chainId, destinationChain: 'solana-mainnet',
    inputAsset: 'xusdc', outputAsset: 'solana_usdc' }),
});
const ACTION_ROUTES = Object.freeze({ buy: 'solana_usdc_to_native_xrs', redeem: 'xusdc_to_solana_usdc', sell: 'native_xrs_to_solana_usdc' });
const INTENT_KEYS = ['schema_version', 'domain', 'audience', 'action', 'route', 'nonce', 'created_at', 'expires_at',
  'network', 'amount_raw', 'min_output_raw', 'source_owner', 'destination_owner', 'solana_token_account', 'quote_id', 'client_request_id'];
const encoder = new TextEncoder();
const fail = message => { throw new Error(message); };
function keys(value, expected) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join('\0') !== [...expected].sort().join('\0')) fail('Unexpected settlement response shape.');
}
function id(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) fail('Invalid settlement request or quote ID.');
  return value;
}
function actionRoute(action, route) {
  if (!Object.prototype.hasOwnProperty.call(ACTION_ROUTES, action) || ACTION_ROUTES[action] !== route) fail('Settlement action and route do not match.');
}
function network(value) {
  keys(value, Object.keys(SETTLEMENT_NETWORK));
  for (const key of Object.keys(SETTLEMENT_NETWORK)) if (value[key] !== SETTLEMENT_NETWORK[key]) fail('Settlement mainnet identity changed.');
}
function timestamp(value) {
  if (!Number.isSafeInteger(value) || value < 0) fail('Invalid settlement timestamp.');
}
function dates(value, now, allowExpired = false) {
  timestamp(value.created_at); timestamp(value.expires_at);
  if (value.expires_at <= value.created_at || (!allowExpired && (value.created_at > now + 5 || value.expires_at <= now))) {
    fail('Settlement authorization or quote has expired.');
  }
}

/** JSON order is recursive lexical key order; v2 values are strictly ASCII. */
export function canonicalSettlementJSON(value) {
  if (value === null || typeof value === 'boolean' || (typeof value === 'number' && Number.isSafeInteger(value))) return JSON.stringify(value);
  if (typeof value === 'string') {
    if (!/^[\x20-\x7e]*$/.test(value)) fail('Settlement intent values must be printable ASCII.');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalSettlementJSON).join(',')}]`;
  if (value && typeof value === 'object' && [Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    return `{${Object.keys(value).sort().map(key => `${canonicalSettlementJSON(key)}:${canonicalSettlementJSON(value[key])}`).join(',')}}`;
  }
  fail('Settlement intent cannot be encoded canonically.');
}

export function validateSettlementIntent(intent, { now = Math.floor(Date.now() / 1000), allowExpired = false } = {}) {
  keys(intent, INTENT_KEYS);
  if (intent.schema_version !== 2 || intent.domain !== SETTLEMENT_DOMAIN || intent.audience !== SETTLEMENT_AUDIENCE) fail('Settlement authorization domain changed.');
  actionRoute(intent.action, intent.route); network(intent.network);
  if (typeof intent.nonce !== 'string' || !/^[0-9a-f]{64}$/.test(intent.nonce)) fail('Invalid settlement challenge nonce.');
  dates(intent, now, allowExpired);
  rawAmount(intent.amount_raw); rawAmount(intent.min_output_raw);
  for (const field of ['source_owner', 'destination_owner', 'solana_token_account']) settlementAddress(intent[field]);
  id(intent.quote_id); id(intent.client_request_id);
  canonicalSettlementJSON(intent);
  return intent;
}

export function settlementSigningMessage(intent, options) {
  validateSettlementIntent(intent, options);
  return `Xeris settlement ownership proof\n${SETTLEMENT_DOMAIN}\n${canonicalSettlementJSON(intent)}`;
}

/** Verify caller-supplied proof only; this function never asks a wallet to sign. */
export async function verifySettlementProof(intent, signature, { crypto = globalThis.crypto, now } = {}) {
  const message = settlementSigningMessage(intent, { now });
  const bytes = base58Decode(signature, 64);
  if (bytes.every(byte => byte === 0) || !crypto?.subtle) fail('A verifiable ownership proof is required.');
  let verified = false;
  try {
    const publicKey = await crypto.subtle.importKey('raw', base58Decode(intent.source_owner), { name: 'Ed25519' }, false, ['verify']);
    verified = await crypto.subtle.verify('Ed25519', publicKey, bytes, encoder.encode(message));
  } catch { fail('This browser cannot verify the settlement ownership proof.'); }
  if (!verified) fail('Settlement ownership proof does not match the reviewed source and destination.');
  return message;
}

function decimals(asset) {
  if (!Object.prototype.hasOwnProperty.call(ASSETS, asset)) throw new Error('Unsupported settlement asset.');
  return ASSETS[asset];
}
function rawAmount(value, positive = true) {
  if (typeof value !== 'string' || value.length > 20 || !/^(0|[1-9][0-9]*)$/.test(value)) {
    throw new Error('Settlement amounts must be canonical integer strings.');
  }
  const n = BigInt(value);
  if (n > U64 || (positive && n === 0n)) throw new Error('Settlement amount is outside the supported range.');
  return n;
}

/** Human decimal text to exact u64 raw units; no Number, exponent or rounding. */
export function parseSettlementAmount(text, asset) {
  const precision = decimals(asset);
  if (typeof text !== 'string' || text.length > 80) throw new Error('Enter a positive decimal amount.');
  const clean = text.trim();
  if (!/^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(clean)) throw new Error('Enter a positive decimal amount using a decimal point.');
  const [whole, fraction = ''] = clean.split('.');
  if (fraction.length > precision) throw new Error(`Use at most ${precision} decimal places.`);
  const value = BigInt(whole) * 10n ** BigInt(precision) + BigInt(fraction.padEnd(precision, '0') || '0');
  if (value < 1n || value > U64) throw new Error('Settlement amount is outside the supported range.');
  return value.toString();
}

export function formatSettlementAmount(raw, asset) {
  const precision = decimals(asset), value = rawAmount(raw, false), base = 10n ** BigInt(precision);
  const fraction = (value % base).toString().padStart(precision, '0').replace(/0+$/, '');
  return `${value / base}${fraction ? `.${fraction}` : ''}`;
}

/** Canonical public account format only; this does not establish ownership. */
export function settlementAddress(value) {
  if (typeof value !== 'string' || value !== value.trim()) throw new Error('Enter a canonical public account address.');
  const bytes = base58Decode(value, 32);
  if (bytes.every(byte => byte === 0)) throw new Error('The system address is not a settlement recipient.');
  return value;
}

export function validateSettlementBounds({ inputRaw, expectedOutputRaw, minimumOutputRaw, maximumInputRaw }) {
  const input = rawAmount(inputRaw), expected = rawAmount(expectedOutputRaw), minimum = rawAmount(minimumOutputRaw);
  const maximum = rawAmount(maximumInputRaw);
  if (minimum > expected || input > maximum) throw new Error('Settlement quote bounds do not match the reviewed amounts.');
  return Object.freeze({ inputRaw: input.toString(), expectedOutputRaw: expected.toString(),
    minimumOutputRaw: minimum.toString(), maximumInputRaw: maximum.toString() });
}

const QUOTE_BINDINGS = ['client_request_id', 'action', 'route', 'amount_raw', 'source_owner', 'destination_owner', 'solana_token_account'];
const QUOTE_KEYS = ['schema_version', 'quote_id', ...QUOTE_BINDINGS, 'network', 'min_output_raw', 'expected_output_raw', 'created_at', 'expires_at'];
const STATUS_KEYS = ['client_request_id', 'quote_id', 'action', 'route', 'network', 'source_owner', 'destination_owner',
  'solana_token_account', 'amount_raw', 'min_output_raw', 'status', 'updated_at', 'source_txid', 'destination_txid',
  'actual_output_raw','refund_amount_raw','refund_asset'];
const SERVER_STATES = new Set(['awaiting_funding', 'processing', 'completed', 'failed', 'cancelled', 'manual_review', 'refunded']);
const LOCAL_STATES = new Set(['quoting', 'quote_unknown', 'quoted', 'challenged', 'submitting', 'submission_unknown', 'unresolved', ...SERVER_STATES]);
const JOURNAL = 'xeris-dex:mainnet-v4:settlement:v1:requests';
const PRIVATE_PREFIX = 'xeris-dex:mainnet-v4:settlement:v1:access:';
const SOURCE_PREFIX = 'xeris-dex:mainnet-v4:settlement:v1:source:';
const MAX_BODY = 64 * 1024;
const freeze = value => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
};
const snapshot = value => JSON.parse(JSON.stringify(value));
function bindings(value) {
  actionRoute(value.action, value.route); id(value.client_request_id); rawAmount(value.amount_raw);
  for (const field of ['source_owner', 'destination_owner', 'solana_token_account']) settlementAddress(value[field]);
}
function matches(left, right, fields) {
  for (const key of fields) if (left[key] !== right[key]) fail('Settlement response changed a reviewed identity, amount or request.');
}
function quoteValue(value, expected, now, allowExpired = false) {
  keys(value, QUOTE_KEYS);
  if (value.schema_version !== 1) fail('Unsupported settlement quote version.');
  bindings(value); network(value.network); dates(value, now, allowExpired); id(value.quote_id);
  if(value.expires_at-value.created_at>60)fail('The settlement quote acceptance window exceeds 60 seconds.');
  matches(value, expected, QUOTE_BINDINGS);
  const output = rawAmount(value.expected_output_raw), minimum = rawAmount(value.min_output_raw);
  if (minimum > output || minimum < (output * 9950n + 9999n) / 10000n) fail('The settlement quote exceeds the reviewed 0.5% output bounds.');
  if(value.action==='redeem'&&(value.expected_output_raw!==value.amount_raw||value.min_output_raw!==value.amount_raw))fail('Redemption must deliver the exact reviewed USDC amount without a token deduction.');
  return snapshot(value);
}
function statusValue(value, expected) {
  keys(value, STATUS_KEYS); network(value.network); bindings(value); id(value.quote_id); rawAmount(value.min_output_raw);
  matches(value, expected, [...QUOTE_BINDINGS, 'quote_id', 'min_output_raw']);
  timestamp(value.updated_at);
  if (typeof value.status !== 'string' || value.status.length > 64) fail('Invalid settlement status.');
  for (const key of ['source_txid', 'destination_txid']) {
    if (value[key] !== null) {const bytes=base58Decode(value[key],64);if(bytes.every(byte=>byte===0))fail('Invalid public settlement receipt ID.');}
  }
  const result = snapshot(value);
  if(value.actual_output_raw!==null)rawAmount(value.actual_output_raw);
  if(value.refund_amount_raw!==null)rawAmount(value.refund_amount_raw);
  if(value.refund_asset!==null&&value.refund_asset!=='solana_usdc')fail('Unknown verified refund asset.');
  if((value.refund_amount_raw===null)!==(value.refund_asset===null))fail('Incomplete verified refund amount.');
  if (!SERVER_STATES.has(result.status) || (result.status === 'completed' && (!result.source_txid || !result.destination_txid
    ||result.actual_output_raw===null||BigInt(result.actual_output_raw)<BigInt(result.min_output_raw)))
    ||(result.status==='completed'&&result.action==='redeem'&&result.actual_output_raw!==result.amount_raw)
    ||(result.status==='refunded'&&(!result.source_txid||!result.destination_txid||result.refund_amount_raw===null||BigInt(result.refund_amount_raw)>BigInt(result.amount_raw)))
    ||(result.status!=='completed'&&result.actual_output_raw!==null)||(result.status!=='refunded'&&result.refund_amount_raw!==null)) result.status = 'unresolved';
  return result;
}
function unresolved(record) {
  return ['submitting', 'submission_unknown', 'unresolved', 'awaiting_funding', 'processing', 'manual_review'].includes(record.status)
    || (record.status === 'failed' && record.request?.source_txid !== null);
}
function storageRead(storage, key) {
  try {
    if (!storage || typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') throw new Error();
    return storage.getItem(key);
  } catch { fail('Settlement storage is unavailable. No new request can be submitted.'); }
}
function storageWrite(storage, key, value) {
  try { storage.setItem(key, value); if (storage.getItem(key) !== value) throw new Error(); }
  catch { fail('Settlement recovery information could not be saved. No new request can be submitted.'); }
}

export class SettlementUnavailableError extends Error {
  constructor() { super('This settlement route is unavailable. No purchase or redemption request was sent.'); this.name = 'SettlementUnavailableError'; }
}

/** No I/O at construction. Every mutation is explicit and is never retried. */
export function createSettlementClient(options = {}) {
  const allowed = new Set(['fetcher', 'storage', 'credentialStorage', 'locks', 'crypto', 'now']);
  if (Object.keys(options).some(key => !allowed.has(key))) fail('Settlement client does not accept alternate API origins or unknown options.');
  const fetcher = options.fetcher ?? globalThis.fetch;
  const storage = options.storage ?? globalThis.localStorage;
  const credentials = options.credentialStorage ?? globalThis.localStorage;
  const locks = options.locks ?? globalThis.navigator?.locks;
  const crypto = options.crypto ?? globalThis.crypto;
  const clock = options.now ?? Date.now;
  const now = () => Math.floor(clock() / 1000);
  if (typeof fetcher !== 'function' || typeof clock !== 'function') fail('Settlement transport is unavailable.');

  async function http(path, { method = 'GET', body, token } = {}) {
    if (!/^\/(?:capabilities|quotes|challenges|requests(?:\/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?:\/(?:access|prepare-source|submit-source))?)?)$/.test(path)) fail('Invalid fixed-origin settlement path.');
    const controller = new AbortController(); let timer, reader;
    const timeout = new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new Error('Settlement deadline exceeded.'));},8000);});
    try { return await Promise.race([timeout,(async()=>{
      const headers = { Accept: 'application/json' };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (token !== undefined) {
        if (typeof token !== 'string' || !/^[0-9a-f]{64}$/.test(token)) fail('Invalid settlement status access token.');
        headers.Authorization = `Bearer ${token}`;
      }
      if (path === '/requests' && method === 'POST') headers['Idempotency-Key'] = body.intent.client_request_id;
      const response = await fetcher(SETTLEMENT_AUDIENCE + path, { method, headers, signal: controller.signal,
        redirect: 'error', cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!response?.ok) fail('Settlement service did not confirm this request.');
      const length = response.headers?.get?.('content-length');
      if (length !== null && length !== undefined && (!/^[0-9]+$/.test(length) || BigInt(length) > BigInt(MAX_BODY))) fail('Settlement response exceeds the supported limit.');
      if (!response.body || typeof response.body.getReader !== 'function') fail('Bounded settlement response streaming is unavailable.');
      reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: true });
      let text = '', total = 0;
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          if (!(chunk.value instanceof Uint8Array) || chunk.value.byteLength > MAX_BODY - total) fail('Settlement response exceeds the supported limit.');
          total += chunk.value.byteLength;
          text += decoder.decode(chunk.value, { stream: true });
        }
        text += decoder.decode();
      } finally { try { Promise.resolve(reader.cancel()).catch(()=>{}); } catch {} }
      return JSON.parse(text);
    })()]);
    } catch { fail('Settlement service response is unavailable or invalid. Recover the existing request before trying another.'); }
    finally { clearTimeout(timer); try { Promise.resolve(reader?.cancel()).catch(()=>{}); } catch {} }
  }

  function history() {
    const text = storageRead(storage, JOURNAL);
    if (text === null) return [];
    if (typeof text !== 'string' || text.length > 512 * 1024) fail('Settlement journal needs manual reconciliation.');
    let saved;
    try { saved = JSON.parse(text); } catch { fail('Settlement journal needs manual reconciliation.'); }
    keys(saved, ['version', 'audience', 'records']);
    if (saved.version !== 1 || saved.audience !== SETTLEMENT_AUDIENCE || !Array.isArray(saved.records) || saved.records.length > 100) fail('Settlement journal binding is invalid.');
    const seen = new Set();
    for (const record of saved.records) {
      keys(record, [...QUOTE_BINDINGS, 'status', 'quote', 'intent', 'request', 'created_at', 'updated_at']);
      bindings(record); timestamp(record.created_at); timestamp(record.updated_at);
      if (!LOCAL_STATES.has(record.status) || seen.has(record.client_request_id)) fail('Settlement journal is ambiguous.');
      seen.add(record.client_request_id);
      if (record.quote !== null) quoteValue(record.quote, record, now(), true);
      if (record.intent !== null) {
        validateSettlementIntent(record.intent, { now: now(), allowExpired: true });
        matches(record.intent, record, QUOTE_BINDINGS);
        if (!record.quote) fail('Settlement journal is missing its quote.');
        matches(record.intent, record.quote, ['quote_id', 'min_output_raw']);
      }
      if (record.request !== null) {
        if (!record.intent) fail('Settlement journal is missing its original intent.');
        if(statusValue(record.request, record.intent).status!==record.status)fail('Saved settlement status is inconsistent with its original receipt.');
        const original=sourceRecord(record.client_request_id);
        if(original?.signature&&record.request.source_txid!==null&&record.request.source_txid!==original.signature)fail('Saved settlement receipt differs from the original source signature.');
      }
    }
    return saved.records;
  }
  function save(records) {
    const text = JSON.stringify({ version: 1, audience: SETTLEMENT_AUDIENCE, records });
    if (records.length > 100 || text.length > 512 * 1024) fail('Settlement journal is full. Reconcile saved requests first.');
    storageWrite(storage, JOURNAL, text);
  }
  function find(records, requestId) {
    id(requestId);
    const result = records.find(record => record.client_request_id === requestId);
    if (!result) fail('The original public settlement request is not saved in this browser.');
    return result;
  }
  function access(requestId) {
    const text = storageRead(credentials, PRIVATE_PREFIX + id(requestId));
    if (text === null) return null;
    if (text.length > 16 * 1024) fail('Settlement status access information is invalid.');
    let value;
    try { value = JSON.parse(text); } catch { fail('Settlement status access information is invalid.'); }
    keys(value, ['intent', 'signature', 'status_token']);
    validateSettlementIntent(value.intent, { now: now(), allowExpired: true });
    if (value.intent.client_request_id !== requestId) fail('Settlement status access identity changed.');
    base58Decode(value.signature, 64);
    if (value.status_token !== null && !/^[0-9a-f]{64}$/.test(value.status_token)) fail('Settlement status access token is invalid.');
    return value;
  }
  function saveAccess(intent, signature, statusToken = null) {
    storageWrite(credentials, PRIVATE_PREFIX + intent.client_request_id, JSON.stringify({ intent, signature, status_token: statusToken }));
  }
  function sourceRecord(requestId) {
    const text=storageRead(storage,SOURCE_PREFIX+id(requestId));
    if(text===null)return null;
    if(text.length>32*1024)fail('Saved source payment needs manual reconciliation.');
    let value;try{value=JSON.parse(text);}catch{fail('Saved source payment needs manual reconciliation.');}
    keys(value,['version','client_request_id','source','signature','signed_transaction','state']);
    if(value.version!==1||value.client_request_id!==requestId||!['prepared','signed','submitting','submission_unknown','submitted','recovery_only'].includes(value.state))fail('Saved source payment binding is invalid.');
    if(value.state==='prepared'&&(value.signature!==null||value.signed_transaction!==null))fail('Unsigned preparation cannot contain a saved source signature.');
    if(value.state!=='prepared'){base58Decode(value.signature,64);if(typeof value.signed_transaction!=='string'||value.signed_transaction.length>8192)fail('Saved source payment is invalid.');}
    return value;
  }
  function saveSource(value){storageWrite(storage,SOURCE_PREFIX+id(value.client_request_id),JSON.stringify(value));}
  async function sourceValue(value,intent,allowExpired=false){
    keys(value,['client_request_id','intent_digest','chain','stage','signer','transaction_base64','message_base64','message_sha256','expires_at','fee_raw','fee_asset']);
    if(value.client_request_id!==intent.client_request_id||value.expires_at>intent.expires_at)fail('Prepared source request or acceptance window changed.');
    const message=settlementSigningMessage(intent,{now:now(),allowExpired:true});
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(message))),b=>b.toString(16).padStart(2,'0')).join('');
    if(value.intent_digest!==digest||value.fee_asset!==(intent.action==='buy'?'SOL':'XRS'))fail('Prepared source identity or fee asset changed.');
    rawAmount(value.fee_raw,false);
    if(intent.action==='redeem'&&value.fee_raw!=='1000000')fail('The Xeris burn fee changed.');
    await reviewPreparedSource(value,intent,message,{crypto,now:clock,allowExpired});
    return snapshot(value);
  }
  function sourceAccess(record){const saved=access(record.client_request_id);if(!saved?.status_token||!record.intent||canonicalSettlementJSON(saved.intent)!==canonicalSettlementJSON(record.intent))fail('Restore the original request status access before continuing.');return saved;}
  async function locked(action) {
    if (typeof locks?.request !== 'function') fail('This browser cannot lock settlement requests across tabs.');
    return locks.request(JOURNAL, { mode: 'exclusive', ifAvailable: true }, async lock => {
      if (!lock) fail('Another tab is processing settlement. Recover the existing request first.');
      return action();
    });
  }
  async function getCapabilities() {
    const value = await http('/capabilities');
    keys(value, ['schema_version', 'network', 'can_accept_funds', 'routes', 'issued_at', 'expires_at']);
    if (value.schema_version !== 1 || typeof value.can_accept_funds !== 'boolean') fail('Invalid settlement capabilities.');
    network(value.network); timestamp(value.issued_at); timestamp(value.expires_at);
    if (value.issued_at > now() + 5 || value.issued_at < now() - 30 || value.expires_at <= now() || value.expires_at <= value.issued_at) fail('Settlement availability is stale. Refresh before continuing.');
    keys(value.routes, Object.keys(SETTLEMENT_ROUTES));
    for (const route of Object.values(value.routes)) {
      keys(route, ['enabled']);
      if (typeof route.enabled !== 'boolean') fail('Invalid settlement route availability.');
    }
    return freeze(snapshot(value));
  }
  async function available(route) {
    const value = await getCapabilities();
    if (!value.can_accept_funds || value.routes[route]?.enabled !== true) throw new SettlementUnavailableError();
  }
  function newId() {
    if (typeof crypto?.getRandomValues !== 'function') fail('Secure public request IDs are unavailable.');
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return 'xrq_' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function applyStatus(record, result) {
    const original=sourceRecord(record.client_request_id);
    if(original?.signature&&result.source_txid!==null&&result.source_txid!==original.signature)fail('The service receipt does not match the original saved source transaction. Recover that original ID.');
    if(['completed','refunded'].includes(result.status)&&!original?.signature)result.status='unresolved';
    if (record.request && result.updated_at < record.request.updated_at) return record.request;
    if (record.request && ['completed', 'refunded', 'cancelled'].includes(record.request.status) && result.status !== record.request.status) result.status = 'unresolved';
    record.request = result; record.status = result.status; record.updated_at = now();
    return result;
  }
  return Object.freeze({
    getCapabilities,
    getSource(requestId){return freeze(snapshot(sourceRecord(requestId)));},
    getHistory(sourceOwner) {
      if (sourceOwner !== undefined) settlementAddress(sourceOwner);
      return freeze(snapshot(history().filter(value => sourceOwner === undefined || value.source_owner === sourceOwner)));
    },
    quote(input) {
      return locked(async () => {
        const request = snapshot(input);
        keys(request, ['action', 'amount_raw', 'source_owner', 'destination_owner', 'solana_token_account']);
        request.route = ACTION_ROUTES[request.action]; request.client_request_id = newId(); bindings(request);
        const records = history();
        if (records.some(value => value.source_owner === request.source_owner && (unresolved(value)
          || (!['completed','refunded'].includes(value.status)&&sourceRecord(value.client_request_id)?.signature)))) fail('Recover the unresolved settlement request before starting another.');
        await available(request.route);
        const record = { ...request, status: 'quoting', quote: null, intent: null, request: null, created_at: now(), updated_at: now() };
        records.push(record); save(records); // Durable public ID before any POST.
        try {
          const result = quoteValue(await http('/quotes', { method: 'POST', body: { ...request, slippage_bps: 50 } }), request, now());
          record.quote = result; record.status = 'quoted'; record.updated_at = now(); save(records);
          return freeze(snapshot(result));
        } catch (error) { record.status = 'quote_unknown'; save(records); throw error; }
      });
    },
    challenge(requestId) {
      return locked(async () => {
        const records = history(), record = find(records, requestId);
        if (record.status !== 'quoted' || !record.quote) fail('Obtain a current quote before requesting ownership authorization.');
        quoteValue(record.quote, record, now()); await available(record.route);
        const value = await http('/challenges', { method: 'POST', body: { quote_id: record.quote.quote_id, client_request_id: requestId } });
        keys(value, ['schema_version', 'intent', 'signing_message_utf8']);
        if (value.schema_version !== 1) fail('Invalid settlement challenge version.');
        validateSettlementIntent(value.intent, { now: now() });
        matches(value.intent, record.quote, [...QUOTE_BINDINGS, 'quote_id', 'min_output_raw']);
        if (value.intent.expires_at > record.quote.expires_at || value.signing_message_utf8 !== settlementSigningMessage(value.intent, { now: now() })) fail('The service changed the reviewed ownership message.');
        record.intent = snapshot(value.intent); record.status = 'challenged'; record.updated_at = now(); save(records);
        return freeze(snapshot(value));
      });
    },
    submit(intent, signature) {
      return locked(async () => {
        const safe = snapshot(intent), records = history(), record = find(records, safe.client_request_id);
        if (record.status !== 'challenged' || !record.intent || canonicalSettlementJSON(record.intent) !== canonicalSettlementJSON(safe)) fail('This request is already pending or differs from the reviewed challenge. Recover its existing ID.');
        await available(record.route);
        await verifySettlementProof(safe, signature, { crypto, now: now() });
        saveAccess(safe, signature);
        record.status = 'submitting'; record.updated_at = now(); save(records);
        try {
          const value = await http('/requests', { method: 'POST', body: { intent: safe, signature } });
          keys(value, ['schema_version', 'request', 'status_token']);
          if (value.schema_version !== 1 || typeof value.status_token !== 'string' || !/^[0-9a-f]{64}$/.test(value.status_token)) fail('Invalid settlement status authorization.');
          const result = statusValue(value.request, safe);
          saveAccess(safe, signature, value.status_token); applyStatus(record, result); save(records);
          return freeze(snapshot(result));
        } catch { record.request=null;record.status = 'submission_unknown'; record.updated_at = now(); save(records); fail('Settlement submission is unresolved. Recover this request ID; do not submit another payment or request.'); }
      });
    },
    recover(requestId) {
      return locked(async () => {
        const records = history(), record = find(records, requestId), saved = access(requestId);
        if (!record.intent || !saved?.status_token || canonicalSettlementJSON(saved.intent) !== canonicalSettlementJSON(record.intent)) fail('Restore read-only status access for this original request before recovery.');
        const value = await http('/requests/' + id(requestId), { token: saved.status_token });
        keys(value, ['schema_version', 'request']);
        if (value.schema_version !== 1) fail('Invalid settlement recovery version.');
        const result = applyStatus(record, statusValue(value.request, record.intent)); save(records);
        return freeze(snapshot(result));
      });
    },
    restoreAccess(intent, signature) {
      return locked(async () => {
        const safe = snapshot(intent), records = history(), record = find(records, safe.client_request_id);
        if (!record.intent || canonicalSettlementJSON(record.intent) !== canonicalSettlementJSON(safe)) fail('Only the original existing request can restore read-only access.');
        // Existing ownership is rechecked after intent expiry. This endpoint
        // cannot create a request, accept funds or change the original intent.
        await verifySettlementProof(safe, signature, { crypto, now: safe.created_at });
        const value = await http('/requests/' + id(safe.client_request_id) + '/access', { method: 'POST', body: { intent: safe, signature } });
        keys(value, ['schema_version', 'request', 'status_token']);
        if (value.schema_version !== 1 || typeof value.status_token !== 'string' || !/^[0-9a-f]{64}$/.test(value.status_token)) fail('Invalid read-only recovery authorization.');
        const result = statusValue(value.request, safe);
        saveAccess(safe, signature, value.status_token); applyStatus(record, result); save(records);
        return freeze(snapshot(result));
      });
    },
    restoreSavedAccess(requestId){const saved=access(requestId);if(!saved)fail('The original ownership proof is unavailable in this browser. Do not create a replacement funded request.');return this.restoreAccess(saved.intent,saved.signature);},
    prepareSource(requestId){return locked(async()=>{
      const records=history(),record=find(records,requestId),saved=sourceAccess(record),existing=sourceRecord(requestId);
      if(existing&&existing.state!=='prepared')fail('An original signed payment already exists. Recover its status; do not sign a replacement.');
      if(record.status!=='awaiting_funding'||!record.intent)fail('This request cannot accept a new source payment. Recover its status.');
      validateSettlementIntent(record.intent,{now:now()});await available(record.route);
      const reply=await http('/requests/'+id(requestId)+'/prepare-source',{method:'POST',body:{},token:saved.status_token});
      keys(reply,['schema_version','source']);if(reply.schema_version!==1)fail('Invalid source preparation version.');
      const source=await sourceValue(reply.source,record.intent);
      saveSource({version:1,client_request_id:requestId,source,signature:null,signed_transaction:null,state:'prepared'});
      return freeze(snapshot(source));
    });},
    saveSignedSource(requestId,signed){return locked(async()=>{
      const record=find(history(),requestId),saved=sourceRecord(requestId);
      if(!saved||saved.state!=='prepared'||record.status!=='awaiting_funding')fail('Recover the original payment before another signature.');
      const source=await sourceValue(saved.source,record.intent,true);
      const reviewed=await reviewPreparedSource(source,record.intent,settlementSigningMessage(record.intent,{now:now(),allowExpired:true}),{crypto,now:clock,allowExpired:true});
      const verified=await verifySignedTransaction(signed,reviewed,record.source_owner,crypto);
      saveSource({...saved,signature:verified.signature,signed_transaction:verified.signedTransaction,state:'signed'});
      return freeze({signature:verified.signature,state:'signed'});
    });},
    submitSource(requestId){return locked(async()=>{
      const records=history(),record=find(records,requestId),saved=sourceAccess(record),payment=sourceRecord(requestId);
      if(!payment||payment.state!=='signed'||record.status!=='awaiting_funding')fail('Payment submission is already pending or unavailable. Recover this original request.');
      await available(record.route);const source=await sourceValue(payment.source,record.intent);
      const reviewed=await reviewPreparedSource(source,record.intent,settlementSigningMessage(record.intent,{now:now(),allowExpired:true}),{crypto,now:clock});
      const verified=await verifySignedTransaction({signature:payment.signature,signedTransaction:payment.signed_transaction},reviewed,record.source_owner,crypto);
      if(verified.signature!==payment.signature)fail('The saved payment signature changed.');
      payment.state='submitting';saveSource(payment);
      try{
        const reply=await http('/requests/'+id(requestId)+'/submit-source',{method:'POST',body:{transaction_base64:payment.signed_transaction},token:saved.status_token});
        keys(reply,['schema_version','request']);if(reply.schema_version!==1)fail('Invalid source submission version.');
        const result=applyStatus(record,statusValue(reply.request,record.intent));save(records);
        payment.state='submitted';saveSource(payment);return freeze(snapshot(result));
      }catch{payment.state='submission_unknown';saveSource(payment);fail('Payment submission is unresolved. Check this saved request and original transaction ID. Do not pay or burn again.');}
    });},
    recoverExpiredSource(requestId){return locked(async()=>{
      const records=history(),record=find(records,requestId),saved=sourceAccess(record),payment=sourceRecord(requestId);
      if(!payment?.signature||payment.source.expires_at>now())fail('Only an expired original signed payment can use observation-only recovery.');
      const source=await sourceValue(payment.source,record.intent,true);
      const reviewed=await reviewPreparedSource(source,record.intent,settlementSigningMessage(record.intent,{now:now(),allowExpired:true}),{crypto,now:clock,allowExpired:true});
      await verifySignedTransaction({signature:payment.signature,signedTransaction:payment.signed_transaction},reviewed,record.source_owner,crypto);
      // Contract: expired originals are observation-only, never broadcast. This
      // is an explicit recovery action, not a live-payment retry or replacement.
      const reply=await http('/requests/'+id(requestId)+'/submit-source',{method:'POST',body:{transaction_base64:payment.signed_transaction},token:saved.status_token});
      keys(reply,['schema_version','request']);if(reply.schema_version!==1)fail('Invalid original-payment recovery version.');
      const result=applyStatus(record,statusValue(reply.request,record.intent));save(records);payment.state='recovery_only';saveSource(payment);return freeze(snapshot(result));
    });},
  });
}
