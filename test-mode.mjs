import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DEFAULT_NETWORK,DEX_PAGES,selectedNetwork,selectedPage,networkHref,bootDex,renderMode} from './dist/mode.mjs';

const base='https://dex.xerisweb.com/';

test('the single configurable default currently opens the inert Testnet homepage', () => {
  assert.equal(DEFAULT_NETWORK,'testnet');
  assert.equal(selectedNetwork(base),DEFAULT_NETWORK);
  for(const path of ['', '#swap','swap','bridge#portfolio']) {
    assert.equal(selectedNetwork(base+path),'testnet');
    assert.equal(selectedPage(base+path),'home');
  }
});
test('only one exact recognized network parameter is accepted', () => {
  for(const query of ['network=', 'network=TESTNET', 'network=other', 'network=mainnet&network=testnet', 'network=testnet&network=testnet']) {
    assert.equal(selectedNetwork(base+'?'+query),null);
  }
  assert.equal(selectedNetwork(base+'?network=mainnet'),'mainnet');
  assert.equal(selectedNetwork(base+'?network=testnet'),'testnet');
});
test('Testnet and invalid mode never import the active app, regardless of route', async () => {
  for(const suffix of ['','#swap','?network=testnet#bridge','swap?network=testnet','?network=invalid','?network=mainnet&network=testnet']) {
    const rendered=[];let imports=0;
    const result=await bootDex({url:base+suffix,render:state=>rendered.push(state),loadMainnet:()=>{imports++;throw new Error('Must never import active app');}});
    assert.equal(imports,0);assert.equal(result.started,false);assert.equal(rendered[0].page,'home');
  }
});
test('explicit Mainnet imports once after rendering mode and retains safe direct routes', async () => {
  for(const page of DEX_PAGES) {
    const order=[];
    const result=await bootDex({url:base+'?network=mainnet#'+page,render:state=>order.push(['render',state.mode,state.page]),loadMainnet:async()=>order.push(['import'])});
    assert.equal(result.started,true);assert.deepEqual(order,[['render','mainnet',page],['import']]);
  }
  assert.equal(selectedPage(base+'swap?network=mainnet'),'swap');
  for(const route of ['unknown','<script>','%73wap','/swap']) assert.equal(selectedPage(base+'?network=mainnet#'+route),'home');
});
test('network switch keeps safe Mainnet route but always resets Testnet to home', () => {
  assert.equal(networkHref(base+'?network=mainnet#swap','testnet'),'/?network=testnet');
  assert.equal(networkHref(base+'?network=testnet#home','mainnet'),'/?network=mainnet#home');
  assert.equal(networkHref(base+'swap?network=invalid&network=testnet','mainnet'),'/?network=mainnet#swap');
  assert.equal(networkHref(base+'?network=testnet#nonsense','mainnet'),'/?network=mainnet#home');
  assert.throws(()=>networkHref(base,'other'));
});
function element(id='') {
  return {id,textContent:'',hidden:false,inert:false,disabled:false,attrs:{},dataset:{},hash:'',
    setAttribute(name,value){this.attrs[name]=value;},
    classList:{values:new Set(),add(name){this.values.add(name);},toggle(name,on){on?this.values.add(name):this.values.delete(name);}}};
}
function fixture() {
  const ids=['dex-shell','dex-startup','dex-mode-notice','dex-mainnet-link','dex-mode-title','dex-mode-description','dex-version-label','startup-message',
    'launchpad-tickers','pool-refresh-status','pool-list-status','connection-status','network-live-status','block-stream-status','network-update-time','observed-time','block-observed-time'];
  const nodes=new Map(ids.map(id=>[id,element(id)]));
  const choices=['mainnet','testnet'].map(mode=>Object.assign(element(),{dataset:{dexNetwork:mode}}));
  const pages=DEX_PAGES.map(element),controls=[element('wallet'),element('quote'),element('sign')],links=[element('nav')],feed=[element()];
  nodes.get('dex-shell').querySelectorAll=selector=>selector.startsWith('button')?controls:links;
  const document={documentElement:{dataset:{}},title:'',getElementById:id=>nodes.get(id)||null,
    querySelectorAll:selector=>selector==='[data-dex-network]'?choices:selector==='main>.page'?pages:selector==='nav a'?links:feed};
  return {document,nodes,choices,pages,controls,links};
}
test('Testnet locks the full product surface while exact notice and Mainnet CTA remain usable', () => {
  const f=fixture();renderMode(f.document,{mode:'testnet',page:'home'});
  assert.equal(f.nodes.get('dex-shell').hidden,false);assert.equal(f.nodes.get('dex-shell').inert,true);
  assert.equal(f.nodes.get('dex-shell').attrs['aria-disabled'],'true');
  assert.equal(f.nodes.get('dex-mode-title').textContent,'Available only on Xeris Mainnet Beta');
  assert.equal(f.nodes.get('dex-mainnet-link').hidden,false);assert.equal(f.nodes.get('dex-mainnet-link').disabled,false);
  assert.equal(f.nodes.get('dex-mode-notice').hidden,false);assert.equal(f.nodes.get('dex-startup').hidden,true);
  assert.ok(f.controls.every(node=>node.disabled));assert.ok(f.links.every(node=>node.attrs.tabindex==='-1'));
  assert.ok(f.choices.every(node=>!node.disabled));assert.equal(f.choices[1].attrs['aria-pressed'],'true');
  assert.ok(f.pages.every(node=>node.classList.values.has('hidden')===(node.id!=='home')));
});
test('invalid modes show only safe choices; Mainnet hides the Testnet-only CTA', () => {
  const invalid=fixture();renderMode(invalid.document,{mode:null,page:'home'});
  assert.equal(invalid.nodes.get('dex-shell').hidden,true);assert.equal(invalid.nodes.get('dex-startup').hidden,true);
  assert.equal(invalid.nodes.get('dex-mainnet-link').hidden,true);assert.ok(invalid.choices.every(node=>node.attrs['aria-pressed']==='false'));
  const mainnet=fixture();renderMode(mainnet.document,{mode:'mainnet',page:'swap'});
  assert.equal(mainnet.nodes.get('dex-mode-notice').hidden,true);assert.equal(mainnet.nodes.get('dex-mainnet-link').hidden,true);
  assert.equal(mainnet.nodes.get('dex-shell').inert,false);assert.ok(mainnet.controls.every(node=>!node.disabled));
});
test('HTML exposes only gated startup and keeps selector/CTA outside the disabled shell', () => {
  const html=readFileSync(new URL('./dist/index.html',import.meta.url),'utf8');
  assert.deepEqual([...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(match=>match[1]),['/startup.mjs']);
  assert.ok(html.indexOf('class="dex-network-bar"')<html.indexOf('id="dex-shell"'));
  assert.ok(html.indexOf('id="dex-mainnet-link"')<html.indexOf('id="dex-shell"'));
  const startup=readFileSync(new URL('./dist/startup.mjs',import.meta.url),'utf8');
  assert.match(startup,/location\.assign\(networkHref/);assert.doesNotMatch(startup,/history\.pushState/);
  const module=readFileSync(new URL('./dist/mode.mjs',import.meta.url),'utf8');
  assert.doesNotMatch(module,/\bimport\s|\bfetch\s*\(|\bEventSource\b|localStorage|window\.(xeris|phantom|solana)/);
});
