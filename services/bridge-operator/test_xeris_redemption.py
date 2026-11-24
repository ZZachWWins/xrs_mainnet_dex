"""Offline redemption intent, SDK wire, fee and accounting checks; no signing."""
import base64
import json
import subprocess
import unittest
import xeris_purchase as p
import xeris_redemption as r
from test_xeris_purchase import OPERATOR, RECIPIENT, SIGNATURE, BLOCKHASH, ROUTE, ROOT, snapshot, advance, receipt
from test_xeris_accounting import evidence, OTHER, OTHER_SIG


def plan(stage, amount, minimum=None, route_kind='native_xrs'):
    return r.build_stage(stage, OPERATOR, RECIPIENT, BLOCKHASH, amount, minimum, route_id=ROUTE, route_kind=route_kind)


def intent(prepared):
    return {'route_id': ROUTE, 'route_kind': prepared['route_kind'], 'source_wallet': OPERATOR,
            'solana_recipient': RECIPIENT, 'burn_amount_raw': prepared['fields']['amount_raw'],
            'signed_message_sha256': prepared['message_sha256'], 'burn_signature': SIGNATURE,
            'authenticated_before_submission': True}


class RedemptionTests(unittest.TestCase):
    def verify(self, before, after, prepared, other=()):
        result = receipt(prepared, after['slot'])
        proof = evidence(before, after, [*other, (prepared, SIGNATURE, after['slot'], True)])
        return r.verify_accounted_transition(before, after, prepared, result, SIGNATURE, proof)

    def test_quote_existing_lp_fee_and_native_cost_are_separate(self):
        q = r.quote_redemption(676_316_238_254, 10_599_031, 33_383_761_746)
        self.assertEqual(q['expected_xusdc_raw'], 497_145)
        self.assertEqual(q['minimum_xusdc_raw'], 494_660)
        self.assertEqual(q['user_native_fee_allowance_raw'], 3_000_000)
        self.assertIsNone(q['payout_fee_raw']); self.assertFalse(q['payout_ready'])

    def test_fees_reserved_before_native_wrap_and_direct_burn(self):
        r.validate_fee_allowance(1_003_000_000, 1_000_000_000, 'native_xrs')
        r.validate_fee_allowance(1_000_000, 500_000, 'xusdc')
        for balance, amount, kind in [(1_002_999_999, 1_000_000_000, 'native_xrs'), (999999, 500000, 'xusdc'), (True, 1, 'xusdc')]:
            with self.assertRaises(ValueError): r.validate_fee_allowance(balance, amount, kind)

    def test_three_wire_operations_match_independent_existing_rust_sdk(self):
        binary = ROOT / 'work/pool-setup/pilot/xeris/sdk-check/target/debug/xeris-pilot-sdk-check'
        if not binary.is_file(): self.skipTest('Public-only SDK checker not built')
        transactions = [plan('wrap', 33_383_761_746), plan('sell', 33_383_761_746, 494_660), plan('burn', 497_145)]
        checked = json.loads(subprocess.run([str(binary)], input=json.dumps({'blockhash_hex': BLOCKHASH.hex(), 'transactions': transactions}),
                                text=True, capture_output=True, timeout=15, check=True).stdout)
        self.assertEqual(checked['verified_operations'], ['WrapXrs', 'ContractCall', 'TokenBurn'])
        self.assertTrue(checked['instruction_bincode_and_message_and_transaction_match_sdk'])
        self.assertFalse(checked['keys_loaded']); self.assertFalse(checked['transactions_signed_or_submitted'])
        for tx in transactions: self.assertEqual(base64.b64decode(tx['unsigned_transaction_base64'])[:65], b'\1' + bytes(64))

    def test_three_stages_preserve_preexisting_wrapped_and_usdc_holdings(self):
        before = snapshot(); amount = 1_000_000_000
        before.update(native_raw=amount + 3_000_000, wxrs_raw=700, xusdc_raw=900)
        wrapped = advance(before)
        wrapped.update(native_raw=2_000_000, wxrs_raw=700 + amount, wxrs_supply_raw=before['wxrs_supply_raw'] + amount)
        self.verify(before, wrapped, plan('wrap', amount))
        q = r.quote_redemption(wrapped['reserve_a_raw'], wrapped['reserve_b_raw'], amount); output = q['expected_xusdc_raw']
        sold = advance(wrapped)
        sold.update(native_raw=1_000_000, wxrs_raw=700, xusdc_raw=900 + output,
                    reserve_a_raw=wrapped['reserve_a_raw'] + amount, reserve_b_raw=wrapped['reserve_b_raw'] - output)
        self.assertEqual(self.verify(wrapped, sold, plan('sell', amount, q['minimum_xusdc_raw']))['actual_output_xusdc_raw'], output)
        burned = advance(sold)
        burned.update(native_raw=0, xusdc_raw=900, xusdc_supply_raw=sold['xusdc_supply_raw'] - output)
        result = self.verify(sold, burned, plan('burn', output))
        self.assertEqual(result['burned_xusdc_raw'], output)
        self.assertEqual(burned['wxrs_raw'], 700); self.assertEqual(burned['xusdc_raw'], 900)
        self.assertFalse(result['payout_authorized'])

    def test_direct_xusdc_burn_does_not_require_swap_or_custody(self):
        before = snapshot(); before.update(native_raw=1_000_000, xusdc_raw=2_000_000, wxrs_raw=700)
        after = advance(before); after.update(native_raw=0, xusdc_raw=1_500_000, xusdc_supply_raw=before['xusdc_supply_raw'] - 500_000)
        prepared = plan('burn', 500_000, route_kind='xusdc')
        self.assertEqual(self.verify(before, after, prepared)['burned_xusdc_raw'], 500_000)
        self.assertEqual(prepared['expected_receipt_fields']['to'], '')

    def test_native_sale_accounts_for_preceding_concurrent_buy(self):
        from xeris_accounting import swap_output
        before = snapshot(); before.update(native_raw=2_000_000, wxrs_raw=1_000_000_000, xusdc_raw=900)
        earlier = p.build_stage('buy', OTHER, RECIPIENT, BLOCKHASH, 1000, 1, route_id='other')
        first = swap_output(before['reserve_a_raw'], before['reserve_b_raw'], 1000, True)
        a, b = before['reserve_a_raw'] - first, before['reserve_b_raw'] + 1000
        output = swap_output(a, b, 1_000_000_000, False)
        after = advance(before); after.update(native_raw=1_000_000, wxrs_raw=0, xusdc_raw=900 + output,
            reserve_a_raw=a + 1_000_000_000, reserve_b_raw=b - output)
        self.assertEqual(self.verify(before, after, plan('sell', 1_000_000_000, 1), [(earlier, OTHER_SIG, 101, True)])['actual_output_xusdc_raw'], output)

    def test_burn_intent_binds_destination_source_amount_message_and_signature(self):
        prepared = plan('burn', 500_000, route_kind='xusdc')
        result = r.verify_burn_intent(intent(prepared), prepared, receipt(prepared), SIGNATURE)
        self.assertEqual(result['gross_usdc_obligation_raw'], 500_000); self.assertFalse(result['payout_authorized'])
        for field, value in [('solana_recipient', OTHER), ('source_wallet', OTHER), ('burn_amount_raw', '500001'),
                             ('signed_message_sha256', 'aa' * 32), ('burn_signature', OTHER_SIG),
                             ('route_id', 'other'), ('authenticated_before_submission', False)]:
            changed = intent(prepared); changed[field] = value
            with self.assertRaises(ValueError): r.verify_burn_intent(changed, prepared, receipt(prepared), SIGNATURE)

    def test_mutated_prepared_metadata_rejected(self):
        for field, value in [('solana_recipient', OTHER), ('recipient', OTHER), ('route_kind', 'other'), ('fee_raw', '0')]:
            prepared = plan('burn', 500_000); prepared[field] = value
            with self.assertRaises(ValueError): r.validate_prepared(prepared)

    def test_failed_partial_wrong_token_and_false_integer_receipts_rejected(self):
        prepared = plan('burn', 500_000)
        for mutate in (lambda result: result['meta'].update(err='ExecutionFailed'),
            lambda result: result['meta'].update(instructionOutcomes=[{'index': 0, 'ok': True}, {'index': 1, 'ok': False}]),
            lambda result: result['transaction']['details'].update(token_id=p.WRAPPED),
            lambda result: result['transaction'].update(amount=True)):
            result = receipt(prepared); mutate(result)
            with self.assertRaises(ValueError): r.verify_receipt(result, SIGNATURE, prepared)

    def test_route_kind_and_amount_bounds(self):
        for stage in ('wrap', 'sell'):
            with self.assertRaises(ValueError): plan(stage, 1, 1 if stage == 'sell' else None, route_kind='xusdc')
        for amount in (0, True, -1, 1.0, '1', p.U64_MAX + 1):
            with self.assertRaises(ValueError): plan('burn', amount)
        with self.assertRaises(ValueError): plan('burn', 1, 1)
        with self.assertRaises(ValueError): r.quote_redemption(1_000_000, 1000, 1, 51)


if __name__ == '__main__': unittest.main()
