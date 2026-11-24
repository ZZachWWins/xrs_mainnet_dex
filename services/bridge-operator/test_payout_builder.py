"""Offline builder conformance and adversarial independent-review tests.

The frozen vectors came from solana-payout/src/main.rs using its pinned SDKs,
with public synthetic owners/hashes only. These tests need no Rust executable,
keys, network or signatures. Independent review is solana_payout's wire decoder,
not the builder's metadata or instruction summary.
"""

import base64
import copy
import hashlib
import unittest
from unittest.mock import patch

import payout_builder as b
import solana_payout as p


OWNER = p.b58(bytes([8]) * 32)
BLOCKHASH = p.b58(bytes([18]) * 32)
MAX_U64 = (1 << 64) - 1

RUST_VECTORS = [
    {"owner": OWNER, "amount": "500000", "index": 8, "bytes": 796,
     "message": "bfd91a359a869715ecc793db40717126d837f0229fa6a555a2daaebfd8889c22",
     "template": "69f172ab3ff3a6c4e7a038b7575e569928b9c2adc7e087317b2b7f76b386bee7",
     "transaction": "H6mvYQfVG7qkGAaNG3HJ4AQ4vqjfft2xvqn2gjhub8ge",
     "proposal": "7HxGPiGT2B1d1QKJcKNR7N3kSBM26pzZCnqvBcDaPMro",
     "ata": "EGwn5qiqAfaRFfdcp2mfWdLKmWvL5nUBbtxiL72Y2PyV",
     "bumps": {"destination_ata": 255, "proposal": 254, "transaction": 255, "vault": 255}},
    {"owner": p.FUNDING, "amount": "1", "index": 1, "bytes": 764,
     "message": "5cf8a4a0e9cb7aa3efc707f43c1312eb3a0f58424ce65349435a522e9fc2f117",
     "template": "3de62da303cc0dbed859a64cfe1239afbeff6c0401739a89f2e7d0938502292a",
     "transaction": "6tYK96jet2L87XGdHtMFunscsAnZ21FUTzUGRYPs4Tcc",
     "proposal": "KioD8qmn4o1GRjhQmc2uDwqHCVYAAmG5bYvcoFvoUkx",
     "ata": "FuU2wx2q5c5TJCbMr9RXPJ6UdExvYGUkt9diVTxf9jaB",
     "bumps": {"destination_ata": 254, "proposal": 254, "transaction": 255, "vault": 255}},
    {"owner": p.b58(bytes([7]) * 32), "amount": str(MAX_U64), "index": MAX_U64, "bytes": 796,
     "message": "4526c7beab215ba3427b95cb44da9219b9a74351359b8c2c9d05e1db70d8b251",
     "template": "fa019dbc815d92292a5ec8e5c34a51dbca613c85ea1f954cd157c00eba359477",
     "transaction": "41rM54vY7t71PG6RfgyQKHsYKMsUjcwFMpwEN3sS3YE9",
     "proposal": "HUimr9dmn4XkkhvpZnU5bJSM3QR5Y2785ZvE4gixqwuM",
     "ata": "7EJSueeCjseYzghxU2XhcGEUn7RJDh43Z2dL6dvGy9mw",
     "bumps": {"destination_ata": 255, "proposal": 255, "transaction": 253, "vault": 255}},
]


def make(owner=OWNER, amount="500000", index=8, blockhash=BLOCKHASH):
    plan = b.build_payout(owner, amount, index, blockhash)
    intent = {"intent_id": "offline-builder", "destination_owner": owner,
              "amount_raw": amount, "transaction_index": index}
    return plan, intent


def account(owner, data, lamports=2039280):
    return {"owner": owner, "executable": False, "lamports": lamports,
            "data": [base64.b64encode(data).decode(), "base64"]}


def token(owner, amount):
    data = bytearray(165)
    data[:32], data[32:64] = p.pub(p.USDC), p.pub(owner)
    data[64:72], data[108] = amount.to_bytes(8, "little"), 1
    return account(p.TOKEN, data)


def multisig(index=7):
    data = (hashlib.sha256(b"account:Multisig").digest()[:8] + p.pub(p.CREATION_SEED)
            + bytes(32) + bytes([1, 0]) + bytes(4) + index.to_bytes(8, "little")
            + bytes(8) + bytes([1]) + p.pub(p.FUNDING) + bytes([255])
            + (1).to_bytes(4, "little") + p.pub(p.FUNDING) + bytes([7]))
    return account(p.PROGRAM, data)


