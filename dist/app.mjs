import {CONFIG, getPool, getQuote, getBalances, checkChain, parseAmount, formatAmount} from './model.mjs';
import {execute, reconcile, getHistory, getPending} from './trade.mjs';
import {getLaunchpadTokens, mergeLaunchpadMarkets} from './markets.mjs';
import {createMarketFeed, formatQuantity} from './feed.mjs';
import {CURRENCIES, FX_SOURCE_URL, createCurrencyController, formatQuoteAmount, formatQuoteRatio} from './currency.mjs';
const $ = id => document.getElementById(id);
let provider = null, address = null, balances = null, buying = true, quote = null;
let quoteTimer, quoteGeneration = 0, busy = false, poolBusy = false, reconcileBusy = false, historyError = false;
let connectionGeneration = 0, connecting = false, connectingProvider = null;
const balanceReads = new Map();
const listeningProviders = new WeakSet();
let observations = [], currentPool = null, lastObserved = 0;
let currencySnapshot = null;
const selectedCurrency = () => currencySnapshot?.currency || 'USD';
const money = (raw, options = {}) => currencySnapshot ? formatQuoteAmount(raw,currencySnapshot,options) : null;
const ratioMoney = (numerator,denominator,options = {}) => currencySnapshot ? formatQuoteRatio(numerator,denominator,currencySnapshot,options) : null;
function nativeFiat(raw, options = {}) {
  if (!currentPool || Date.now() - lastObserved > 45000) return null;
  return ratioMoney(raw * currentPool.reserveB,currentPool.reserveA,options);
}
function renderFiatEstimates() {
  const activeQuote = quote && Date.now() - quote.quotedAt <= 30000 ? quote : null;
  const pay = activeQuote ? (activeQuote.buying ? money(activeQuote.inputRaw) : nativeFiat(activeQuote.inputRaw)) : null;
  const receive = activeQuote ? (activeQuote.buying ? nativeFiat(activeQuote.outputRaw) : money(activeQuote.outputRaw)) : null;
  $('pay-fiat').textContent = pay ? `≈ ${pay}` : '—'; $('receive-fiat').textContent = receive ? `≈ ${receive}` : '—';
  for (const [id,value] of [['native-fiat',balances ? nativeFiat(balances.nativeRaw) : null],['wrapped-fiat',balances ? nativeFiat(balances.wrappedRaw) : null],['usdc-fiat',balances ? money(balances.usdcRaw) : null]]) {
    $(id).textContent = value ? `≈ ${value}` : '—';
  }
}
function renderValuations() {
  const currency = selectedCurrency();
  document.querySelectorAll('[data-display-currency]').forEach(el => { el.textContent = currency; });
  document.querySelectorAll('[data-currency-description]').forEach(el => { el.textContent = `${currency} equivalent`; });
  const snapshot = marketSnapshot?.expiresAt > Date.now() ? marketSnapshot : null;
  const cap = snapshot ? money(snapshot.marketCapRaw) : null;
  setFeedValue('market-cap',cap && cap.length > 19 ? money(snapshot.marketCapRaw,{compact:true,maximumFractionDigits:2}) : cap || '—');
  document.querySelectorAll('[data-feed="market-cap"]').forEach(el => {
    if (snapshot && cap) { const detail = `${money(snapshot.marketCapRaw,{maximumFractionDigits:6})} equivalent · ${formatQuantity(snapshot.marketCapRaw,6)} xUSDC`; el.title = detail; el.setAttribute('aria-label',`XRS market capitalization: ${detail}`); }
    else { el.removeAttribute('title'); el.setAttribute('aria-label',`XRS market capitalization in ${currency} unavailable`); }
  });
  const compact = snapshot ? money(snapshot.marketCapRaw,{compact:true,maximumFractionDigits:2}) : null;
  $('xrs-market-cap').textContent = compact || '—';
  const exactCap = snapshot && cap ? `${money(snapshot.marketCapRaw,{maximumFractionDigits:6})} equivalent · ${formatQuantity(snapshot.marketCapRaw,6)} xUSDC` : `${currency} valuation unavailable`;
  $('xrs-market-ticker').title = exactCap; $('xrs-market-ticker').setAttribute('aria-label',`XRS market capitalization: ${exactCap}`);
  $('spot-price').textContent = nativeFiat(1000000000n,{maximumFractionDigits:8}) || '—';
  $('spot-description').textContent = `${currency} equivalent per XRS · pool spot price`;
  $('display-currency').value = currency;
  $('fx-source').href = FX_SOURCE_URL;
  if (currency === 'USD') $('fx-status').textContent = 'USD reference · xUSDC intended 1:1 USDC backing';
  else if (currencySnapshot?.status === 'ready' && currencySnapshot.expiresAt > Date.now()) {
    const timestamp = new Date(currencySnapshot.asOf).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
    $('fx-status').textContent = `${currency} / USD FX quote · ${timestamp} · xUSDC 1:1 USD reference`;
  } else $('fx-status').textContent = `${currency} FX ${currencySnapshot?.status === 'loading' ? 'updating' : 'unavailable'} · token quotes remain in their native units`;
  renderMarkets(); renderChart(); renderFiatEstimates();
}
const say = message => { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(say.timer); say.timer = setTimeout(() => $('toast').classList.add('hidden'), 9000); };
const message = (text, error = false) => { $('trade-message').textContent = text; $('trade-message').classList.toggle('error', error); };
function modal(title, html) { $('dialog-title').textContent = title; $('dialog-body').innerHTML = html; if (!$('info-dialog').open) $('info-dialog').showModal(); }
function closeModal() { $('info-dialog').close(); }
$('close-dialog').onclick = closeModal;
$('info-dialog').addEventListener('click', e => { if (e.target === $('info-dialog')) closeModal(); });
function route() {
  const id = location.hash.slice(1) || location.pathname.replace(/^\/|\/$/g,'') || 'home';
  const allowed = ['home','swap','pools','bridge','network','portfolio','activity'];
  const page = allowed.includes(id) ? id : 'home';
  document.querySelectorAll('main>.page').forEach(el => el.classList.toggle('hidden', el.id !== page));
  document.querySelectorAll('nav a').forEach(el => el.classList.toggle('active', el.hash === '#' + page));
  document.querySelector('nav').classList.remove('open'); $('menu-button').setAttribute('aria-expanded','false');
  if (page === 'portfolio' && address) refreshBalances();
  if (page === 'activity') renderHistory();
  window.scrollTo({top: 0, behavior: 'instant'});
}
window.addEventListener('hashchange', route);
$('menu-button').onclick = () => { const open = document.querySelector('nav').classList.toggle('open'); $('menu-button').setAttribute('aria-expanded', String(open)); };
$('more-button').onclick = () => modal('Network services', `<p>Service availability</p><div class="more-grid"><div>Stake<small class="native-badge">Restricted producer roster</small></div><div>Agents<small class="native-badge">Unavailable</small></div><div>Alexandria<small class="native-badge">Unavailable</small></div><div>Governance<small class="native-badge">Unavailable</small></div></div><p class="footnote">Access pool trading, native asset conversion, account balances and transaction records through this interface.</p>`);
function docs() {
  modal('How mainnet trading works', `<p>Solana USDC backs xUSDC on Xeris. The pool trades xUSDC and wXRS, which converts 1:1 to native XRS.</p><ol class="review-list"><li>USDC deposit and redemption services are currently unavailable.</li><li>With xUSDC and native XRS for fees, approve a pool swap in your Xeris wallet.</li><li>Convert the received wXRS to native XRS in Portfolio. Selling reverses these steps.</li></ol><p>The pool takes 0.3% per swap. Each Xeris transaction costs 0.001 native XRS. Review price impact and the minimum received amount before approving each trade.</p><p class="detail-code">${CONFIG.chainId}<br>${CONFIG.rpc}<br>${CONFIG.poolId}</p><p class="footnote">Your wallet signs locally. The website never requests a seed phrase or private key.</p>`);
}
document.querySelectorAll('[data-docs]').forEach(el => el.onclick = docs);
function renderChart() {
  if (!observations.length || !money(1000000n)) {
    $('chart-line').setAttribute('d',''); $('chart-area').setAttribute('d',''); $('chart-point').classList.add('hidden');
    $('chart-caption').textContent = `${selectedCurrency()} price observations unavailable.`; return;
  }
  $('chart-point').classList.remove('hidden');
  const values = observations.map(v => v.price), min = Math.min(...values), max = Math.max(...values);
  const range = Math.max(max - min, max * .003, .00000001);
  const center = (max + min) / 2;
  const points = observations.map((v, i) => [observations.length === 1 ? 400 : 16 + i * 768 / (observations.length - 1), 150 - (v.price - center) / range * 200]);
  const path = points.map(([x,y],i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ');
  $('chart-line').setAttribute('d', path);
  $('chart-area').setAttribute('d', points.length > 1 ? `${path} L${points.at(-1)[0]} 290 L${points[0][0]} 290 Z` : '');
  const [x,y] = points.at(-1); $('chart-point').setAttribute('cx',x); $('chart-point').setAttribute('cy',y);
  $('chart-caption').textContent = `Session observations · ${selectedCurrency()} equivalents using the current display rate.`;
}
async function refreshPool() {
  if (poolBusy) return;
  poolBusy = true; $('refresh-pool').disabled = true;
  try {
    await checkChain();
    const pool = await getPool(); currentPool = pool; lastObserved = Date.now();
    const price = formatAmount(pool.reserveB * 100000000000n / pool.reserveA, 8);
    $('reserve-a').textContent = formatAmount(pool.reserveA,9,5); $('reserve-b').textContent = formatAmount(pool.reserveB,6);
    $('list-reserve-a').textContent = formatAmount(pool.reserveA,9); $('list-reserve-b').textContent = formatAmount(pool.reserveB,6);
    $('connection-status').textContent = 'Mainnet connected';
    const time = new Date(lastObserved).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    $('observed-time').textContent = time; $('pool-refresh-status').textContent = `Updated ${time}`;
    observations.push({time:lastObserved,price:Number(price)}); if (observations.length > 90) observations.shift(); renderValuations();
  } catch (error) {
    $('connection-status').textContent = 'Mainnet unavailable';
    $('pool-refresh-status').textContent = currentPool ? 'Last known pool values · refresh unavailable' : error.message;
  } finally { poolBusy = false; $('refresh-pool').disabled = false; renderValuations(); }
}
$('refresh-pool').onclick = refreshPool;
let marketSnapshot = null, launchpadRegistry = null, marketsBusy = false;
function marketMethodology() {
  modal('Circulating market capitalization', '<p>Market capitalization is the current pool price multiplied by circulating supply. The on-chain quote is in xUSDC. Financial estimates use its intended 1:1 USDC backing as a USD reference, then apply current FX for your selected currency.</p><p>All unlocked on-chain XRS is included: genesis allocations, mined wallet balances and active approved AMM inventory. Treasury and company ownership alone do not exclude holdings. Burned, staked and locked balances are excluded; wrapped XRS is counted once.</p><p class="footnote">These are USD-equivalent estimates, not a guaranteed redemption price. xUSDC is issued by the Xeris treasury and is separate from Circle-issued USDC. A dash means pricing, circulating supply or a current FX quote is unavailable; expired FX rates are never reused.</p><a class="button outline" href="#network" id="view-market-network">View network intelligence</a>');
  $('view-market-network').onclick = closeModal;
}
$('xrs-market-ticker').onclick = marketMethodology;
function supplyMethodology() {
  marketMethodology();
  if (!marketSnapshot) return;
  $('dialog-title').textContent = 'Circulating supply policy';
  const intro = document.createElement('p'); intro.textContent = marketSnapshot.policy.description;
  const label = document.createElement('h3'); label.textContent = 'Company wallets · unlocked holdings included';
  const list = document.createElement('div'); list.className = 'policy-wallets';
  for (const row of marketSnapshot.policy.companyWallets) {
    const item = document.createElement('div'), role = document.createElement('strong'), address = document.createElement('span');
    role.textContent = row.role; address.textContent = row.address; item.append(role,address); list.append(item);
  }
  const policy = document.createElement('p'); policy.className = 'footnote'; policy.textContent = `Policy: ${marketSnapshot.policy.id}. As of block ${marketSnapshot.slot}.`;
  $('dialog-body').replaceChildren(intro,label,list,policy);
}
$('supply-methodology').onclick = supplyMethodology;
function renderMarkets() {
  const nodes = [];
  try {
    if (!launchpadRegistry) throw new Error('Listings unavailable');
    for (const token of mergeLaunchpadMarkets(launchpadRegistry, marketSnapshot)) {
      const button = document.createElement('button'); button.className = 'market-ticker';
      const exactValue = token.marketCapRaw === null ? null : formatQuantity(token.marketCapRaw,6);
      button.title = `${token.name} · ${token.id}${exactValue === null ? '' : ' · ' + exactValue + ' xUSDC'}`;
      const fullMoney = token.marketCapRaw === null ? null : money(token.marketCapRaw,{maximumFractionDigits:6});
      const valueText = token.marketCapRaw === null ? '—' : money(token.marketCapRaw,{compact:true,maximumFractionDigits:2}) || '—';
      button.setAttribute('aria-label', `${token.symbol} market capitalization ${fullMoney === null ? selectedCurrency() + ' equivalent unavailable' : fullMoney + ' equivalent · ' + exactValue + ' xUSDC'}`);
      const symbol = document.createElement('b'); symbol.textContent = token.symbol;
      const label = document.createElement('small'); label.textContent = 'Market cap';
      const value = document.createElement('strong'); value.textContent = valueText;
      const unit = document.createElement('span'); unit.className = 'ticker-unit'; unit.textContent = selectedCurrency();
      button.append(symbol,label,value,unit); button.onclick = marketMethodology; nodes.push(button);
    }
    if (!nodes.length) {
      const empty = document.createElement('span'); empty.className = 'market-ticker market-empty';
      empty.textContent = 'Launchpad markets · No listings yet'; nodes.push(empty);
    }
  } catch {
    const status = document.createElement('span'); status.className = 'market-ticker market-empty';
    status.textContent = 'Launchpad markets unavailable'; nodes.splice(0,nodes.length,status);
  }
  $('launchpad-tickers').replaceChildren(...nodes);
}
async function refreshMarkets() {
  if (marketsBusy) return;
  marketsBusy = true;
  try { await checkChain(); launchpadRegistry = await getLaunchpadTokens(); }
  catch { launchpadRegistry = null; }
  finally { marketsBusy = false; renderMarkets(); }
}
const timeLabel = millis => new Date(millis).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
function setFeedValue(name, value) { document.querySelectorAll(`[data-feed="${name}"]`).forEach(el => { el.textContent = value; }); }
let lastBlockHashes = new Set();
function renderNetwork(state) {
  marketSnapshot = state.status === 'ready' ? state : null;
  const label = state.status === 'ready' ? 'Updating live' : state.status === 'paused' ? 'Paused' : state.status === 'stale' ? 'Data expired' : 'Reconnecting';
  $('network-live-status').textContent = label; $('network-live-status').dataset.state = state.status;
  setFeedValue('state',label);
  if (!marketSnapshot) {
    ['circulating','market-cap','mined','slot'].forEach(name => setFeedValue(name,'—'));
    $('xrs-market-cap').textContent = '—'; $('xrs-market-ticker').setAttribute('aria-label','XRS market capitalization unavailable');
    $('xrs-market-ticker').title = 'XRS market capitalization unavailable';
    document.querySelectorAll('[data-feed="market-cap"]').forEach(el => el.removeAttribute('title'));
    document.querySelectorAll('[data-feed="mined"], [data-feed="circulating"]').forEach(el => { el.removeAttribute('title'); el.removeAttribute('aria-label'); });
    $('block-reward').textContent = '—'; $('block-observed-time').textContent = 'Awaiting network';
    $('network-update-time').textContent = state.reason || 'Connecting to the public feed.';
    $('block-stream-status').textContent = state.reason || 'Connecting to mainnet…';
    $('block-stream-status').classList.remove('hidden'); $('block-rows').replaceChildren();
    renderValuations(); return;
  }
  setFeedValue('circulating',formatQuantity(state.circulatingRaw,9,2));
  document.querySelectorAll('[data-feed="circulating"]').forEach(el => { const exact = `${formatQuantity(state.circulatingRaw,9)} XRS`; el.title = exact; el.setAttribute('aria-label', `Circulating supply: ${exact}`); });
  setFeedValue('mined',formatQuantity(state.minedRaw,9,2));
  document.querySelectorAll('[data-feed="mined"]').forEach(el => { const exact = `${formatQuantity(state.minedRaw,9)} XRS`; el.title = exact; el.setAttribute('aria-label', `Total mined: ${exact}`); });
  setFeedValue('slot',formatQuantity(state.slot,0));
  $('block-reward').textContent = `${state.reward.replace(/(\.\d*?[1-9])0+$|\.0+$/,'$1')} XRS`;
  $('block-observed-time').textContent = timeLabel(state.blocks[0].timestamp);
  $('network-update-time').textContent = `Census updated ${timeLabel(state.observedAt)}`;
  $('block-stream-status').classList.add('hidden');
  const rows = state.blocks.map(block => {
    const tr = document.createElement('tr');
    if (lastBlockHashes.size && !lastBlockHashes.has(block.hash)) tr.className = 'new-block';
    const slot = document.createElement('td'), blockNumber = document.createElement('strong'), hash = document.createElement('span');
    blockNumber.textContent = formatQuantity(block.slot,0); hash.textContent = block.hash.slice(0,8) + '…' + block.hash.slice(-6); hash.className = 'block-hash'; hash.title = block.hash; slot.append(blockNumber,hash);
    const time = document.createElement('td'); time.textContent = timeLabel(block.timestamp); time.title = new Date(block.timestamp).toLocaleString();
    const producer = document.createElement('td'); producer.textContent = block.proposer.slice(0,6) + '…' + block.proposer.slice(-5); producer.title = block.proposer; producer.className = 'producer-address';
    const tx = document.createElement('td'); tx.textContent = formatQuantity(block.transactionCount,0);
    tr.append(slot,time,producer,tx); return tr;
  });
  lastBlockHashes = new Set(state.blocks.map(row => row.hash)); $('block-rows').replaceChildren(...rows); renderValuations();
}
const marketFeed = createMarketFeed({onState:renderNetwork});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { marketFeed.stop(); currencyController.stop(); }
  else { marketFeed.start(); currencyController.start(); refreshMarkets(); }
});
window.addEventListener('pagehide', () => { marketFeed.stop(); currencyController.stop(); });
window.addEventListener('pageshow', () => { if (!document.hidden) { marketFeed.start(); currencyController.start(); } });

