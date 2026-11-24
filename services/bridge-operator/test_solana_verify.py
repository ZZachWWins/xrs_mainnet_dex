"""Offline synthetic evidence only: no wallets, keys, sockets or submissions."""
import copy
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import solana_verify as sv

# Public address from the prior pilot, used as a public-only ATA golden vector.
OWNER = "BzyrcS63PR4kxt8WAbtCptsbN1czdSsyTt4AaaWJpKTA"
SOURCE = "FuU2wx2q5c5TJCbMr9RXPJ6UdExvYGUkt9diVTxf9jaB"
OTHER = sv.b58encode(bytes([8]) * 32)
SIGNATURE = sv.b58encode(bytes([7]) * 64)  # Synthetic locator, not a signed tx.
BLOCKHASH = sv.b58encode(bytes([9]) * 32)
NOW = 1_795_452_000
SLOT = 380_000_000


def fixture(amount=500_000, source_before=4_000_000, reserve_before=10_099_031):
    request = {"source_owner": OWNER, "source_token_account": SOURCE,
               "amount_raw": str(amount), "not_before": NOW - 90, "expires_at": NOW + 300,
               "min_slot": SLOT - 10}

    def token(index, raw, owner):
        return {"accountIndex": index, "mint": sv.USDC_MINT, "owner": owner,
                "programId": sv.TOKEN_PROGRAM,
                "uiTokenAmount": {"amount": str(raw), "decimals": 6,
                                  "uiAmount": None, "uiAmountString": "ignored"}}

    tx = {"version": "legacy", "slot": SLOT, "blockTime": NOW - 30,
          "transaction": {"signatures": [SIGNATURE], "message": {
              "accountKeys": [OWNER, SOURCE, sv.RESERVE_TOKEN_ACCOUNT, sv.USDC_MINT, sv.TOKEN_PROGRAM],
              "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0, "numReadonlyUnsignedAccounts": 2},
              "recentBlockhash": BLOCKHASH,
              "instructions": [{"programIdIndex": 4, "accounts": [1, 3, 2, 0],
                                "data": sv.b58encode(bytes([12]) + amount.to_bytes(8, "little") + bytes([6])),
                                "stackHeight": 1}],
          }},
          "meta": {"err": None, "status": {"Ok": None}, "fee": 5000,
                   "preBalances": [50_000_000, 2_039_280, 2_039_280, 1_461_600, 1],
                   "postBalances": [49_995_000, 2_039_280, 2_039_280, 1_461_600, 1],
                   "preTokenBalances": [token(1, source_before, OWNER), token(2, reserve_before, sv.RESERVE_OWNER)],
                   "postTokenBalances": [token(1, source_before - amount, OWNER), token(2, reserve_before + amount, sv.RESERVE_OWNER)],
                   "innerInstructions": [], "loadedAddresses": {"readonly": [], "writable": []},
                   "rewards": [], "logMessages": ["Not used as transaction evidence"]}}
    evidence = {"signature": SIGNATURE, "genesis_hash": sv.SOLANA_GENESIS, "transaction": tx,
                "signature_status": {"context": {"slot": SLOT + 40}, "value": [
                    {"slot": SLOT, "confirmationStatus": "finalized", "confirmations": None,
                     "err": None, "status": {"Ok": None}}]}}
    return request, evidence


def bound_fixture():
    request, evidence = fixture()
    request['intent_digest'] = 'ab' * 32
    transaction = evidence['transaction']
    message = transaction['transaction']['message']
    message['accountKeys'].append(sv.MEMO_PROGRAM)
    message['header']['numReadonlyUnsignedAccounts'] = 3
    message['instructions'].append({'programIdIndex': 5, 'accounts': [0],
        'data': sv.b58encode((sv.MEMO_PREFIX + request['intent_digest']).encode('ascii')), 'stackHeight': 1})
    transaction['meta']['preBalances'].append(1)
    transaction['meta']['postBalances'].append(1)
    return request, evidence


