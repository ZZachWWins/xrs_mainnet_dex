"""Offline durability/state-machine tests; no keys, signing or RPC.

State-machine tests mock ONLY public signature verification. The separate public
fixture test runs real OpenSSL verification against existing public proofs.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import journal as j


def b58(raw):
    value = int.from_bytes(raw, 'big')
    result = ''
    while value:
        value, remainder = divmod(value, 58)
        result = j.B58[remainder] + result
    return '1' * (len(raw) - len(raw.lstrip(b'\0'))) + result


OWNER = b58(bytes([3]) * 32)
DESTINATION = b58(bytes([4]) * 32)
ATA = b58(bytes([5]) * 32)
SIGNATURE = b58(bytes([6]) * 64)
HASH = 'ab' * 32


def tx(n):
    return b58(n.to_bytes(64, 'big'))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'new-settlement.sqlite3'
        self.now = 1_800_000_000
        self.db = j.Journal(self.path, clock=lambda: self.now)
        self.verify = patch.object(j, 'verify_ed25519', return_value=None)
        self.verifier = self.verify.start()
        self.counter = 0

    def tearDown(self):
        self.verify.stop()
        self.db.close()
        self.tmp.cleanup()

    def fields(self, action='buy', **overrides):
        result = dict(action=action, source_owner=OWNER, destination_owner=DESTINATION,
            solana_token_account=ATA, amount_raw='500000' if action != 'sell' else '33383761746',
            min_output_raw='33000000000' if action == 'buy' else '490000',
            quote_id='quote-1', client_request_id='client-1')
        return {**result, **overrides}

    def intent(self, action='buy', **overrides):
        return self.db.issue_challenge(**self.fields(action, **overrides))['intent']

    def accepted(self, action='buy', **overrides):
        return self.db.accept_intent(self.intent(action, **overrides), SIGNATURE)['id']

    def active(self, intent_id):
        self.db.resume('Offline unit test worker')
        return self.db.acquire_route(intent_id, 'worker-1')

    def prepare(self, intent_id, stage, lease):
        self.counter += 1
        original = tx(1000 + self.counter)
        self.db.prepare_stage(intent_id, stage, j.SOLANA if stage in ('deposit', 'payout', 'refund') else j.XERIS,
                              original, HASH, lease=lease)
        return original

    def confirm(self, intent_id, stage, amount, lease):
        original = self.prepare(intent_id, stage, lease)
        self.assertTrue(self.db.mark_submitted(intent_id, stage, original, lease=lease)['first_submission'])
        return self.db.record_outcome(intent_id, stage, original, 'confirmed', HASH, amount, lease=lease)

    def deposit(self, intent_id, original=10):
        return self.db.claim_source(intent_id, j.SOLANA, tx(original), HASH, '500000')

    def test_wal_full_permissions_and_default_pause(self):
        self.assertEqual(self.db.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(self.db.db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        intent_id = self.accepted()
        state = self.db.status(intent_id)
        self.assertEqual(state['status'], 'awaiting_funding')
        self.assertTrue(state['processing_paused'])
        self.assertFalse(state['can_accept_funds'])
        self.assertIsNone(state['obligation'])

    def test_refuses_unrelated_database_without_mutation(self):
        path = Path(self.tmp.name) / 'unrelated.sqlite3'
        other = sqlite3.connect(path)
        other.execute('CREATE TABLE harmless_public_fixture(v TEXT)')
        other.commit()
        other.close()
        before = path.read_bytes()
        with self.assertRaises(j.JournalError):
            j.Journal(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + '-wal').exists())

    def test_refuses_symlink_and_changed_policy(self):
        link = Path(self.tmp.name) / 'link.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaises(j.JournalError):
            j.Journal(link)
        with self.assertRaises(j.JournalError):
            j.Journal(self.path, policy=j.Policy(maximum_usdc_raw=1))

    def test_quote_expiry_and_full_canonical_message(self):
        result = self.db.issue_challenge(**self.fields(), expires_at=self.now + 60)
        intent = result['intent']
        self.assertEqual(intent['expires_at'], self.now + 60)
        self.assertEqual(result['signing_message_utf8'], 'Xeris settlement ownership proof\n' + j.DOMAIN + '\n' + j.canonical(intent))
        self.assertEqual(intent['network'], j.NETWORK)
        self.assertEqual(intent['route'], j.ROUTES['buy'])
        self.db.accept_intent(intent, SIGNATURE)
        self.verifier.assert_called_with(OWNER, SIGNATURE, result['signing_message_utf8'].encode(), None)
        for expiry in (self.now, self.now - 1, self.now + 601, True, 1.5):
            with self.subTest(expiry=expiry), self.assertRaises(j.JournalError):
                self.db.issue_challenge(**self.fields(client_request_id='other'), expires_at=expiry)

    def test_challenge_idempotency_rejects_changed_economic_terms(self):
        first = self.intent()
        self.now += 5
        self.assertEqual(self.intent(), first)
        for changes in ({'amount_raw': '400000'}, {'destination_owner': OWNER},
                        {'min_output_raw': '34000000000'}, {'quote_id': 'other'}):
            with self.subTest(changes=changes), self.assertRaises(j.JournalError):
                self.intent(**changes)

    def test_nonce_single_use_acceptance_and_expired_reauthentication(self):
        intent = self.intent()
        first = self.db.accept_intent(intent, SIGNATURE)
        self.now = intent['expires_at'] + 500
        self.assertEqual(self.db.accept_intent(intent, SIGNATURE)['id'], first['id'])
        self.assertEqual(self.db.verify_existing_intent(intent, SIGNATURE)['id'], first['id'])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM settlement_intents').fetchone()[0], 1)
        self.assertIsNone(self.db.status(first['id'])['obligation'])

    def test_expired_unaccepted_challenge_does_not_accept(self):
        intent = self.intent()
        self.now = intent['expires_at']
        with self.assertRaises(j.JournalError):
            self.db.accept_intent(intent, SIGNATURE)
        with self.assertRaises(j.JournalError):
            self.db.verify_existing_intent(intent, SIGNATURE)

    def test_changed_domain_network_or_fields_never_reinterpret_old_proof(self):
        original = self.intent()
        changes = [{'domain': 'xeris-mainnet-usdc-operator-intent-v1'}, {'audience': 'https://other.example'},
            {'route': j.ROUTES['sell']}, {'nonce': 'cd' * 32}, {'amount_raw': '400000'},
            {'network': {**j.NETWORK, 'pool_id': 'different'}}, {'schema_version': True}, {'extra': 'x'}]
        for changeset in changes:
            with self.subTest(changes=changeset), self.assertRaises(j.JournalError):
                self.db.accept_intent({**original, **changeset}, SIGNATURE)

    def test_amounts_and_ids_are_strict(self):
        for value in (0, 1, True, 0.5, '0', '-1', '01', '1.0', '1e6', str(1 << 64)):
            with self.subTest(value=value), self.assertRaises(j.JournalError):
                self.intent(amount_raw=value)
        for value in ('', 'a\n', '../x', 'a' * 129):
            with self.subTest(value=value), self.assertRaises(j.JournalError):
                self.intent(client_request_id=value)
        with self.assertRaises(j.JournalError):
            self.intent(source_owner='1' * 32)

    def test_u64_values_remain_exact_strings(self):
        path = Path(self.tmp.name) / 'large.sqlite3'
        large = j.Journal(path, policy=j.Policy(maximum_usdc_raw=j.MAX_RAW), clock=lambda: self.now)
        try:
            intent = large.issue_challenge(**self.fields(amount_raw=str(j.MAX_RAW)))['intent']
            intent_id = large.accept_intent(intent, SIGNATURE)['id']
            result = large.claim_source(intent_id, j.SOLANA, tx(1), HASH, str(j.MAX_RAW))
            self.assertEqual(result['obligation']['amount_raw'], str(j.MAX_RAW))
            self.assertEqual(large.obligations()['totals'][0]['amount_raw'], str(j.MAX_RAW))
        finally:
            large.close()

    def test_pause_does_not_hide_incoming_obligation(self):
        intent_id = self.accepted()
        result = self.deposit(intent_id)
        self.assertEqual(result['obligation']['kind'], 'pending_issuance')
        lease = self.db.acquire_route(intent_id, 'worker-1')
        with self.assertRaises(j.JournalError):
            self.prepare(intent_id, 'mint', lease)
        self.assertFalse(self.db.resume('Test adapter ready')['can_accept_funds'])

    def test_global_deposit_claim_unique_and_idempotent(self):
        first = self.accepted()
        second = self.accepted(client_request_id='client-2')
        self.deposit(first)
        self.deposit(first)
        with self.assertRaises(j.JournalError):
            self.deposit(second)
        with self.assertRaises(j.JournalError):
            self.db.claim_source(first, j.SOLANA, tx(10), 'cd' * 32, '500000')
        self.assertEqual(len(self.db.obligations()['items']), 1)

    def test_source_chain_and_amount_must_match_intent(self):
        intent_id = self.accepted()
        for chain, amount in ((j.XERIS, '500000'), (j.SOLANA, '499999'), (j.SOLANA, '500001')):
            with self.subTest(chain=chain, amount=amount), self.assertRaises(j.JournalError):
                self.db.claim_source(intent_id, chain, tx(10), HASH, amount)
        self.assertEqual(self.db.obligations()['items'], [])

    def test_full_buy_retains_liability_until_exact_native_delivery(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        result = self.confirm(intent_id, 'mint', '500000', lease)
        self.assertEqual(result['obligation']['kind'], 'purchase_settlement')
        result = self.confirm(intent_id, 'buy', '33383761746', lease)
        self.assertEqual(result['obligation']['amount_raw'], '33383761746')
        self.assertEqual(result['obligation']['asset'], 'XRS')
        self.confirm(intent_id, 'unwrap', '33383761746', lease)
        original = self.prepare(intent_id, 'deliver', lease)
        with self.assertRaises(j.JournalError):
            self.db.record_outcome(intent_id, 'deliver', original, 'confirmed', HASH, '33383761745', lease=lease)
        result = self.db.record_outcome(intent_id, 'deliver', original, 'confirmed', HASH, '33383761746', lease=lease)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['obligation'])
        self.db.release_route(intent_id, lease=lease)
        self.assertIsNone(self.db.recovery_items()['active_route']['intent_id'])

    def test_sell_burn_binds_actual_verified_swap_output(self):
        intent_id = self.accepted('sell')
        lease = self.active(intent_id)
        with self.assertRaises(j.JournalError):
            self.db.claim_source(intent_id, j.XERIS, tx(20), HASH, '497145')
        self.confirm(intent_id, 'wrap', '33383761746', lease)
        original = self.prepare(intent_id, 'sell', lease)
        with self.assertRaises(j.JournalError):
            self.db.record_outcome(intent_id, 'sell', original, 'confirmed', HASH, '489999', lease=lease)
        self.db.record_outcome(intent_id, 'sell', original, 'confirmed', HASH, '497145', lease=lease)
        with self.assertRaises(j.JournalError):
            self.db.claim_source(intent_id, j.XERIS, tx(20), HASH, '490000')
        result = self.db.claim_source(intent_id, j.XERIS, tx(20), HASH, '497145')
        self.assertEqual(result['obligation']['kind'], 'unpaid_redemption')
        self.assertEqual(result['obligation']['amount_raw'], '497145')
        result = self.confirm(intent_id, 'payout', '497145', lease)
        self.assertEqual(result['status'], 'completed')

    def test_redeem_cannot_pay_before_burn_or_double_claim_burn(self):
        first = self.accepted('redeem')
        second = self.accepted('redeem', client_request_id='client-2')
        lease = self.active(first)
        with self.assertRaises(j.JournalError):
            self.prepare(first, 'payout', lease)
        self.db.claim_source(first, j.XERIS, tx(30), HASH, '500000')
        with self.assertRaises(j.JournalError):
            self.db.claim_source(second, j.XERIS, tx(30), HASH, '500000')
        self.assertEqual(self.confirm(first, 'payout', '500000', lease)['status'], 'completed')

    def test_prepared_original_survives_restart_before_submit(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        original = self.prepare(intent_id, 'mint', lease)
        self.db.close()
        self.db = j.Journal(self.path, clock=lambda: self.now)
        recovered = self.db.recovery_items()
        self.assertEqual(recovered['stages'][0]['transaction_id'], original)
        self.assertEqual(recovered['stages'][0]['state'], 'prepared')
        self.assertEqual(recovered['stages'][0]['signed_payload_digest'], HASH)
        self.assertEqual(recovered['obligations']['items'][0]['kind'], 'pending_issuance')
        with self.assertRaises(j.JournalError):
            self.db.prepare_stage(intent_id, 'mint', j.XERIS, tx(999), HASH, lease=lease)

    def test_pre_submit_commit_and_unknown_recovery_never_resend(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        original = self.prepare(intent_id, 'mint', lease)
        self.assertTrue(self.db.mark_submitted(intent_id, 'mint', original, lease=lease)['first_submission'])
        self.assertFalse(self.db.mark_submitted(intent_id, 'mint', original, lease=lease)['first_submission'])
        self.db.record_outcome(intent_id, 'mint', original, 'unknown', HASH, lease=lease)
        self.assertFalse(self.db.mark_submitted(intent_id, 'mint', original, lease=lease)['first_submission'])
        with self.assertRaises(j.JournalError):
            self.prepare(intent_id, 'buy', lease)
        with self.assertRaises(j.JournalError):
            self.db.mark_refund_due(intent_id, 'Unknown mint cannot refund', lease=lease)
        with self.assertRaises(j.JournalError):
            self.db.prepare_stage(intent_id, 'mint', j.XERIS, tx(999), HASH, lease=lease)
        self.assertEqual(self.db.status(intent_id)['status'], 'manual_review')
        self.db.pause('Observe only')
        # Confirmations may clear uncertainty while processing remains paused.
        result = self.db.record_outcome(intent_id, 'mint', original, 'confirmed', HASH, '500000', lease=lease)
        self.assertEqual(result['obligation']['kind'], 'purchase_settlement')

    def test_submission_without_original_or_changed_id_fails(self):
        intent_id = self.accepted()
        lease = self.active(intent_id)
        with self.assertRaises(j.JournalError):
            self.db.mark_submitted(intent_id, 'deposit', tx(4), lease=lease)
        original = self.prepare(intent_id, 'deposit', lease)
        with self.assertRaises(j.JournalError):
            self.db.mark_submitted(intent_id, 'deposit', tx(999), lease=lease)
        self.db.mark_submitted(intent_id, 'deposit', original, lease=lease)
        self.db.record_outcome(intent_id, 'deposit', original, 'unknown', HASH, lease=lease)
        with self.assertRaises(j.JournalError):
            self.db.record_outcome(intent_id, 'deposit', original, 'confirmed', HASH, '500000', lease=lease)
        self.db.claim_source(intent_id, j.SOLANA, original, HASH, '500000')
        self.assertEqual(self.db.status(intent_id)['stages'][0]['state'], 'confirmed')

    def test_fencing_and_expired_lease_only_recover_same_route(self):
        first = self.accepted()
        second = self.accepted(client_request_id='client-2')
        lease = self.active(first)
        with self.assertRaises(j.JournalError):
            self.db.acquire_route(first, 'worker-2')
        self.now += 31
        with self.assertRaises(j.JournalError):
            self.db.acquire_route(second, 'worker-2')
        renewed = self.db.acquire_route(first, 'worker-2')
        self.assertGreater(renewed['fence'], lease['fence'])
        with self.assertRaises(j.JournalError):
            self.prepare(first, 'deposit', lease)
        self.prepare(first, 'deposit', renewed)
        with self.assertRaises(j.JournalError):
            self.db.release_route(first, lease=renewed)

    def test_renewal_invalidates_old_lease_snapshot(self):
        intent_id = self.accepted()
        lease = self.active(intent_id)
        self.now += 1
        renewed = self.db.renew_route(intent_id, lease=lease)
        with self.assertRaises(j.JournalError):
            self.prepare(intent_id, 'deposit', lease)
        self.prepare(intent_id, 'deposit', renewed)

    def test_preissuance_refund_obligation_and_exact_refund(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        mint = self.prepare(intent_id, 'mint', lease)
        self.db.record_outcome(intent_id, 'mint', mint, 'failed', HASH, lease=lease)
        state = self.db.mark_refund_due(intent_id, 'Definitively failed issuance', lease=lease)
        self.assertEqual(state['obligation']['kind'], 'refund_due')
        refund = self.prepare(intent_id, 'refund', lease)
        with self.assertRaises(j.JournalError):
            self.db.record_outcome(intent_id, 'refund', refund, 'confirmed', HASH, '499999', lease=lease)
        state = self.db.record_outcome(intent_id, 'refund', refund, 'confirmed', HASH, '500000', lease=lease)
        self.assertEqual(state['status'], 'refunded')
        self.assertEqual(self.db.obligations()['items'], [])

    def test_issued_or_merely_prepared_mint_cannot_refund(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        mint = self.prepare(intent_id, 'mint', lease)
        with self.assertRaises(j.JournalError):
            self.db.mark_refund_due(intent_id, 'Not safe', lease=lease)
        self.db.record_outcome(intent_id, 'mint', mint, 'confirmed', HASH, '500000', lease=lease)
        with self.assertRaises(j.JournalError):
            self.db.mark_refund_due(intent_id, 'Not safe', lease=lease)

    def test_terminal_crash_before_release_can_recover_and_free_singleton(self):
        first = self.accepted('redeem')
        second = self.accepted(client_request_id='client-2')
        lease = self.active(first)
        self.db.claim_source(first, j.XERIS, tx(30), HASH, '500000')
        self.confirm(first, 'payout', '500000', lease)
        self.db.close()
        self.now += 31
        self.db = j.Journal(self.path, clock=lambda: self.now)
        with self.assertRaises(j.JournalError):
            self.db.acquire_route(second, 'recovery')
        recovered = self.db.acquire_route(first, 'recovery')
        self.db.release_route(first, lease=recovered)
        self.assertEqual(self.db.acquire_route(second, 'recovery')['intent_id'], second)

    def test_failed_refund_is_manual_review_and_retains_liability(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        self.db.mark_refund_due(intent_id, 'Before issuance', lease=lease)
        original = self.prepare(intent_id, 'refund', lease)
        result = self.db.record_outcome(intent_id, 'refund', original, 'failed', HASH, lease=lease)
        self.assertEqual(result['status'], 'manual_review')
        self.assertEqual(result['obligation']['kind'], 'refund_due')
        self.assertEqual(result['obligation']['amount_raw'], '500000')
        with self.assertRaises(j.JournalError):
            self.db.release_route(intent_id, lease=lease)

    def test_terminal_outcome_is_immutable(self):
        intent_id = self.accepted()
        self.deposit(intent_id)
        lease = self.active(intent_id)
        original = self.prepare(intent_id, 'mint', lease)
        self.db.record_outcome(intent_id, 'mint', original, 'failed', HASH, lease=lease)
        self.db.record_outcome(intent_id, 'mint', original, 'failed', HASH, lease=lease)
        with self.assertRaises(j.JournalError):
            self.db.record_outcome(intent_id, 'mint', original, 'confirmed', HASH, '500000', lease=lease)

    def test_event_failure_rolls_back_claim_stage_and_liability_atomically(self):
        intent_id = self.accepted()
        with patch.object(self.db, '_event', side_effect=RuntimeError('synthetic crash before commit')):
            with self.assertRaises(RuntimeError):
                self.deposit(intent_id)
        self.assertEqual(self.db.status(intent_id)['stages'], [])
        self.assertEqual(self.db.obligations()['items'], [])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM settlement_claims').fetchone()[0], 0)
        self.deposit(intent_id)
        self.assertEqual(len(self.db.obligations()['items']), 1)

    def test_two_threads_consuming_nonce_get_one_intent(self):
        intent = self.intent()
        def accept(_):
            db = j.Journal(self.path, clock=lambda: self.now)
            try:
                return db.accept_intent(intent, SIGNATURE)['id']
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(accept, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM settlement_intents').fetchone()[0], 1)

    def test_racing_claims_credit_only_one_intent(self):
        ids = [self.accepted(client_request_id=f'client-{n}') for n in (1, 2)]
        def claim(intent_id):
            db = j.Journal(self.path, clock=lambda: self.now)
            try:
                return db.claim_source(intent_id, j.SOLANA, tx(10), HASH, '500000')['id']
            except j.JournalError:
                return None
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(claim, ids))
        self.assertEqual(sum(v is not None for v in result), 1)
        self.assertEqual(self.db.obligations()['totals'][0]['amount_raw'], '500000')

    def test_unfunded_cancel_cannot_erase_late_verified_deposit(self):
        intent_id = self.accepted()
        lease = self.active(intent_id)
        self.assertEqual(self.db.cancel_unfunded(intent_id, 'Expired without observed funding', lease=lease)['status'], 'cancelled')
        self.db.release_route(intent_id, lease=lease)
        state = self.deposit(intent_id)
        self.assertIsNone(state.get('terminal'))
        self.assertEqual(state['route_mode'], 'refund')
        self.assertEqual(state['obligation']['kind'], 'refund_due')
        with self.assertRaises(j.JournalError):
            self.db.cancel_unfunded(intent_id, 'Cannot cancel real liability', lease=self.db.acquire_route(intent_id, 'worker-2'))

    def test_independent_obligation_totals_do_not_mix_assets(self):
        first = self.accepted()
        second = self.accepted('redeem', client_request_id='second')
        self.deposit(first)
        self.db.claim_source(second, j.XERIS, tx(11), HASH, '500000')
        lease = self.active(first)
        self.confirm(first, 'mint', '500000', lease)
        totals = self.db.obligations()['totals']
        self.assertEqual({(v['kind'], v['asset'], v['amount_raw']) for v in totals},
            {('purchase_settlement', 'xUSDC', '500000'), ('unpaid_redemption', 'USDC', '500000')})


class PublicVerifierTests(unittest.TestCase):
    def test_existing_public_proof_verifies_and_changed_message_fails(self):
        # This file contains only previously published public owner/message/signature.
        path = Path(__file__).parent / 'fixtures/public-proof.json'
        proof = json.loads(path.read_text())['proofs']['deposit']
        message = proof['signing_message_utf8'].encode()
        j.verify_ed25519(proof['intent']['source_owner'], proof['signature'], message)
        with self.assertRaises(j.JournalError):
            j.verify_ed25519(proof['intent']['source_owner'], proof['signature'], message + b'changed')


if __name__ == '__main__':
    unittest.main()
