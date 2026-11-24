// Presentation gating only. This module has no API, wallet or storage dependency.
export const DEFAULT_NETWORK = 'testnet';
export const DEX_PAGES = Object.freeze(['home','swap','pools','bridge','network','portfolio','activity']);
const NETWORKS = new Set(['mainnet','testnet']);

export function selectedNetwork(input) {
  const values = new URL(input).searchParams.getAll('network');
  if (!values.length) return DEFAULT_NETWORK;
  return values.length === 1 && NETWORKS.has(values[0]) ? values[0] : null;
}
export function selectedPage(input, network = selectedNetwork(input)) {
  if (network !== 'mainnet') return 'home';
  const url = new URL(input);
  const candidate = url.hash.slice(1) || url.pathname.replace(/^\/|\/$/g,'') || 'home';
  return DEX_PAGES.includes(candidate) ? candidate : 'home';
}
export function networkHref(input, network) {
  if (!NETWORKS.has(network)) throw new Error('Unknown DEX version.');
  const url = new URL(input), page = selectedPage(input,network);
  // Keep a supported page across a full-document switch. Duplicate/invalid
  // network parameters are replaced, never copied into the new selection.
  url.pathname = '/'; url.searchParams.delete('network'); url.searchParams.set('network',network);
  url.hash = network === 'testnet' ? '' : page;
  return url.pathname + url.search + url.hash;
}
export async function bootDex({url, render, loadMainnet}) {
  const mode = selectedNetwork(url), page = selectedPage(url);
  render(Object.freeze({mode,page}));
  if (mode !== 'mainnet') return Object.freeze({mode,started:false});
  await loadMainnet();
  return Object.freeze({mode,started:true});
}

export function renderMode(document, {mode,page}) {
  const shell = document.getElementById('dex-shell'), startup = document.getElementById('dex-startup');
  const notice = document.getElementById('dex-mode-notice');
  document.getElementById('dex-mainnet-link').hidden = mode !== 'testnet';
  document.documentElement.dataset.network = mode || 'invalid';
  for (const choice of document.querySelectorAll('[data-dex-network]')) {
    choice.setAttribute('aria-pressed',String(choice.dataset.dexNetwork === mode));
  }
  document.getElementById('dex-version-label').textContent = mode === 'mainnet' ? 'Mainnet Beta' : mode === 'testnet' ? 'Testnet' : 'Choose a version';
  if (mode === 'mainnet') {
    notice.hidden = true;
    document.title = 'Xeris DEX · Mainnet Beta';
    document.getElementById('startup-message').textContent = 'Connecting to Xeris mainnet…';
    return;
  }
  startup.hidden = true; notice.hidden = false;
  document.title = mode === 'testnet' ? 'Xeris DEX · Testnet preview' : 'Xeris DEX · Choose a version';
  document.getElementById('dex-mode-title').textContent = mode === 'testnet' ? 'Available only on Xeris Mainnet Beta' : 'Choose a DEX version';
  document.getElementById('dex-mode-description').textContent = mode === 'testnet'
    ? 'Switch to Mainnet Beta for markets, balances and trading.'
    : 'Select Mainnet Beta to use the DEX, or Testnet to view availability.';
  shell.hidden = mode !== 'testnet'; shell.inert = true;
  shell.setAttribute('aria-disabled','true'); shell.classList.add('presentation-only');
  for (const control of shell.querySelectorAll('button,input,select,textarea,fieldset')) control.disabled = true;
  for (const link of shell.querySelectorAll('a,[tabindex]')) {
    link.setAttribute('tabindex','-1'); link.setAttribute('aria-disabled','true');
  }
  for (const section of document.querySelectorAll('main>.page')) section.classList.toggle('hidden',section.id !== page);
  for (const link of document.querySelectorAll('nav a')) link.classList.toggle('active',link.hash === '#' + page);
  // Static placeholders must not imply that a disabled preview is connecting.
  for (const id of ['launchpad-tickers','pool-refresh-status','pool-list-status','connection-status','network-live-status',
    'block-stream-status','network-update-time','observed-time','block-observed-time']) {
    const node = document.getElementById(id); if (node) node.textContent = 'Available on Mainnet Beta';
  }
  for (const node of document.querySelectorAll('[data-feed="state"]')) node.textContent = 'Unavailable on Testnet';
}
