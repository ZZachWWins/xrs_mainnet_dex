// Invoked only after explicit Mainnet selection. Never loads a runtime CDN.
export function loadSolanaLibrary(document,environment=globalThis){
  return new Promise((resolve,reject)=>{
    const script=document.createElement('script');
    script.src='/assets/vendor/solana-web3-1.98.4.iife.min.js';
    script.integrity='sha256-Cc2+qVGy7Q4RvL466x7p8DX5+1HtISrKZFR1roJojMM=';
    script.onload=()=>environment.solanaWeb3?.Transaction&&environment.solanaWeb3?.PublicKey?resolve():reject(new Error('The local Solana transaction library did not initialize.'));
    script.onerror=()=>reject(new Error('The local Solana transaction library could not load.'));
    document.head.append(script);
  });
}