class VerifierTests(unittest.TestCase):
    def reject(self, request, evidence, code=None):
        with self.assertRaises(sv.VerificationError) as caught:
            sv.verify_deposit(request, evidence, now=NOW)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_exact_half_dollar_receipt_binds_public_evidence_not_settlement(self):
        request, evidence = fixture()
        original = copy.deepcopy((request, evidence))
        result = sv.verify_deposit(request, evidence, now=NOW)
        self.assertEqual(result["status"], "verified_finalized_deposit")
        self.assertEqual(result["amount_raw"], "500000")
        self.assertEqual(result["source_after_raw"], "3500000")
        self.assertEqual(result["reserve_after_raw"], "10599031")
        self.assertEqual(result["signature"], SIGNATURE)
        self.assertEqual(result["instruction_index"], 0)
        self.assertEqual(result["source_owner"], OWNER)
        self.assertEqual(result["reserve_owner"], sv.RESERVE_OWNER)
        self.assertEqual(result["commitment"], "finalized")
        self.assertFalse(result["settlement_authorized"])
        self.assertTrue(result["global_deduplication_required"])
        self.assertEqual(result["evidence_source"], "trusted_rpc_observation")
        self.assertEqual((request, evidence), original)

    def test_settlement_deposit_binds_exact_intent_digest_and_owner_memo(self):
        request, evidence = bound_fixture()
        result = sv.verify_deposit(request, evidence, now=NOW)
        self.assertEqual(result['intent_digest'], request['intent_digest'])
        self.assertEqual(result['memo_instruction_index'], 1)
        self.assertEqual(result['instruction_index'], 0)
        self.assertEqual(result['amount_raw'], '500000')

    def test_bound_request_never_falls_back_to_legacy_transfer(self):
        request, evidence = fixture()
        request['intent_digest'] = 'ab' * 32
        self.reject(request, evidence, 'noncanonical_transaction_accounts')
        request, evidence = bound_fixture()
        evidence['transaction']['transaction']['message']['instructions'].pop()
        self.reject(request, evidence, 'transfer_and_bound_memo_required')

    def test_legacy_request_cannot_accept_unbound_extra_memo(self):
        request, evidence = bound_fixture()
        del request['intent_digest']
        self.reject(request, evidence, 'noncanonical_transaction_accounts')

    def test_invalid_intent_digest_never_downgrades_verification(self):
        for value in (None, '', False, 123, 'AB' * 32, 'ab' * 31, 'ab' * 33, 'g' * 64):
            with self.subTest(value=value):
                request, evidence = bound_fixture()
                request['intent_digest'] = value
                self.reject(request, evidence, 'invalid_intent_digest')

    def test_memo_cannot_name_another_intent_or_add_content(self):
        for text in (sv.MEMO_PREFIX + 'cd' * 32, 'X' + sv.MEMO_PREFIX[1:] + 'ab' * 32,
                     sv.MEMO_PREFIX + 'ab' * 32 + '\n', sv.MEMO_PREFIX + 'ab' * 31):
            with self.subTest(text=text):
                request, evidence = bound_fixture()
                evidence['transaction']['transaction']['message']['instructions'][1]['data'] = sv.b58encode(text.encode())
                self.reject(request, evidence)

    def test_memo_requires_pinned_program_and_source_signer(self):
        changes = {'programIdIndex': [4, True, '5'], 'accounts': [[], [1], [False], [0, 0], '0'],
                   'stackHeight': [2, True, '1']}
        for field, values in changes.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    request, evidence = bound_fixture()
                    evidence['transaction']['transaction']['message']['instructions'][1][field] = value
                    self.reject(request, evidence)
        request, evidence = bound_fixture()
        evidence['transaction']['transaction']['message']['accountKeys'][5] = OTHER
        self.reject(request, evidence, 'noncanonical_transaction_accounts')

    def test_memo_does_not_relax_transfer_amount_or_effect_checks(self):
        request, evidence = bound_fixture()
        evidence['transaction']['meta']['postTokenBalances'][1]['uiTokenAmount']['amount'] = '10599032'
        self.reject(request, evidence, 'token_balance_delta_mismatch')
        request, evidence = bound_fixture()
        evidence['transaction']['meta']['postBalances'][5] += 1
        self.reject(request, evidence, 'unrelated_lamport_effects')

    def test_bound_instruction_order_and_count_are_exact(self):
        for edit in (lambda rows: list(reversed(rows)), lambda rows: rows + [rows[1]], lambda rows: [rows[1]]):
            request, evidence = bound_fixture()
            message = evidence['transaction']['transaction']['message']
            message['instructions'] = edit(message['instructions'])
            self.reject(request, evidence)
        request, evidence = bound_fixture()
        evidence['transaction']['transaction']['message']['header']['numReadonlyUnsignedAccounts'] = 2
        self.reject(request, evidence, 'wrong_transaction_signers_or_writability')

    def test_late_observation_still_requires_bound_in_window_memo_payment(self):
        request, evidence = bound_fixture()
        request['expires_at'] = NOW - 10
        result = sv.verify_deposit(request, evidence, now=NOW + 86400)
        self.assertEqual(result['intent_digest'], request['intent_digest'])

    def test_amounts_above_javascript_safe_integer_remain_exact(self):
        amount = 9_007_199_254_740_993
        request, evidence = fixture(amount, source_before=amount + 3, reserve_before=7)
        result = sv.verify_deposit(request, evidence, now=NOW)
        self.assertEqual(result["amount_raw"], "9007199254740993")
        self.assertEqual(result["source_after_raw"], "3")
        self.assertEqual(result["reserve_after_raw"], "9007199254741000")

    def test_canonical_usdc_ata_public_golden_vectors(self):
        self.assertEqual(sv.associated_token_address(OWNER), SOURCE)
        self.assertEqual(sv.associated_token_address(sv.RESERVE_OWNER), sv.RESERVE_TOKEN_ACCOUNT)
        # Official PDA example: bump255 is on-curve, bump254 is canonical.
        suffix = bytes(32) + b"ProgramDerivedAddress"
        self.assertTrue(sv._is_curve_point(sv.hashlib.sha256(b"helloWorld" + bytes([255]) + suffix).digest()))
        point = sv.hashlib.sha256(b"helloWorld" + bytes([254]) + suffix).digest()
        self.assertFalse(sv._is_curve_point(point))
        self.assertEqual(sv.b58encode(point), "46GZzzetjCURsdFPb7rcnspbEMnCBXe9kpjrsZAkKb6X")

    def test_bad_request_fields_fail_before_chain_evidence(self):
        bad = {
            "source_owner": [None, "1" * 32, "bad", OTHER],
            "source_token_account": [OTHER, sv.RESERVE_TOKEN_ACCOUNT, None],
            "amount_raw": [0, 500000, "0", "0500000", "1.0", "1e6", "-1", str(1 << 64), True],
            "not_before": [None, 0, True, float(NOW), "1795452000", NOW + 61],
            "expires_at": [False, "1", NOW - 91], "min_slot": [True, -1, "1"],
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    request, evidence = fixture()
                    request[field] = value
                    self.reject(request, evidence)

    def test_request_cannot_use_reserve_as_source(self):
        request, evidence = fixture()
        request.update(source_owner=sv.RESERVE_OWNER, source_token_account=sv.RESERVE_TOKEN_ACCOUNT)
        self.reject(request, evidence, "overlapping_accounts")

    def test_chain_signature_and_message_signature_are_bound(self):
        for change in [
            lambda e: e.update(genesis_hash="testnet"),
            lambda e: e.update(signature=sv.b58encode(bytes([8]) * 64)),
            lambda e: e.update(signature="1" * 64),
            lambda e: e["transaction"]["transaction"].update(signatures=[]),
            lambda e: e["transaction"]["transaction"].update(signatures=[SIGNATURE, SIGNATURE]),
        ]:
            request, evidence = fixture()
            change(evidence)
            self.reject(request, evidence)

    def test_finality_status_must_be_explicit_success_and_consistent_slot(self):
        for field, values in {
            "confirmationStatus": [None, "confirmed", "processed"], "confirmations": [0, 32, "null"],
            "err": [False, {}, {"InstructionError": [0, "InvalidAccountData"]}],
            "status": [{"Err": None}, {}, {"Ok": None, "Err": None}],
            "slot": [SLOT + 1, True, str(SLOT)],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    request, evidence = fixture()
                    evidence["signature_status"]["value"][0][field] = value
                    self.reject(request, evidence)
        for field in ["err", "confirmations", "confirmationStatus"]:
            request, evidence = fixture()
            del evidence["signature_status"]["value"][0][field]
            self.reject(request, evidence)

    def test_missing_or_misindexed_statuses_and_stale_context_fail(self):
        for statuses in [None, {}, {"value": []}, {"value": [None]}, {"value": [None, None]}]:
            request, evidence = fixture()
            evidence["signature_status"] = statuses
            self.reject(request, evidence)
        request, evidence = fixture()
        evidence["signature_status"]["context"]["slot"] = SLOT - 1
        self.reject(request, evidence, "status_context_before_transaction")

    def test_missing_old_future_or_after_expiry_timestamps_fail(self):
        for value, code in [(None, "missing_or_invalid_block_time"), (True, "missing_or_invalid_block_time"),
                            (NOW - 91, "transaction_before_request"), (NOW + 61, "transaction_time_in_future")]:
            request, evidence = fixture()
            evidence["transaction"]["blockTime"] = value
            self.reject(request, evidence, code)
        request, evidence = fixture()
        request["expires_at"] = NOW - 31
        self.reject(request, evidence, "transaction_after_request_expiry")
        request, evidence = fixture()
        request["min_slot"] = SLOT + 1
        self.reject(request, evidence, "transaction_before_request_slot")

    def test_late_observation_of_an_in_window_transfer_can_reconcile(self):
        request, evidence = fixture()
        request["expires_at"] = NOW - 10
        result = sv.verify_deposit(request, evidence, now=NOW + 86400)
        self.assertEqual(result["amount_raw"], "500000")

    def test_versioned_parsed_or_missing_receipts_fail(self):
        for value in [None, 0, "0", True, "future"]:
            request, evidence = fixture()
            evidence["transaction"]["version"] = value
            self.reject(request, evidence, "unsupported_transaction_version")
        request, evidence = fixture()
        evidence["transaction"] = None
        self.reject(request, evidence, "transaction_unavailable")
        request, evidence = fixture()
        evidence["transaction"]["transaction"]["message"]["accountKeys"][0] = {"pubkey": OWNER, "signer": True}
        self.reject(request, evidence, "noncanonical_transaction_accounts")
        request, evidence = fixture()
        evidence["transaction"]["transaction"]["message"]["unrecognizedFields"] = {"claim": "safe"}
        self.reject(request, evidence, "unsupported_message_encoding")

    def test_wrong_mint_reserve_source_program_and_account_order_fail(self):
        for index in range(5):
            request, evidence = fixture()
            evidence["transaction"]["transaction"]["message"]["accountKeys"][index] = OTHER
            self.reject(request, evidence, "noncanonical_transaction_accounts")
        request, evidence = fixture()
        keys = evidence["transaction"]["transaction"]["message"]["accountKeys"]
        keys[1], keys[2] = keys[2], keys[1]
        self.reject(request, evidence, "noncanonical_transaction_accounts")

    def test_delegate_multisig_wrong_signer_and_readonly_mismatch_fail(self):
        for field, value in [("numRequiredSignatures", 2), ("numRequiredSignatures", True),
                             ("numReadonlySignedAccounts", 1), ("numReadonlyUnsignedAccounts", 1)]:
            request, evidence = fixture()
            evidence["transaction"]["transaction"]["message"]["header"][field] = value
            self.reject(request, evidence, "wrong_transaction_signers_or_writability")

    def test_multiple_transfers_or_other_top_level_instructions_fail(self):
        for value in [[], [{"parsed": {"type": "transferChecked"}}], "instructions"]:
            request, evidence = fixture()
            evidence["transaction"]["transaction"]["message"]["instructions"] = value
            self.reject(request, evidence)
        request, evidence = fixture()
        instructions = evidence["transaction"]["transaction"]["message"]["instructions"]
        instructions.append(copy.deepcopy(instructions[0]))
        self.reject(request, evidence, "exactly_one_transfer_required")

    def test_instruction_opcode_exact_amount_decimals_and_size_are_checked(self):
        valid = bytes.fromhex("0c20a107000000000006")
        for data in [bytes([3]) + valid[1:], bytes([7]) + valid[1:], valid[:-1] + bytes([9]),
                     bytes([12]) + (500001).to_bytes(8, "little") + bytes([6]), valid + bytes([0]), valid[:-1]]:
            request, evidence = fixture()
            evidence["transaction"]["transaction"]["message"]["instructions"][0]["data"] = sv.b58encode(data)
            self.reject(request, evidence)

    def test_instruction_indexes_must_be_exact_integers_and_top_level(self):
        for field, values in {"programIdIndex": [True, 0, "4"], "accounts": [[True, 3, 2, 0], [1, 3, 2, 4], [1, 2, 3, 0]],
                              "stackHeight": [2, True, "1"]}.items():
            for value in values:
                request, evidence = fixture()
                evidence["transaction"]["transaction"]["message"]["instructions"][0][field] = value
                self.reject(request, evidence)

    def test_inner_instructions_lookup_accounts_and_unrelated_rewards_fail(self):
        for field, values in {"innerInstructions": [None, [{"index": 0, "instructions": []}], [{"index": 0, "instructions": [{"data": "abc"}]}]],
                              "loadedAddresses": [None, {"readonly": [OTHER], "writable": []}],
                              "rewards": [[{"lamports": 1}]], "returnData": [{"programId": sv.TOKEN_PROGRAM, "data": ["", "base64"]}]}.items():
            for value in values:
                request, evidence = fixture()
                evidence["transaction"]["meta"][field] = value
                self.reject(request, evidence)
        request, evidence = fixture()
        evidence["transaction"]["transaction"]["message"]["addressTableLookups"] = [{"accountKey": OTHER}]
        self.reject(request, evidence, "address_lookups_not_allowed")

    def test_failed_or_missing_meta_never_credits_even_with_correct_deltas(self):
        for meta in [None, {}, {"err": {"InstructionError": [0, 1]}}]:
            request, evidence = fixture()
            evidence["transaction"]["meta"] = meta
            self.reject(request, evidence, "transaction_not_successful")
        request, evidence = fixture()
        evidence["transaction"]["meta"]["status"] = {"Err": None}
        self.reject(request, evidence, "transaction_not_successful")

    def test_missing_duplicate_extra_or_invalid_balance_rows_fail(self):
        for row_key in ["preTokenBalances", "postTokenBalances"]:
            for edit in [lambda rows: [], lambda rows: None, lambda rows: rows[:1],
                         lambda rows: [rows[0], rows[0]], lambda rows: rows + [rows[0]]]:
                request, evidence = fixture()
                rows = evidence["transaction"]["meta"][row_key]
                evidence["transaction"]["meta"][row_key] = edit(rows)
                self.reject(request, evidence)

    def test_both_source_and_reserve_pre_and_post_authorities_mints_programs_required(self):
        for row_key in ["preTokenBalances", "postTokenBalances"]:
            for index in [0, 1]:
                for field in ["owner", "mint", "programId", "accountIndex"]:
                    request, evidence = fixture()
                    evidence["transaction"]["meta"][row_key][index][field] = OTHER
                    self.reject(request, evidence)

    def test_token_balance_amounts_are_raw_decimal_strings_not_ui_floats(self):
        for value in [500000, 500000.0, True, "0500000", "5e5", "0.5", "-1", str(1 << 64)]:
            request, evidence = fixture()
            evidence["transaction"]["meta"]["postTokenBalances"][1]["uiTokenAmount"]["amount"] = value
            self.reject(request, evidence)
        request, evidence = fixture()
        evidence["transaction"]["meta"]["postTokenBalances"][1]["uiTokenAmount"]["decimals"] = True
        self.reject(request, evidence, "wrong_token_decimals")

    def test_each_exact_debit_and_credit_required_and_cannot_use_balance_only(self):
        for row_key in ["preTokenBalances", "postTokenBalances"]:
            for index in [0, 1]:
                request, evidence = fixture()
                row = evidence["transaction"]["meta"][row_key][index]["uiTokenAmount"]
                row["amount"] = str(int(row["amount"]) + 1)
                self.reject(request, evidence, "token_balance_delta_mismatch")
        request, evidence = fixture()
        evidence["transaction"]["transaction"]["message"]["instructions"][0]["data"] = sv.b58encode(bytes([7]) + (500000).to_bytes(8, "little") + bytes([6]))
        self.reject(request, evidence, "wrong_transfer_type_amount_or_decimals")

    def test_token_balance_ranges_cannot_overflow_or_underflow(self):
        request, evidence = fixture()
        evidence["transaction"]["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "1"
        self.reject(request, evidence, "invalid_transfer_balance_range")
        request, evidence = fixture()
        evidence["transaction"]["meta"]["preTokenBalances"][1]["uiTokenAmount"]["amount"] = str(sv.MAX_U64)
        self.reject(request, evidence, "invalid_transfer_balance_range")

    def test_only_fee_lamports_may_move(self):
        for index in range(5):
            request, evidence = fixture()
            evidence["transaction"]["meta"]["postBalances"][index] += 1
            self.reject(request, evidence, "unrelated_lamport_effects")
        for value in [None, 0, True, 5000.0, "5000"]:
            request, evidence = fixture()
            evidence["transaction"]["meta"]["fee"] = value
            self.reject(request, evidence, "invalid_transaction_fee")

    def test_absent_optional_legacy_metadata_and_shuffled_token_rows_are_supported(self):
        request, evidence = fixture()
        meta = evidence["transaction"]["meta"]
        for key in ["status", "loadedAddresses", "rewards", "logMessages"]:
            del meta[key]
        del evidence["signature_status"]["value"][0]["status"]
        del evidence["transaction"]["transaction"]["message"]["instructions"][0]["stackHeight"]
        meta["preTokenBalances"].reverse()
        meta["postTokenBalances"].reverse()
        self.assertEqual(sv.verify_deposit(request, evidence, now=NOW)["amount_raw"], "500000")


class FakeResponse:
    def __init__(self, data, *, status=200, headers=None):
        self.data = io.BytesIO(data)
        self.status = status
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(data)), **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        return self.data.read(size)


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ReaderTests(unittest.TestCase):
    def client(self, data=None, **response_options):
        response = FakeResponse(data or b'{"jsonrpc":"2.0","id":1,"result":"ok"}', **response_options)
        client = sv.ReadOnlySolanaClient()
        client.opener = FakeOpener(response)
        return client

    def test_json_preserves_all_integer_precision_and_decimal_ui_amounts(self):
        parsed = sv.checked_json('{"raw":18446744073709551615,"ui":0.000001,"scientific":1e-6}')
        self.assertEqual(parsed["raw"], sv.MAX_U64)
        self.assertEqual(parsed["ui"], Decimal("0.000001"))
        self.assertEqual(parsed["scientific"], Decimal("0.000001"))
        for text in ['{"x":1,"x":2}', '{"x":{"y":1,"y":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":01}', '{', '{"x":1e99999999999999999999999999}']:
            with self.subTest(text=text), self.assertRaises(sv.VerificationError):
                sv.checked_json(text)

    def test_proxy_discovery_disabled_and_redirects_refused(self):
        with patch.object(sv.urllib.request, "build_opener") as build:
            sv.ReadOnlySolanaClient()
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], sv.NoRedirect)
        with self.assertRaisesRegex(sv.VerificationError, "rpc_redirect_refused"):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")

    def test_endpoint_rejects_credentials_non_https_query_and_unbounded_timeouts(self):
        for url in ["http://example.com", "https://user:secret@example.com", "https://@example.com", "https://example.com?api-key=secret",
                    "https://example.com#fragment", "https://example.com:8443", "file:///private/file", "https://example.com\n", "https://example.com:bad", None]:
            with self.subTest(url=url), self.assertRaises(sv.VerificationError):
                sv.ReadOnlySolanaClient(url)
        for timeout in [0, -1, 31, True, "10", float("inf"), float("nan")]:
            with self.subTest(timeout=timeout), self.assertRaises(sv.VerificationError):
                sv.ReadOnlySolanaClient(timeout=timeout)

    def test_methods_and_parameter_shapes_are_allowlisted_before_io(self):
        client = self.client()
        cases = [("sendTransaction", []), ("simulateTransaction", []), ("getAccountInfo", []), ([], []), (None, []),
                 ("getGenesisHash", ["ignored"]), ("getTransaction", [SIGNATURE, {"encoding": "jsonParsed", "commitment": "finalized"}]),
                 ("getTransaction", [SIGNATURE, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}]),
                 ("getTransaction", [SIGNATURE, {"encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": False}]),
                 ("getSignatureStatuses", [[SIGNATURE, SIGNATURE], {"searchTransactionHistory": True}]),
                 ("getSignatureStatuses", [[SIGNATURE], {"searchTransactionHistory": 1}])]
        for method, params in cases:
            with self.subTest(method=method, params=params), self.assertRaises(sv.VerificationError):
                client.read(method, params)
        self.assertEqual(client.opener.calls, [])

    def test_transport_is_exact_readonly_post_without_auth_and_finite_timeout(self):
        client = self.client()
        self.assertEqual(client.read("getGenesisHash", []), "ok")
        request, timeout = client.opener.calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []})
        self.assertEqual(request.get_header("Cache-control"), "no-store")
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 10)

    def test_rpc_errors_and_bad_envelopes_never_return_asserted_result(self):
        for data in [b'[]', b'{"jsonrpc":"2.0","id":true,"result":1}', b'{"jsonrpc":"2.0","id":2,"result":1}',
                     b'{"jsonrpc":"2.0","id":1,"error":{"message":"secret"},"result":1}',
                     b'{"id":1,"result":1}', b'{"jsonrpc":"2.0","id":1}']:
            with self.subTest(data=data), self.assertRaisesRegex(sv.VerificationError, "invalid_rpc_envelope"):
                self.client(data).read("getGenesisHash", [])

    def test_http_encoding_status_content_size_and_truncation_fail_closed(self):
        for kwargs in [{"status": 429}, {"status": 503}, {"headers": {"Content-Type": "text/html"}},
                       {"headers": {"Content-Encoding": "gzip"}}, {"headers": {"Content-Length": "1000001"}},
                       {"headers": {"Content-Length": "1"}}, {"headers": {"Content-Length": "no"}},
                       {"headers": {"Content-Length": "9" * 5000}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(sv.VerificationError):
                self.client(**kwargs).read("getGenesisHash", [])
        client = self.client(b" " * (sv.MAX_BYTES + 1))
        del client.opener.response.headers["Content-Length"]
        with self.assertRaisesRegex(sv.VerificationError, "rpc_response_too_large"):
            client.read("getGenesisHash", [])

    def test_network_failures_and_elapsed_reads_have_sanitized_reasons(self):
        for error in [TimeoutError("private endpoint details"), urllib.error.URLError("secret URL")]:
            client = sv.ReadOnlySolanaClient()
            client.opener = FakeOpener(error)
            with self.assertRaisesRegex(sv.VerificationError, "^rpc_unavailable$"):
                client.read("getGenesisHash", [])
        client = self.client()
        with patch.object(sv.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(sv.VerificationError, "rpc_timeout"):
                client.read("getGenesisHash", [])

    def test_reader_requests_finalized_receipt_and_exact_status_locator(self):
        request, evidence = fixture()
        calls = []

        class PublicReader:
            def read(self, method, params):
                calls.append((method, params))
                return {"getGenesisHash": sv.SOLANA_GENESIS, "getTransaction": evidence["transaction"],
                        "getSignatureStatuses": evidence["signature_status"]}[method]

        result = sv.read_and_verify(PublicReader(), SIGNATURE, request, now=NOW)
        self.assertEqual(result["amount_raw"], "500000")
        self.assertEqual(calls, [("getGenesisHash", []), ("getTransaction", [SIGNATURE, {
            "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0}]),
            ("getSignatureStatuses", [[SIGNATURE], {"searchTransactionHistory": True}])])

    def test_wrong_network_and_invalid_request_stop_before_other_reads(self):
        request, _ = fixture()
        calls = []

        class WrongNetwork:
            def read(self, method, params):
                calls.append(method)
                return "wrong-genesis"

        with self.assertRaisesRegex(sv.VerificationError, "wrong_solana_genesis"):
            sv.read_and_verify(WrongNetwork(), SIGNATURE, request, now=NOW)
        self.assertEqual(calls, ["getGenesisHash"])
        calls.clear()
        request["amount_raw"] = "invalid"
        with self.assertRaises(sv.VerificationError):
            sv.read_and_verify(WrongNetwork(), SIGNATURE, request, now=NOW)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
