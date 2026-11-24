"""Offline public-wire/Merkle and concurrent-accounting adversarial cases."""
import base64
import copy
import hashlib
import json
import unittest

import xeris_purchase as p
import xeris_redemption as r
import xeris_accounting as a
from test_xeris_purchase import (b58, OPERATOR, RECIPIENT, SIGNATURE, BLOCKHASH,
                                 ROUTE, snapshot, advance, prepared, receipt, buy_case)

OTHER = b58(bytes([11]) * 32)
OTHER_SIG = b58(bytes([12]) * 64)
THIRD_SIG = b58(bytes([13]) * 64)


def signed_wire(plan, signature=SIGNATURE):
    return base64.b64encode(b'\x01' + p._decode58(signature, 64) + base64.b64decode(plan['message_base64'])).decode()


def evidence(before, after, plans):
    """plans: [(prepared, signature, slot, committed)], in block execution order."""
    raws, receipts, blocks = {}, {}, []
    for slot in range(before['slot'], after['slot'] + 1):
        summaries, wires = [], []
        for plan, signature, at, committed in plans:
            if at != slot: continue
            wire = signed_wire(plan, signature)
            raws[signature] = wire; wires.append(base64.b64decode(wire))
            summaries.append({'signature': signature, 'block_slot': slot, 'status': 'included',
                              **copy.deepcopy(plan['expected_receipt_fields'])})
            result = receipt(plan, slot)
            result['transaction']['signatures'] = [signature]
            if not committed:
                result['meta'].update(err={'InstructionError': [0, 'ExecutionFailed']},
                                      instructionOutcomes=[{'index': 0, 'ok': False}])
            receipts[signature] = result
        blocks.append({'slot': slot, 'hash': f'{slot:064x}', 'previous_hash': f'{slot - 1:064x}',
                       'merkle_root': a.merkle_root(wires), 'proposer': p.TREASURY,
                       'transaction_count': len(summaries), 'transactions': summaries})
    return {'route_id': ROUTE, 'operator_wallet': before['wallet'], 'exclusive_wallet': True,
            'pending_transaction_ids': [], 'canonical_blocks_by_node': {node: copy.deepcopy(blocks) for node in p.NODES},
            'raw_transactions_by_signature': raws,
            'receipts_by_node': {node: copy.deepcopy(receipts) for node in p.NODES}}


def concurrent_buy_case():
    before, _, plan, target, _ = buy_case()
    earlier = p.build_stage('buy', OTHER, RECIPIENT, BLOCKHASH, 1000, 1, route_id='other-route')
    after = advance(before)
    first = a.swap_output(before['reserve_a_raw'], before['reserve_b_raw'], 1000, True)
    expected_output = a.swap_output(before['reserve_a_raw'] - first, before['reserve_b_raw'] + 1000, 500_000, True)
    after.update(native_raw=2_000_000, xusdc_raw=0, wxrs_raw=expected_output,
                 reserve_a_raw=before['reserve_a_raw'] - first - expected_output,
                 reserve_b_raw=before['reserve_b_raw'] + 501_000)
    proof = evidence(before, after, [(earlier, OTHER_SIG, 101, True), (plan, SIGNATURE, 101, True)])
    return before, after, plan, target, proof


