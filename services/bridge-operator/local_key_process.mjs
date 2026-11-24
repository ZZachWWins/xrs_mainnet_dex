// Isolated local signing helper. No network imports or secret output.
import * as fs from 'node:fs/promises';
import {constants} from 'node:fs';
import {createPrivateKey,createPublicKey,sign,verify} from 'node:crypto';
const alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function check(ok){if(!ok)throw new Error('local_signing_failed');}
function base58(bytes){let n=BigInt('0x'+bytes.toString('hex')),s='';while(n){s=alphabet[Number(n%58n)]+s;n/=58n;}for(const b of bytes){if(b)break;s='1'+s;}return s;}
let fileBytes,secret,der;
try{
  const parts=[];let count=0;
  for await(const chunk of process.stdin){count+=chunk.length;check(count<=16000);parts.push(chunk);}
  const input=JSON.parse(Buffer.concat(parts).toString('utf8'));
  const inspecting=input?.operation==='inspect';
  check(input&&Object.keys(input).sort().join(',')===(inspecting?'operation,path,public_key':'message_base64,path,public_key'));
  check(typeof input.path==='string'&&input.path.startsWith('/')&&await fs.realpath(input.path)===input.path);
  let message;
  if(!inspecting){
    check(typeof input.message_base64==='string'&&input.message_base64.length<=15000);
    message=Buffer.from(input.message_base64,'base64');
    check(message.toString('base64')===input.message_base64&&message.length>=101&&message[0]===1&&message[3]>0&&message[3]<128);
    check(base58(message.subarray(4,36))===input.public_key);
  }
  const handle=await fs.open(input.path,constants.O_RDONLY|constants.O_NOFOLLOW);
  try{
    const st=await handle.stat();check(st.isFile()&&st.uid===process.getuid()&&(st.mode&0o777)===0o600&&st.size>0&&st.size<4096);
    fileBytes=await handle.readFile();const values=JSON.parse(fileBytes.toString('utf8'));
    check(Array.isArray(values)&&values.length===64&&values.every(v=>Number.isInteger(v)&&v>=0&&v<=255));
    secret=Buffer.from(values);values.fill(0);
  }finally{await handle.close();}
  der=Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'),secret.subarray(0,32)]);
  const key=createPrivateKey({key:der,format:'der',type:'pkcs8'}),pub=createPublicKey(key);
  const bytes=Buffer.from(pub.export({format:'jwk'}).x,'base64url');
  check(base58(bytes)===input.public_key&&bytes.equals(secret.subarray(32)));
  if(inspecting){process.stdout.write('{"identity_verified":true}');}
  else{
    const signature=sign(null,message,key);check(signature.length===64&&verify(null,message,pub,signature));
    process.stdout.write(JSON.stringify({signed_transaction_base64:Buffer.concat([Buffer.from([1]),signature,message]).toString('base64')}));
  }
}catch{
  process.exitCode=1;process.stdout.write('{"error":"local_signing_failed"}');
}finally{fileBytes?.fill(0);secret?.fill(0);der?.fill(0);}
