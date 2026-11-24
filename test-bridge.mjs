import test from 'node:test';
import assert from 'node:assert/strict';
import { base58Encode, base58Decode } from './dist/trade.mjs';
import { CONFIG } from './dist/model.mjs';
import { BRIDGE_DRAFT_CONFIG, validateRecipient, createSolanaConnection } from './dist/bridge.mjs';

// Synthetic public addresses only. No keypairs, RPC calls, or browser wallet.
const A = base58Encode(new Uint8Array(32).fill(7));
const B = base58Encode(new Uint8Array(32).fill(8));
const C = base58Encode(new Uint8Array(32).fill(9));
const publicKey = address => ({ toBase58: () => address });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const tick = () => Promise.resolve();

function mockProvider() {
  const listeners = new Map();
  const provider = {
    isPhantom: true, isConnected: false, publicKey: null, connectCalls: 0, disconnectCalls: 0,
    on(event, handler) { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(handler); },
    removeListener(event, handler) { listeners.get(event)?.delete(handler); },
    emit(event, value) { for (const handler of [...(listeners.get(event) || [])]) handler(value); },
    listenerCount(event) { return listeners.get(event)?.size || 0; },
    change(address) { this.publicKey = address == null ? null : publicKey(address); this.isConnected = address != null; this.emit('accountChanged', this.publicKey); },
    async connect() { this.connectCalls++; this.publicKey = publicKey(A); this.isConnected = true; return { publicKey: this.publicKey }; },
    async disconnect() { this.disconnectCalls++; this.publicKey = null; this.isConnected = false; this.emit('disconnect'); },
  };
  for (const forbidden of ['signMessage', 'signTransaction', 'signAllTransactions', 'signAndSendTransaction', 'sendTransaction', 'request', 'secretKey']) {
    Object.defineProperty(provider, forbidden, { get() { throw new Error(`Forbidden wallet property accessed: ${forbidden}`); } });
  }
  return provider;
}
function setup(provider = mockProvider(), options = {}) {
  const environment = { phantom: { solana: provider } };
  return { provider, environment, connection: createSolanaConnection({ environment, ...options }) };
}

test('recipient validates canonical public format, permits new and company wallets, trims outer spaces', () => {
  assert.equal(validateRecipient(`  ${B}\n`), B);
  assert.equal(validateRecipient(CONFIG.treasury), CONFIG.treasury);
  assert.equal(validateRecipient(base58Encode(Uint8Array.from({ length: 32 }, (_, i) => i))), base58Encode(Uint8Array.from({ length: 32 }, (_, i) => i)));
});

test('recipient rejects malformed, zero, wrong-length and known mistaken Solana infrastructure addresses', () => {
  for (const invalid of ['', ' ', null, undefined, 123, new Uint8Array(32), {}, '0'.repeat(32), 'O'.repeat(44), '1'.repeat(32),
    '1'.repeat(31), '1'.repeat(33), `${B.slice(0, 5)} ${B.slice(5)}`, `1${B}`, '__launchpad_managed__', CONFIG.reserve, CONFIG.solanaMint, 'a'.repeat(129)]) {
    assert.throws(() => validateRecipient(invalid));
  }
});

test('generic Solana and Xeris namespaces are never inspected, and construction never prompts', async () => {
  const environment = {};
  for (const namespace of ['solana', 'xeris']) Object.defineProperty(environment, namespace, { get() { throw new Error('Wrong wallet namespace'); } });
  const connection = createSolanaConnection({ environment });
  assert.equal(connection.getState().status, 'unavailable');
  assert.equal((await connection.connect()).status, 'unavailable');
  const provider = mockProvider();
  environment.phantom = { solana: provider };
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(provider.connectCalls, 0);
  assert.equal((await connection.connect()).address, A);
  assert.equal(provider.connectCalls, 1);
  assert.equal(connection.getState().networkVerified, false);
});

test('only a valid Phantom injection is accepted', () => {
  for (const provider of [{}, { ...mockProvider(), isPhantom: false }, { ...mockProvider(), on: undefined }, { ...mockProvider(), disconnect: undefined }]) {
    assert.equal(createSolanaConnection({ environment: { phantom: { solana: provider } } }).getState().status, 'unavailable');
  }
  const environment = Object.defineProperty({}, 'phantom', { get() { throw new Error('Unavailable extension'); } });
  assert.equal(createSolanaConnection({ environment }).getState().status, 'unavailable');
});

