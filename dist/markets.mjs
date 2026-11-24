import {integer, request} from './model.mjs';

// Protocol-managed provenance prevents ordinary tokens (including duplicate
// symbols) from being advertised as launchpad listings.
export function parseLaunchpadToken(token) {
  if (token?.mint_policy !== 'LaunchpadManaged') return null;
  if (token.mint_authority !== '__launchpad_managed__' || String(token.decimals) !== '9' ||
      typeof token.token_id !== 'string' || !token.token_id.startsWith('lp_') || token.token_id.length > 128 ||
      typeof token.symbol !== 'string' || !token.symbol.trim() || token.symbol.length > 32 ||
      typeof token.name !== 'string' || !token.name.trim() || token.name.length > 256 ||
      /[\u0000-\u001f\u007f]/.test(token.symbol + token.name)) throw new Error('Launchpad metadata is unavailable.');
  const supply = integer(token.current_supply), maximum = integer(token.max_supply, true);
  if (supply > maximum) throw new Error('Launchpad supply is unavailable.');
  return {id: token.token_id, symbol: token.symbol, name: token.name, supply, maximum};
}

export async function getLaunchpadTokens({read = request} = {}) {
  const ids = new Set(), tokens = [], cursors = new Set();
  let cursor = null;
  for (let page = 0; page < 25; page++) {
    const response = await read(`/v2/tokens?limit=32${cursor === null ? '' : `&after=${encodeURIComponent(cursor)}`}`);
    if (response?.success !== true || !Array.isArray(response.data) ||
        !Object.hasOwn(response, 'next_after')) throw new Error('Launchpad listings are unavailable.');
    for (const token of response.data) {
      if (typeof token?.token_id !== 'string' || ids.has(token.token_id)) throw new Error('Ambiguous token registry.');
      ids.add(token.token_id);
      const launch = parseLaunchpadToken(token);
      if (launch) tokens.push(launch);
    }
    const next = response.next_after;
    if (next === null) return tokens.sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
    if (typeof next !== 'string' || !next || next.length > 128 || cursors.has(next)) throw new Error('Incomplete token registry.');
    cursors.add(next); cursor = next;
  }
  throw new Error('Token registry exceeds the supported listing size.');
}

// Feed valuations may decorate only a fresh protocol registry listing. Matching
// metadata and circulating <= minted supply prevent cross-token substitutions.
export function mergeLaunchpadMarkets(tokens, snapshot) {
  const quotes = new Map();
  if (snapshot?.status === 'ready') {
    for (const row of snapshot.launchpad) {
      if (quotes.has(row.id)) throw new Error('Ambiguous launchpad market data.');
      quotes.set(row.id,row);
    }
  }
  return tokens.map(token => {
    const market = quotes.get(token.id);
    if (!market || market.status !== 'ready') return {...token,marketCapRaw:null,status:'unavailable'};
    if (market.name !== token.name || market.symbol !== token.symbol || market.circulatingRaw > token.supply) {
      throw new Error('Launchpad market identity changed.');
    }
    return {...token,marketCapRaw:market.marketCapRaw,circulatingRaw:market.circulatingRaw,status:'ready'};
  });
}
