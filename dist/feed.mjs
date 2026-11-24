import {CONFIG, integer, parseExactJSON, request, MAX_RESPONSE_BYTES} from './model.mjs';

const HASH = /^[0-9a-f]{64}$/, ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const fail = () => { throw new Error('Verified network data is unavailable.'); };
const same = (left, right) => { if (left !== right) fail(); };
function text(value, maximum = 256) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\u0000-\u001f\u007f]/.test(value)) fail();
  return value;
}
function exact(value, positive = false) {
  // Feed quantities are decimal strings; accepting floats or rounded JSON
  // numbers would turn a census into an estimate.
  if (typeof value !== 'string' || !/^(0|[1-9]\d*)$/.test(value)) fail();
  return integer(value, positive);
}
function quantity(value) {
  if (typeof value !== 'string' || !/^(0|[1-9]\d{0,77})$/.test(value)) fail();
  return BigInt(value);
}
function millis(value) {
  const n = exact(value, true);
  if (n > BigInt(Number.MAX_SAFE_INTEGER)) fail();
  return Number(n);
}
function components(source, names) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) fail();
  return names.reduce((total, name) => total + exact(source[name]), 0n);
}

export function parseMarketSnapshot(input, now = Date.now()) {
  if (typeof input === 'string' && (input.length > MAX_RESPONSE_BYTES || new TextEncoder().encode(input).byteLength > MAX_RESPONSE_BYTES)) fail();
  const data = typeof input === 'string' ? parseExactJSON(input) : input;
  same(data?.schema_version, '2'); same(data?.chain_id, CONFIG.chainId);
  if (data.status === 'unavailable') return {status:'unavailable', reason:'A complete network census is temporarily unavailable.'};
  same(data.status, 'ready'); same(data.coverage?.complete, true);
  const observedAt = millis(data.observed_at_ms), expiresAt = millis(data.expires_at_ms);
  if (expiresAt <= observedAt || expiresAt - observedAt > 60000 || observedAt > now + 5000) fail();
  if (expiresAt <= now) return {status:'stale', reason:'Network data expired. Reconnecting…'};
  const slot = exact(data.as_of?.slot, true), blockHash = data.as_of?.block_hash;
  if (!HASH.test(blockHash)) fail();
  const supply = data.supply, market = data.market, mining = data.mining, policy = data.policy;
  same(supply?.status, 'available'); same(supply?.basis, 'unlocked_on_chain_v2');
  same(supply?.chain_id, CONFIG.chainId); same(String(supply?.decimals), '9');
  same(exact(supply.slot), slot); same(supply.block_hash, blockHash);
  const circulatingRaw = exact(supply.circulating_raw);
  same(components(supply.components_raw, ['native_wallets','company_native','wrapped_wallets','company_wrapped','amm_inventory']), circulatingRaw);
  same(market?.token_id, 'native_xrs'); same(market?.symbol, 'XRS');
  same(market?.decimals, '9'); same(market?.quote_token_id, CONFIG.usdc);
  same(market?.quote_symbol, 'xUSDC'); same(market?.quote_decimals, '6');
  same(market?.valuation_basis, 'circulating_supply'); same(market?.price_basis, 'pool_spot');
  same(market?.price_pool_id, CONFIG.poolId); same(exact(market.circulating_raw), circulatingRaw);
  const priceNumerator = exact(market.price_quote_raw_numerator, true);
  const priceDenominator = exact(market.price_xrs_raw_denominator, true);
  const marketCapRaw = quantity(market.market_cap_quote_raw);
  same(marketCapRaw, circulatingRaw * priceNumerator / priceDenominator);
  const minedRaw = exact(mining?.total_mined_raw);
  same(minedRaw, exact(supply.total_emitted_raw));
  const stakedRaw = exact(mining.total_staked_raw);
  const reward = text(mining.current_block_reward_xrs, 32);
  if (!/^\d+(\.\d{1,9})?$/.test(reward)) fail();
  if (!Array.isArray(data.blocks) || data.blocks.length < 1 || data.blocks.length > 12) fail();
  const hashes = new Set();
  let previousSlot = null;
  const blocks = data.blocks.map(row => {
    const blockSlot = exact(row?.slot, true), timestamp = millis(row?.timestamp_ms);
    if (!HASH.test(row?.hash) || hashes.has(row.hash) || !ADDRESS.test(row?.proposer) ||
        (previousSlot !== null && blockSlot >= previousSlot) || timestamp > observedAt + 30000) fail();
    hashes.add(row.hash); previousSlot = blockSlot;
    return {slot:blockSlot, hash:row.hash, proposer:row.proposer, timestamp, transactionCount:exact(row.transaction_count)};
  });
  same(blocks[0].slot, slot); same(blocks[0].hash, blockHash);
  same(millis(mining.last_block_timestamp_ms), blocks[0].timestamp);
  if (observedAt - blocks[0].timestamp > 120000) fail();
  if (!policy || !HASH.test(policy.sha256) || !Array.isArray(policy.company_wallets) ||
      !Array.isArray(policy.included_pool_ids) || !policy.included_pool_ids.includes(CONFIG.poolId)) fail();
  const companyAddresses = new Set();
  const companyWallets = policy.company_wallets.map(row => {
    if (!ADDRESS.test(row?.address) || companyAddresses.has(row.address)) fail();
    companyAddresses.add(row.address);
    return {address:row.address, role:text(row.role)};
  });
  const policyId = text(policy.id, 128), description = text(policy.description, 1000);
  same(policyId,'xeris-mainnet-circulation-v2');
  if (!Array.isArray(data.launchpad_tokens) || data.launchpad_tokens.length > 800) fail();
  const ids = new Set();
  const launchpad = data.launchpad_tokens.map(row => {
    const id = text(row?.token_id, 128), name = text(row?.name), symbol = text(row?.symbol, 32);
    if (!id.startsWith('lp_') || ids.has(id)) fail();
    ids.add(id);
    if (row.status !== 'ready') {
      if (row.market_cap_quote_raw !== null) fail();
      return {id,name,symbol,status:'unavailable',marketCapRaw:null};
    }
    same(row.decimals,'9'); same(row.quote_decimals,'6'); same(row.quote_token_id,CONFIG.usdc);
    same(row.quote_symbol,'xUSDC'); same(row.valuation_basis,'circulating_supply');
    const circulating = exact(row.circulating_raw), cap = quantity(row.market_cap_quote_raw);
    const tokenPriceNumerator = exact(row.price_xrs_raw_numerator, true), tokenPriceDenominator = exact(row.price_token_raw_denominator, true);
    same(cap, circulating * tokenPriceNumerator * priceNumerator / (tokenPriceDenominator * priceDenominator));
    same(row.supply?.status,'available'); same(row.supply?.basis,'unlocked_on_chain_v2');
    same(row.supply?.token_id,id); same(String(row.supply?.decimals),'9');
    same(exact(row.supply.circulating_raw),circulating);
    same(components(row.supply.components_raw,['wallets','company_wallets','amm_inventory']),circulating);
    if (circulating > exact(row.supply.issued_supply_raw)) fail();
    return {id,name,symbol,status:'ready',circulatingRaw:circulating,marketCapRaw:cap};
  });
  return {status:'ready', observedAt, expiresAt, slot, blockHash, circulatingRaw, marketCapRaw,
    minedRaw, stakedRaw, reward, priceNumerator, priceDenominator, blocks, launchpad,
    policy:{id:policyId,hash:policy.sha256,description,companyWallets}};
}