test('connection is explicit, coalesces prompts, and never touches signing or network APIs', async () => {
  const { provider, connection } = setup();
  const wait = deferred();
  provider.connect = async function () { this.connectCalls++; await wait.promise; this.publicKey = publicKey(A); this.isConnected = true; return { publicKey: this.publicKey }; };
  assert.equal(provider.connectCalls, 0);
  const first = connection.connect();
  const second = connection.connect();
  assert.equal(first, second);
  assert.equal(connection.getState().status, 'connecting');
  await tick();
  assert.equal(provider.connectCalls, 1);
  wait.resolve();
  const state = await first;
  assert.equal(state.status, 'connected');
  assert.equal(state.address, A);
  assert.equal(state.canAcceptFunds, false);
  assert.ok(Object.isFrozen(state));
  await connection.connect();
  assert.equal(provider.connectCalls, 1);
});

test('declined connections use a sanitized message and can be retried explicitly', async () => {
  const { provider, connection } = setup();
  const normalConnect = provider.connect;
  provider.connect = async () => { throw { code: 4001, message: 'Do not display arbitrary wallet error text' }; };
  const state = await connection.connect();
  assert.equal(state.status, 'disconnected');
  assert.equal(state.address, null);
  assert.equal(state.error, 'Phantom connection was declined.');
  provider.connect = normalConnect;
  assert.equal((await connection.connect()).address, A);
});

test('malformed or mismatched public keys fail closed', async () => {
  for (const result of [null, {}, { publicKey: 'bad-key' }, { publicKey: publicKey(B) }, { publicKey: new Uint8Array(32) }]) {
    const { provider, connection } = setup();
    provider.connect = async function () { this.publicKey = publicKey(A); this.isConnected = true; return result; };
    const state = await connection.connect();
    assert.equal(state.status, 'disconnected');
    assert.equal(state.address, null);
    assert.match(state.error, /valid public account/);
  }
});

test('public keys with documented toString representation are accepted', async () => {
  const { provider, connection } = setup();
  provider.connect = async function () { this.isConnected = true; this.publicKey = { toString: () => A }; return { publicKey: this.publicKey }; };
  assert.equal((await connection.connect()).address, A);
});

test('review binds distinct source and manual recipient without replacing user input', async () => {
  const { connection } = setup();
  let userInput = B;
  assert.throws(() => connection.createReview({ recipient: userInput }), /Connect/);
  await connection.connect();
  const review = connection.createReview({ recipient: userInput });
  assert.deepEqual(review, {
    kind: 'deposit_recipient_draft', sourceChain: 'solana-mainnet', sourceSolanaAddress: A,
    sourceWalletId: 'legacy:phantom', sourceWalletName: 'Phantom',
    destinationChain: 'xeris-mainnet-v4', destinationXerisAddress: B, recipientSource: 'manual',
    sourceMint: CONFIG.solanaMint, connectionRevision: connection.getState().revision, canAcceptFunds: false,
  });
  assert.equal(userInput, B);
  assert.ok(Object.isFrozen(review));
  assert.equal(BRIDGE_DRAFT_CONFIG.canAcceptFunds, false);
  assert.ok(connection.isReviewCurrent(review, { recipient: userInput }));
  userInput = C;
  assert.equal(connection.isReviewCurrent(review, { recipient: userInput }), false);
  assert.equal(connection.isReviewCurrent({ ...review }, { recipient: B }), false);
  for (const forbidden of ['amount', 'amountRaw', 'transaction', 'signature', 'settlement', 'reserve', 'depositAddress']) assert.equal(forbidden in review, false);
});

test('explicit connected-Xeris recipient works, and source-mode changes invalidate a review', async () => {
  const { connection } = setup();
  await connection.connect();
  const review = connection.createReview({ recipient: B, recipientSource: 'connected_xeris' });
  assert.ok(connection.isReviewCurrent(review, { recipient: B, recipientSource: 'connected_xeris' }));
  assert.equal(connection.isReviewCurrent(review, { recipient: B, recipientSource: 'manual' }), false);
  assert.equal(connection.isReviewCurrent(review, { recipient: 'invalid', recipientSource: 'connected_xeris' }), false);
  assert.throws(() => connection.createReview({ recipient: B, recipientSource: 'automatic' }));
  // Sharing the same public address on both chains is permitted, not assumed.
  assert.equal(connection.createReview({ recipient: A }).destinationXerisAddress, A);
});

