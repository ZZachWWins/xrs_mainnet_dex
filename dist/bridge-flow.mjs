import {createSettlementClient,settlementSigningMessage,parseSettlementAmount,formatSettlementAmount} from './settlement.mjs';
import {associatedUsdcAccount,reviewPreparedSource,signXerisOwnership,signXerisSource} from './bridge-source.mjs';

export {parseSettlementAmount,formatSettlementAmount,associatedUsdcAccount};
const encoder=new TextEncoder();
const routes={buy:'solana_usdc_to_native_xrs',redeem:'xusdc_to_solana_usdc'};
const fail=message=>{throw new Error(message);};

/** Explicit user steps only. Construction and recovery never prompt a wallet. */
export function createBridgeFlow({client=createSettlementClient(),solana,getXeris=()=>({}),crypto=globalThis.crypto,now=Date.now}={}){
  let working=false;
  const record=id=>client.getHistory().find(r=>r.client_request_id===id)||fail('The original request is not saved in this browser.');
  async function available(action){const cap=await client.getCapabilities();if(!cap.can_accept_funds||cap.routes[routes[action]]?.enabled!==true)fail('This route is paused. No new payment or burn can be approved.');return cap;}
  function owner(intent){
    if(intent.action==='buy'){
      const current=solana?.getState();
      if(current?.status!=='connected'||current.address!==intent.source_owner||!current.canSignOwnership||!current.canSignTransaction)fail('Reconnect the original Solana source account with message and transaction signing support.');
      return {kind:'solana',revision:current.revision,providerId:current.providerId,address:current.address};
    }
    const current=getXeris();
    if(!current?.provider||current.address!==intent.source_owner)fail('Connect the original Xeris source account before approving.');
    return {kind:'xeris',...current};
  }
  function unchanged(snapshot,intent){const current=owner(intent);if(current.kind!==snapshot.kind||current.address!==snapshot.address||(current.kind==='solana'?(current.revision!==snapshot.revision||current.providerId!==snapshot.providerId):current.provider!==snapshot.provider))fail('The wallet changed. Recover the original request before continuing.');}
  async function one(action){if(working)fail('Another bridge step is in progress.');working=true;try{return await action();}finally{working=false;}}
  return Object.freeze({
    client,
    isWorking:()=>working,
    history:()=>client.getHistory(),
    quote(input){return one(async()=>{if(!routes[input.action])fail('Choose a supported settlement route.');return client.quote(input);});},
    authorize(id){return one(async()=>{
      const current=record(id);await available(current.action);
      const challenge=current.status==='challenged'&&current.intent?{intent:current.intent,signing_message_utf8:settlementSigningMessage(current.intent,{now:Math.floor(now()/1000)})}:await client.challenge(id);
      const before=owner(challenge.intent);
      const proof=before.kind==='solana'?await solana.signOwnership(encoder.encode(challenge.signing_message_utf8)):
        await signXerisOwnership(before.provider,before.address,challenge.signing_message_utf8,{crypto});
      unchanged(before,challenge.intent);
      return client.submit(challenge.intent,proof.signature);
    });},
    prepare(id){return one(()=>client.prepareSource(id));},
    sign(id){return one(async()=>{
      const current=record(id),saved=client.getSource(id);
      if(saved?.state!=='prepared')fail('Prepare this original payment before approving it.');
      await available(current.action);const before=owner(current.intent);
      const reviewed=await reviewPreparedSource(saved.source,current.intent,settlementSigningMessage(current.intent,{allowExpired:true}),{crypto,now});
      const signed=before.kind==='solana'?await solana.signSourceTransaction(saved.source.transaction_base64,saved.source.message_base64):
        await signXerisSource(before.provider,before.address,saved.source,reviewed,{crypto});
      unchanged(before,current.intent);
      return client.saveSignedSource(id,signed);
    });},
    submit(id){return one(()=>client.submitSource(id));},
    recover(id){return one(async()=>{
      let result;try{result=await client.recover(id);}catch{result=await client.restoreSavedAccess(id);}
      const source=client.getSource(id);
      if(source?.signature&&source.source.expires_at<=Math.floor(now()/1000)&&!['completed','refunded'].includes(result.status))return client.recoverExpiredSource(id);
      return result;
    });},
  });
}