export function formatQuantity(raw, decimals, places = decimals) {
  const n = typeof raw === 'bigint' ? raw : quantity(raw);
  if (n < 0n || !Number.isInteger(decimals) || decimals < 0 || decimals > 18) fail();
  const base = 10n ** BigInt(decimals), whole = (n / base).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = (n % base).toString().padStart(decimals,'0').slice(0,places).replace(/0+$/,'');
  return whole + (fraction ? '.' + fraction : '');
}

export function formatMarketCap(raw) {
  const n = typeof raw === 'bigint' ? raw : quantity(raw);
  if (n < 0n) fail();
  if (n > 0n && n < 10000n) return '<0.01';
  // Presentation only: retain exact six-decimal xUSDC quantities for arithmetic,
  // accessible labels and detail titles. Round the displayed amount to cents.
  const cents = (n + 5000n) / 10000n;
  return `${formatQuantity(cents / 100n,0)}.${(cents % 100n).toString().padStart(2,'0')}`;
}


export function formatCompactMarketCap(raw) {
  const n = typeof raw === 'bigint' ? raw : quantity(raw);
  if (n < 0n) fail();
  for (const [scale,suffix] of [[1000000000000000000n,'T'],[1000000000000000n,'B'],[1000000000000n,'M']]) {
    if (n < scale) continue;
    const hundredths = (n * 100n + scale / 2n) / scale;
    return `${formatQuantity(hundredths / 100n,0)}.${(hundredths % 100n).toString().padStart(2,'0')}${suffix}`;
  }
  return formatMarketCap(n);
}