function renderBalances() {
  $('wallet-address').textContent = address || 'Connect your Xeris wallet to view balances.';
  $('native-balance').textContent = balances ? formatAmount(balances.nativeRaw,9) : '—';
  $('wrapped-balance').textContent = balances ? formatAmount(balances.wrappedRaw,9) : '—';
  $('usdc-balance').textContent = balances ? formatAmount(balances.usdcRaw,6) : '—';
  $('pay-balance').textContent = balances ? `Balance: ${formatAmount(buying ? balances.usdcRaw : balances.wrappedRaw,buying?6:9,6)}` : 'Connect for balance';
  renderFiatEstimates();
}
async function refreshBalances() {
  if (!address) return;
  const owner = address, generation = connectionGeneration;
  let read = balanceReads.get(owner);
  if (!read) {
    read = getBalances(owner).finally(() => { if (balanceReads.get(owner) === read) balanceReads.delete(owner); });
    balanceReads.set(owner, read);
  }
  try { const result = await read; if (address === owner && connectionGeneration === generation) { balances = result; renderBalances(); } }
  catch(error) { if (address === owner && connectionGeneration === generation) { balances = null; renderBalances(); say(error.message); } }
}
function pending() {
  if (!address) return null;
  try { const item = getPending(address); historyError = false; return item; }
  catch(error) { historyError = true; message(error.message,true); return {status:'pending'}; }
}
function updateAction() {
  const outstanding = pending();
  $('trade-button').textContent = connecting ? 'Connecting wallet…' : busy ? 'Waiting for wallet / transaction…' : outstanding ? 'Check pending transaction' : address ? 'Review swap' : 'Connect Wallet';
  $('trade-button').disabled = busy || connecting || historyError || (!!address && !outstanding && !quote);
  ['pay-amount','buy-tab','sell-tab','reverse'].forEach(id => $(id).disabled = busy);
}
async function connect() {
  if (busy || connecting) return;
  if (address) {
    modal('Wallet connected', `<p class="detail-code" id="connected-address"></p><p>Only public balances and transaction IDs are stored by this website.</p><button class="outline button" id="disconnect-button">Disconnect</button>`);
    $('connected-address').textContent = address;
    $('disconnect-button').onclick = async () => { try { await provider.disconnect(); } catch {} clearConnection(); closeModal(); };
    return;
  }
  const candidate = window.xeris;
  if (!candidate || !(candidate.isXerisWallet || candidate.isXeris) || typeof candidate.signTransaction !== 'function') {
    modal('Connect your Xeris wallet', '<p>Open this site inside the Xeris Android wallet’s browser, or use a compatible Xeris wallet extension.</p><p>Choose the mainnet network in your wallet, then connect here.</p><p class="footnote">No seed phrase or private key is needed on this website. Solana wallets are used for the future USDC deposit step, not for signing Xeris trades.</p>'); return;
  }
  connecting = true; connectingProvider = candidate; const generation = ++connectionGeneration;
  if (!listeningProviders.has(candidate)) {
    const changed = () => { if (provider === candidate || connectingProvider === candidate) clearConnection(); };
    candidate.on?.('accountChanged',changed); candidate.on?.('disconnect',changed); listeningProviders.add(candidate);
  }
  $('connect-button').disabled = true; updateAction();
  try {
    const result = await candidate.connect(); const owner = String(result.publicKey ?? result);
    if (generation !== connectionGeneration) return;
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(owner)) throw new Error('The wallet did not return a valid public address.');
    if (String(await candidate.getRpcUrl()).replace(/\/$/,'') !== CONFIG.rpc) throw new Error('Select Xeris mainnet in your wallet first.');
    await checkChain(); if (generation !== connectionGeneration) return;
    provider = candidate; connectingProvider = null; address = owner; balances = null; renderBalances();
    $('connect-button').textContent = `${owner.slice(0,5)}…${owner.slice(-4)}`;
    await refreshBalances(); if (generation !== connectionGeneration) return;
    renderHistory(); updateAction();
    if (pending()) { location.hash = 'activity'; say('A saved transaction needs confirmation. Check its original ID before trading again.'); }
  } catch(error) { if (generation === connectionGeneration) { clearConnection(); say(error.message); } }
  finally { if (generation === connectionGeneration) { connecting = false; connectingProvider = null; $('connect-button').disabled = false; updateAction(); } }
}
function clearConnection() { connectionGeneration++; connecting = false; connectingProvider = null; $('connect-button').disabled = false; provider = null; address = null; balances = null; $('connect-button').textContent = 'Connect Wallet'; renderBalances(); renderHistory(); updateAction(); }
$('connect-button').onclick = connect;
function setDirection(value) {
  if (busy) return;
  buying = value; quote = null; quoteGeneration++; clearTimeout(quoteTimer);
  $('buy-tab').classList.toggle('selected',buying); $('sell-tab').classList.toggle('selected',!buying);
  $('pay-token').innerHTML = buying ? '<img src="/assets/usdc.png" alt="">xUSDC' : '<img src="/assets/xeris.png" alt="">wXRS';
  $('receive-token').innerHTML = buying ? '<img src="/assets/xeris.png" alt="">wXRS' : '<img src="/assets/usdc.png" alt="">xUSDC';
  $('receive-note').textContent = buying ? 'Convert to native after swap' : 'Redemption is a separate step';
  renderBalances(); scheduleQuote();
}
$('buy-tab').onclick = () => setDirection(true); $('sell-tab').onclick = () => setDirection(false); $('reverse').onclick = () => setDirection(!buying);
function clearQuote() { quote = null; ['minimum-output','price-impact','receive-amount'].forEach(id => $(id).textContent = '—'); $('trade-fee').textContent = '0.3%'; renderFiatEstimates(); updateAction(); }
function scheduleQuote() {
  const generation = ++quoteGeneration; clearTimeout(quoteTimer); clearQuote();
  if (!$('pay-amount').value.trim()) { message('Enter an amount to get a live quote.'); return; }
  let amount; try { amount = parseAmount($('pay-amount').value,buying?6:9); } catch(error) { message(error.message,true); return; }
  message('Fetching the current pool quote…');
  const direction = buying;
  quoteTimer = setTimeout(async () => {
    try {
      const result = await getQuote(direction,amount); if (generation !== quoteGeneration) return;
      quote = result; const decimals = direction?9:6, token = direction?'wXRS':'xUSDC';
      $('receive-amount').textContent = formatAmount(result.outputRaw,decimals,decimals);
      $('minimum-output').textContent = `${formatAmount(result.minimumOutputRaw,decimals)} ${token}`;
      $('price-impact').textContent = result.priceImpactPercent + '%';
      $('trade-fee').textContent = `${formatAmount(result.feeRaw,direction?6:9)} ${direction?'xUSDC':'wXRS'}`;
      message(Number(result.priceImpactPercent) >= 5 ? 'This small pool gives this trade a price impact of 5% or more. Review the amount carefully.' : direction ? 'Swap to wXRS, then convert it 1:1 to native XRS.' : 'Selling native XRS? Convert it to wXRS in Portfolio first.');
      renderFiatEstimates(); updateAction();
    } catch(error) { if (generation === quoteGeneration) { clearQuote(); message(error.message,true); } }
  },650);
}
$('pay-amount').addEventListener('input',scheduleQuote);
$('swap-form').onsubmit = async e => {
  e.preventDefault(); if (busy) return;
  if (!address) { await connect(); return; }
  if (pending()) { location.hash='activity'; await reconcilePending(); return; }
  if (!quote || Date.now() - quote.quotedAt > 30000) { scheduleQuote(); message('Quote expired. Review the refreshed amount before signing.'); return; }
  const q = quote, owner = address;
  modal('Review your swap', `<p class="review-amount">${formatAmount(q.inputRaw,q.buying?6:9)} ${q.buying?'xUSDC':'wXRS'}</p><p>Estimated received: ${formatAmount(q.outputRaw,q.buying?9:6)} ${q.buying?'wXRS':'xUSDC'}</p><p>Minimum received: ${formatAmount(q.minimumOutputRaw,q.buying?9:6)} ${q.buying?'wXRS':'xUSDC'}</p><p>Pool fee: 0.3%, included. Network fee: 0.001 native XRS.</p><p>${q.buying?'A separate 1:1 unwrap makes the received wXRS native. That step costs another 0.001 XRS.':'This gives you xUSDC on Xeris. Solana USDC redemption is currently unavailable.'}</p><button class="primary wide" id="approve-swap">Approve in wallet</button>`);
  $('approve-swap').onclick = async () => {
    if (busy || address !== owner) return;
    closeModal(); await submitStep({action:q.buying?'buy':'sell',amountRaw:q.inputRaw,minOutputRaw:q.minimumOutputRaw,expectedOutputRaw:q.outputRaw,quoteTime:q.quotedAt});
  };
};
async function submitStep(request) {
  if (busy || !address || !provider) return;
  busy = true; const owner = address; const selectedProvider = provider; updateAction();
  message('Review the exact transaction in your wallet.');
  try {
    const result = await execute({provider:selectedProvider,address:owner,...request}, {onStatus:text => { if (address === owner) message(text); }});
    if (address !== owner) { say('Transaction saved for the original wallet. Reconnect it to check activity.'); return; }
    renderHistory();
    if (result.status === 'confirmed') { say(request.action === 'buy' ? 'Swap confirmed. Convert the received wXRS to native XRS in Portfolio.' : `${request.action === 'sell'?'Swap':'Conversion'} confirmed.`); }
    else if (result.status === 'failed') say('Execution failed. The transaction ID is saved in Activity.');
    else { say('Transaction submitted. Its original ID is saved. Check Activity for confirmation.'); location.hash='activity'; }
    await refreshBalances(); clearQuote();
    message('Review Activity for the submitted transaction, then request a fresh quote.');
  } catch(error) { message(error.message,true); say(error.message); renderHistory(); }
  finally { busy = false; updateAction(); }
}
function conversion() {
  if (busy) return;
  if (!address) { connect(); return; }
  if (pending()) { location.hash='activity'; say('Check the pending transaction before starting another.'); return; }
  modal('Convert XRS 1:1', `<p>Native XRS and wXRS stay on Xeris. This conversion does not cross chains.</p><label for="conversion-action">Direction</label><select id="conversion-action"><option value="unwrap">Wrapped XRS to native XRS</option><option value="wrap">Native XRS to wrapped XRS</option></select><label for="conversion-amount">Amount of XRS</label><input id="conversion-amount" type="text" inputmode="decimal" autocomplete="off" placeholder="0.00"><p class="footnote" id="conversion-balance"></p><p>Network fee: 0.001 native XRS. Keep another 0.001 XRS if you plan to swap next.</p><p id="conversion-error" class="danger" role="status"></p><button class="primary wide" id="conversion-submit">Review conversion</button>`);
  const owner = address;
  const update = () => { $('conversion-balance').textContent = balances ? `Available: ${formatAmount($('conversion-action').value==='unwrap'?balances.wrappedRaw:balances.nativeRaw,9)} ${$('conversion-action').value==='unwrap'?'wXRS':'native XRS'}` : 'Refresh wallet balances before converting.'; };
  update(); $('conversion-action').onchange=update;
  $('conversion-submit').onclick = () => {
    if (busy || owner !== address) return;
    let amount; try { amount = parseAmount($('conversion-amount').value,9); } catch(error) { $('conversion-error').textContent=error.message; return; }
    const action = $('conversion-action').value;
    modal('Review conversion', `<p class="review-amount">${formatAmount(amount,9)} XRS</p><p>${action==='unwrap'?'Wrapped XRS → native XRS':'Native XRS → wrapped XRS'}</p><p>1:1 conversion · network fee 0.001 native XRS.</p><button class="primary wide" id="approve-conversion">Approve in wallet</button>`);
    $('approve-conversion').onclick=async()=>{ if (owner!==address || busy) return; closeModal(); await submitStep({action,amountRaw:amount}); };
  };
}
document.querySelectorAll('[data-convert]').forEach(el=>el.onclick=conversion);
function renderHistory() {
  const container = $('activity-list'); container.replaceChildren();
  if (!address) { container.textContent='Connect your wallet to view this browser’s transaction history.'; return; }
  let history; try { history = getHistory(address); historyError = false; } catch(error) { container.textContent = error.message; historyError = true; return; }
  if (!history.length) { container.textContent = 'No transactions have been submitted from this wallet in this browser.'; return; }
  for (const item of [...history].reverse()) {
    const article = document.createElement('article'); article.className='activity-item';
    const heading=document.createElement('strong'); heading.textContent=`${item.action} · ${item.status}`;
    const signature=document.createElement('p'); signature.className='signature'; signature.textContent=item.signature;
    const note=document.createElement('p'); note.textContent=item.status==='pending'?'Pending or unknown. Check this ID; do not submit a replacement.':item.status==='confirmed'?'Xeris execution confirmed. Cross-chain redemption is separate.':'Execution failed. Review the receipt before another attempt.';
    article.append(heading,signature,note); container.append(article);
  }
}
async function reconcilePending() {
  if (!address || reconcileBusy || busy) return;
  reconcileBusy=true; const owner=address, generation=connectionGeneration; $('reconcile-button').disabled=true;
  try { const result=await reconcile(owner); if (address!==owner || generation!==connectionGeneration) return; renderHistory(); updateAction(); await refreshBalances(); if(address===owner && generation===connectionGeneration) say(result ? `Transaction status: ${result.status}.` : 'No pending transaction.'); }
  catch(error) { if(address===owner && generation===connectionGeneration) say(error.message); }
  finally { reconcileBusy=false; $('reconcile-button').disabled=false; }
}
$('reconcile-button').onclick=reconcilePending;
const CURRENCY_PREFERENCE_KEY = 'xeris-display-currency-v1';
let savedCurrency = 'USD';
try { const preference = localStorage.getItem(CURRENCY_PREFERENCE_KEY); if (CURRENCIES.some(item => item.code === preference)) savedCurrency = preference; } catch {}
$('display-currency').replaceChildren(...CURRENCIES.map(item => { const option = document.createElement('option'); option.value = item.code; option.textContent = item.code; option.title = item.label; option.setAttribute('aria-label',item.label); return option; }));
const currencyController = createCurrencyController({initialCurrency:savedCurrency,onChange:snapshot => { currencySnapshot = snapshot; renderValuations(); }});
currencySnapshot = currencyController.getSnapshot();
$('display-currency').value = savedCurrency;
$('display-currency').onchange = () => {
  const code = $('display-currency').value;
  if (!CURRENCIES.some(item => item.code === code)) return;
  try { localStorage.setItem(CURRENCY_PREFERENCE_KEY,code); } catch {}
  currencyController.select(code);
};
route(); refreshPool(); refreshMarkets(); updateAction(); renderValuations();
if (!document.hidden) { marketFeed.start(); currencyController.start(); }
setInterval(() => { if (!document.hidden) refreshMarkets(); }, 60000);
setInterval(()=>{ if (!document.hidden && !busy) refreshPool(); if (quote && Date.now()-quote.quotedAt>30000) { clearQuote(); message('Quote expired. Refresh by editing the amount.'); } },30000);
