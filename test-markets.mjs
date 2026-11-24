import test from 'node:test';
import assert from 'node:assert/strict';
import {getLaunchpadTokens, parseLaunchpadToken} from './dist/markets.mjs';

const launch = (id = 'lp_example') => ({token_id: id, symbol: 'EXAMPLE', name: 'Example token',
  mint_policy: 'LaunchpadManaged', mint_authority: '__launchpad_managed__', decimals: '9',
  current_supply: '9007199254740993', max_supply: '9007199254740994'});

test('ordinary tokens cannot impersonate launchpad listings by symbol or token ID', async () => {
  const tokens = await getLaunchpadTokens({read: async () => ({success: true, next_after: null,
    data: [{...launch(), mint_policy: 'Standard'}, {token_id: 'xrs_native', symbol: 'XRS'}, launch('lp_real')]})});
  assert.deepEqual(tokens.map(t => t.id), ['lp_real']);
  assert.equal(tokens[0].supply, 9007199254740993n);
});

test('launchpad discovery follows encoded cursors and includes later registry pages', async () => {
  const paths = [];
  const tokens = await getLaunchpadTokens({read: async path => {
    paths.push(path);
    return paths.length === 1 ? {success: true, data: [launch('lp_z')], next_after: 'lp_z&x'} :
      {success: true, data: [launch('lp_a')], next_after: null};
  }});
  assert.deepEqual(tokens.map(t => t.id), ['lp_a', 'lp_z']);
  assert.equal(paths[1], '/v2/tokens?limit=32&after=lp_z%26x');
});

test('empty successful registry is distinct from failed or incomplete discovery', async () => {
  assert.deepEqual(await getLaunchpadTokens({read: async () => ({success: true, data: [], next_after: null})}), []);
  for (const result of [{success: false, data: [], next_after: null}, {success: true, data: []}]) {
    await assert.rejects(getLaunchpadTokens({read: async () => result}));
  }
  await assert.rejects(getLaunchpadTokens({read: async () => { throw new Error('offline'); }}));
});

test('ambiguous registry pages and cursor loops fail instead of showing partial listings', async () => {
  await assert.rejects(getLaunchpadTokens({read: async () => ({success: true, data: [launch(), launch()], next_after: null})}));
  await assert.rejects(getLaunchpadTokens({read: async () => ({success: true, data: [], next_after: 'repeat'})}));
});

test('unsafe or inconsistent launchpad metadata fails validation', () => {
  for (const change of [{current_supply: 9007199254740992}, {current_supply: '9007199254740995'},
    {mint_authority: 'ordinary-wallet'}, {symbol: 'XRS\nFake'}, {decimals: '6'}]) {
    assert.throws(() => parseLaunchpadToken({...launch(), ...change}));
  }
});

test('market caps decorate only matched registry identities and circulating bounds', async () => {
  const {mergeLaunchpadMarkets} = await import('./dist/markets.mjs');
  const token = parseLaunchpadToken(launch());
  const snapshot = {status:'ready',launchpad:[{id:token.id,name:token.name,symbol:token.symbol,status:'ready',circulatingRaw:100n,marketCapRaw:123456n}]};
  assert.equal(mergeLaunchpadMarkets([token],snapshot)[0].marketCapRaw,123456n);
  assert.equal(mergeLaunchpadMarkets([token],null)[0].marketCapRaw,null);
  assert.equal(mergeLaunchpadMarkets([token],{status:'stale',launchpad:snapshot.launchpad})[0].marketCapRaw,null);
  assert.equal(mergeLaunchpadMarkets([],{status:'ready',launchpad:snapshot.launchpad}).length,0);
  for (const change of [{name:'Impersonated name'},{symbol:'XRS'},{circulatingRaw:token.supply+1n}]) {
    assert.throws(()=>mergeLaunchpadMarkets([token],{status:'ready',launchpad:[{...snapshot.launchpad[0],...change}]}));
  }
});