test('account changes invalidate reviews and never mutate the manual destination', async () => {
  const { provider, connection } = setup();
  await connection.connect();
  const recipient = C;
  const review = connection.createReview({ recipient });
  provider.change(B);
  assert.equal(connection.getState().address, B);
  assert.equal(connection.isReviewCurrent(review, { recipient }), false);
  assert.equal(connection.createReview({ recipient }).destinationXerisAddress, C);
  provider.change(null);
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(provider.connectCalls, 1);
  provider.change(A); // No reconnection from unsolicited events after losing consent.
  assert.equal(connection.getState().status, 'disconnected');
});

test('silent public account changes are detected before reviews, and malformed changes clear the source', async () => {
  const { provider, connection } = setup();
  await connection.connect();
  const review = connection.createReview({ recipient: C });
  provider.publicKey = publicKey(B);
  assert.equal(connection.isReviewCurrent(review, { recipient: C }), false);
  assert.equal(connection.createReview({ recipient: C }).sourceSolanaAddress, B);
  provider.change('malformed');
  assert.equal(connection.getState().address, null);
  assert.equal(connection.getState().status, 'disconnected');
});

test('null public key or explicit disconnected property clears a silent stale connection', async () => {
  for (const clear of [provider => { provider.publicKey = null; }, provider => { provider.isConnected = false; }]) {
    const { provider, connection } = setup();
    await connection.connect();
    clear(provider);
    assert.equal(connection.getState().status, 'disconnected');
    assert.throws(() => connection.createReview({ recipient: B }));
  }
});

test('a late connect response cannot restore state after disconnect', async () => {
  const { provider, connection } = setup();
  const wait = deferred();
  provider.connect = async function () { this.connectCalls++; await wait.promise; this.publicKey = publicKey(A); this.isConnected = true; return { publicKey: this.publicKey }; };
  const connecting = connection.connect();
  await tick();
  await connection.disconnect();
  wait.resolve();
  await connecting;
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(connection.getState().address, null);
  assert.equal(provider.disconnectCalls, 1);
});

test('account change during a prompt supersedes its late old response', async () => {
  const { provider, connection } = setup();
  const wait = deferred();
  provider.connect = async function () { this.connectCalls++; await wait.promise; return { publicKey: publicKey(A) }; };
  const connecting = connection.connect();
  await tick();
  provider.change(B);
  wait.resolve();
  await connecting;
  assert.equal(connection.getState().address, B);
});

test('disconnect clears reviews immediately, coalesces calls and prevents overlapping reconnect prompts', async () => {
  const { provider, connection } = setup();
  const wait = deferred();
  provider.disconnect = async function () { this.disconnectCalls++; await wait.promise; this.publicKey = null; this.isConnected = false; this.emit('disconnect'); };
  await connection.connect();
  const review = connection.createReview({ recipient: B });
  const disconnecting = connection.disconnect();
  assert.equal(connection.getState().status, 'disconnecting');
  assert.equal(connection.getState().address, null);
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
  assert.equal(connection.disconnect(), disconnecting);
  assert.equal(connection.connect(), disconnecting);
  await tick();
  assert.equal(provider.disconnectCalls, 1);
  assert.equal(provider.connectCalls, 1);
  wait.resolve();
  await disconnecting;
  assert.equal(connection.getState().status, 'disconnected');
  await connection.connect();
  assert.equal(provider.connectCalls, 2);
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
});

test('wallet disconnect events invalidate reviews; disconnect errors leave local state cleared', async () => {
  const { provider, connection } = setup();
  provider.disconnect = async () => { throw new Error('sensitive extension details'); };
  await connection.connect();
  const review = connection.createReview({ recipient: B });
  provider.emit('disconnect');
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
  await connection.connect();
  const state = await connection.disconnect();
  assert.equal(state.status, 'disconnected');
  assert.equal(state.address, null);
  assert.match(state.error, /page has cleared/);
  assert.doesNotMatch(state.error, /sensitive/);
});

