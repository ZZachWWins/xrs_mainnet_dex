import {bootDex, networkHref, renderMode, selectedNetwork} from './mode.mjs';

// Version choice is outside the product surface and always stays usable.
for (const button of document.querySelectorAll('[data-dex-network]')) {
  button.onclick = () => {
    if (selectedNetwork(location.href) === button.dataset.dexNetwork) return;
    location.assign(networkHref(location.href,button.dataset.dexNetwork));
  };
}
document.getElementById('dex-mainnet-link').onclick = () => location.assign(networkHref(location.href,'mainnet'));

const panel = document.getElementById('dex-startup');
const retry = document.getElementById('startup-retry');
retry.onclick = () => location.reload();
function failed() {
  if (panel.hidden) return;
  document.getElementById('startup-spinner').hidden = true;
  document.getElementById('startup-title').textContent = 'Xeris DEX could not start';
  document.getElementById('startup-message').textContent = 'The app did not finish loading. Check your connection and retry the page.';
  retry.hidden = false;
}
async function loadMainnet() {
  let timer;
  try {
    await Promise.race([import('./app.mjs'),new Promise((_,reject) => {timer=setTimeout(()=>reject(new Error('Startup timeout')),15000);})]);
  } finally { clearTimeout(timer); }
}
function showMode(state) {
  renderMode(document,state);
  if (state.mode === 'testnet') {
    const url = new URL(location.href); url.pathname = '/'; url.hash = '';
    history.replaceState(history.state,'',url.pathname + url.search + url.hash);
    window.scrollTo({top:0,behavior:'instant'});
  }
}
try {
  await bootDex({url:location.href,render:showMode,loadMainnet});
} catch { failed(); }
// Testnet always stays on its inert homepage. Active pages and network
// switches require explicit Mainnet selection and full document navigation.
if (selectedNetwork(location.href) === 'testnet') {
  window.addEventListener('hashchange',() => showMode({mode:'testnet',page:'home'}));
}

// Keep the version switch visible when the browser restores a prior scroll position.
window.addEventListener('pageshow', () => {
  if (selectedNetwork(location.href) !== 'mainnet') window.scrollTo({top:0,behavior:'instant'});
});
