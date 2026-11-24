import {CONFIG} from './model.mjs';
import {base58Decode,base58Encode,fromBase64,toBase64,unsigned,checkProvider,verifySignedTransaction} from './trade.mjs';

export const SOLANA_TOKEN_PROGRAM='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const SOLANA_MEMO_PROGRAM='MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr';
const ATA_PROGRAM='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL';
const encoder=new TextEncoder();
const equal=(a,b)=>a.length===b.length&&a.every((v,i)=>v===b[i]);
const fail=()=>{throw new Error('The prepared payment differs from the reviewed settlement. Nothing was signed or sent.');};
const concat=(...parts)=>Uint8Array.from(parts.flatMap(p=>Array.from(p)));
function little(value,count){let n=BigInt(value);const out=new Uint8Array(count);for(let i=0;i<count;i++){out[i]=Number(n&255n);n>>=8n;}if(n)fail();return out;}
function compact(value){let n=value;const out=[];do{let b=n&127;n>>>=7;if(n)b|=128;out.push(b);}while(n);return Uint8Array.from(out);}
function string(text){const data=encoder.encode(text);return concat(little(data.length,8),data);}
function vector(data){return concat(compact(data.length),data);}

/** Deterministic public ATA derivation through the pinned official SDK only. */
export function associatedUsdcAccount(owner,sdk){
  base58Decode(owner);
  if(!sdk?.PublicKey?.findProgramAddressSync)throw new Error('The Solana transaction library is unavailable. Reload before continuing.');
  const Key=sdk.PublicKey;
  return Key.findProgramAddressSync([new Key(owner).toBytes(),new Key(SOLANA_TOKEN_PROGRAM).toBytes(),new Key(CONFIG.solanaMint).toBytes()],new Key(ATA_PROGRAM))[0].toBase58();
}

/** Exact narrow wire templates: no arbitrary instruction returned by a service is signable. */
export async function expectedSourceMessage(intent,blockhash,ownershipMessage,{crypto=globalThis.crypto}={}){
  const owner=base58Decode(intent.source_owner),amount=unsigned(intent.amount_raw);
  if(!amount||blockhash.length!==32||blockhash.every(v=>v===0)||!crypto?.subtle)fail();
  if(intent.action==='buy'){
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(ownershipMessage))),b=>b.toString(16).padStart(2,'0')).join('');
    const keys=[intent.source_owner,intent.solana_token_account,CONFIG.reserve,CONFIG.solanaMint,SOLANA_TOKEN_PROGRAM,SOLANA_MEMO_PROGRAM];
    if(new Set(keys).size!==6)fail();
    const transfer=concat(Uint8Array.of(12),little(amount,8),Uint8Array.of(6));
    const memo=encoder.encode('xeris:settlement:v2:'+digest);
    return concat(Uint8Array.of(1,0,3),compact(6),...keys.map(k=>base58Decode(k)),blockhash,
      compact(2),Uint8Array.of(4),vector(Uint8Array.of(1,3,2,0)),vector(transfer),
      Uint8Array.of(5),vector(Uint8Array.of(0)),vector(memo));
  }
  if(intent.action!=='redeem'||intent.route!=='xusdc_to_solana_usdc'||intent.amount_raw!==intent.min_output_raw)fail();
  const burn=concat(little(2,4),string(CONFIG.usdc),string(intent.source_owner),little(amount,8));
  return concat(Uint8Array.of(1,0,1,2),owner,new Uint8Array(32),blockhash,
    Uint8Array.of(1,1,1,0),vector(burn));
}

export async function reviewPreparedSource(prepared,intent,ownershipMessage,{crypto=globalThis.crypto,now=Date.now,allowExpired=false}={}){
  if(!prepared||prepared.signer!==intent.source_owner||prepared.chain!==(intent.action==='buy'?'solana:mainnet':CONFIG.chainId)
    ||prepared.stage!==(intent.action==='buy'?'deposit':'burn')||!Number.isSafeInteger(prepared.expires_at)
    ||(!allowExpired&&prepared.expires_at<=Math.floor(now()/1000))||prepared.expires_at>Math.floor(now()/1000)+300)fail();
  const wire=fromBase64(prepared.transaction_base64),message=fromBase64(prepared.message_base64);
  if(wire.length>1232||wire[0]!==1||wire.slice(1,65).some(v=>v!==0)||!equal(wire.slice(65),message))fail();
  // Both canonical templates have one-byte compact account counts.
  const hashOffset=intent.action==='buy'?196:68;
  if(message.length<hashOffset+32)fail();
  const expected=await expectedSourceMessage(intent,message.slice(hashOffset,hashOffset+32),ownershipMessage,{crypto});
  if(!equal(message,expected))fail();
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',message)),b=>b.toString(16).padStart(2,'0')).join('');
  if(prepared.message_sha256!==digest)fail();
  return Object.freeze({wire,message,base64:prepared.transaction_base64});
}

export async function signXerisOwnership(provider,address,message,{crypto=globalThis.crypto}={}){
  if(typeof provider?.signMessage!=='function')throw new Error('This Xeris wallet does not support ownership messages. Update it before redeeming.');
  if(!message.startsWith('Xeris settlement ownership proof\nxeris-mainnet-settlement-intent-v2\n')||encoder.encode(message).length>8192)fail();
  await checkProvider(provider,address);
  const response=await provider.signMessage(encoder.encode(message));
  await checkProvider(provider,address);
  const bytes=response?.signature;
  if(!(bytes instanceof Uint8Array)||bytes.length!==64||bytes.every(v=>v===0)||!crypto?.subtle)fail();
  const key=await crypto.subtle.importKey('raw',base58Decode(address),{name:'Ed25519'},false,['verify']);
  if(!await crypto.subtle.verify('Ed25519',key,bytes,encoder.encode(message)))fail();
  await checkProvider(provider,address);
  return {signature:base58Encode(bytes)};
}

export async function signXerisSource(provider,address,prepared,reviewed,{crypto=globalThis.crypto}={}){
  if(prepared.chain!==CONFIG.chainId||prepared.stage!=='burn')fail();
  await checkProvider(provider,address);
  const result=await verifySignedTransaction(await provider.signTransaction(prepared.transaction_base64),reviewed,address,crypto);
  await checkProvider(provider,address);
  return {signature:result.signature,signedTransaction:result.signedTransaction};
}