test('provider replacement invalidates consent and ignores old provider events and responses', async () => {
  const { provider, connection, environment } = setup();
  const wait = deferred();
  provider.connect = async function () { await wait.promise; this.publicKey = publicKey(A); this.isConnected = true; return { publicKey: this.publicKey }; };
  const connecting = connection.connect();
  await tick();
  const replacement = mockProvider();
  environment.phantom.solana = replacement;
  assert.equal(connection.getState().status, 'disconnected');
  provider.change(A);
  wait.resolve();
  await connecting;
  assert.equal(connection.getState().address, null);
  assert.equal(provider.listenerCount('accountChanged'), 0);
  assert.equal(replacement.connectCalls, 0);
  assert.equal((await connection.connect()).address, A);
  provider.change(B);
  assert.equal(connection.getState().address, A);
});

test('dispose removes listeners, invalidates reviews and suppresses late responses without wallet actions', async () => {
  const { provider, connection } = setup();
  await connection.connect();
  const review = connection.createReview({ recipient: B });
  assert.equal(provider.listenerCount('accountChanged'), 1);
  connection.dispose();
  connection.dispose();
  assert.equal(provider.listenerCount('accountChanged'), 0);
  assert.equal(provider.listenerCount('disconnect'), 0);
  assert.equal(provider.disconnectCalls, 0);
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
  assert.throws(() => connection.createReview({ recipient: B }));
  await connection.connect();
  assert.equal(provider.connectCalls, 1);
  const delayed = setup();
  const wait = deferred();
  delayed.provider.connect = async () => { await wait.promise; return { publicKey: publicKey(A) }; };
  const connecting = delayed.connection.connect();
  await tick();
  delayed.connection.dispose();
  wait.resolve();
  await connecting;
  assert.equal(delayed.connection.getState().status, 'disconnected');
});

test('render exceptions cannot interrupt connection state or leak raw errors', async () => {
  const { provider, connection } = setup(mockProvider(), { onChange() { throw new Error('render failed'); } });
  assert.equal((await connection.connect()).address, A);
  provider.change(B);
  assert.equal(connection.getState().address, B);
  assert.equal((await connection.disconnect()).status, 'disconnected');
});

test('reentrant UI callbacks cannot duplicate or cancel the current wallet call', async () => {
  let connection;
  const provider = mockProvider();
  connection = createSolanaConnection({ environment: { phantom: { solana: provider } }, onChange(state) {
    if (state.status === 'connecting') void connection.connect();
    if (state.status === 'disconnecting') void connection.disconnect();
  } });
  assert.equal((await connection.connect()).status, 'connected');
  assert.equal(provider.connectCalls, 1);
  assert.equal((await connection.disconnect()).status, 'disconnected');
  assert.equal(provider.disconnectCalls, 1);
});

test('arbitrary provider error accessors never leak or reject the sanitized connection result', async () => {
  const { provider, connection } = setup();
  provider.connect = async () => { throw Object.defineProperty({}, 'code', { get() { throw new Error('Do not expose extension error contents'); } }); };
  const state = await connection.connect();
  assert.equal(state.status, 'disconnected');
  assert.match(state.error, /valid public account/);
});

