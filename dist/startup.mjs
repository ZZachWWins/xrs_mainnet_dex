// A bounded module-start failure leaves an accessible recovery screen.
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
let timer;
try {
  await Promise.race([import('./app.mjs'),new Promise((_,reject) => {timer=setTimeout(()=>reject(new Error('Startup timeout')),15000);})]);
} catch { failed(); }
finally { clearTimeout(timer); }
