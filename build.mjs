import { access, readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
const modules=['dist/app.mjs','dist/model.mjs','dist/trade.mjs','dist/markets.mjs','dist/feed.mjs','dist/currency.mjs','dist/bridge.mjs'];
for(const file of ['dist/index.html','dist/style.css','dist/assets/xeris.png','dist/assets/usdc.png','dist/assets/inter.woff2','dist/assets/jakarta.woff2','dist/assets/solana.svg','dist/assets/ethereum.png','dist/assets/base.svg','dist/assets/arbitrum.svg','dist/assets/polygon.svg','dist/assets/avalanche.svg',...modules]) await access(file);
for(const file of modules) { const result=spawnSync(process.execPath,['--check',file],{stdio:'inherit'}); if(result.status!==0) process.exit(result.status??1); }
const html=await readFile('dist/index.html','utf8');
if(!html.includes('Content-Security-Policy')) throw new Error('Missing content security policy.');
console.log('Static deployment files verified in dist/. No dependencies or compilation required.');
