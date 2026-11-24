import {createBridgeFlow,associatedUsdcAccount,parseSettlementAmount,formatSettlementAmount} from './bridge-flow.mjs';
import {validateRecipient} from './bridge.mjs';
import {formatAmount} from './model.mjs';

const ACTIVE_KEY='xeris-dex:mainnet-v4:settlement:active';
const routeKey=action=>action==='buy'?'solana_usdc_to_native_xrs':'xusdc_to_solana_usdc';
const pending=new Set(['submitting','submission_unknown','awaiting_funding','processing','manual_review','unresolved']);
const labels={quoted:'Quote ready',challenged:'Ownership approval needed',awaiting_funding:'Waiting for your source payment',processing:'Settlement processing',completed:'Settlement completed',refunded:'Refund completed',failed:'Settlement failed — review receipts',cancelled:'Request cancelled',submission_unknown:'Request submission unresolved',unresolved:'Reconciliation required',quote_unknown:'Quote unavailable',quoting:'Quote pending'};

export function mountBridge({document,solana,getXeris,connectXeris,connectSolana,convert,sell,sdk,storage=globalThis.localStorage,flow=createBridgeFlow({solana,getXeris})}){
  const $=id=>document.getElementById(id);
  let capabilities=null,checking=false,working=false,currentId=null,notice='',noticeError=false,disposed=false,capGeneration=0,lastCheck=0,lastContentKey='',lastTimeKey='';
  try{const saved=storage.getItem(ACTIVE_KEY);if(saved&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(saved))currentId=saved;}catch{}
  const history=()=>flow.history();
  const record=()=>history().find(r=>r.client_request_id===currentId)||null;
  function select(id){currentId=id;try{storage.setItem(ACTIVE_KEY,id||'');}catch{}if(id){const r=record();if(r){$(r.action==='buy'?'deposit-recipient':'redeem-recipient').value=r.destination_owner;$(r.action==='buy'?'deposit-amount':'redeem-amount').value=formatSettlementAmount(r.amount_raw,r.action==='buy'?'solana_usdc':'xusdc');}}render();}
  function node(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
  function rows(target,items){const dl=node('dl');for(const [label,text] of items){const row=node('div');row.append(node('dt',label),node('dd',text));dl.append(row);}target.append(dl);}
  function fresh(){const now=Math.floor(Date.now()/1000);return capabilities&&capabilities.expires_at>now&&capabilities.issued_at>=now-30;}
  function enabled(action){return fresh()&&capabilities.can_accept_funds&&capabilities.routes[routeKey(action)]?.enabled===true;}
  function tell(text,error=false){notice=text;noticeError=error;render();}
  async function run(label,operation){if(working)return;working=true;notice=label;noticeError=false;render();try{await operation();}catch(error){notice=error instanceof Error?error.message:'This step could not be confirmed. Check the saved request before another attempt.';noticeError=true;}finally{working=false;render();}}
  async function refresh(){if(checking||disposed)return;checking=true;lastCheck=Date.now();const generation=++capGeneration;render();try{const next=await flow.client.getCapabilities();if(!disposed&&generation===capGeneration)capabilities=next;}catch{if(!disposed&&generation===capGeneration)capabilities=null;}finally{if(generation===capGeneration){checking=false;render();}}}
  function unresolved(r){return !!r&&(pending.has(r.status)||!['completed','refunded'].includes(r.status)&&!!flow.client.getSource(r.client_request_id)?.signature);}
  function timeKey(){const r=record(),payment=r&&flow.client.getSource(r.client_request_id),now=Math.floor(Date.now()/1000);return JSON.stringify([fresh(),!!r?.quote&&r.quote.expires_at<=now,!!payment&&payment.source.expires_at<=now]);}
  function input(action){const xeris=getXeris(),source=action==='buy'?solana.getState().address:xeris.address;
    if(!source)throw new Error(action==='buy'?'Connect your Solana funding wallet.':'Connect your Xeris source wallet.');
    const destination=validateRecipient($(action==='buy'?'deposit-recipient':'redeem-recipient').value);
    const amount=parseSettlementAmount($(action==='buy'?'deposit-amount':'redeem-amount').value,action==='buy'?'solana_usdc':'xusdc');
    const token=associatedUsdcAccount(action==='buy'?source:destination,sdk());
    return {action,amount_raw:amount,source_owner:source,destination_owner:destination,solana_token_account:token};
  }
  async function quote(action){return run('Checking availability and the exact receiving amount…',async()=>{
    const result=await flow.quote(input(action));select(result.client_request_id);notice='Review the full source, receiving address and minimum. Authorizing the request does not transfer funds.';
  });}
  async function advance(action){return run('Checking the original request…',async()=>{
    const r=record();if(!r||r.action!==action)throw new Error('Review a current quote first.');
    const fields=input(action);for(const key of ['amount_raw','source_owner','destination_owner','solana_token_account'])if(fields[key]!==r[key])throw new Error('The wallet, amount or destination changed. Review the original request details before continuing.');
    const source=flow.client.getSource(r.client_request_id);
    if(['quoted','challenged'].includes(r.status)){
      notice='Review the ownership message in your source wallet. No funds move in this step.';render();
      await flow.authorize(r.client_request_id);notice='Ownership request accepted. Prepare the original payment for review.';
    }else if(r.status==='awaiting_funding'&&(!source||source.state==='prepared'&&source.source.expires_at<=Math.floor(Date.now()/1000))){
      await flow.prepare(r.client_request_id);notice='Review the exact payment and network fee below. The next button requests one sign-only wallet approval.';
    }else if(r.status==='awaiting_funding'&&source?.state==='prepared'){
      notice=action==='buy'?'Approve this exact USDC transfer in the Solana wallet. It will not broadcast yet.':'Approve this exact xUSDC burn in the Xeris wallet. The destination is fixed by your original signed request. It will not broadcast yet.';render();
      await flow.sign(r.client_request_id);notice='Signed payment saved in this browser. Submit it once with the button below; do not create another payment.';
    }else if(r.status==='awaiting_funding'&&source?.state==='signed'){
      notice='Submitting the saved signed payment once…';render();await flow.submit(r.client_request_id);notice='The original source payment has been submitted. Check its status until verified delivery or payout.';
    }else throw new Error('This request requires status recovery. Do not submit another payment.');
  });}
  function summary(r){const target=$(r.action==='buy'?'deposit-summary':'redeem-summary'),q=r.quote;
    target.hidden=false;target.replaceChildren(node('p',r.action==='buy'?'USDC → native XRS':'xUSDC → Solana USDC','bridge-step-label'));
    rows(target,[['You pay',`${formatSettlementAmount(r.amount_raw,r.action==='buy'?'solana_usdc':'xusdc')} ${r.action==='buy'?'USDC':'xUSDC'}`],
      ['Expected received',q?`${formatSettlementAmount(q.expected_output_raw,r.action==='buy'?'native_xrs':'solana_usdc')} ${r.action==='buy'?'native XRS':'USDC'}`:'Unavailable'],
      ['Minimum received',q?`${formatSettlementAmount(q.min_output_raw,r.action==='buy'?'native_xrs':'solana_usdc')} ${r.action==='buy'?'native XRS':'USDC'}`:'Unavailable']]);
    target.append(node('p','From · '+r.source_owner,'detail-code'),node('p','To · '+r.destination_owner,'detail-code'));
    target.append(node('p',r.action==='buy'?'The 0.3% pool fee and price impact are included. The 0.5% slippage limit sets the native XRS delivery minimum. Operator Xeris fees are paid separately by the operator.':'No USDC deduction: the payout equals the confirmed xUSDC burn. The operator pays Solana payout fees and any receiving token-account creation.','bridge-approval-note'));
    const payment=flow.client.getSource(r.client_request_id);
    if(payment){rows(target,[['Your network fee',`${formatAmount(payment.source.fee_raw,9)} ${payment.source.fee_asset}`],['Source payment',payment.source.stage==='burn'?'Burn xUSDC on Xeris':'Transfer Solana USDC to reserve']]);}
    else target.append(node('p',r.action==='buy'?'Your Solana network fee is shown after source preparation, before payment approval.':'Your burn requires 0.001 native XRS for its network fee.','bridge-approval-note'));
    if(q)target.append(node('p',`Quote acceptance ends ${new Date(q.expires_at*1000).toLocaleTimeString()}. Settlement confirmation can take longer.`,'bridge-approval-note'));
  }
  function render(){if(disposed)return;
    try{
      const r=record(),account=getXeris(),sol=solana.getState();
      const contentKey=JSON.stringify([currentId,history(),r&&flow.client.getSource(r.client_request_id)]),contentChanged=contentKey!==lastContentKey;
      $('bridge-availability').textContent=checking?'Checking settlement availability…':!fresh()?'Settlement availability could not be verified. Payments and burns are disabled.':capabilities.can_accept_funds?`Purchase ${enabled('buy')?'available':'paused'} · withdrawal ${enabled('redeem')?'available':'paused'}. Each approval rechecks readiness.`:'Settlement is paused. No new USDC payment or xUSDC burn can be approved.';
      $('bridge-availability').classList.toggle('loading-inline',checking);$('bridge-availability').setAttribute('aria-busy',String(checking));
      $('bridge-refresh').disabled=checking||working;$('bridge-message').textContent=notice;$('bridge-message').classList.toggle('error',noticeError);$('bridge-message').classList.toggle('loading-inline',working);$('bridge-message').setAttribute('aria-busy',String(working));
      $('redeem-xeris-connect').disabled=working;$('redeem-xeris-connect').textContent=account.address?'Manage Xeris connection':'Connect Xeris wallet';
      $('redeem-xeris-state').textContent=account.address||'No Xeris wallet connected.';$('redeem-solana-connect').disabled=working;$('use-solana-recipient').disabled=working||sol.status!=='connected';
      if(sol.status==='connected'&&(!sol.canSignOwnership||!sol.canSignTransaction))$('solana-wallet-state').textContent='This wallet does not expose both message and sign-only transaction approval. Choose a supported wallet before purchasing.';
      if(working){$('solana-connect').disabled=true;$('solana-disconnect').disabled=true;$('bridge-xeris-connect').disabled=true;$('use-xeris-recipient').disabled=true;}
      if(r&&pending.has(r.status)){if(r.action==='buy')$('use-xeris-recipient').disabled=true;else $('use-solana-recipient').disabled=true;}
      $('redeem-balance').textContent=account.balances?`Available: ${formatAmount(account.balances.usdcRaw,6)} xUSDC · ${formatAmount(account.balances.nativeRaw,9)} native XRS for fees.`:'Connect Xeris and refresh balances before approving a burn.';
      for(const action of ['buy','redeem']){
        const prefix=action==='buy'?'deposit':'redeem',current=r?.action===action?r:null,locked=unresolved(current);
        for(const id of [prefix+'-amount',action==='buy'?'deposit-recipient':'redeem-recipient'])$(id).disabled=working||!!locked;
        const review=$(action==='buy'?'review-deposit-addresses':'review-redemption');let valid=false;try{input(action);valid=true;}catch{}
        review.disabled=working||!!locked||!enabled(action)||!valid||(action==='buy'?(!sol.canSignOwnership||!sol.canSignTransaction):typeof account.provider?.signMessage!=='function');
        const button=$(prefix+'-submit'),target=$(prefix+'-summary');target.hidden=!current;
        button.disabled=true;button.textContent=action==='buy'?'Purchases unavailable':'Withdrawals unavailable';
        if(current){if(contentChanged)summary(current);const payment=flow.client.getSource(current.client_request_id),ready=enabled(action)&&!working;
          if(['quoted','challenged'].includes(current.status)){button.textContent=current.quote.expires_at<=Math.floor(Date.now()/1000)?'Quote expired — review again':'1. Authorize request in wallet';button.disabled=!ready||current.quote.expires_at<=Math.floor(Date.now()/1000);}
          else if(current.status==='awaiting_funding'){
            if(current.intent.expires_at<=Math.floor(Date.now()/1000)&&!payment?.signature){button.textContent='Acceptance expired — check status';}
            else if(!payment||payment.state==='prepared'&&payment.source.expires_at<=Math.floor(Date.now()/1000)){button.textContent='2. Prepare source payment';button.disabled=!ready;}
            else if(payment.state==='prepared'){button.textContent=action==='buy'?'3. Approve USDC transfer in wallet':'3. Approve xUSDC burn in wallet';button.disabled=!ready;}
            else if(payment.state==='signed'){button.textContent=payment.source.expires_at<=Math.floor(Date.now()/1000)?'Signed payment expired — check status':action==='buy'?'4. Submit signed USDC transfer once':'4. Submit signed xUSDC burn once';button.disabled=!ready||payment.source.expires_at<=Math.floor(Date.now()/1000);}
            else button.textContent='Payment unresolved — check status';
          }else button.textContent=labels[current.status]||'Check request status';
        }
      }
      $('withdraw-convert').disabled=working;$('withdraw-sell').disabled=working;
      const panel=$('bridge-current');panel.hidden=!r;
      if(r&&contentChanged){const local=flow.client.getSource(r.client_request_id);panel.replaceChildren(node('h3',local?.signature&&['cancelled','failed'].includes(r.status)?'Original signed payment needs recovery':labels[r.status]||'Reconciliation required'),node('p',r.client_request_id,'detail-code'));
        const source=flow.client.getSource(r.client_request_id);rows(panel,[['Source owner',r.source_owner],['Receiving wallet',r.destination_owner],['Route',r.action==='buy'?'Solana USDC → native XRS':'Xeris xUSDC → Solana USDC']]);
        if(source?.signature)panel.append(node('p','Original source transaction · '+source.signature,'detail-code'));
        if(r.request?.source_txid)panel.append(node('p','Verified source transaction · '+r.request.source_txid,'detail-code'));
        if(r.request?.destination_txid)panel.append(node('p','Destination transaction · '+r.request.destination_txid,'detail-code'));
        if(r.status==='completed'&&r.request?.actual_output_raw)rows(panel,[['Actually delivered',`${formatSettlementAmount(r.request.actual_output_raw,r.action==='buy'?'native_xrs':'solana_usdc')} ${r.action==='buy'?'native XRS':'Solana USDC'}`]]);
        if(r.status==='refunded'&&r.request?.refund_asset==='solana_usdc'&&r.request?.refund_amount_raw)rows(panel,[['Verified refund',`${formatSettlementAmount(r.request.refund_amount_raw,'solana_usdc')} USDC on Solana`]]);
        panel.append(node('p',r.status==='completed'?'The service reports verified destination delivery. Check the destination wallet and receipt.':source&&['submitting','submission_unknown','submitted'].includes(source.state)?'The original payment may have been broadcast. Check this request; never pay or burn again.':'A request or source signature is not a completed settlement. Each step requires your deliberate approval.','bridge-approval-note'));
      }
      $('bridge-recover').disabled=working||!r?.intent;$('bridge-new').disabled=working||unresolved(r);
      if(contentChanged){const list=$('bridge-history');list.replaceChildren();const saved=history().slice().reverse();
        if(!saved.length)list.append(node('p','No saved settlement requests.'));
        for(const item of saved){const row=node('div',undefined,'bridge-history-row'),text=node('p',(item.action==='buy'?'Purchase':'Withdrawal')+' · '+(labels[item.status]||'Unresolved'));text.append(node('small',item.client_request_id));const button=node('button','Open request','text-button');button.type='button';button.disabled=working;button.onclick=()=>select(item.client_request_id);row.append(text,button);list.append(row);}
      }
      for(const button of $('bridge-history').querySelectorAll('button'))button.disabled=working;
      lastContentKey=contentKey;lastTimeKey=timeKey();
    }catch{noticeError=true;$('bridge-message').textContent='Saved settlement data could not be validated. New bridge actions are blocked; preserve this browser’s data for reconciliation.';for(const id of ['deposit-submit','redeem-submit','review-deposit-addresses','review-redemption'])$(id).disabled=true;}
  }
  $('review-deposit-addresses').onclick=()=>quote('buy');$('review-redemption').onclick=()=>quote('redeem');
  $('deposit-submit').onclick=()=>advance('buy');$('redeem-submit').onclick=()=>advance('redeem');
  $('bridge-refresh').onclick=refresh;$('bridge-recover').onclick=()=>run('Checking only the saved original request…',async()=>{if(!currentId)return;await flow.recover(currentId);notice='Original request status checked. No payment was signed or sent.';});
  $('bridge-new').onclick=()=>{const r=record();if(unresolved(r)){tell('Recover this unresolved request before another payment.',true);return;}select(null);tell('Review cleared. Saved request records remain available.');};
  $('redeem-xeris-connect').onclick=connectXeris;$('redeem-solana-connect').onclick=connectSolana;$('use-solana-recipient').onclick=()=>{const current=solana.getState();if(current.status==='connected'){$('redeem-recipient').value=current.address;render();}};
  $('withdraw-convert').onclick=convert;$('withdraw-sell').onclick=sell;
  for(const id of ['deposit-amount','deposit-recipient','redeem-amount','redeem-recipient'])$(id).addEventListener('input',()=>{try{const r=record();if(r&&!pending.has(r.status))select(null);}catch{}render();});
  const timer=setInterval(()=>{if(document.hidden||working)return;try{if(timeKey()!==lastTimeKey)render();}catch{render();}if(location.hash==='#bridge'&&Date.now()-lastCheck>=15000&&(!fresh()||capabilities.expires_at-Math.floor(Date.now()/1000)<5))refresh();},1000);
  render();refresh();
  return Object.freeze({render,refresh,dispose(){disposed=true;capGeneration++;clearInterval(timer);}});
}
