"""Independent real-policy/journal regression tests for signer recovery.

Only ownership-proof verification and the local signing subprocess are mocked.
Signer policy files are real 0600 files pointing at inert text placeholders,
never private keys. RPC preflight is supplied synthetic public account evidence.
There is no network transport, real signing process, or funded transaction.
"""

import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import journal as j
import payout_builder
import runtime_adapters as adapters
from runtime_store import RuntimeStore
import solana_payout as payout
import xeris_purchase as purchase
from test_payout_builder import mock_preflight


OWNER = payout.b58(bytes([6]) * 32)
DESTINATION = payout.b58(bytes([7]) * 32)
OPERATOR = payout.b58(bytes([10]) * 32)
AUTH_SIGNATURE = payout.b58(bytes([11]) * 64)
DEPOSIT_SIGNATURE = payout.b58(bytes([12]) * 64)
AMOUNT = "500000"


class SignerRecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.database = self.directory / "review.sqlite3"
        self.now = 1_800_000_000
        self.policy_file = self.directory / "review-policy.json"
        self.policy = {
            "schema_version": 1, "policy_id": "settlement-v2-recovery-review",
            "enabled": True, "valid_from": self.now - 1, "expires_at": self.now + 1800,
            "maximum_usdc_raw": AMOUNT, "maximum_total_usdc_raw": AMOUNT,
            "maximum_requests": 2,
            "maximum_sol_debit_lamports": "10000000", "operator_wallet": OPERATOR, "keys": {},
        }
        for role, public in (("treasury", purchase.TREASURY), ("operator", OPERATOR),
                             ("solana", payout.FUNDING)):
            path = self.directory / (role + "-inert-placeholder.txt")
            path.write_text("Public test placeholder. Contains no key material.")
            path.chmod(0o600)
            self.policy["keys"][role] = {"public_key": public, "path": str(path)}
        self.save_policy()
        self.reopen()
        self.journal.resume("Isolated recovery regression")
        self.intent = self.accept_intent("first")
        self.intent_id = self.intent["intent_id"]
        self.lease = self.journal.acquire_route(self.intent_id, "review-worker")

    def reopen(self):
        self.journal = j.Journal(self.database, clock=lambda: self.now)
        self.store = RuntimeStore(self.journal)
        self.scope = adapters.ScopedPolicy(self.policy_file, lambda: self.now)
        self.signer = adapters.LocalScopedSigner(
            self.journal, self.scope, node=sys.executable,
            key_identity_check=lambda entry: True,
        )

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def save_policy(self):
        self.policy_file.write_text(json.dumps(self.policy))
        self.policy_file.chmod(0o600)

    def accept_intent(self, label, amount=AMOUNT):
        intent = self.journal.issue_challenge(
            action="buy", source_owner=OWNER, destination_owner=DESTINATION,
            solana_token_account=payout.sv.associated_token_address(OWNER),
            amount_raw=amount, min_output_raw="1", quote_id="quote-" + label,
            client_request_id="client-" + label,
        )["intent"]
        with patch.object(j, "verify_ed25519"):
            accepted = self.journal.accept_intent(intent, AUTH_SIGNATURE)
        return {**intent, "intent_id": accepted["id"]}

    def fake_helper(self, command, **kwargs):
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]).name, "local_key_process.mjs")
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin"})
        request = json.loads(kwargs["input"])
        self.assertEqual(set(request), {"path", "public_key", "message_base64"})
        self.assertIn(request["path"], {entry["path"] for entry in self.policy["keys"].values()})
        message = base64.b64decode(request["message_base64"], validate=True)
        # An opaque synthetic signature suffices at this adapter boundary;
        # runtime's independent Ed25519 verification is outside these tests.
        signature = hashlib.sha512(message).digest()
        encoded = base64.b64encode(b"\1" + signature + message).decode()
        return subprocess.CompletedProcess(command, 0, json.dumps({"signed_transaction_base64": encoded}).encode(), b"")

    def fund_and_freeze_mint(self):
        self.signer.reserve_intent(self.intent, self.lease)
        self.journal.claim_source(self.intent_id, j.SOLANA, DEPOSIT_SIGNATURE, "ab" * 32, AMOUNT)
        plan = purchase.build_stage("mint", OPERATOR, DESTINATION, bytes([18]) * 32,
                                    int(AMOUNT), route_id=self.intent_id)
        self.store.put(self.intent_id, "mint", {
            "prepared": plan, "context": {}, "expires_at": self.now + 60, "fee_raw": "1000000",
        }, self.lease)
        return plan

    def sign_and_record_mint(self, state="submitted"):
        plan = self.fund_and_freeze_mint()
        with patch.object(subprocess, "run", side_effect=self.fake_helper) as helper:
            encoded = self.signer.sign(self.intent, "mint", plan, self.lease)
            self.assertEqual(helper.call_count, 1)
        wire = base64.b64decode(encoded)
        signature = payout.b58(wire[1:65])
        self.journal.prepare_stage(self.intent_id, "mint", j.XERIS, signature,
            hashlib.sha256(wire).hexdigest(), lease=self.lease, signed_payload_base64=encoded)
        if state != "prepared":
            self.journal.mark_submitted(self.intent_id, "mint", signature, lease=self.lease)
        if state in ("unknown", "failed", "confirmed"):
            self.journal.record_outcome(self.intent_id, "mint", signature, state, "cd" * 32,
                AMOUNT if state == "confirmed" else None, lease=self.lease)
        return plan, signature, encoded

    def refund_plan(self):
        unsigned = payout_builder.build_payout(OWNER, AMOUNT, 8, payout.b58(bytes([18]) * 32))
        rpc, calls, _ = mock_preflight(unsigned)
        plan = payout.preflight(unsigned, {
            "intent_id": self.intent_id, "amount_raw": AMOUNT,
            "destination_owner": OWNER, "transaction_index": 8,
        }, rpc, simulate=False)["plan"]
        self.assertTrue(calls)
        return plan

    def freeze_refund(self):
        self.journal.mark_refund_due(self.intent_id, "Independently proved original mint failure", lease=self.lease)
        plan = self.refund_plan()
        self.store.put(self.intent_id, "refund", {
            "prepared": plan, "context": {}, "expires_at": self.now + 60,
            "fee_raw": str(plan["quoted_total_sol_debit_lamports"]),
        }, self.lease)
        return plan

    def test_failed_mint_refund_fits_exact_one_intent_budget_and_preserves_original(self):
        _, mint_signature, mint_encoded = self.sign_and_record_mint("failed")
        self.assertEqual(adapters.charged_amount(self.journal, intent_id=self.intent_id), 0)
        self.assertEqual(self.signer.readiness()["remaining_total_usdc_raw"], "0")
        plan = self.freeze_refund()
        with patch.object(subprocess, "run", side_effect=self.fake_helper) as helper:
            refunded = self.signer.sign(self.intent, "refund", plan, self.lease)
            self.assertEqual(self.signer.sign(self.intent, "refund", plan, self.lease), refunded)
            self.assertEqual(helper.call_count, 1)
        self.assertEqual(adapters.charged_amount(self.journal, policy_id=self.policy["policy_id"]), int(AMOUNT))
        records = {row["stage"]: dict(row) for row in self.journal.db.execute(
            "SELECT * FROM settlement_signer_records WHERE intent_id=?", (self.intent_id,))}
        self.assertEqual(set(records), {"mint", "refund"})
        self.assertEqual(records["mint"]["signed_base64"], mint_encoded)
        self.assertEqual(records["mint"]["charge_raw"], AMOUNT)
        self.assertEqual(self.journal._stage(self.intent_id, "mint")["transaction_id"], mint_signature)
        self.assertEqual(self.journal._stage(self.intent_id, "mint")["state"], "failed")
        # Both signed public originals and the reservation survive process restart.
        self.journal.close()
        self.reopen()
        self.lease = self.journal.acquire_route(self.intent_id, "review-worker")
        with patch.object(subprocess, "run", side_effect=AssertionError("Must reuse original")):
            self.assertEqual(self.signer.sign(self.intent, "refund", plan, self.lease), refunded)
        self.assertEqual(adapters.charged_amount(self.journal, intent_id=self.intent_id), int(AMOUNT))

    def test_unknown_mint_keeps_full_charge_and_blocks_refund_without_helper(self):
        _, signature, _ = self.sign_and_record_mint("unknown")
        self.assertEqual(adapters.charged_amount(self.journal, intent_id=self.intent_id), int(AMOUNT))
        with patch.object(subprocess, "run") as helper:
            with self.assertRaisesRegex(ValueError, "unresolved mint"):
                self.journal.mark_refund_due(self.intent_id, "Unknown is not failure", lease=self.lease)
            with self.assertRaises(ValueError):
                self.signer.sign(self.intent, "refund", self.refund_plan(), self.lease)
            helper.assert_not_called()
        # A failure for a different transaction cannot release the charge.
        with self.assertRaisesRegex(ValueError, "original"):
            self.journal.record_outcome(self.intent_id, "mint", payout.b58(bytes([23]) * 64),
                "failed", "ef" * 32, lease=self.lease)
        self.assertEqual(adapters.charged_amount(self.journal, intent_id=self.intent_id), int(AMOUNT))
        self.journal.record_outcome(self.intent_id, "mint", signature, "failed", "ef" * 32, lease=self.lease)
        plan = self.freeze_refund()
        with patch.object(subprocess, "run", side_effect=self.fake_helper) as helper:
            self.signer.sign(self.intent, "refund", plan, self.lease)
            self.assertEqual(helper.call_count, 1)

    def test_prepared_submitted_and_confirmed_mint_do_not_release_charge(self):
        # Subtests each get a fresh independent policy and journal.
        for state in ("prepared", "submitted", "confirmed"):
            with self.subTest(state=state):
                case = SignerRecoveryReviewTests("runTest")
                case.setUp()
                try:
                    case.sign_and_record_mint(state)
                    self.assertEqual(adapters.charged_amount(case.journal, intent_id=case.intent_id), int(AMOUNT))
                    with self.assertRaisesRegex(ValueError, "unresolved mint"):
                        case.journal.mark_refund_due(case.intent_id, "Cannot compensate this state", lease=case.lease)
                finally:
                    case.tearDown()

    def test_reservation_is_idempotent_bound_and_durable(self):
        with patch.object(subprocess, "run") as helper:
            self.signer.reserve_intent(self.intent, self.lease)
            self.signer.reserve_intent(copy.deepcopy(self.intent), self.lease)
            helper.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_reservations").fetchone()[0], 1)
        self.assertEqual(self.signer.readiness()["remaining_total_usdc_raw"], "0")
        self.assertEqual(self.signer.readiness()["remaining_requests"], 1)
        changed = {**self.intent, "destination_owner": OWNER}
        self.assertFalse(self.signer.has_reservation(changed))
        with self.assertRaisesRegex(ValueError, "reservation intent"):
            self.signer.reserve_intent(changed, self.lease)
        self.journal.close()
        self.reopen()
        self.lease = self.journal.acquire_route(self.intent_id, "review-worker")
        self.signer.reserve_intent(self.intent, self.lease)
        self.assertTrue(self.signer.has_reservation(self.intent))
        self.assertEqual(self.signer.readiness()["remaining_total_usdc_raw"], "0")
        self.assertEqual(self.signer.readiness()["remaining_requests"], 1)

    def test_two_dust_reservations_exhaust_count_despite_unused_usdc_after_cancel_and_restart(self):
        self.journal.cancel_unfunded(self.intent_id, "Discard unreserved test setup", lease=self.lease)
        self.journal.release_route(self.intent_id, lease=self.lease)
        with patch.object(subprocess, "run") as helper:
            for number in (1, 2):
                intent = self.accept_intent("dust-" + str(number), "1")
                lease = self.journal.acquire_route(intent["intent_id"], "review-worker")
                self.signer.reserve_intent(intent, lease)
                self.signer.reserve_intent(copy.deepcopy(intent), lease)
                self.assertEqual(self.signer.readiness()["remaining_requests"], 2-number)
                self.journal.cancel_unfunded(intent["intent_id"], "Dust reservation remains consumed", lease=lease)
                self.journal.release_route(intent["intent_id"], lease=lease)
            self.assertEqual(self.signer.readiness()["remaining_total_usdc_raw"], "499998")
            third = self.accept_intent("dust-three", "1")
            lease = self.journal.acquire_route(third["intent_id"], "review-worker")
            with self.assertRaisesRegex(ValueError, "request count exhausted"):
                self.signer.reserve_intent(third, lease)
            self.journal.close()
            self.reopen()
            lease = self.journal.acquire_route(third["intent_id"], "review-worker")
            with self.assertRaisesRegex(ValueError, "request count exhausted"):
                self.signer.reserve_intent(third, lease)
            helper.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_reservations").fetchone()[0], 2)
        self.assertEqual(self.signer.readiness()["remaining_requests"], 0)
        self.assertFalse(self.signer.has_reservation(third))

    def test_lowered_request_count_does_not_strand_a_reserved_active_intent(self):
        self.policy["maximum_total_usdc_raw"] = "1000000"
        self.save_policy()
        self.signer.reserve_intent(self.intent, self.lease)
        self.journal.cancel_unfunded(self.intent_id, "Prior request retains its slot", lease=self.lease)
        self.journal.release_route(self.intent_id, lease=self.lease)
        self.intent = self.accept_intent("active-second")
        self.intent_id = self.intent["intent_id"]
        self.lease = self.journal.acquire_route(self.intent_id, "review-worker")
        plan = self.fund_and_freeze_mint()
        self.policy["maximum_requests"] = 1
        self.save_policy()
        self.assertEqual(self.signer.readiness()["remaining_requests"], 0)
        self.assertTrue(self.signer.has_reservation(self.intent))
        self.signer.reserve_intent(self.intent, self.lease)
        with patch.object(subprocess, "run", side_effect=self.fake_helper) as helper:
            self.assertTrue(self.signer.sign(self.intent, "mint", plan, self.lease))
            helper.assert_called_once()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_reservations").fetchone()[0], 2)

    def test_exhausted_reservation_prevents_another_request_even_after_unfunded_cancel(self):
        self.signer.reserve_intent(self.intent, self.lease)
        self.journal.cancel_unfunded(self.intent_id, "No source transaction was supplied", lease=self.lease)
        self.journal.release_route(self.intent_id, lease=self.lease)
        second = self.accept_intent("second", "1")
        lease = self.journal.acquire_route(second["intent_id"], "review-worker")
        with patch.object(subprocess, "run") as helper:
            with self.assertRaisesRegex(ValueError, "reservation budget exhausted"):
                self.signer.reserve_intent(second, lease)
            helper.assert_not_called()
        self.assertFalse(self.signer.has_reservation(second))
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_reservations").fetchone()[0], 1)

    def test_lowered_request_or_total_limit_and_policy_rotation_block_existing_reservation(self):
        plan = self.fund_and_freeze_mint()
        original = copy.deepcopy(self.policy)
        changes = ({"maximum_usdc_raw": "499999"},
                   {"maximum_usdc_raw": "499999", "maximum_total_usdc_raw": "499999"},
                   {"policy_id": "settlement-v2-other-scope"})
        with patch.object(subprocess, "run") as helper:
            for change in changes:
                with self.subTest(change=change):
                    self.policy = {**copy.deepcopy(original), **change}
                    self.save_policy()
                    self.assertFalse(self.signer.has_reservation(self.intent))
                    with self.assertRaises(ValueError):
                        self.signer.reserve_intent(self.intent, self.lease)
                    with self.assertRaisesRegex(ValueError, "reserved settlement budget"):
                        self.signer.sign(self.intent, "mint", plan, self.lease)
            helper.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_records").fetchone()[0], 0)

    def test_new_reservation_requires_runway_and_current_fence(self):
        self.policy["expires_at"] = self.now + 599
        self.save_policy()
        with self.assertRaisesRegex(ValueError, "runway"):
            self.signer.reserve_intent(self.intent, self.lease)
        self.policy["expires_at"] = self.now + 1800
        self.save_policy()
        with self.assertRaisesRegex(ValueError, "fencing"):
            self.signer.reserve_intent(self.intent, {**self.lease, "fence": 999})
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM settlement_signer_reservations").fetchone()[0], 0)

    def test_lowered_aggregate_alone_blocks_when_individual_requests_still_fit(self):
        self.policy["maximum_total_usdc_raw"] = "1000000"
        self.save_policy()
        self.signer.reserve_intent(self.intent, self.lease)
        self.journal.cancel_unfunded(self.intent_id, "Unfunded reservation remains accounted", lease=self.lease)
        self.journal.release_route(self.intent_id, lease=self.lease)
        self.intent = self.accept_intent("second-reservation")
        self.intent_id = self.intent["intent_id"]
        self.lease = self.journal.acquire_route(self.intent_id, "review-worker")
        plan = self.fund_and_freeze_mint()
        self.assertTrue(self.signer.has_reservation(self.intent))
        self.assertEqual(self.signer.readiness()["remaining_total_usdc_raw"], "0")
        self.policy["maximum_total_usdc_raw"] = "750000"
        self.save_policy()
        self.assertEqual(self.scope.read()["maximum_usdc_raw"], AMOUNT)
        self.assertFalse(self.signer.has_reservation(self.intent))
        with patch.object(subprocess, "run") as helper:
            with self.assertRaisesRegex(ValueError, "reserved settlement budget"):
                self.signer.sign(self.intent, "mint", plan, self.lease)
            helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