function legacyProvider(name) {
  const provider = mockProvider();
  delete provider.isPhantom;
  provider[`is${name}`] = true;
  if (name === 'Solflare') {
    provider.connect = async function () { this.connectCalls++; this.publicKey = publicKey(A); this.isConnected = true; };
  }
  return provider;
}
function standardAccount(address = A, chains = ['solana:mainnet']) {
  return { address, publicKey: base58Decode(address), chains, features: ['solana:signTransaction'] };
}
function standardWallet(name = 'Test Standard') {
  const handlers = new Set();
  const wallet = { version: '1.0.0', name, chains: ['solana:mainnet'], accounts: [], connectCalls: 0, disconnectCalls: 0,
    features: {
      'standard:connect': { version: '1.0.0', async connect(options) {
        wallet.connectCalls++; wallet.lastConnectOptions = options;
        wallet.accounts = [standardAccount()]; return { accounts: wallet.accounts };
      } },
      'standard:disconnect': { version: '1.0.0', async disconnect() { wallet.disconnectCalls++; wallet.accounts = []; wallet.emit({ accounts: [] }); } },
      'standard:events': { version: '1.0.0', on(event, handler) {
        assert.equal(event, 'change'); handlers.add(handler); return () => handlers.delete(handler);
      } },
    },
    emit(properties) { for (const handler of [...handlers]) handler(properties); },
    listenerCount: () => handlers.size,
    changeAccounts(accounts) { wallet.accounts = accounts; wallet.emit({ accounts }); },
  };
  for (const forbidden of ['icon', 'url', 'secretKey', 'request', 'signTransaction', 'signMessage']) {
    Object.defineProperty(wallet, forbidden, { get() { throw new Error(`Forbidden standard property: ${forbidden}`); } });
  }
  for (const forbidden of ['solana:signMessage', 'solana:signTransaction', 'solana:signAndSendTransaction']) {
    Object.defineProperty(wallet.features, forbidden, { configurable: true, get() { throw new Error(`Forbidden signing feature: ${forbidden}`); } });
  }
  return wallet;
}
function standardEnvironment(before = []) {
  const environment = new EventTarget();
  environment.Event = Event;
  environment.addEventListener('wallet-standard:app-ready', event => {
    assert.equal(typeof event.detail.register, 'function');
    assert.ok(Object.isFrozen(event.detail));
    for (const wallet of before) event.detail.register(wallet);
  });
  environment.register = wallet => {
    let unregister;
    const event = new Event('wallet-standard:register-wallet');
    Object.defineProperty(event, 'detail', { value: api => { unregister = api.register(wallet); } });
    environment.dispatchEvent(event);
    return unregister;
  };
  return environment;
}

test('picker lists known providers even unavailable and exposes no provider object or metadata URLs', () => {
  const connection = createSolanaConnection({ environment: {} });
  assert.deepEqual(connection.getWallets().map(({id,name,available,kind}) => ({id,name,available,kind})), [
    { id: 'legacy:phantom', name: 'Phantom', available: false, kind: 'injected' },
    { id: 'legacy:solflare', name: 'Solflare', available: false, kind: 'injected' },
    { id: 'legacy:backpack', name: 'Backpack', available: false, kind: 'injected' },
  ]);
  assert.ok(Object.isFrozen(connection.getWallets()));
  for (const item of connection.getWallets()) {
    assert.ok(Object.isFrozen(item)); assert.ok(Object.isFrozen(item.chains));
    assert.deepEqual(Object.keys(item).sort(), ['available','chains','id','kind','name']);
  }
});

test('explicit legacy selection uses Solflare void response and Backpack direct namespace', async () => {
  const phantom = mockProvider(), solflare = legacyProvider('Solflare'), backpack = legacyProvider('Backpack');
  const environment = { phantom: { solana: phantom }, solflare, backpack };
  Object.defineProperty(environment, 'solana', { get() { throw new Error('Ambiguous global must not be read'); } });
  Object.defineProperty(environment, 'xeris', { get() { throw new Error('Xeris must not be read'); } });
  const connection = createSolanaConnection({ environment });
  assert.equal(connection.getWallets().filter(wallet => wallet.available).length, 3);
  assert.equal(phantom.connectCalls + solflare.connectCalls + backpack.connectCalls, 0);
  assert.equal((await connection.connect('legacy:solflare')).provider, 'Solflare');
  assert.equal(connection.getState().providerId, 'legacy:solflare');
  assert.equal(connection.getState().providerType, 'injected');
  assert.equal(phantom.connectCalls, 0);
  assert.equal(solflare.connectCalls, 1);
  assert.equal((await connection.connect('legacy:backpack')).provider, 'Backpack');
  assert.equal(backpack.connectCalls, 1);
  assert.equal(solflare.disconnectCalls, 0); // Switching clears local consent only.
  await connection.disconnect();
  assert.equal(backpack.disconnectCalls, 1);
});

test('wrong legacy namespaces and flags are not accepted', async () => {
  const provider = legacyProvider('Backpack');
  const environment = { backpack: { solana: provider }, solflare: { ...provider, isSolflare: false }, solana: provider };
  const connection = createSolanaConnection({ environment });
  assert.equal(connection.getWallets().filter(wallet => wallet.available).length, 0);
  assert.equal((await connection.connect('legacy:backpack')).status, 'unavailable');
  assert.equal(provider.connectCalls, 0);
});

