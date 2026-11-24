import copy
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import collector
from aggregate import SupplyUnavailable, public_address


NOW = 1_800_000_000


def policy():
    return json.loads(Path(__file__).with_name('policy.json').read_text())


def responses():
    company = policy()['company_addresses'][0]
    block = {'slot': 30000, 'hash': 'ab' * 32, 'proposer': company,
             'tx_count': 0, 'poh_timestamp': NOW * 1000 - 4000}
    pool = {'pool_id': collector.POOL, 'token_a': collector.WRAPPED, 'token_b': collector.USDC,
            'reserve_a': 709700000000, 'reserve_b': 10101886, 'fee_bps': 30,
            'is_active': True, 'total_lp_shares': 2677178048}
    detail = {'contract_id': collector.POOL, 'contract_type': 'Swap', 'owner': collector.POOL_OWNER,
              'created_slot': collector.CHECKPOINT_SLOT, 'is_active': True,
              'state': {'Swap': {'token_a': collector.WRAPPED, 'token_b': collector.USDC,
                                'reserve_a': pool['reserve_a'], 'reserve_b': pool['reserve_b'],
                                'total_shares': pool['total_lp_shares'], 'fee_bps': 30}}}
    return {
        (50008, f'/v2/block/slot/{collector.CHECKPOINT_SLOT}'): {'success': True, 'data': {'slot': collector.CHECKPOINT_SLOT, 'hash': collector.CHECKPOINT_HASH}},
        (50008, '/v2/blocks?page=1&page_size=12'): {'success': True, 'data': [block]},
        (50008, '/v2/stats'): {'success': True, 'data': {'current_slot': 30000, 'block_height': 30000, 'total_accounts': 1}},
        (50008, '/balances'): {company: 199999000000000000},
        (50008, '/v2/tokens?limit=32'): {'success': True, 'count': 2, 'next_after': None, 'data': [
            {'token_id': collector.WRAPPED, 'current_supply': 709700000000, 'decimals': 9, 'mint_policy': 'Standard'},
            {'token_id': collector.USDC, 'current_supply': 10101886, 'decimals': 6, 'mint_policy': 'Standard'}]},
        (50008, '/v2/token/xrs_native/holders?limit=32'): {'success': True, 'holder_count': 0, 'holders': [], 'next_after': None},
        (50008, '/v2/pools?limit=32'): {'success': True, 'count': 1, 'pools': [pool], 'next_after': None},
        (56001, '/network/economics'): {'current_slot': 30000, 'total_mined_lamports': 300000000000000,
                                       'total_staked_lamports': 3000000000000, 'current_block_reward_xrs': Decimal('10.0')},
        (56001, f'/contract/{collector.POOL}'): {'success': True, 'contract': detail},
    }


def add_launch(data, *, graduated=True, token_id='lp_example'):
    token = {'token_id': token_id, 'name': 'Example', 'symbol': 'EXAMPLE', 'decimals': 9,
             'mint_policy': 'LaunchpadManaged', 'mint_authority': '__launchpad_managed__',
             'created_slot': 28000, 'current_supply': 1000 * 10**9, 'max_supply': 10000 * 10**9}
    tokens = data[(50008, '/v2/tokens?limit=32')]
    tokens['data'].append(token)
    tokens['count'] += 1
    # All seven configured wallet addresses are company-owned. A distinct,
    # synthetic 32-byte public key is sufficient for offline census fixtures.
    public_user = '11111111111111111111111111111112'
    data[(50008, f'/v2/token/{token_id}/holders?limit=32')] = {
        'success': True, 'holder_count': 2, 'next_after': None,
        'holders': [{'address': policy()['company_addresses'][0], 'balance': 100 * 10**9},
                    {'address': public_user, 'balance': 200 * 10**9}]}
    if graduated:
        pid = collector.launch_pool_id(token_id)
        pool = {'pool_id': pid, 'token_a': token_id, 'token_b': collector.WRAPPED,
                'reserve_a': 500 * 10**9, 'reserve_b': 50 * 10**9,
                'fee_bps': 30, 'is_active': True, 'total_lp_shares': 1000}
        pools = data[(50008, '/v2/pools?limit=32')]
        pools['pools'].append(pool)
        pools['count'] += 1
        tokens['data'][0]['current_supply'] += pool['reserve_b']
        data[(56001, f'/contract/{pid}')] = {'success': True, 'contract': {
            'contract_id': pid, 'contract_type': 'Swap', 'owner': '__launchpad_pool__',
            'created_slot': 29000, 'is_active': True, 'state': {'Swap': {
                'token_a': token_id, 'token_b': collector.WRAPPED, 'reserve_a': pool['reserve_a'],
                'reserve_b': pool['reserve_b'], 'fee_bps': 30, 'total_shares': pool['total_lp_shares']}}}}
    return token


