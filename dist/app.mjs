import {CONFIG, getPool, getQuote, getBalances, checkChain, parseAmount, formatAmount} from './model.mjs';
import {execute, reconcile, getHistory, getPending} from './trade.mjs';
const $ = id => document.getElementById(id);
let provider = null, address = null, balances = null, buying = true, quote = null;
let quoteTimer, quoteGeneration = 0, busy = false, poolBusy = false, reconcileBusy = false, historyError = false;
let connectionGeneration = 0, connecting = false, connectingProvider = null;
const balanceReads = new Map();
const listeningProviders = new WeakSet();
let observations = [], currentPool = null, lastObserved = 0;
const say = message => { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(say.timer); say.timer = setTimeout(() => $('toast').classList.add('hidden'), 9000); };
const message = (text, error = false) => { $('trade-message').textContent = text; $('trade-message').classList.toggle('error', error); };
function modal(title, html) { $('dialog-title').textContent = title; $('dialog-body').innerHTML = html; if (!$('info-dialog').open) $('info-dialog').showModal(); }
function closeModal() { $('info-dialog').close(); }
$('close-dialog').onclick = closeModal;
$('info-dialog').addEventListener('click', e => { if (e.target === $('info-dialog')) closeModal(); });
function route() {
  const id = location.hash.slice(1) || location.pathname.replace(/^\/|\/$/g,'') || 'home';
  const allowed = ['home','swap','pools','bridge','portfolio','activity'];
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
  if (!observations.length) return;
  const values = observations.map(v => v.price), min = Math.min(...values), max = Math.max(...values);
  const range = Math.max(max - min, max * .003, .00000001);
  const center = (max + min) / 2;
  const points = observations.map((v, i) => [observations.length === 1 ? 400 : 16 + i * 768 / (observations.length - 1), 150 - (v.price - center) / range * 200]);
  const path = points.map(([x,y],i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ');
  $('chart-line').setAttribute('d', path);
  $('chart-area').setAttribute('d', points.length > 1 ? `${path} L${points.at(-1)[0]} 290 L${points[0][0]} 290 Z` : '');
  const [x,y] = points.at(-1); $('chart-point').setAttribute('cx',x); $('chart-point').setAttribute('cy',y);
  $('chart-caption').textContent = 'Pool pricing in xUSDC. Refreshed every 30 seconds.';
}
async function refreshPool() {
  if (poolBusy) return;
  poolBusy = true; $('refresh-pool').disabled = true;
  try {
    await checkChain();
    const pool = await getPool(); currentPool = pool; lastObserved = Date.now();
    const price = formatAmount(pool.reserveB * 100000000000n / pool.reserveA, 8);
    $('spot-price').textContent = price; $('ticker-price').textContent = price;
    $('ticker-reserve').textContent = `${formatAmount(pool.reserveB,6,4)} xUSDC`;
    $('reserve-a').textContent = formatAmount(pool.reserveA,9,5); $('reserve-b').textContent = formatAmount(pool.reserveB,6);
    $('list-reserve-a').textContent = formatAmount(pool.reserveA,9); $('list-reserve-b').textContent = formatAmount(pool.reserveB,6);
    $('connection-status').textContent = 'Mainnet connected';
    const time = new Date(lastObserved).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    $('observed-time').textContent = time; $('pool-refresh-status').textContent = `Updated ${time}`;
    observations.push({time:lastObserved,price:Number(price)}); if (observations.length > 90) observations.shift(); renderChart();
  } catch (error) {
    $('connection-status').textContent = 'Mainnet unavailable';
    $('pool-refresh-status').textContent = currentPool ? 'Last known pool values · refresh unavailable' : error.message;
  } finally { poolBusy = false; $('refresh-pool').disabled = false; }
}
$('refresh-pool').onclick = refreshPool;
function renderBalances() {
  $('wallet-address').textContent = address || 'Connect your Xeris wallet to view balances.';
  $('native-balance').textContent = balances ? formatAmount(balances.nativeRaw,9) : '—';
  $('wrapped-balance').textContent = balances ? formatAmount(balances.wrappedRaw,9) : '—';
  $('usdc-balance').textContent = balances ? formatAmount(balances.usdcRaw,6) : '—';
  $('pay-balance').textContent = balances ? `Balance: ${formatAmount(buying ? balances.usdcRaw : balances.wrappedRaw,buying?6:9,6)}` : 'Connect for balance';
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
function clearQuote() { quote = null; ['minimum-output','price-impact','receive-amount'].forEach(id => $(id).textContent = '—'); $('trade-fee').textContent = '0.3%'; updateAction(); }
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
      updateAction();
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
route(); refreshPool(); updateAction();
setInterval(()=>{ if (!document.hidden && !busy) refreshPool(); if (quote && Date.now()-quote.quotedAt>30000) { clearQuote(); message('Quote expired. Refresh by editing the amount.'); } },30000);