class AccountingTests(unittest.TestCase):
    def verify(self, values):
        before, after, plan, result, proof = values
        return p.verify_accounted_transition(before, after, plan, result, SIGNATURE, proof)

    def test_concurrent_earlier_pool_swap_changes_actual_output_not_old_quote(self):
        values = concurrent_buy_case()
        result = self.verify(values)
        self.assertEqual(result['actual_output_xrs_raw'], values[1]['wxrs_raw'])
        self.assertNotEqual(result['actual_output_xrs_raw'], 33_383_761_746)
        self.assertEqual(result['concurrent_transactions_accounted'], 1)

    def test_later_pool_swap_and_wrap_replay_without_attributing_to_target(self):
        before, after, plan, target, _ = buy_case()
        wrap = r.build_stage('wrap', OTHER, RECIPIENT, BLOCKHASH, 100_000_000, route_id='other')
        sell = r.build_stage('sell', OTHER, RECIPIENT, BLOCKHASH, 100_000_000, 1, route_id='other')
        output = a.swap_output(after['reserve_a_raw'], after['reserve_b_raw'], 100_000_000, False)
        after['wxrs_supply_raw'] += 100_000_000
        after['reserve_a_raw'] += 100_000_000; after['reserve_b_raw'] -= output
        proof = evidence(before, after, [(plan, SIGNATURE, 101, True), (wrap, OTHER_SIG, 101, True), (sell, THIRD_SIG, 101, True)])
        result = self.verify((before, after, plan, target, proof))
        self.assertEqual(result['actual_output_xrs_raw'], 33_383_761_746)
        self.assertEqual(result['concurrent_transactions_accounted'], 2)

    def test_unrelated_native_transfer_is_ignored_but_receipted(self):
        before, after, plan, target, _ = buy_case()
        transfer = p.build_stage('deliver', OTHER, RECIPIENT, BLOCKHASH, 123, route_id='other')
        proof = evidence(before, after, [(transfer, OTHER_SIG, 101, True), (plan, SIGNATURE, 101, True)])
        self.assertEqual(self.verify((before, after, plan, target, proof))['actual_output_xrs_raw'], 33_383_761_746)

    def test_concurrent_mint_and_burn_update_global_supply(self):
        before, after, plan, target, _ = buy_case()
        mint = p.build_stage('mint', OTHER, RECIPIENT, BLOCKHASH, 3000, route_id='other')
        burn = r.build_stage('burn', OTHER, RECIPIENT, BLOCKHASH, 1000, route_id='other', route_kind='xusdc')
        after['xusdc_supply_raw'] += 2000
        proof = evidence(before, after, [(mint, OTHER_SIG, 101, True), (plan, SIGNATURE, 101, True), (burn, THIRD_SIG, 101, True)])
        self.assertEqual(self.verify((before, after, plan, target, proof))['concurrent_transactions_accounted'], 2)

    def test_failed_supported_swap_has_no_pool_effect(self):
        before, after, plan, target, _ = buy_case()
        failed = p.build_stage('buy', OTHER, RECIPIENT, BLOCKHASH, 1000, p.U64_MAX, route_id='other')
        proof = evidence(before, after, [(failed, OTHER_SIG, 101, False), (plan, SIGNATURE, 101, True)])
        self.verify((before, after, plan, target, proof))

    def test_route_wallet_donation_cannot_inflate_purchase_output(self):
        before, after, plan, target, _ = buy_case()
        transfer = p.build_stage('deliver', OTHER, OPERATOR, BLOCKHASH, 123, route_id='other')
        after['native_raw'] += 123
        proof = evidence(before, after, [(transfer, OTHER_SIG, 101, True), (plan, SIGNATURE, 101, True)])
        with self.assertRaisesRegex(ValueError, 'route-wallet'):
            self.verify((before, after, plan, target, proof))

    def test_receipts_missing_partial_unknown_or_disagree_rejected(self):
        for mutate in (
            lambda e: e['receipts_by_node']['droplet'].pop(OTHER_SIG),
            lambda e: e['receipts_by_node']['droplet'][OTHER_SIG]['meta'].update(err='OutcomeUnknown'),
            lambda e: e['receipts_by_node']['droplet'][OTHER_SIG]['meta'].update(instructionOutcomes=[{'index': 0, 'ok': True}, {'index': 1, 'ok': False}]),
            lambda e: e['receipts_by_node']['droplet'][OTHER_SIG]['transaction'].update(amount=1),
            lambda e: e['receipts_by_node']['droplet'][OTHER_SIG]['meta'].update(instructionOutcomes=[{'index': False, 'ok': 1}]),
        ):
            values = concurrent_buy_case(); mutate(values[-1])
            with self.assertRaises(ValueError): self.verify(values)

    def test_raw_message_tamper_or_root_or_parent_mismatch_rejected(self):
        for mutate in (
            lambda e: e['raw_transactions_by_signature'].pop(OTHER_SIG),
            lambda e: e['raw_transactions_by_signature'].update({OTHER_SIG: signed_wire(prepared('buy', 1000, 1), OTHER_SIG)}),
            lambda e: e['canonical_blocks_by_node']['droplet'][-1].update(previous_hash='ab' * 32),
            lambda e: [blocks[-1].update(merkle_root='ab' * 32) for blocks in e['canonical_blocks_by_node'].values()],
        ):
            values = concurrent_buy_case(); mutate(values[-1])
            with self.assertRaises(ValueError): self.verify(values)

    def test_target_different_recent_blockhash_rejected_even_with_same_summary(self):
        values = concurrent_buy_case()
        altered = p.build_stage('buy', OPERATOR, RECIPIENT, bytes([19]) * 32, 500_000, 33_216_842_938, route_id=ROUTE)
        proof = values[-1]
        proof['raw_transactions_by_signature'][SIGNATURE] = signed_wire(altered)
        wires = [base64.b64decode(proof['raw_transactions_by_signature'][s]) for s in (OTHER_SIG, SIGNATURE)]
        for blocks in proof['canonical_blocks_by_node'].values(): blocks[-1]['merkle_root'] = a.merkle_root(wires)
        with self.assertRaisesRegex(ValueError, 'signed message'): self.verify(values)

    def test_multi_instruction_wire_cannot_hide_second_effect(self):
        plan = prepared('buy', 500_000, 1)
        tx = a.decode_transaction(signed_wire(plan))
        # Header/key/blockhash prefix is100 bytes; duplicate the one compiled ix.
        message = tx['message'][:100] + b'\x02' + tx['message'][101:] * 2
        wire = b'\x01' + p._decode58(SIGNATURE, 64) + message
        with self.assertRaisesRegex(ValueError, 'multi-instruction'):
            a.decode_transaction(base64.b64encode(wire).decode())

    def test_unknown_contract_and_duplicate_arguments_fail_closed(self):
        for args in ('{"amount":1,"amount":2,"min_output":1}', '{"amount":1,"min_output":true}'):
            payload = (4).to_bytes(4, 'little') + p._string(p.POOL) + p._string('swap_a_to_b') + p._string(args)
            with self.assertRaises(ValueError): a.decode_effect({'signer': OTHER, 'signatures': [OTHER_SIG], 'instructions': [{'data': payload}]})
        payload = (4).to_bytes(4, 'little') + p._string(p.POOL) + p._string('add_liquidity') + p._string('{}')
        with self.assertRaisesRegex(ValueError, 'Unsupported contract'):
            a.decode_effect({'signer': OTHER, 'signatures': [OTHER_SIG], 'instructions': [{'data': payload}]})

    def test_merkle_empty_single_and_odd_layers_match_source_formula(self):
        self.assertEqual(a.merkle_root([]), hashlib.sha256(b'\x01XRS_MERKLE_EMPTY_V2').hexdigest())
        wires = [b'first', b'second', b'third']
        leaves = [hashlib.sha256(b'\0' + w).digest() for w in wires]
        left = hashlib.sha256(b'\1' + leaves[0] + leaves[1]).digest()
        right = hashlib.sha256(b'\1' + leaves[2] + hashlib.sha256(b'\1XRS_MERKLE_ODD_PAD_V2').digest()).digest()
        self.assertEqual(a.merkle_root(wires), hashlib.sha256(b'\1' + left + right).hexdigest())
        self.assertNotEqual(a.merkle_root(wires), a.merkle_root(wires + [wires[-1]]))

    def test_sdk_serde_shortvec_json_serializes_to_exact_prepared_wire(self):
        plan = prepared('buy', 500_000, 1)
        payload = list(bytes.fromhex(plan['instruction_hex']))
        sv = lambda items: [list(p._shortvec(len(items))), *items]
        raw = {'signatures': sv([list(p._decode58(SIGNATURE, 64))]), 'message': {
            'header': {'num_required_signatures': 1, 'num_readonly_signed_accounts': 0, 'num_readonly_unsigned_accounts': 1},
            'account_keys': sv([list(p._decode58(OPERATOR, 32)), [0] * 32]), 'recent_blockhash': list(BLOCKHASH),
            'instructions': sv([{'program_id_index': 1, 'accounts': sv([0]), 'data': sv(payload)}])}}
        self.assertEqual(a.encode_rpc_transaction(raw), signed_wire(plan))
        raw['message']['instructions'][0] = [129, 0]
        with self.assertRaisesRegex(ValueError, 'Noncanonical'): a.encode_rpc_transaction(raw)


