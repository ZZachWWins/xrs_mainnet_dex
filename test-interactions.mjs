import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const app=readFileSync(new URL('./dist/app.mjs',import.meta.url),'utf8');
const span=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
function element(id='') {
  const values=new Set();
  return {id,attrs:{},focused:[],children:[],
    classList:{toggle(name,on){if(on===undefined)on=!values.has(name);on?values.add(name):values.delete(name);return on;},contains:name=>values.has(name)},
    setAttribute(name,value){this.attrs[name]=value;},removeAttribute(name){delete this.attrs[name];},
    focus(options){this.focused.push(options);},contains(child){return child===this||this.children.includes(child);}};
}
function navigation() {
  const pages=['home','swap','pools','bridge','network','portfolio','activity'].map(id=>{
    const page=element(id);page.heading=element(id+'-heading');page.querySelector=()=>page.heading;return page;
  });
  const links=pages.map(page=>Object.assign(element(),{hash:'#'+page.id}));
  const menu=element('menu-button'),nav=element('nav');nav.children=links;
  const handlers={},scrolls=[];
  const context={location:{hash:'#swap',pathname:'/'},address:null,
    $:id=>id==='menu-button'?menu:pages.find(page=>page.id===id),
    document:{querySelector:()=>nav,querySelectorAll:selector=>selector==='main>.page'?pages:links,
      addEventListener(name,fn){handlers[name]=fn;}},
    window:{scrollTo:options=>scrolls.push(options)},refreshBalances(){assert.fail('Unexpected account read');},renderHistory(){}};
  runInNewContext(span('function setMenuOpen(',"window.addEventListener('hashchange'")+
    span("$('menu-button').onclick =", "$('more-button').onclick"),context);
  return {pages,links,menu,nav,handlers,context,scrolls};
}
test('user navigation identifies and focuses the selected view; initial loading does not steal focus',()=>{
  const f=navigation();f.context.route();
  const swap=f.pages.find(page=>page.id==='swap');
  assert.equal(swap.heading.focused.length,0);assert.equal(swap.classList.contains('view-arriving'),false);
  assert.equal(f.links.find(link=>link.hash==='#swap').attrs['aria-current'],'page');
  f.context.route({type:'hashchange'});
  assert.equal(swap.heading.attrs.tabindex,'-1');assert.equal(swap.heading.focused.length,1);
  assert.equal(swap.heading.focused[0].preventScroll,true);assert.equal(swap.classList.contains('view-arriving'),true);
  f.context.location.hash='#pools';f.context.route({type:'hashchange'});
  assert.equal(swap.classList.contains('view-arriving'),false);assert.equal(swap.classList.contains('hidden'),true);
  assert.equal(f.links.find(link=>link.hash==='#swap').attrs['aria-current'],undefined);
  assert.equal(f.links.find(link=>link.hash==='#pools').attrs['aria-current'],'page');
});
test('mobile menu Escape restores trigger focus, outside click closes, and inner controls stay usable',()=>{
  const f=navigation();let prevented=0;
  f.menu.onclick();assert.equal(f.menu.attrs['aria-expanded'],'true');
  f.handlers.click({target:f.links[0]});assert.equal(f.nav.classList.contains('open'),true);
  f.handlers.keydown({key:'Escape',preventDefault(){prevented++;}});
  assert.equal(prevented,1);assert.equal(f.menu.attrs['aria-expanded'],'false');assert.equal(f.menu.focused.length,1);
  f.menu.onclick();f.handlers.click({target:{id:'outside'}});
  assert.equal(f.nav.classList.contains('open'),false);assert.equal(f.menu.focused.length,1);
});
test('buy/sell pressed state follows the existing direction guard without triggering a financial action',()=>{
  const nodes=new Map(['buy-tab','sell-tab','pay-token','receive-token','receive-note'].map(id=>[id,element(id)]));
  let reads=0;
  const context={busy:true,buying:true,quote:{},quoteGeneration:0,quoteTimer:null,$:id=>nodes.get(id),
    clearTimeout(){},renderBalances(){},scheduleQuote(){reads++;}};
  runInNewContext(span('function setDirection(',"$('buy-tab').onclick"),context);
  context.setDirection(false);assert.equal(context.buying,true);assert.equal(reads,0);
  context.busy=false;context.setDirection(false);
  assert.equal(context.buying,false);assert.equal(nodes.get('sell-tab').attrs['aria-pressed'],'true');
  assert.equal(nodes.get('buy-tab').attrs['aria-pressed'],'false');assert.equal(reads,1);
});
test('navigation disclosure, initial toggle state and dialog names are explicit in the static page',()=>{
  const html=readFileSync(new URL('./dist/index.html',import.meta.url),'utf8');
  assert.match(html,/id="menu-button" aria-controls="main-navigation"/);
  assert.match(html,/<nav id="main-navigation" aria-label="Main navigation">/);
  assert.match(html,/id="buy-tab" class="selected" aria-pressed="true"/);
  assert.match(html,/id="sell-tab" aria-pressed="false"/);
  assert.match(html,/<dialog id="info-dialog" aria-labelledby="dialog-title">/);
  assert.ok(html.includes('href="/interactions.css"'));
});