test('selected unavailable wallet cannot fall back to Phantom or a previous connection', async () => {
  const { provider, connection } = setup();
  await connection.connect();
  const review = connection.createReview({ recipient: B });
  assert.equal((await connection.connect('legacy:solflare')).status, 'unavailable');
  assert.equal(connection.getState().providerId, 'legacy:solflare');
  assert.equal(connection.getState().address, null);
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
  await connection.connect(); // No-argument retry stays pinned to the selection.
  assert.equal(provider.connectCalls, 1);
  await connection.connect('standard:missing');
  assert.equal(connection.getState().providerId, 'standard:missing');
  assert.equal(connection.getState().status, 'unavailable');
});

test('Backpack structured account events and legacy alternate events re-read the public account', async () => {
  const backpack = legacyProvider('Backpack');
  const connection = createSolanaConnection({ environment: { backpack } });
  await connection.connect('legacy:backpack');
  const review = connection.createReview({ recipient: C });
  backpack.publicKey = publicKey(B);
  backpack.emit('accountChanged', { publicKey: B });
  assert.equal(connection.getState().address, B);
  assert.equal(connection.isReviewCurrent(review, { recipient: C }), false);
  const second = connection.createReview({ recipient: C });
  backpack.emit('activeWalletDidChange', { metadata: true });
  assert.equal(connection.getState().address, B);
  assert.equal(connection.isReviewCurrent(second, { recipient: C }), false);
  backpack.publicKey = null; backpack.isConnected = false;
  backpack.emit('publicKeyUpdate', null);
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(backpack.connectCalls, 1);
});

test('switching providers invalidates same-address reviews and late responses from the previous provider', async () => {
  const phantom = mockProvider(), solflare = legacyProvider('Solflare'), wait = deferred();
  const environment = { phantom: { solana: phantom }, solflare };
  const connection = createSolanaConnection({ environment });
  await connection.connect();
  const review = connection.createReview({ recipient: C });
  await connection.connect('legacy:solflare');
  assert.equal(connection.getState().address, A);
  assert.equal(connection.isReviewCurrent(review, { recipient: C }), false);
  const second = connection.createReview({ recipient: C });
  assert.equal(second.sourceWalletId, 'legacy:solflare');
  assert.equal(second.sourceWalletName, 'Solflare');
  phantom.change(B);
  assert.equal(connection.getState().address, A);
  await connection.disconnect();
  phantom.connect = async function () { this.connectCalls++; await wait.promise; this.publicKey = publicKey(B); this.isConnected = true; return { publicKey: this.publicKey }; };
  const late = connection.connect('legacy:phantom');
  await tick();
  await connection.connect('legacy:solflare');
  wait.resolve(); await late;
  assert.equal(connection.getState().providerId, 'legacy:solflare');
  assert.equal(connection.getState().address, A);
});

test('Wallet Standard discovery handles preloaded and late registration without connecting', async () => {
  const first = standardWallet('First'), second = standardWallet('Second');
  const environment = standardEnvironment([first]);
  const notices = [];
  const connection = createSolanaConnection({ environment, onWalletsChange: list => notices.push(list) });
  assert.equal(connection.getWallets()[0].name, 'First');
  assert.equal(connection.getWallets()[0].kind, 'wallet-standard');
  const unregister = environment.register(second);
  assert.equal(connection.getWallets()[1].name, 'Second');
  await tick();
  assert.equal(notices.length, 1); // Synchronous registration burst is coalesced.
  assert.equal(first.connectCalls + second.connectCalls, 0);
  unregister(); await tick();
  assert.equal(connection.getWallets().filter(wallet => wallet.kind === 'wallet-standard').length, 1);
  assert.equal(notices.length, 2);
});

test('Wallet Standard explicit connection validates mainnet bytes, binds provider and does not inspect signing features', async () => {
  const wallet = standardWallet();
  const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
  const id = connection.getWallets()[0].id;
  const state = await connection.connect(id);
  assert.equal(state.status, 'connected'); assert.equal(state.address, A);
  assert.equal(state.provider, 'Test Standard'); assert.equal(state.providerId, id);
  assert.equal(state.providerType, 'wallet-standard'); assert.equal(state.networkVerified, false);
  assert.deepEqual(wallet.lastConnectOptions, { silent: false });
  assert.equal(connection.createReview({ recipient: B }).sourceWalletId, id);
  await connection.disconnect();
  assert.equal(wallet.disconnectCalls, 1);
  assert.equal(connection.getState().address, null);
});