class FailedOriginalTests(unittest.TestCase):
    def values(self):
        before=snapshot(100);after=snapshot(113)
        plan=prepared('mint',500_000)
        proof=evidence(before,after,[(plan,SIGNATURE,101,False)])
        proof['exclusive_wallet']=False
        result=proof['receipts_by_node']['droplet'][SIGNATURE]
        return before,after,plan,result,proof

    def test_exact_failed_original_with_twelve_successors_is_evidence_not_authorization(self):
        before,after,plan,result,proof=self.values()
        checked=a.verify_failed_original(before,after,plan,result,SIGNATURE,proof)
        self.assertEqual(checked['status'],'VERIFIED_FAILED_ORIGINAL')
        self.assertFalse(checked['refund_authorized'])
        self.assertFalse(checked['replacement_authorized'])

    def test_missing_ambiguous_successful_or_insufficiently_confirmed_is_not_failure(self):
        for mutate in (
            lambda before,after,plan,result,proof:proof['receipts_by_node']['mac_treasury'].pop(SIGNATURE),
            lambda before,after,plan,result,proof:result['meta'].update(err='OutcomeUnknown'),
            lambda before,after,plan,result,proof:result['meta'].update(err=None,instructionOutcomes=[{'index':0,'ok':True}]),
            lambda before,after,plan,result,proof:after.update(slot=112),
        ):
            values=self.values();mutate(*values)
            with self.assertRaises(ValueError):
                a.verify_failed_original(*values[:4],SIGNATURE,values[4])

    def test_variable_swap_accounting_still_requires_exclusive_operator(self):
        before,after,plan,result,_=buy_case()
        proof=evidence(before,after,[(plan,SIGNATURE,101,True)])
        proof['exclusive_wallet']=False
        with self.assertRaisesRegex(ValueError,'Exclusive'):
            p.verify_accounted_transition(before,after,plan,result,SIGNATURE,proof)


if __name__ == '__main__': unittest.main()
