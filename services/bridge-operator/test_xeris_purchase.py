"""Offline tests: synthetic public identities, public SDK fixtures, no signing."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import unittest

import xeris_purchase as purchase


def b58(data):
    number, output = int.from_bytes(data, 'big'), ''
    while number:
        number, digit = divmod(number, 58)
        output = purchase.ALPHABET[digit] + output
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + output


OPERATOR = b58(bytes([7]) * 32)
RECIPIENT = b58(bytes([8]) * 32)
SIGNATURE = b58(bytes([9]) * 64)
BLOCKHASH = bytes([18]) * 32
ROUTE = 'offline-route-001'
ROOT = Path(__file__).resolve().parents[2]


def snapshot(slot=100):
    block_hash = f'{slot:064x}'
    return {'chain_id': purchase.CHAIN, 'wallet': OPERATOR, 'slot': slot,
            'block_hash': block_hash,
            'read_boundary': {'start_slot': slot, 'end_slot': slot,
                              'start_hash': block_hash, 'end_hash': block_hash},
            'native_raw': 3_000_000, 'xusdc_raw': 0, 'wxrs_raw': 0,
            'xusdc_supply_raw': 10_099_031, 'wxrs_supply_raw': 709_700_000_000,
            'reserve_a_raw': 709_700_000_000, 'reserve_b_raw': 10_099_031,
            'pool_invariant': {'contract_id': purchase.POOL, 'contract_type': 'Swap',
                'owner': purchase.LP, 'is_active': True,
                'state': {'Swap': {'token_a': purchase.WRAPPED, 'token_b': purchase.USDC, 'fee_bps': 30}}},
            'token_metadata': {token: {'token_id': token, 'symbol': symbol, 'decimals': decimals,
                'mint_authority': purchase.TREASURY, 'mint_policy': 'Standard',
                'max_supply': 1_000_000_000_000_000 if token == purchase.USDC else 700_000_000_000_000_000}
                for token, symbol, decimals in ((purchase.USDC, 'xUSDC', 6), (purchase.WRAPPED, 'wXRS', 9))}}


def advance(state):
    state = copy.deepcopy(state)
    state['slot'] += 1
    state['block_hash'] = f"{state['slot']:064x}"
    state['read_boundary'] = {'start_slot': state['slot'], 'end_slot': state['slot'],
                              'start_hash': state['block_hash'], 'end_hash': state['block_hash']}
    return state


def prepared(stage, amount, minimum=None):
    return purchase.build_stage(stage, OPERATOR, RECIPIENT, BLOCKHASH, amount, minimum, route_id=ROUTE)


def receipt(plan, slot=101):
    return {'slot': slot, 'meta': {'err': None, 'fee': purchase.BASE_FEE_RAW,
                                  'instructionOutcomes': [{'index': 0, 'ok': True}]},
            'transaction': {'signatures': [SIGNATURE], **copy.deepcopy(plan['expected_receipt_fields'])}}


def evidence(before, after, plan, result):
    blocks = []
    for slot in range(before['slot'], after['slot'] + 1):
        txs = []
        if slot == result['slot']:
            txs = [{'signature': SIGNATURE, 'block_slot': slot, 'status': 'included',
                    **copy.deepcopy(plan['expected_receipt_fields'])}]
        blocks.append({'slot': slot, 'hash': f'{slot:064x}', 'merkle_root': 'ab' * 32,
                       'proposer': purchase.TREASURY, 'transaction_count': len(txs), 'transactions': txs})
    return {'route_id': ROUTE, 'operator_wallet': OPERATOR, 'exclusive_wallet': True,
            'pending_transaction_ids': [],
            'canonical_blocks_by_node': {node: copy.deepcopy(blocks) for node in purchase.NODES}}


def buy_case():
    before = snapshot()
    before['xusdc_raw'] = 500_000
    before['xusdc_supply_raw'] += 500_000
    plan = prepared('buy', 500_000, 33_216_842_938)
    result = receipt(plan)
    after = advance(before)
    after.update(native_raw=2_000_000, xusdc_raw=0, wxrs_raw=33_383_761_746,
                 reserve_a_raw=676_316_238_254, reserve_b_raw=10_599_031)
    return before, after, plan, result, evidence(before, after, plan, result)


class PurchaseTests(unittest.TestCase):
    def verify(self, values):
        before, after, plan, result, proof = values
        return purchase.verify_isolated_transition(before, after, plan, result, SIGNATURE, proof)

    def test_exact_quote_matches_public_executed_half_usdc_arithmetic(self):
        quote = purchase.quote_purchase(709_700_000_000, 10_099_031, 500_000)
        self.assertEqual(quote['expected_xrs_raw'], 33_383_761_746)
        self.assertEqual(quote['minimum_xrs_raw'], 33_216_842_938)
        self.assertEqual(quote['lp_fee_usdc_raw'], 1_500)
        self.assertEqual(quote['total_xeris_fees_raw'], 4_000_000)

    def test_quote_rounding_and_zero_fee_dust(self):
        quote = purchase.quote_purchase(1_000_000_000, 100, 1)
        self.assertEqual(quote['lp_fee_usdc_raw'], 0)
        self.assertEqual(quote['expected_xrs_raw'], 9_900_990)
        self.assertEqual(quote['minimum_xrs_raw'], 9_851_486)

    def test_quote_rejects_inexact_or_unsafe_values(self):
        for amount in (True, 0, -1, 1.0, '1', purchase.U64_MAX + 1):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                purchase.quote_purchase(1_000_000, 10_000, amount)
        for slippage in (True, -1, 51):
            with self.subTest(slippage=slippage), self.assertRaises(ValueError):
                purchase.quote_purchase(1_000_000, 10_000, 100, slippage)
        with self.assertRaises(ValueError):
            purchase.quote_purchase(10, purchase.U64_MAX, 1)
        with self.assertRaises(ValueError):
            purchase.quote_purchase(1, 1000, 1)

    def test_fee_allowance_is_upfront_native_and_separate_from_output(self):
        purchase.validate_fee_allowance(3_000_000, 1_000_000)
        for operator, authority in ((2_999_999, 1_000_000), (3_000_000, 999_999), (True, 1_000_000)):
            with self.subTest(operator=operator, authority=authority), self.assertRaises(ValueError):
                purchase.validate_fee_allowance(operator, authority)

    def test_unsigned_builder_exactly_matches_public_sdk_verified_fixture(self):
        path = Path(__file__).parent / 'fixtures/unsigned-purchase-sdk.json'
        fixture = json.loads(path.read_text())
        for legacy in fixture['transactions']:
            if legacy['stage'] not in ('mint', 'buy', 'unwrap'):
                continue
            plan = purchase.build_stage(legacy['stage'], legacy['pilot_wallet'], RECIPIENT,
                bytes.fromhex(fixture['blockhash_hex']), int(legacy['fields']['amount_raw']),
                int(legacy['fields']['min_output_raw']) if legacy['stage'] == 'buy' else None, route_id=ROUTE)
            for field in ('instruction_hex', 'message_base64', 'message_sha256', 'unsigned_transaction_base64', 'template_sha256'):
                with self.subTest(stage=legacy['stage'], field=field):
                    self.assertEqual(plan[field], legacy[field])

    def test_all_four_operations_match_independent_rust_sdk_without_keys(self):
        binary = ROOT / 'work/pool-setup/pilot/xeris/sdk-check/target/debug/xeris-pilot-sdk-check'
        if not binary.is_file():
            self.skipTest('Local public-only Rust SDK checker is not built')
        transactions = [prepared('mint', 500_000), prepared('buy', 500_000, 33_216_842_938),
                        prepared('unwrap', 33_383_761_746), prepared('deliver', 33_383_761_746)]
        result = subprocess.run([str(binary)], input=json.dumps({'blockhash_hex': BLOCKHASH.hex(),
            'transactions': transactions}), text=True, capture_output=True, timeout=15, check=True)
        checked = json.loads(result.stdout)
        self.assertTrue(checked['instruction_bincode_and_message_and_transaction_match_sdk'])
        self.assertFalse(checked['keys_loaded'])
        self.assertFalse(checked['transactions_signed_or_submitted'])
        self.assertEqual(checked['verified_operations'], ['TokenMint', 'ContractCall', 'UnwrapXrs', 'NativeTransfer'])

    def test_wire_has_only_zero_signature_and_separate_signer(self):
        for stage in ('mint', 'buy', 'unwrap', 'deliver'):
            plan = prepared(stage, 500_000, 1 if stage == 'buy' else None)
            wire = base64.b64decode(plan['unsigned_transaction_base64'])
            self.assertEqual(wire[:65], bytes([1]) + bytes(64))
            self.assertEqual(plan['signer_public_address'], purchase.TREASURY if stage == 'mint' else OPERATOR)
            self.assertNotEqual(plan['signer_public_address'], RECIPIENT)

    def test_builder_rejects_nonisolated_wallet_wrong_addresses_and_bounds(self):
        for wallet in purchase.PRODUCERS | {purchase.LP, '1' * 32, RECIPIENT}:
            with self.subTest(wallet=wallet), self.assertRaises(ValueError):
                purchase.build_stage('buy', wallet, RECIPIENT, BLOCKHASH, 500_000, 1, route_id=ROUTE)
        with self.assertRaises(ValueError):
            purchase.build_stage('mint', OPERATOR, RECIPIENT, bytes(32), 1, route_id=ROUTE)
        with self.assertRaises(ValueError):
            prepared('deliver', 1, 1)
        with self.assertRaises(ValueError):
            prepared('buy', 1, 0)

    def test_prepared_intent_cannot_be_changed_independently_of_wire(self):
        for mutate in (
            lambda p: p['fields'].update(amount_raw='1000000'),
            lambda p: p['expected_receipt_fields'].update(to=OPERATOR),
            lambda p: p.update(recipient=purchase.LP),
            lambda p: p.update(message_base64='Z' + p['message_base64'][1:]),
        ):
            plan = prepared('deliver', 500_000)
            mutate(plan)
            with self.assertRaises(ValueError):
                purchase.validate_prepared(plan)

    def test_receipt_requires_exact_success_signature_fee_and_decoded_fields(self):
        plan = prepared('buy', 500_000, 33_216_842_938)
        purchase.verify_receipt(receipt(plan), SIGNATURE, plan)
        for mutate in (
            lambda r: r['meta'].update(err='ExecutionFailed'),
            lambda r: r['meta'].update(instructionOutcomes=[{'index': 0, 'ok': True}, {'index': 1, 'ok': False}]),
            lambda r: r['meta'].update(instructionOutcomes=[{'index': False, 'ok': 1}]),
            lambda r: r['meta'].update(fee=999_999),
            lambda r: r['transaction'].update(signatures=[b58(bytes([10]) * 64)]),
            lambda r: r['transaction']['details']['args'].update(min_output=1),
            lambda r: r.update(slot=True),
        ):
            result = receipt(plan)
            mutate(result)
            with self.assertRaises(ValueError):
                purchase.verify_receipt(result, SIGNATURE, plan)

    def test_all_stages_account_for_native_delivery_without_user_gas(self):
        before = snapshot()
        minted = advance(before)
        minted['xusdc_raw'] = 500_000
        minted['xusdc_supply_raw'] += 500_000
        mint = prepared('mint', 500_000)
        r = receipt(mint, minted['slot'])
        self.verify((before, minted, mint, r, evidence(before, minted, mint, r)))
        before, bought, buy, r, proof = buy_case()
        output = self.verify((before, bought, buy, r, proof))['actual_output_xrs_raw']
        self.assertEqual(output, 33_383_761_746)
        unwrapped = advance(bought)
        unwrapped['native_raw'] = bought['native_raw'] - 1_000_000 + output
        unwrapped['wxrs_raw'] = 0
        unwrapped['wxrs_supply_raw'] -= output
        unwrap = prepared('unwrap', output)
        r = receipt(unwrap, unwrapped['slot'])
        self.verify((bought, unwrapped, unwrap, r, evidence(bought, unwrapped, unwrap, r)))
        delivered = advance(unwrapped)
        delivered['native_raw'] -= output + 1_000_000
        delivery = prepared('deliver', output)
        r = receipt(delivery, delivered['slot'])
        result = self.verify((unwrapped, delivered, delivery, r, evidence(unwrapped, delivered, delivery, r)))
        self.assertEqual(delivered['native_raw'], 0)
        self.assertEqual(result['delivered_native_xrs_raw'], output)
        self.assertEqual(result['recipient'], RECIPIENT)
        self.assertFalse(result['irreversible_finality_proven'])

    def test_buy_does_not_treat_quote_or_minimum_as_actual_output(self):
        values = buy_case()
        values[1]['wxrs_raw'] = values[2]['expected_receipt_fields']['details']['args']['min_output']
        with self.assertRaises(ValueError):
            self.verify(values)

    def test_unsolicited_balance_transfer_cannot_inflate_bought_output(self):
        values = buy_case()
        values[1]['wxrs_raw'] += 1
        with self.assertRaises(ValueError):
            self.verify(values)

    def test_exact_pool_change_also_required(self):
        values = buy_case()
        values[1]['reserve_a_raw'] += 1
        with self.assertRaises(ValueError):
            self.verify(values)

    def test_preexisting_wrapped_holdings_never_swept(self):
        values = buy_case()
        values[0]['wxrs_raw'] = 100
        values[1]['wxrs_raw'] += 100
        with self.assertRaises(ValueError):
            self.verify(values)

    def test_exclusivity_pending_ids_and_route_binding_are_required(self):
        for change in ({'exclusive_wallet': False}, {'pending_transaction_ids': [SIGNATURE]},
                       {'route_id': 'other'}, {'operator_wallet': RECIPIENT}):
            values = buy_case()
            values[-1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(values)

    def test_incoherent_or_wrong_chain_snapshots_rejected(self):
        for mutate in (
            lambda s: s.update(chain_id='xeris-testnet-v1'),
            lambda s: s.update(wallet=RECIPIENT),
            lambda s: s['read_boundary'].update(end_slot=s['slot'] + 1),
            lambda s: s.update(block_hash='ee' * 32),
            lambda s: s['pool_invariant'].update(owner=RECIPIENT),
            lambda s: s['pool_invariant'].update(state=None),
            lambda s: s['token_metadata'][purchase.USDC].update(decimals=9),
            lambda s: s['token_metadata'][purchase.USDC].update(max_supply=1),
        ):
            values = buy_case()
            mutate(values[1])
            with self.assertRaises(ValueError):
                self.verify(values)

    def test_other_transaction_in_same_block_rejected_even_when_wallet_delta_matches(self):
        values = buy_case()
        for blocks in values[-1]['canonical_blocks_by_node'].values():
            blocks[-1]['transactions'].append({'signature': b58(bytes([10]) * 64)})
            blocks[-1]['transaction_count'] += 1
        with self.assertRaisesRegex(ValueError, 'Concurrent'):
            self.verify(values)

    def test_missing_inconsistent_duplicate_and_truncated_block_evidence_rejected(self):
        for mutate in (
            lambda e: e['canonical_blocks_by_node'].pop('droplet'),
            lambda e: e['canonical_blocks_by_node']['droplet'].pop(0),
            lambda e: e['canonical_blocks_by_node']['droplet'][-1].update(hash='ee' * 32),
            lambda e: e['canonical_blocks_by_node']['droplet'][-1].update(transaction_count=2),
            lambda e: e['canonical_blocks_by_node']['droplet'][-1]['transactions'][0].update(amount=1),
        ):
            values = buy_case()
            mutate(values[-1])
            with self.assertRaises(ValueError):
                self.verify(values)

    def test_insufficient_fee_reserve_and_below_minimum_are_rejected(self):
        values = buy_case()
        values[0]['native_raw'] = 2_000_000
        values[1]['native_raw'] = 1_000_000
        with self.assertRaises(ValueError):
            self.verify(values)
        values = list(buy_case())
        values[2] = prepared('buy', 500_000, 33_383_761_747)
        values[3] = receipt(values[2])
        values[4] = evidence(values[0], values[1], values[2], values[3])
        with self.assertRaisesRegex(ValueError, 'minimum'):
            self.verify(values)


if __name__ == '__main__':
    unittest.main()