class CollectorTests(unittest.TestCase):
    def run_collect(self, data=None, selected_policy=None, reader=None):
        data = data or responses()
        with patch.object(collector.time, 'time', return_value=NOW):
            return collector.collect(selected_policy or policy(), read=reader or (lambda port, path: copy.deepcopy(data[(port, path)])))

    def test_market_cap_includes_unlocked_treasury_and_pool_exactly_once(self):
        result = self.run_collect()
        self.assertEqual(result['schema_version'], '2')
        self.assertEqual(result['supply']['basis'], 'unlocked_on_chain_v2')
        self.assertEqual(result['market']['circulating_raw'], '199999709700000000')
        self.assertEqual(result['market']['market_cap_quote_raw'], str(199999709700000000 * 10101886 // 709700000000))
        self.assertIn('company_wallets', result['policy'])
        self.assertNotIn('excluded_wallets', result['policy'])
        self.assertEqual(result['market']['quote_symbol'], 'xUSDC')
        self.assertEqual(result['market']['price_quote_raw_numerator'], '10101886')
        self.assertEqual(result['market']['price_xrs_raw_denominator'], '709700000000')
        self.assertEqual(result['mining']['last_block_age_ms'], '4000')
        self.assertEqual(result['mining']['current_block_reward_xrs'], '10.0')
        json.dumps(result)  # No Decimal or integer amount escapes the public schema.

    def test_all_public_company_labels_match_valid_classification_addresses(self):
        p = policy()
        self.assertTrue(all(public_address(a) for a in p['company_addresses']))
        self.assertEqual(set(p['company_addresses']), {r['address'] for r in p['company_wallets']})
        p['company_wallets'].pop()
        with self.assertRaises(collector.CollectionError):
            self.run_collect(selected_policy=p)

    def test_wrong_chain_checkpoint_or_policy_is_rejected(self):
        data = responses()
        data[(50008, f'/v2/block/slot/{collector.CHECKPOINT_SLOT}')]['data']['hash'] = 'cd' * 32
        with self.assertRaises(collector.CollectionError):
            self.run_collect(data)
        p = policy()
        p['chain_id'] = 'xeris-testnet-v2'
        with self.assertRaises(collector.CollectionError):
            self.run_collect(selected_policy=p)

    def test_reorg_at_same_height_fails_block_bracketing(self):
        data, tips = responses(), 0
        def read(port, path):
            nonlocal tips
            result = copy.deepcopy(data[(port, path)])
            if path.startswith('/v2/blocks?'):
                tips += 1
                if tips == 2:
                    result['data'][0]['hash'] = 'cd' * 32
            return result
        with self.assertRaises(SupplyUnavailable):
            self.run_collect(reader=read)

    def test_disconnected_old_tip_and_future_tip_are_not_live(self):
        for timestamp in [NOW * 1000 - 120001, NOW * 1000 + 30001]:
            data = responses()
            data[(50008, '/v2/blocks?page=1&page_size=12')]['data'][0]['poh_timestamp'] = timestamp
            with self.assertRaises(collector.CollectionError):
                self.run_collect(data)

    def test_quote_decimal_or_pool_pin_changes_fail(self):
        for kind in ['decimals', 'owner', 'created_slot', 'reserves', 'fees']:
            data = responses()
            contract = data[(56001, f'/contract/{collector.POOL}')]['contract']
            if kind == 'decimals':
                data[(50008, '/v2/tokens?limit=32')]['data'][1]['decimals'] = 9
            elif kind == 'reserves':
                contract['state']['Swap']['reserve_b'] += 1
            elif kind == 'fees':
                contract['state']['Swap']['fee_bps'] = 77
            else:
                contract[kind] = 1 if kind == 'created_slot' else 'different-owner'
            with self.subTest(kind=kind), self.assertRaises(collector.CollectionError):
                self.run_collect(data)

    def test_native_census_limit_and_mixed_economics_are_not_partial_success(self):
        for path, key, val in [('/v2/stats', 'total_accounts', 101), ('/network/economics', 'current_slot', 29999)]:
            data = responses()
            row = data[(50008 if path == '/v2/stats' else 56001, path)]
            (row['data'] if 'data' in row else row)[key] = val
            with self.assertRaises(SupplyUnavailable):
                self.run_collect(data)

    def test_registry_cursor_is_encoded_and_count_complete(self):
        calls = []
        def read(port, path):
            calls.append((port, path))
            return {'success': True, 'count': 2, 'data': [1 if len(calls) == 1 else 2],
                    'next_after': 'a&b' if len(calls) == 1 else None}
        self.assertEqual(collector.registry(read, '/v2/tokens', 'data'), ([1, 2], 2))
        self.assertEqual(calls[1][1], '/v2/tokens?limit=32&after=a%26b')
        with self.assertRaises(collector.CollectionError):
            collector.registry(lambda *_: {'success': True, 'count': 2, 'data': [1], 'next_after': None}, '/v2/tokens', 'data')

    def test_registry_loops_and_changed_totals_fail(self):
        with self.assertRaises(collector.CollectionError):
            collector.registry(lambda *_: {'success': True, 'count': 2, 'data': [], 'next_after': 'a'}, '/v2/tokens', 'data')
        calls = 0
        def read(*_):
            nonlocal calls
            calls += 1
            return {'success': True, 'count': calls, 'data': [], 'next_after': str(calls)}
        with self.assertRaises(collector.CollectionError):
            collector.registry(read, '/v2/tokens', 'data')

    def test_nonfinite_and_duplicate_json_are_rejected_losslessly(self):
        for body in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}']:
            with self.assertRaises(collector.CollectionError):
                json.loads(body, parse_float=Decimal, parse_constant=collector.reject_constant,
                           object_pairs_hook=collector.exact_object)
        got = json.loads('{"amount":9007199254740993,"price":0.01423}',
                         parse_float=Decimal, object_pairs_hook=collector.exact_object)
        self.assertEqual(got['amount'], 9007199254740993)
        self.assertEqual(got['price'], Decimal('0.01423'))

    def test_reader_only_accepts_fixed_loopback_ports_and_no_redirect(self):
        reader = collector.NodeReader()
        for port, path in [(80, '/v2/stats'), (50008, '//example.com/'), (50008, 'https://example.com/')]:
            with self.assertRaises(collector.CollectionError):
                reader(port, path)
        with self.assertRaises(collector.CollectionError):
            collector.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_graduation_auto_approves_only_verified_pool_and_prices_actual_circulation(self):
        data = responses()
        add_launch(data)
        result = self.run_collect(data)
        launch = result['launchpad_tokens'][0]
        self.assertEqual(launch['status'], 'ready')
        self.assertEqual(launch['circulating_raw'], str(800 * 10**9))
        self.assertEqual(launch['supply']['issued_supply_raw'], str(1000 * 10**9))
        self.assertEqual(launch['market_cap_quote_raw'],
                         str(800 * 10**9 * 50 * 10**9 * 10101886 // (500 * 10**9 * 709700000000)))
        self.assertEqual(result['market']['circulating_raw'], '199999759700000000')
        self.assertIn('lp_example_xrs_pool', result['policy']['included_pool_ids'])
        self.assertEqual(policy()['included_pool_ids'], [collector.POOL])  # Input policy is not mutated.

    def test_pregraduation_keeps_actual_supply_but_price_and_cap_unavailable(self):
        data = responses()
        add_launch(data, graduated=False)
        result = self.run_collect(data)
        launch = result['launchpad_tokens'][0]
        self.assertEqual(launch['status'], 'pricing_unavailable')
        self.assertIsNone(launch['market_cap_quote_raw'])
        self.assertEqual(launch['circulating_raw'], str(300 * 10**9))
        self.assertNotIn('price_pool_id', launch)

    def test_ordinary_token_cannot_spoof_canonical_launch_pool(self):
        data = responses()
        token = add_launch(data)
        token['mint_policy'] = 'Standard'
        with self.assertRaises(SupplyUnavailable):
            self.run_collect(data)

    def test_unrelated_funded_xrs_pool_is_not_auto_approved(self):
        data = responses()
        add_launch(data)
        data[(50008, '/v2/pools?limit=32')]['pools'][1]['pool_id'] = 'user-chosen-pool'
        with self.assertRaises(SupplyUnavailable):
            self.run_collect(data)

    def test_launch_mint_authority_cannot_be_self_asserted(self):
        data = responses()
        token = add_launch(data)
        token['mint_authority'] = policy()['company_addresses'][0]
        with self.assertRaises(SupplyUnavailable):
            self.run_collect(data)

    def test_canonical_contract_owner_pair_reserves_and_creation_must_match(self):
        for kind in ['owner', 'pair', 'reserves', 'created_before_token', 'fee']:
            data = responses()
            add_launch(data)
            contract = data[(56001, '/contract/lp_example_xrs_pool')]['contract']
            if kind == 'owner':
                contract['owner'] = policy()['company_addresses'][0]
            elif kind == 'pair':
                contract['state']['Swap']['token_b'] = collector.USDC
            elif kind == 'reserves':
                contract['state']['Swap']['reserve_b'] += 1
            elif kind == 'fee':
                contract['state']['Swap']['fee_bps'] = 77
            else:
                contract['created_slot'] = 27000
            with self.subTest(kind=kind), self.assertRaises(collector.CollectionError):
                self.run_collect(data)

    def test_launch_holders_must_be_complete_and_nonduplicate(self):
        for kind in ['missing', 'duplicate']:
            data = responses()
            add_launch(data)
            holders = data[(50008, '/v2/token/lp_example/holders?limit=32')]
            if kind == 'missing':
                holders['holder_count'] = 3
            else:
                holders['holders'][1] = copy.deepcopy(holders['holders'][0])
            with self.subTest(kind=kind), self.assertRaises((collector.CollectionError, SupplyUnavailable)):
                self.run_collect(data)

    def test_reorg_after_launch_holder_reads_rejects_entire_snapshot(self):
        data = responses()
        add_launch(data)
        launched_read = False
        def read(port, path):
            nonlocal launched_read
            result = copy.deepcopy(data[(port, path)])
            if path == '/v2/token/lp_example/holders?limit=32':
                launched_read = True
            if launched_read and path.startswith('/v2/blocks?'):
                result['data'][0]['hash'] = 'cd' * 32
            return result
        with self.assertRaises(SupplyUnavailable):
            self.run_collect(reader=read)
        self.assertTrue(launched_read)

    def test_shared_deadline_stops_before_another_launch_read(self):
        data = responses()
        add_launch(data)
        now, calls = [0.0], []
        def read(port, path):
            calls.append(path)
            if path == '/v2/pools?limit=32':
                now[0] = 12.001
            return copy.deepcopy(data[(port, path)])
        with patch.object(collector.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(collector.CollectionError, 'read deadline'):
                self.run_collect(reader=read)
        self.assertNotIn('/v2/token/lp_example/holders?limit=32', calls)

    def test_canonical_id_fallback_uses_utf8_byte_length_and_sha256(self):
        self.assertEqual(collector.launch_pool_id('lp_example'), 'lp_example_xrs_pool')
        tid = 'lp_' + 'x' * 117
        self.assertEqual(collector.launch_pool_id(tid),
                         'xeris_lp_' + collector.hashlib.sha256(tid.encode()).hexdigest() + '_xrs_pool')
        # Rust String::len measures UTF-8 bytes, not Python character count.
        unicode_tid = 'lp_' + '\u00e9' * 60
        self.assertTrue(collector.launch_pool_id(unicode_tid).startswith('xeris_lp_'))


if __name__ == '__main__':
    unittest.main()