test('Standard duplicates use object identity and a duplicate registration cannot unregister the original', () => {
  const wallet = standardWallet('Phantom'), sameName = standardWallet('Phantom');
  const environment = standardEnvironment([wallet]);
  environment.phantom = { solana: mockProvider() };
  const connection = createSolanaConnection({ environment });
  const originalId = connection.getWallets()[0].id;
  const undoDuplicate = environment.register(wallet);
  undoDuplicate();
  assert.equal(connection.getWallets()[0].id, originalId);
  environment.register(sameName);
  const names = connection.getWallets().filter(item => item.name === 'Phantom');
  assert.equal(names.length, 3); // Two Standard objects and the real legacy slot.
  assert.equal(new Set(names.map(item => item.id)).size, 3);
});

test('Standard account changes and partial events invalidate reviews without clearing omitted accounts', async () => {
  const wallet = standardWallet();
  const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
  await connection.connect(connection.getWallets()[0].id);
  let review = connection.createReview({ recipient: C });
  wallet.emit({ chains: ['solana:mainnet'] });
  assert.equal(connection.getState().address, A);
  assert.equal(connection.isReviewCurrent(review, { recipient: C }), false);
  review = connection.createReview({ recipient: C });
  wallet.changeAccounts([standardAccount(B)]);
  assert.equal(connection.getState().address, B);
  assert.equal(connection.isReviewCurrent(review, { recipient: C }), false);
  wallet.changeAccounts([]);
  assert.equal(connection.getState().status, 'disconnected');
  wallet.changeAccounts([standardAccount(A)]);
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(wallet.connectCalls, 1);
});

test('Standard wallet chain or capability loss invalidates consent and never chooses another wallet', async () => {
  for (const change of [wallet => { wallet.chains = ['solana:devnet']; wallet.emit({ chains: wallet.chains }); },
    wallet => { wallet.features['standard:connect'] = { version: '2.0.0', connect: async () => ({ accounts: [] }) }; wallet.emit({ features: wallet.features }); }]) {
    const wallet = standardWallet(), replacement = standardWallet('Other');
    const connection = createSolanaConnection({ environment: standardEnvironment([wallet, replacement]) });
    const id = connection.getWallets()[0].id;
    await connection.connect(id);
    const review = connection.createReview({ recipient: B });
    change(wallet);
    assert.equal(connection.getState().status, 'unavailable');
    assert.equal(connection.getState().providerId, id);
    assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
    await connection.connect();
    assert.equal(replacement.connectCalls, 0);
  }
});

test('Standard account chain, public bytes, ambiguity and result mismatch are rejected', async () => {
  const malformed = [
    [], [standardAccount(A, ['solana:devnet'])], [standardAccount(), standardAccount(B)],
    [{ ...standardAccount(), publicKey: base58Decode(B) }], [{ ...standardAccount(), publicKey: Array.from(base58Decode(A)) }],
    [{ ...standardAccount(), chains: ['solana:mainnet', 'unadvertised:chain'] }],
    [{ ...standardAccount(), features: ['unadvertised:feature'] }], [{ ...standardAccount(), address: ` ${A}` }],
  ];
  for (const accounts of malformed) {
    const wallet = standardWallet();
    wallet.features['standard:connect'].connect = async () => { wallet.accounts = accounts; return { accounts }; };
    const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
    assert.equal((await connection.connect(connection.getWallets()[0].id)).status, 'disconnected');
    assert.equal(connection.getState().address, null);
  }
  for (const response of [undefined, null, {}, { accounts: [standardAccount(B)] }]) {
    const wallet = standardWallet();
    wallet.features['standard:connect'].connect = async () => { wallet.accounts = [standardAccount()]; return response; };
    const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
    assert.equal((await connection.connect(connection.getWallets()[0].id)).status, 'disconnected');
  }
});

test('non-Solana, devnet-only, incompatible versions and malformed Standard providers stay undiscoverable', () => {
  for (const change of [wallet => { wallet.chains = ['eip155:1']; }, wallet => { wallet.chains = ['solana:devnet']; },
    wallet => { wallet.version = '2.0.0'; }, wallet => { wallet.features['standard:events'].version = '2.0.0'; },
    wallet => { delete wallet.features['standard:connect']; }, wallet => { wallet.name = ' '; }, wallet => { wallet.name = 'A'.repeat(81); }]) {
    const wallet = standardWallet(); change(wallet);
    const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
    assert.equal(connection.getWallets().filter(item => item.kind === 'wallet-standard').length, 0);
  }
});

