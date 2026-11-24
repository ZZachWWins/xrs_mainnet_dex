import { CONFIG } from './model.mjs';
import { base58Decode } from './trade.mjs';

// This module only connects a public account and prepares a recipient review.
// It does not authorize an operator request, prove ownership, or accept funds.
export const BRIDGE_DRAFT_CONFIG = Object.freeze({
  sourceChain: 'solana-mainnet',
  destinationChain: CONFIG.chainId,
  sourceMint: CONFIG.solanaMint,
  canAcceptFunds: false,
});
const RECIPIENT_SOURCES = new Set(['manual', 'connected_xeris']);
// These public Solana infrastructure addresses are easy to paste by mistake.
// Company Xeris wallets and unfunded addresses are intentionally permitted.
const WRONG_DESTINATIONS = new Set([CONFIG.solanaMint, CONFIG.reserve]);

function publicAddress(text) {
  if (typeof text !== 'string' || text.length > 128) throw new Error('Enter a public wallet address.');
  const address = text.trim();
  let bytes;
  try { bytes = base58Decode(address, 32); }
  catch { throw new Error('Enter a valid 32-byte base58 public wallet address.'); }
  if (bytes.every(byte => byte === 0)) throw new Error('The system address cannot receive this deposit.');
  return address;
}

/** Format validation only: no ownership, account existence, or chain assertion. */
export function validateRecipient(text) {
  const address = publicAddress(text);
  if (WRONG_DESTINATIONS.has(address)) throw new Error('Enter your Xeris wallet address, not the Solana mint or reserve address.');
  return address;
}

function providerAddress(value) {
  // Read the public-key representation only. Never inspect wallet key material.
  let text;
  if (typeof value === 'string') text = value;
  else if (value && typeof value.toBase58 === 'function') text = value.toBase58();
  else if (value && typeof value.toString === 'function') text = value.toString();
  return publicAddress(text);
}

const SOLANA_MAINNET = 'solana:mainnet';
const MAX_STANDARD_WALLETS = 32;
const LEGACY_WALLETS = Object.freeze([
  { id: 'legacy:phantom', name: 'Phantom', flag: 'isPhantom', read: environment => environment?.phantom?.solana },
  { id: 'legacy:solflare', name: 'Solflare', flag: 'isSolflare', read: environment => environment?.solflare },
  { id: 'legacy:backpack', name: 'Backpack', flag: 'isBackpack', read: environment => environment?.backpack },
]);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

function walletName(value) {
  if (typeof value !== 'string' || value.length > 80) return null;
  // Treat extension names as untrusted display text, never markup or identity.
  const name = value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, '').replace(/\s+/g, ' ').trim();
  return name || null;
}
function chainList(chains) {
  return Array.isArray(chains) && chains.length <= 64
    && chains.every(chain => typeof chain === 'string' && chain.length <= 128);
}
function standardParts(wallet) {
  try {
    if (wallet?.version !== '1.0.0' || !walletName(wallet.name)
      || !chainList(wallet.chains) || !wallet.chains.includes(SOLANA_MAINNET)) return null;
    const connect = wallet.features?.['standard:connect'];
    const events = wallet.features?.['standard:events'];
    const disconnect = wallet.features?.['standard:disconnect'];
    if (connect?.version !== '1.0.0' || typeof connect.connect !== 'function'
      || events?.version !== '1.0.0' || typeof events.on !== 'function') return null;
    if (disconnect !== undefined && (disconnect?.version !== '1.0.0' || typeof disconnect.disconnect !== 'function')) return null;
    return { connect, events, disconnect };
  } catch { return null; }
}
function standardAddress(wallet, accounts) {
  if (!standardParts(wallet) || !Array.isArray(accounts) || accounts.length > 32) throw new Error('invalid_standard_accounts');
  const matching = accounts.filter(account => account && chainList(account.chains) && account.chains.includes(SOLANA_MAINNET));
  if (!matching.length) return null;
  if (matching.length !== 1) throw new Error('multiple_standard_accounts');
  const account = matching[0];
  if (!account.chains.every(chain => wallet.chains.includes(chain))
    || !Array.isArray(account.features) || account.features.length > 64
    || !account.features.every(feature => typeof feature === 'string' && own(wallet.features, feature))) throw new Error('invalid_standard_accounts');
  const address = publicAddress(account.address), bytes = base58Decode(address, 32), key = account.publicKey;
  if (address !== account.address || !ArrayBuffer.isView(key) || Object.prototype.toString.call(key) !== '[object Uint8Array]'
    || key.byteLength !== 32 || !bytes.every((byte, index) => byte === key[index])) throw new Error('invalid_standard_accounts');
  return address;
}
function sameAdapter(left, right) {
  return left === right || !!left && !!right && left.owner === right.owner && left.kind === right.kind
    && left.connectFunction === right.connectFunction && left.eventsFunction === right.eventsFunction
    && left.disconnectFunction === right.disconnectFunction;
}