def mock_preflight(plan, *, created=True, current_index=7):
    mint = bytearray(82)
    mint[44:46] = bytes([6, 1])
    before = [multisig(current_index), None, None, token(p.VAULT, 5_000_000),
              None if created else token(plan["destination_owner"], 700),
              account(p.TOKEN, mint), account(p.SYSTEM, b"", 100_000_000), None]
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "getGenesisHash":
            return p.sv.SOLANA_GENESIS
        if method == "isBlockhashValid":
            return {"value": True}
        if method == "getMultipleAccounts":
            return {"context": {"slot": 1000}, "value": before}
        if method == "getMinimumBalanceForRentExemption":
            return {285: 3_000_000, 166: 2_000_000, 165: 2_039_280}[params[0]]
        if method == "getFeeForMessage":
            return {"value": 5000}
        raise AssertionError("Unexpected RPC method: " + method)

    return rpc, calls, before


class PayoutBuilderTests(unittest.TestCase):
    def test_pinned_rust_vectors_and_independent_wire_decoder(self):
        for vector in RUST_VECTORS:
            with self.subTest(index=vector["index"], owner=vector["owner"]):
                plan, intent = make(vector["owner"], vector["amount"], vector["index"])
                review = p.review_plan(plan, intent)
                for field, expected in [("message_base64", "message"), ("message_template_base64", "template")]:
                    self.assertEqual(hashlib.sha256(base64.b64decode(plan[field])).hexdigest(), vector[expected])
                self.assertEqual(plan["transaction_pda"], vector["transaction"])
                self.assertEqual(plan["proposal_pda"], vector["proposal"])
                self.assertEqual(plan["destination_token_account"], vector["ata"])
                self.assertEqual(plan["bumps"], vector["bumps"])
                self.assertEqual(review["packet_bytes"], vector["bytes"])
                self.assertEqual((plan["transaction_space"], plan["proposal_space"]), (285, 166))
                self.assertEqual(review["required_signers"], [p.FUNDING])
                self.assertEqual(base64.b64decode(plan["unsigned_transaction_base64"])[:65], b"\1" + bytes(64))

    def test_exact_positive_u64_inputs_reject_lossy_coercions(self):
        for amount in (None, True, 1, 1.0, "", "0", "01", "-1", "+1", "1.0", "1e6", " 1", "1\n", "١", str(1 << 64), "1" * 1000):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                make(amount=amount)
        for index in (None, True, False, 0, -1, "8", 8.0, 1 << 64):
            with self.subTest(index=index), self.assertRaises(ValueError):
                make(index=index)
        plan, intent = make(amount="9007199254740993", index=9007199254740993)
        self.assertEqual(p.review_plan(plan, intent)["amount_raw"], "9007199254740993")

    def test_public_keys_and_blockhashes_must_be_canonical(self):
        for owner in (None, True, "", "0" * 32, " " + OWNER, "1" + OWNER, OWNER[:-1]):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                make(owner=owner)
        for blockhash in (None, True, "", "1" * 32, "0" * 32, "1" + BLOCKHASH, BLOCKHASH[:-1]):
            with self.subTest(blockhash=blockhash), self.assertRaises(ValueError):
                make(blockhash=blockhash)

    def test_reserved_recipients_including_index_specific_accounts_are_rejected(self):
        plan, _ = make()
        for owner in (p.SYSTEM, p.PROGRAM, p.TOKEN, p.USDC, p.ATA, p.MULTISIG,
                      p.VAULT, p.SOURCE, plan["transaction_pda"], plan["proposal_pda"]):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, "Reserved"):
                make(owner=owner)

    def test_intent_binding_rejects_recipient_amount_and_index_changes(self):
        for changes in ({"destination_owner": p.FUNDING}, {"amount_raw": "500001"},
                        {"transaction_index": 9}, {"transaction_index": True}):
            plan, intent = make()
            intent.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                p.review_plan(plan, intent)

    def test_independent_decoder_rejects_each_message_byte_tamper(self):
        plan, intent = make()
        review = p.review_plan(plan, intent)
        original = base64.b64decode(plan["message_base64"])
        offset = review["blockhash_offset"]
        # Blockhash replacement is intentionally separately preflight-validated.
        # Rebuild envelope/template consistently so failures exercise the decoder
        # rather than merely disagreeing duplicate representations.
        for position in range(len(original)):
            if offset <= position < offset + 32:
                continue
            changed = bytearray(original)
            changed[position] ^= 1
            template = changed.copy()
            template[offset:offset + 32] = bytes(32)
            mutated = copy.deepcopy(plan)
            mutated.update(message_base64=base64.b64encode(changed).decode(),
                           unsigned_transaction_base64=base64.b64encode(b"\1" + bytes(64) + changed).decode(),
                           message_template_base64=base64.b64encode(template).decode())
            with self.subTest(position=position), self.assertRaises(ValueError):
                p.review_plan(mutated, intent)

    def test_forged_metadata_signatures_and_oversize_envelope_are_rejected(self):
        for field, value in (("transaction_pda", OWNER), ("destination_authority", p.FUNDING),
                             ("decimals", 9), ("ata_space", True), ("transaction_space", 286),
                             ("transaction_bytes", 1), ("real_signatures", True),
                             ("private_keys_accessed", True), ("transactions_submitted", 1)):
            plan, intent = make()
            plan[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.review_plan(plan, intent)
        for suffix in (b"\0", bytes(1233)):
            plan, intent = make()
            wire = base64.b64decode(plan["unsigned_transaction_base64"]) + suffix
            plan["unsigned_transaction_base64"] = base64.b64encode(wire).decode()
            with self.assertRaises(ValueError):
                p.review_plan(plan, intent)
        plan, intent = make()
        wire = bytearray(base64.b64decode(plan["unsigned_transaction_base64"]))
        wire[1] = 1
        plan["unsigned_transaction_base64"] = base64.b64encode(wire).decode()
        with self.assertRaises(ValueError):
            p.review_plan(plan, intent)
        with patch.object(b, "MAX_PACKET_BYTES", 795), self.assertRaisesRegex(ValueError, "packet"):
            make()

    def test_new_and_existing_recipient_ata_preflight(self):
        for owner in (OWNER, p.FUNDING):
            for created in (True, False):
                with self.subTest(owner=owner, created=created):
                    plan, intent = make(owner=owner)
                    rpc, calls, _ = mock_preflight(plan, created=created)
                    checked = p.preflight(plan, intent, rpc, simulate=False)
                    self.assertEqual(checked["plan"]["destination_created"], created)
                    self.assertEqual(checked["plan"]["quoted_total_sol_debit_lamports"], 7_044_280 if created else 5_005_000)
                    self.assertFalse(checked["real_signing_authorized"])
                    self.assertNotIn("destination_created", plan)
                    self.assertEqual(plan["destination_token_account"], p.sv.associated_token_address(owner))
                    self.assertEqual(plan["instructions"][0]["data_base64"], "AQ==")
                    self.assertTrue(all(m in {"getGenesisHash", "isBlockhashValid", "getMultipleAccounts",
                                             "getMinimumBalanceForRentExemption", "getFeeForMessage"} for m, _ in calls))

    def test_stale_or_occupied_index_must_reconcile(self):
        for current in (6, 8):
            plan, intent = make()
            rpc, _, _ = mock_preflight(plan, current_index=current)
            with self.subTest(current=current), self.assertRaisesRegex(ValueError, "index changed"):
                p.preflight(plan, intent, rpc, simulate=False)
        for position in (1, 2):
            plan, intent = make()
            rpc, _, before = mock_preflight(plan)
            before[position] = account(p.PROGRAM, b"")
            with self.subTest(position=position), self.assertRaisesRegex(ValueError, "reconcile"):
                p.preflight(plan, intent, rpc, simulate=False)

    def test_preflight_rejects_wrong_ata_owner_and_insufficient_reserve(self):
        for account_index, replacement in ((4, token(p.FUNDING, 0)), (3, token(p.VAULT, 499999))):
            plan, intent = make()
            rpc, _, before = mock_preflight(plan, created=False)
            before[account_index] = replacement
            with self.subTest(account_index=account_index), self.assertRaises(ValueError):
                p.preflight(plan, intent, rpc, simulate=False)

    def test_only_blockhash_changes_template_stays_stable(self):
        first, intent = make()
        second, _ = make(blockhash=p.b58(bytes([19]) * 32))
        self.assertEqual(first["message_template_base64"], second["message_template_base64"])
        self.assertNotEqual(first["message_base64"], second["message_base64"])
        self.assertNotEqual(p.review_plan(first, intent)["message_sha256"], p.review_plan(second, intent)["message_sha256"])
        for kwargs in ({"amount": "500001"}, {"index": 9}, {"owner": p.FUNDING}):
            changed, _ = make(**kwargs)
            self.assertNotEqual(first["message_template_base64"], changed["message_template_base64"])

    def test_builder_is_deterministic_and_has_no_io_or_external_processes(self):
        # Imports are completed before these guards; all candidate preparation is
        # then executed with file/process/network entry points denied.
        with patch("builtins.open", side_effect=AssertionError("No files")), \
             patch("subprocess.Popen", side_effect=AssertionError("No process")), \
             patch("socket.socket", side_effect=AssertionError("No network")):
            first, _ = make()
            second, _ = make()
        self.assertEqual(first, second)
        first["instructions"][0]["accounts"][0]["public_key"] = OWNER
        self.assertEqual(make()[0], second)


if __name__ == "__main__":
    unittest.main()