test('Standard unregister invalidates pending connection and old events cannot revive it', async () => {
  const wallet = standardWallet(), environment = standardEnvironment(), wait = deferred();
  wallet.features['standard:connect'].connect = async () => { wallet.connectCalls++; await wait.promise; wallet.accounts = [standardAccount()]; return { accounts: wallet.accounts }; };
  const connection = createSolanaConnection({ environment });
  const unregister = environment.register(wallet), id = connection.getWallets()[0].id;
  const connecting = connection.connect(id); await tick();
  unregister();
  assert.equal(connection.getState().status, 'unavailable');
  wait.resolve(); await connecting;
  assert.equal(connection.getState().address, null);
  assert.equal(wallet.listenerCount(), 0);
  wallet.emit({ accounts: [standardAccount()] });
  assert.equal(connection.getState().address, null);
  environment.register(wallet);
  assert.notEqual(connection.getWallets()[0].id, id);
  await connection.connect(id);
  assert.equal(wallet.connectCalls, 1);
});

test('Standard connection events may precede connect resolution and still require matching current public account', async () => {
  const wallet = standardWallet();
  wallet.features['standard:connect'].connect = async () => {
    wallet.connectCalls++; wallet.changeAccounts([standardAccount()]); return { accounts: wallet.accounts };
  };
  const connection = createSolanaConnection({ environment: standardEnvironment([wallet]) });
  assert.equal((await connection.connect(connection.getWallets()[0].id)).address, A);
  const review = connection.createReview({ recipient: B });
  wallet.emit({ accounts: [standardAccount(C)] }); // Payload/current account disagreement.
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(connection.isReviewCurrent(review, { recipient: B }), false);
});

test('optional Standard disconnect still clears local session, and dispose unsubscribes without prompting', async () => {
  const wallet = standardWallet(); delete wallet.features['standard:disconnect'];
  const environment = standardEnvironment([wallet]);
  const connection = createSolanaConnection({ environment });
  const id = connection.getWallets()[0].id;
  await connection.connect(id);
  await connection.disconnect();
  assert.equal(connection.getState().status, 'disconnected');
  assert.equal(wallet.disconnectCalls, 0);
  await connection.connect(id);
  connection.dispose();
  assert.equal(wallet.listenerCount(), 0);
  assert.equal(wallet.disconnectCalls, 0);
  const after = standardWallet('After dispose');
  assert.equal(environment.register(after), undefined);
  assert.equal(after.connectCalls, 0);
});

test('Standard discovery notifications stabilize under UI re-reads and resource limits bound descriptors', async () => {
  const wallets = Array.from({ length: 40 }, (_, index) => standardWallet(`Wallet ${index}`));
  const environment = standardEnvironment(wallets);
  let notices = 0, connection;
  connection = createSolanaConnection({ environment, onWalletsChange() { notices++; connection.getWallets(); } });
  await tick(); await tick();
  assert.equal(notices, 1);
  assert.equal(connection.getWallets().filter(item => item.kind === 'wallet-standard').length, 32);
  for (const wallet of wallets) assert.equal(wallet.connectCalls, 0);
  const odd = standardWallet('  Untrusted\n Wallet\u202e   Name  ');
  const second = createSolanaConnection({ environment: standardEnvironment([odd]) });
  assert.equal(second.getWallets()[0].name, 'Untrusted Wallet Name');
  assert.equal(second.getWallets()[0].icon, undefined);
});

test('malformed Standard registration events and throwing getters do not break known wallet discovery', () => {
  const environment = standardEnvironment();
  environment.phantom = { solana: mockProvider() };
  const connection = createSolanaConnection({ environment });
  for (const detail of [null, {}, () => { throw new Error('extension fault'); }]) {
    const event = new Event('wallet-standard:register-wallet'); Object.defineProperty(event, 'detail', { value: detail });
    environment.dispatchEvent(event);
  }
  environment.register(Object.defineProperty({}, 'version', { get() { throw new Error('extension fault'); } }));
  assert.equal(connection.getWallets().find(item => item.id === 'legacy:phantom').available, true);
});