/**
 * Explicit public-account connection only. No signing, submission, eager connect,
 * generic window.solana, wallet-supplied HTML/icon/URL, or stored account consent.
 * Wallet Standard discovery is the register/app-ready protocol, with a real
 * register API in event.detail; registration itself never authorizes an account.
 * Sources: github.com/wallet-standard/wallet-standard (core app/base/features),
 * docs.phantom.com/solana/detecting-the-provider,
 * github.com/solflare-wallet/solflare-sdk/blob/master/src/index.ts,
 * github.com/coral-xyz/backpack (provider-injection and provider-core).
 */
export function createSolanaConnection({ environment = globalThis, onChange = () => {}, onWalletsChange = () => {} } = {}) {
  if (typeof onChange !== 'function' || typeof onWalletsChange !== 'function') throw new TypeError('Wallet callbacks must be functions.');
  const registered = new Map();
  let nextWalletId = 1;
  let selectedId = 'legacy:phantom'; // Preserve no-argument connect() compatibility.
  let selectedName = 'Phantom', selectedKind = 'injected', adapter = null;
  let generation = 0;
  let disposed = false;
  let sessionEnabled = false;
  let removeActiveListeners = null;
  let pendingConnect = null;
  let pendingDisconnect = null;
  let walletList = Object.freeze([]), walletListKey = '', walletNoticePending = false;
  let discoveryListener = null;
  const reviews = new WeakSet();
  adapter = findAdapter(selectedId);
  let state = Object.freeze({
    status: adapter ? 'disconnected' : 'unavailable', address: null, revision: 0,
    provider: selectedName, providerId: selectedId, providerType: selectedKind,
    networkVerified: false, error: null, canAcceptFunds: false,
  });

  function findAdapter(id) {
    const spec = LEGACY_WALLETS.find(wallet => wallet.id === id);
    if (spec) {
      try {
        const owner = spec.read(environment);
        if (owner?.[spec.flag] !== true || typeof owner.connect !== 'function'
          || typeof owner.disconnect !== 'function' || typeof owner.on !== 'function') return null;
        const currentAddress = () => owner.isConnected === false || owner.publicKey == null ? null : providerAddress(owner.publicKey);
        return { id, name: spec.name, kind: 'injected', owner,
          connectFunction: owner.connect, disconnectFunction: owner.disconnect, eventsFunction: owner.on,
          currentAddress,
          async connect() {
            const response = await owner.connect();
            // Solflare's documented connection method may resolve void.
            return spec.id === 'legacy:solflare' && response === undefined ? currentAddress() : providerAddress(response?.publicKey);
          },
          disconnect: () => owner.disconnect(),
          subscribe(changed, disconnected) {
            const events = spec.id === 'legacy:backpack' ? ['accountChanged', 'activeWalletDidChange', 'publicKeyUpdate'] : ['accountChanged'];
            const handler = payload => changed({ legacy: true, payload, checkPayload: spec.id !== 'legacy:backpack' });
            const remove = () => {
              const off = typeof owner.removeListener === 'function' ? owner.removeListener : typeof owner.off === 'function' ? owner.off : null;
              if (off) { for (const event of events) off.call(owner, event, handler); off.call(owner, 'disconnect', disconnected); }
            };
            try { for (const event of events) owner.on(event, handler); owner.on('disconnect', disconnected); }
            catch (error) { try { remove(); } catch {} throw error; }
            return remove;
          },
        };
      } catch { return null; }
    }
    const entry = [...registered.values()].find(value => value.id === id);
    if (!entry) return null;
    const owner = entry.wallet, parts = standardParts(owner);
    if (!parts) return null;
    return { id, name: walletName(owner.name), kind: 'wallet-standard', owner,
      connectFunction: parts.connect.connect, disconnectFunction: parts.disconnect?.disconnect, eventsFunction: parts.events.on,
      currentAddress: () => standardAddress(owner, owner.accounts),
      async connect() { const response = await parts.connect.connect({ silent: false }); return standardAddress(owner, response?.accounts); },
      disconnect: () => parts.disconnect?.disconnect(),
      subscribe(changed) {
        const off = parts.events.on('change', properties => changed({ properties }));
        if (typeof off !== 'function') throw new Error('invalid_standard_events');
        return off;
      },
    };
  }

  function refreshWallets() {
    if (disposed) return walletList;
    const descriptors = [];
    for (const entry of registered.values()) {
      const available = findAdapter(entry.id);
      if (available) descriptors.push({ id: entry.id, name: available.name, available: true, kind: 'wallet-standard' });
    }
    for (const spec of LEGACY_WALLETS) descriptors.push({ id: spec.id, name: spec.name, available: !!findAdapter(spec.id), kind: 'injected' });
    const key = JSON.stringify(descriptors);
    if (key !== walletListKey) {
      walletListKey = key;
      walletList = Object.freeze(descriptors.map(wallet => Object.freeze({ ...wallet, chains: Object.freeze([SOLANA_MAINNET]) })));
      if (!walletNoticePending) {
        walletNoticePending = true;
        Promise.resolve().then(() => {
          walletNoticePending = false;
          if (!disposed) { try { onWalletsChange(walletList); } catch {} }
        });
      }
    }
    return walletList;
  }

  function publish(status, address = null, error = null, force = false) {
    if (!force && state.status === status && state.address === address && state.error === error
      && state.providerId === selectedId && state.provider === selectedName && state.providerType === selectedKind) return state;
    state = Object.freeze({ ...state, status, address, error, providerId: selectedId,
      provider: selectedName, providerType: selectedKind, revision: state.revision + 1 });
    try { onChange(state); } catch { /* UI owns its error reporting. */ }
    return state;
  }

  function removeListeners() {
    const remove = removeActiveListeners;
    removeActiveListeners = null;
    try { remove?.(); } catch { /* Closures reject inactive/replaced adapters too. */ }
  }

  function loseConnection(error = null) {
    generation++;
    sessionEnabled = false;
    return publish(adapter ? 'disconnected' : 'unavailable', null, error);
  }

  function syncProvider() {
    if (disposed) return state;
    refreshWallets();
    const detected = findAdapter(selectedId);
    if (!sameAdapter(detected, adapter)) {
      generation++;
      sessionEnabled = false;
      removeListeners();
      adapter = detected;
      if (adapter) { selectedName = adapter.name; selectedKind = adapter.kind; }
      publish(adapter ? 'disconnected' : 'unavailable', null, null, true);
    } else if (sessionEnabled && state.status === 'connected') {
      try {
        const address = adapter.currentAddress();
        if (!address) return loseConnection();
        if (address !== state.address) {
          generation++;
          publish('connected', address);
        }
      } catch { loseConnection(`${selectedName} returned an invalid or ambiguous mainnet account. Reconnect with one account selected.`); }
    }
    return state;
  }

  function addListeners(owner) {
    removeListeners();
    const accountChanged = event => {
      if (disposed || adapter !== owner || !sessionEnabled) return;
      if (!sameAdapter(findAdapter(selectedId), owner)) { syncProvider(); return; }
      generation++;
      try {
        if (event.legacy && event.checkPayload && event.payload == null) { loseConnection(); return; }
        const address = owner.currentAddress();
        if (!address) { loseConnection(); return; }
        if (event.legacy && event.checkPayload && providerAddress(event.payload) !== address) throw new Error('account_mismatch');
        if (!event.legacy) {
          const properties = event.properties;
          if (!properties || typeof properties !== 'object' || Array.isArray(properties)) throw new Error('invalid_change');
          // Standard change is partial. Omitted accounts must not clear consent.
          if (own(properties, 'accounts') && standardAddress(owner.owner, properties.accounts) !== address) throw new Error('account_mismatch');
          if (own(properties, 'chains') && (!chainList(properties.chains) || !properties.chains.includes(SOLANA_MAINNET))) throw new Error('chain_changed');
        }
        publish('connected', address, null, true);
      } catch { loseConnection(`${selectedName} returned an invalid or ambiguous mainnet account. Reconnect with one account selected.`); }
      refreshWallets();
    };
    const disconnected = () => {
      if (disposed || adapter !== owner || !sessionEnabled) return;
      loseConnection();
    };
    removeActiveListeners = owner.subscribe(accountChanged, disconnected);
  }

  function connect(walletId = selectedId) {
    if (disposed) return Promise.resolve(state);
    if (typeof walletId !== 'string' || walletId.length > 100) return Promise.resolve(state);
    if (walletId !== selectedId) {
      generation++;
      sessionEnabled = false;
      removeListeners();
      pendingConnect = null; pendingDisconnect = null;
      selectedId = walletId;
      adapter = findAdapter(selectedId);
      const known = LEGACY_WALLETS.find(wallet => wallet.id === walletId);
      selectedName = adapter?.name || known?.name || 'Selected Solana wallet';
      selectedKind = adapter?.kind || (known ? 'injected' : 'wallet-standard');
      publish(adapter ? 'disconnected' : 'unavailable', null, null, true);
    }
    syncProvider();
    if (pendingDisconnect || state.status === 'disconnecting') return pendingDisconnect || Promise.resolve(state);
    if (state.status === 'connecting') return pendingConnect || Promise.resolve(state);
    if (state.status === 'connected') return Promise.resolve(state);
    if (!adapter) return Promise.resolve(publish('unavailable', null, `${selectedName} is unavailable. Choose an installed Solana wallet.`));
    const owner = adapter;
    const attempt = ++generation;
    sessionEnabled = false;
    try { addListeners(owner); }
    catch {
      sessionEnabled = false;
      return Promise.resolve(publish('disconnected', null, `${selectedName} account events are unavailable. Reconnect after reloading.`));
    }
    sessionEnabled = true;
    publish('connecting');
    const operation = Promise.resolve().then(async () => {
      if (disposed || attempt !== generation || adapter !== owner || !sessionEnabled) return state;
      try {
        const address = await owner.connect();
        syncProvider();
        if (disposed || attempt !== generation || adapter !== owner || !sessionEnabled) return state;
        if (!address || owner.currentAddress() !== address) throw new Error('public_account_mismatch');
        return publish('connected', address);
      } catch (error) {
        if (disposed || attempt !== generation || adapter !== owner || !sessionEnabled) return state;
        sessionEnabled = false;
        let declined = false;
        try { declined = error?.code === 4001; } catch { /* Do not inspect arbitrary provider errors. */ }
        return publish('disconnected', null, declined
          ? `${selectedName} connection was declined.` : `${selectedName} could not confirm a valid public account. Select one Solana mainnet account and try again.`);
      }
    });
    pendingConnect = operation;
    operation.finally(() => { if (pendingConnect === operation) pendingConnect = null; });
    return operation;
  }

  function disconnect() {
    if (disposed) return Promise.resolve(state);
    if (pendingDisconnect) return pendingDisconnect;
    if (state.status === 'disconnecting') return Promise.resolve(state);
    syncProvider();
    const owner = adapter;
    const hadSession = sessionEnabled || state.status === 'connecting' || state.status === 'connected';
    generation++;
    sessionEnabled = false;
    if (!owner || !hadSession) return Promise.resolve(publish(owner ? 'disconnected' : 'unavailable'));
    const attempt = generation;
    publish('disconnecting');
    const operation = Promise.resolve().then(async () => {
      if (disposed || attempt !== generation || adapter !== owner) return state;
      let error = null;
      try { await owner.disconnect(); }
      catch { error = `${selectedName} could not confirm disconnection. This page has cleared the connection.`; }
      if (disposed || attempt !== generation || adapter !== owner) return state;
      return publish('disconnected', null, error);
    });
    pendingDisconnect = operation;
    operation.finally(() => { if (pendingDisconnect === operation) pendingDisconnect = null; });
    return operation;
  }

  function createReview({ recipient, recipientSource = 'manual' } = {}) {
    const current = syncProvider();
    if (disposed || current.status !== 'connected' || !current.address) throw new Error('Connect a Solana account before reviewing the recipient.');
    if (!RECIPIENT_SOURCES.has(recipientSource)) throw new Error('Choose a manual or explicitly connected Xeris recipient.');
    const review = Object.freeze({
      kind: 'deposit_recipient_draft',
      sourceChain: BRIDGE_DRAFT_CONFIG.sourceChain,
      sourceSolanaAddress: current.address,
      sourceWalletId: current.providerId,
      sourceWalletName: current.provider,
      destinationChain: BRIDGE_DRAFT_CONFIG.destinationChain,
      destinationXerisAddress: validateRecipient(recipient),
      recipientSource,
      sourceMint: BRIDGE_DRAFT_CONFIG.sourceMint,
      connectionRevision: current.revision,
      canAcceptFunds: false,
    });
    reviews.add(review);
    return review;
  }

  function isReviewCurrent(review, { recipient, recipientSource = 'manual' } = {}) {
    const current = syncProvider();
    if (disposed || !reviews.has(review) || current.status !== 'connected') return false;
    try {
      return review.connectionRevision === current.revision
        && review.sourceWalletId === current.providerId
        && review.sourceSolanaAddress === current.address
        && review.destinationXerisAddress === validateRecipient(recipient)
        && review.recipientSource === recipientSource;
    } catch { return false; }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    generation++;
    sessionEnabled = false;
    removeListeners();
    if (discoveryListener) { try { environment.removeEventListener('wallet-standard:register-wallet', discoveryListener); } catch {} }
    registered.clear();
    publish('disconnected');
    adapter = null;
  }

  function register(...wallets) {
    if (disposed) return () => {};
    const added = [];
    for (const wallet of wallets.slice(0, MAX_STANDARD_WALLETS)) {
      if (!wallet || typeof wallet !== 'object' || registered.has(wallet) || registered.size >= MAX_STANDARD_WALLETS) continue;
      const entry = { id: `standard:${nextWalletId++}`, wallet };
      registered.set(wallet, entry); added.push(entry);
    }
    refreshWallets(); syncProvider();
    return () => {
      if (disposed) return;
      for (const entry of added) { if (registered.get(entry.wallet) === entry) registered.delete(entry.wallet); }
      refreshWallets(); syncProvider();
    };
  }
  const api = Object.freeze({ register });
  try {
    if (typeof environment.addEventListener === 'function' && typeof environment.dispatchEvent === 'function') {
      discoveryListener = event => { try { if (typeof event.detail === 'function') event.detail(api); } catch {} };
      environment.addEventListener('wallet-standard:register-wallet', discoveryListener);
      const EventType = environment.Event || globalThis.Event;
      const event = new EventType('wallet-standard:app-ready', { bubbles: false, cancelable: false, composed: false });
      Object.defineProperty(event, 'detail', { value: api });
      environment.dispatchEvent(event);
    }
  } catch { /* Legacy detection still works where the Standard event bus is unavailable. */ }
  refreshWallets();
  function getWallets() { refreshWallets(); syncProvider(); return walletList; }
  return Object.freeze({ connect, disconnect, dispose, getState: syncProvider, getWallets, createReview, isReviewCurrent });
}
