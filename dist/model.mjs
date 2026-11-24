export const CONFIG = Object.freeze({
  rpc: 'https://mainnetrpc.xerisweb.com', chainId: 'xeris-mainnet-v4',
  poolId: 'xrs-xusdc-mainnet-v1', wrapped: 'xrs_native', usdc: 'xeris_usdc',
  feeRaw: 1000000n, checkpointSlot: 27201,
  checkpointHash: '0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3',
  treasury: '4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL',
  reserve: 'BeM8R5hvdKGwiV5jKdug3kXoe6RaFaREjihKFNSjeBD6',
  solanaMint: 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
});
const U64 = (1n << 64n) - 1n;
export function integer(value, positive = false) {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('Exact amount unavailable.');
  if (!['string', 'bigint', 'number'].includes(typeof value) || !/^\d+$/.test(String(value))) throw new Error('Invalid integer amount.');
  const n = BigInt(value);
  if (n > U64 || n < (positive ? 1n : 0n)) throw new Error('Amount is outside the supported range.');
  return n;
}
export function parseAmount(text, decimals) {
  const s = String(text).trim().replace(',', '.');
  if (s.length > 100 || !/^\d+(\.\d+)?$/.test(s)) throw new Error('Enter a positive decimal amount.');
  const [whole, fraction = ''] = s.split('.');
  if (fraction.length > decimals) throw new Error(`Use at most ${decimals} decimal places.`);
  return integer(BigInt(whole) * 10n ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, '0') || '0'), true);
}
export function formatAmount(raw, decimals, places = decimals) {
  const n = integer(raw), base = 10n ** BigInt(decimals);
  const fraction = (n % base).toString().padStart(decimals, '0').slice(0, places).replace(/0+$/, '');
  return `${n / base}${fraction ? '.' + fraction : ''}`;
}
// Preserve JSON integer tokens before JSON.parse can round balances above 2^53.
export function parseExactJSON(text) {
  JSON.parse(text); // Validate JSON's original grammar before replacing tokens.
  return JSON.parse(text.replace(/"(?:\\.|[^"\\])*"|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (token, number) => number ? JSON.stringify(number) : token));
}
export async function request(path, {method = 'GET', body, timeout = 8000, fetcher = fetch} = {}) {
  if (!path.startsWith('/') || path.startsWith('//') || /[?#]/.test(path.split('?')[0])) throw new Error('Invalid RPC path.');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetcher(CONFIG.rpc + path, {
      method, signal: controller.signal, redirect: 'error', cache: 'no-store',
      ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}),
    });
    if (!response.ok) {
      if (response.status === 429) throw new Error('The network is busy. Wait a few seconds, then refresh.');
      throw new Error(`Network request failed (${response.status}).`);
    }
    const data = parseExactJSON(await response.text());
    if (data.error != null || data.success === false) throw new Error('The node could not complete this request.');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Network request timed out. Please refresh.');
    throw error;
  } finally { clearTimeout(timer); }
}
export async function rpc(method, params = [], options = {}) {
  const data = await request('/', {...options, method: 'POST', body: {jsonrpc: '2.0', id: 1, method, params}});
  if (data.result?.error != null) throw new Error('The node could not complete this lookup.');
  return data.result;
}
export async function checkChain() {
  const response = await request(`/v2/block/slot/${CONFIG.checkpointSlot}`);
  if (response.success !== true || String(response.data?.slot) !== String(CONFIG.checkpointSlot) || response.data?.hash !== CONFIG.checkpointHash) {
    throw new Error('Mainnet identity could not be verified. Trading is paused.');
  }
  return true;
}
export function parsePool(data) {
  const c = data?.contract, s = c?.state?.Swap;
  if (data?.success !== true || c?.contract_id !== CONFIG.poolId || c?.contract_type !== 'Swap' || c?.is_active !== true ||
    c?.owner !== 'HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX' || String(c?.created_slot) !== '27201' ||
    s?.token_a !== CONFIG.wrapped || s?.token_b !== CONFIG.usdc || String(s?.fee_bps) !== '30') throw new Error('The expected mainnet pool is unavailable.');
  return {reserveA: integer(s.reserve_a, true), reserveB: integer(s.reserve_b, true), totalShares: integer(s.total_shares, true), feeBps: 30, at: Date.now()};
}
export async function getPool() { return parsePool(await request(`/contract/${CONFIG.poolId}`)); }
export function quoteFromPool(pool, buying, amountRaw) {
  const inputRaw = integer(amountRaw, true), feeRaw = inputRaw * 30n / 10000n;
  const reserveIn = buying ? pool.reserveB : pool.reserveA, reserveOut = buying ? pool.reserveA : pool.reserveB;
  integer(reserveIn + inputRaw); // The execution path also must fit u64 reserves.
  const outputRaw = reserveOut * (inputRaw - feeRaw) / (reserveIn + inputRaw - feeRaw);
  integer(outputRaw, true);
  return {buying, inputRaw, outputRaw, feeRaw, minimumOutputRaw: (outputRaw * 9950n + 9999n) / 10000n,
    quotedAt: Date.now(), priceImpactPercent: (Number((inputRaw * 20000n + reserveIn + inputRaw) / (2n * (reserveIn + inputRaw))) / 100).toFixed(2)};
}
export async function getQuote(buying, amountRaw) {
  const pool = await getPool();
  const expected = quoteFromPool(pool, buying, amountRaw);
  const token = buying ? CONFIG.usdc : CONFIG.wrapped;
  const data = await request(`/contract/${CONFIG.poolId}/quote?input_token=${token}&amount=${expected.inputRaw}`);
  const q = data?.quote;
  if (data?.success !== true || q?.input_token !== token || q?.output_token !== (buying ? CONFIG.wrapped : CONFIG.usdc) ||
    integer(q.input_amount) !== expected.inputRaw || integer(q.output_amount) !== expected.outputRaw || integer(q.fee) !== expected.feeRaw || String(q.fee_bps) !== '30') {
    throw new Error('The pool changed while quoting. Refresh to review the latest price.');
  }
  return expected;
}
export function parseBalances(data, address) {
  if (data?.address !== address || !Array.isArray(data.token_accounts)) throw new Error('Wallet balances are unavailable.');
  const seen = new Set();
  const result = {nativeRaw: integer(data.native_xrs), wrappedRaw: 0n, usdcRaw: 0n};
  for (const token of data.token_accounts) {
    if (![CONFIG.wrapped, CONFIG.usdc].includes(token.token_id)) continue;
    if (seen.has(token.token_id)) throw new Error('Ambiguous token balance.');
    seen.add(token.token_id);
    if (String(token.decimals) !== (token.token_id === CONFIG.wrapped ? '9' : '6')) throw new Error('Token precision changed.');
    result[token.token_id === CONFIG.wrapped ? 'wrappedRaw' : 'usdcRaw'] = integer(token.balance);
  }
  return result;
}
export async function getBalances(address) {
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address)) throw new Error('Invalid public wallet address.');
  return parseBalances(await request(`/token/accounts/${address}`), address);
}