// SSE carries only public read snapshots. The browser does not synthesize
// emissions or blocks. Every display is removed at its data deadline.
export function createMarketFeed({onState, read = () => request('/market/supply'),
  makeSource = url => new EventSource(url), now = Date.now,
  schedule = setTimeout, cancel = clearTimeout, pollMs = 8000} = {}) {
  if (typeof onState !== 'function' || pollMs < 5000 || pollMs > 10000) throw new Error('Invalid feed configuration.');
  let active = false, generation = 0, source = null, pollTimer, expiryTimer, inflight = false;
  let lastObserved = 0, revision = 0, transport = 'polling';
  const clearTimers = () => { cancel(pollTimer); cancel(expiryTimer); pollTimer = expiryTimer = undefined; };
  const publishUnavailable = (status='unavailable', reason='Network feed unavailable. Reconnecting…') => {
    revision++;
    cancel(expiryTimer); expiryTimer = undefined; onState({status,reason,transport});
  };
  function queuePoll() {
    if (!active || pollTimer !== undefined) return;
    pollTimer = schedule(() => { pollTimer = undefined; poll(); },pollMs);
  }
  function accept(raw, via) {
    const result = parseMarketSnapshot(raw,now());
    if (result.status !== 'ready') { publishUnavailable(result.status,result.reason); queuePoll(); return; }
    if (result.observedAt < lastObserved || (via === 'polling' && transport === 'stream' && result.observedAt === lastObserved)) return;
    revision++;
    lastObserved = result.observedAt; transport = via;
    cancel(expiryTimer); cancel(pollTimer); pollTimer = undefined;
    expiryTimer = schedule(() => {
      expiryTimer = undefined;
      if (!active) return;
      publishUnavailable('stale','Network data expired. Reconnecting…'); queuePoll();
    },Math.max(1,result.expiresAt-now()));
    onState({...result,transport});
    if (via !== 'stream') queuePoll();
  }
  async function poll() {
    if (!active || inflight) return;
    inflight = true; const run = generation, revisionBefore = revision;
    try { const value = await read(); if (active && run === generation && revision === revisionBefore) accept(value,'polling'); }
    catch { if (active && run === generation && revision === revisionBefore) publishUnavailable(); }
    finally { inflight = false; if (active && (run !== generation || transport !== 'stream')) queuePoll(); }
  }
  function start() {
    if (active) return;
    active = true; generation++; transport = 'polling'; lastObserved = 0;
    poll();
    try {
      source = makeSource(CONFIG.rpc + '/market/stream');
      const selected = source, run = generation;
      selected.addEventListener('snapshot', event => {
        if (!active || run !== generation || source !== selected) return;
        try { accept(event.data,'stream'); } catch { transport = 'polling'; publishUnavailable(); queuePoll(); }
      });
      selected.onerror = () => {
        if (!active || run !== generation || source !== selected) return;
        transport = 'polling'; publishUnavailable(); queuePoll();
      };
    } catch { source = null; queuePoll(); }
  }
  function stop() {
    active = false; generation++; source?.close(); source = null; clearTimers();
    onState({status:'paused',reason:'Updates paused while this tab is hidden.',transport:'paused'});
  }
  return {start,stop};
}
