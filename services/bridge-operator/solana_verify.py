"""Read-only, fail-closed verification of the narrow mainnet USDC deposit format.

No keys, signing, transaction submission, service deployment or settlement code.
``verify_deposit`` accepts independently collected trusted-RPC evidence, NEVER a
browser's asserted receipt. The caller authenticates the request's source owner,
binds the Xeris destination/quote, and atomically deduplicates signatures globally.
This module cannot authorize issuance or delivery by itself.

Accepted format: one legacy transaction, owner as sole fee-paying signer, one
classic SPL TransferChecked at index 0, canonical owner USDC ATA to pinned reserve
ATA. Public settlement requests bind a second signed Memo to the immutable intent
digest. The old one-instruction format is allowed only when intent_digest is absent
for historical pilot verification. No CPI or unrelated instructions are accepted.
Unsupported but legitimate transfers need reconciliation, never assumed credit.

Primary references checked 2026-11-23:
https://solana.com/docs/rpc/http/gettransaction
https://solana.com/docs/rpc/http/getsignaturestatuses
https://solana.com/docs/rpc/json-structures
https://solana.com/docs/tokens/basics/transfer-tokens
https://solana.com/docs/tokens/basics/create-token-account
https://solana.com/docs/core/pda/pda-derivation
https://github.com/solana-program/token/blob/main/interface/src/instruction.rs
https://developers.circle.com/stablecoins/usdc-contract-addresses
https://www.solana-program.com/docs/memo
"""

from decimal import Decimal
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SOLANA_URL = "https://api.mainnet-beta.solana.com"
SOLANA_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
MEMO_PREFIX = "xeris:settlement:v2:"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
RESERVE_OWNER = "BqLiQacEpjPKon78RHLeTYUaaENgpvzDusCYqT3RpYfZ"
RESERVE_TOKEN_ACCOUNT = "BeM8R5hvdKGwiV5jKdug3kXoe6RaFaREjihKFNSjeBD6"
MAX_U64 = (1 << 64) - 1
MAX_BYTES = 1_000_000
MAX_FUTURE_SECONDS = 60
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
READ_METHODS = frozenset({"getGenesisHash", "getTransaction", "getSignatureStatuses"})


class VerificationError(ValueError):
    """Fixed machine-readable reason; never contains an RPC body or credential."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise VerificationError(code)


def b58encode(data):
    number = int.from_bytes(data, "big")
    result = ""
    while number:
        number, digit = divmod(number, 58)
        result = ALPHABET[digit] + result
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + result


def b58decode(value, size, code="invalid_public_address", *, nonzero=True):
    if type(value) is not str or not 1 <= len(value) <= 128:
        fail(code)
    number = 0
    for char in value:
        digit = ALPHABET.find(char)
        if digit < 0:
            fail(code)
        number = number * 58 + digit
    result = b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(result) != size or b58encode(result) != value or (nonzero and not any(result)):
        fail(code)
    return result


def _uint(value, code, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_U64:
        fail(code)
    return value


def _raw(value, code, *, positive=False):
    # RPC token raw amounts and request monetary values must remain strings.
    if type(value) is not str or len(value) > 20 or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        fail(code)
    return _uint(int(value), code, positive=positive)


def _is_curve_point(data):
    """Compressed Edwards-Y decompression test, not signature verification.

    Solana's PDA test accepts decompression, including torsion points; a stricter
    Ed25519 signature-public-key validity test would derive different addresses.
    Field decoding masks the sign bit and reduces modulo 2**255-19, as dalek does.
    """
    prime = (1 << 255) - 19
    y = (int.from_bytes(data, "little") & ((1 << 255) - 1)) % prime
    d = -121665 * pow(121666, prime - 2, prime) % prime
    yy = y * y % prime
    denominator = (d * yy + 1) % prime
    if denominator == 0:
        return False
    xx = (yy - 1) * pow(denominator, prime - 2, prime) % prime
    return xx == 0 or pow(xx, (prime - 1) // 2, prime) == 1


def associated_token_address(owner):
    """Derive the canonical classic-USDC ATA from public inputs only."""
    seeds = b58decode(owner, 32) + b58decode(TOKEN_PROGRAM, 32) + b58decode(USDC_MINT, 32)
    suffix = b58decode(ATA_PROGRAM, 32) + b"ProgramDerivedAddress"
    # Solana's canonical SDK search is 255 down to 1. Never accept another bump.
    for bump in range(255, 0, -1):
        candidate = hashlib.sha256(seeds + bytes([bump]) + suffix).digest()
        if not _is_curve_point(candidate):
            return b58encode(candidate)
    fail("ata_derivation_failed")


def validate_request(request, *, now=None):
    if type(request) is not dict:
        fail("invalid_request")
    now = int(time.time()) if now is None else _uint(now, "invalid_current_time", positive=True)
    owner = request.get("source_owner")
    b58decode(owner, 32, "invalid_source_owner")
    source = request.get("source_token_account")
    b58decode(source, 32, "invalid_source_token_account")
    if source != associated_token_address(owner):
        fail("source_is_not_owner_usdc_ata")
    if associated_token_address(RESERVE_OWNER) != RESERVE_TOKEN_ACCOUNT:
        fail("reserve_configuration_invalid")
    if len({owner, source, RESERVE_TOKEN_ACCOUNT, USDC_MINT, TOKEN_PROGRAM}) != 5:
        fail("overlapping_accounts")
    amount = _raw(request.get("amount_raw"), "invalid_request_amount", positive=True)
    not_before = _uint(request.get("not_before"), "invalid_not_before", positive=True)
    if not_before > now + MAX_FUTURE_SECONDS:
        fail("request_time_in_future")
    expires_at = request.get("expires_at")
    if expires_at is not None:
        _uint(expires_at, "invalid_expiry", positive=True)
        if expires_at < not_before:
            fail("invalid_expiry")
    min_slot = request.get("min_slot", 0)
    _uint(min_slot, "invalid_min_slot")
    intent_digest = None
    if "intent_digest" in request:
        intent_digest = request["intent_digest"]
        if type(intent_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", intent_digest) is None:
            fail("invalid_intent_digest")
        if len({owner, source, RESERVE_TOKEN_ACCOUNT, USDC_MINT, TOKEN_PROGRAM, MEMO_PROGRAM}) != 6:
            fail("overlapping_accounts")
    return {"source_owner": owner, "source_token_account": source, "amount_raw": str(amount),
            "not_before": not_before, "expires_at": expires_at, "min_slot": min_slot, "now": now,
            "intent_digest": intent_digest}


def _successful_metadata(value, code):
    if type(value) is not dict or "err" not in value or value["err"] is not None:
        fail(code)
    if "status" in value and value["status"] != {"Ok": None}:
        fail(code)


def _token_balances(rows, owner):
    if type(rows) is not list or len(rows) != 2:
        fail("incomplete_token_balances")
    result = {}
    for row in rows:
        if type(row) is not dict:
            fail("invalid_token_balance")
        index = _uint(row.get("accountIndex"), "invalid_token_balance_index")
        if index not in (1, 2) or index in result:
            fail("ambiguous_token_balance")
        if row.get("mint") != USDC_MINT or row.get("programId") != TOKEN_PROGRAM:
            fail("wrong_balance_mint_or_program")
        if row.get("owner") != (owner if index == 1 else RESERVE_OWNER):
            fail("wrong_token_authority")
        amount = row.get("uiTokenAmount")
        if type(amount) is not dict or type(amount.get("decimals")) is not int or amount["decimals"] != 6:
            fail("wrong_token_decimals")
        result[index] = _raw(amount.get("amount"), "invalid_token_balance_amount")
    return result


def verify_deposit(request, evidence, *, now=None):
    """Verify trusted read evidence. The return value is NOT settlement approval.

    Request: source_owner, source_token_account, amount_raw (decimal string),
    not_before (Unix seconds); optional expires_at and min_slot. Public service
    MUST supply intent_digest = SHA256(exact ownership signing message), which
    requires the matching signed second Memo instruction. Times/digest must be
    assigned by the authenticated journal, not supplied unchecked by a browser.
    Evidence: signature, genesis_hash, transaction (getTransaction result), and
    signature_status (getSignatureStatuses result for that exact one signature).
    """
    request = validate_request(request, now=now)
    if type(evidence) is not dict or evidence.get("genesis_hash") != SOLANA_GENESIS:
        fail("wrong_solana_genesis")
    signature = evidence.get("signature")
    b58decode(signature, 64, "invalid_signature_locator")
    receipt = evidence.get("transaction")
    if receipt is None:
        fail("transaction_unavailable")
    if type(receipt) is not dict or receipt.get("version") != "legacy":
        fail("unsupported_transaction_version")
    slot = _uint(receipt.get("slot"), "invalid_transaction_slot", positive=True)
    if slot < request["min_slot"]:
        fail("transaction_before_request_slot")
    block_time = _uint(receipt.get("blockTime"), "missing_or_invalid_block_time", positive=True)
    # blockTime is an RPC/validator time estimate, not a cryptographic timestamp.
    # Never relax not_before to admit a pre-request deposit. Boundary uncertainty
    # is held for reconciliation rather than automatically crediting an old tx.
    if block_time < request["not_before"]:
        fail("transaction_before_request")
    if block_time > request["now"] + MAX_FUTURE_SECONDS:
        fail("transaction_time_in_future")
    if request["expires_at"] is not None and block_time > request["expires_at"]:
        fail("transaction_after_request_expiry")

    statuses = evidence.get("signature_status")
    if type(statuses) is not dict or type(statuses.get("value")) is not list or len(statuses["value"]) != 1:
        fail("missing_signature_status")
    context = statuses.get("context")
    if type(context) is not dict or _uint(context.get("slot"), "invalid_status_context") < slot:
        fail("status_context_before_transaction")
    status = statuses["value"][0]
    _successful_metadata(status, "transaction_not_successful")
    if (status.get("confirmationStatus") != "finalized" or "confirmations" not in status
            or status["confirmations"] is not None):
        fail("transaction_not_finalized")
    if _uint(status.get("slot"), "invalid_status_slot") != slot:
        fail("inconsistent_transaction_slot")

    transaction = receipt.get("transaction")
    if type(transaction) is not dict or transaction.get("signatures") != [signature]:
        fail("signature_does_not_match_receipt")
    message = transaction.get("message")
    if type(message) is not dict:
        fail("missing_transaction_message")
    if set(message) - {"accountKeys", "header", "recentBlockhash", "instructions", "addressTableLookups"}:
        fail("unsupported_message_encoding")
    expected_keys = [request["source_owner"], request["source_token_account"],
                     RESERVE_TOKEN_ACCOUNT, USDC_MINT, TOKEN_PROGRAM]
    bound_intent = request["intent_digest"] is not None
    if bound_intent:
        expected_keys.append(MEMO_PROGRAM)
    if message.get("accountKeys") != expected_keys:
        fail("noncanonical_transaction_accounts")
    header = message.get("header")
    expected_header = {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                       "numReadonlyUnsignedAccounts": 3 if bound_intent else 2}
    if (type(header) is not dict or header != expected_header
            or any(type(value) is not int for value in header.values())):
        fail("wrong_transaction_signers_or_writability")
    b58decode(message.get("recentBlockhash"), 32, "invalid_recent_blockhash")
    if "addressTableLookups" in message and message["addressTableLookups"] != []:
        fail("address_lookups_not_allowed")
    instructions = message.get("instructions")
    if (type(instructions) is not list or len(instructions) != (2 if bound_intent else 1)
            or type(instructions[0]) is not dict):
        fail("transfer_and_bound_memo_required" if bound_intent else "exactly_one_transfer_required")
    instruction = instructions[0]
    if set(instruction) - {"programIdIndex", "accounts", "data", "stackHeight"}:
        fail("unsupported_instruction_encoding")
    if type(instruction.get("programIdIndex")) is not int or instruction["programIdIndex"] != 4:
        fail("wrong_token_program")
    accounts = instruction.get("accounts")
    if (accounts != [1, 3, 2, 0] or type(accounts) is not list
            or any(type(index) is not int for index in accounts)):
        fail("wrong_transfer_accounts")
    if "stackHeight" in instruction and instruction["stackHeight"] is not None:
        if type(instruction["stackHeight"]) is not int or instruction["stackHeight"] != 1:
            fail("transfer_not_top_level")
    data = b58decode(instruction.get("data"), 10, "invalid_transfer_data")
    amount = int(request["amount_raw"])
    if data != bytes([12]) + amount.to_bytes(8, "little") + bytes([6]):
        fail("wrong_transfer_type_amount_or_decimals")
    if bound_intent:
        memo = instructions[1]
        if type(memo) is not dict or set(memo) - {"programIdIndex", "accounts", "data", "stackHeight"}:
            fail("invalid_memo_instruction")
        if type(memo.get("programIdIndex")) is not int or memo["programIdIndex"] != 5:
            fail("wrong_memo_program")
        if (type(memo.get("accounts")) is not list or memo["accounts"] != [0]
                or type(memo["accounts"][0]) is not int):
            fail("memo_owner_signer_required")
        if "stackHeight" in memo and memo["stackHeight"] is not None:
            if type(memo["stackHeight"]) is not int or memo["stackHeight"] != 1:
                fail("memo_not_top_level")
        expected_memo = (MEMO_PREFIX + request["intent_digest"]).encode('ascii')
        if b58decode(memo.get("data"), len(expected_memo), "invalid_memo_data") != expected_memo:
            fail("memo_intent_digest_mismatch")

    meta = receipt.get("meta")
    _successful_metadata(meta, "transaction_not_successful")
    if meta.get("innerInstructions") != []:
        fail("inner_instructions_not_allowed_or_unavailable")
    if "loadedAddresses" in meta and meta["loadedAddresses"] != {"readonly": [], "writable": []}:
        fail("loaded_addresses_not_allowed")
    if meta.get("rewards") not in (None, []) or meta.get("returnData") is not None:
        fail("unrelated_transaction_effects")
    before = _token_balances(meta.get("preTokenBalances"), request["source_owner"])
    after = _token_balances(meta.get("postTokenBalances"), request["source_owner"])
    if before[1] < amount or before[2] + amount > MAX_U64:
        fail("invalid_transfer_balance_range")
    if before[1] - after[1] != amount or after[2] - before[2] != amount:
        fail("token_balance_delta_mismatch")
    fee = _uint(meta.get("fee"), "invalid_transaction_fee", positive=True)
    lamport_rows = []
    for key in ("preBalances", "postBalances"):
        row = meta.get(key)
        if type(row) is not list or len(row) != len(expected_keys):
            fail("incomplete_lamport_balances")
        lamport_rows.append([_uint(value, "invalid_lamport_amount") for value in row])
    pre_lamports, post_lamports = lamport_rows
    if pre_lamports[0] - post_lamports[0] != fee or pre_lamports[1:] != post_lamports[1:]:
        fail("unrelated_lamport_effects")
    if any(value == 0 for value in pre_lamports[1:]):
        fail("unfunded_required_account")

    return {
        "status": "verified_finalized_deposit", "signature": signature, "instruction_index": 0,
        "slot": slot, "block_time": block_time, "genesis_hash": SOLANA_GENESIS,
        "commitment": "finalized", "source_owner": request["source_owner"],
        "source_token_account": request["source_token_account"],
        "reserve_token_account": RESERVE_TOKEN_ACCOUNT, "reserve_owner": RESERVE_OWNER,
        "mint": USDC_MINT, "token_program": TOKEN_PROGRAM, "decimals": 6,
        "amount_raw": str(amount), "fee_lamports": str(fee),
        "source_before_raw": str(before[1]), "source_after_raw": str(after[1]),
        "reserve_before_raw": str(before[2]), "reserve_after_raw": str(after[2]),
        "evidence_source": "trusted_rpc_observation", "settlement_authorized": False,
        "global_deduplication_required": True,
        "intent_digest": request["intent_digest"],
        "memo_instruction_index": 1 if bound_intent else None,
    }


def checked_json(data):
    """Python integers and Decimal retain exact lexemes; ambiguous JSON fails."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        fail("nonfinite_json_number")

    try:
        return json.loads(data, object_pairs_hook=unique_keys, parse_float=Decimal,
                          parse_constant=invalid_constant)
    except VerificationError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError, ArithmeticError):
        fail("invalid_rpc_json")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("rpc_redirect_refused")


class ReadOnlySolanaClient:
    """Fixed read-only RPC allowlist, no proxy discovery, auth, retries or writes.

    Endpoint is trusted server configuration, never a request field. Socket IO
    has a finite timeout; response size and elapsed reads are also bounded.
    """
    def __init__(self, url=SOLANA_URL, *, timeout=10):
        try:
            if type(url) is not str or len(url) > 2048 or any(ord(char) <= 32 for char in url):
                fail("invalid_rpc_url")
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment or parsed.query
                    or parsed.port not in (None, 443)):
                fail("invalid_rpc_url")
        except ValueError:
            fail("invalid_rpc_url")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            fail("invalid_rpc_timeout")
        self.url = url
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(self, method, params):
        if type(method) is not str or method not in READ_METHODS:
            fail("rpc_method_not_allowed")
        if method == "getGenesisHash":
            if params != []:
                fail("invalid_rpc_parameters")
        elif method == "getTransaction":
            if type(params) is not list or len(params) != 2 or params[1] != {
                "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0,
            } or type(params[1].get("maxSupportedTransactionVersion")) is not int:
                fail("invalid_rpc_parameters")
            b58decode(params[0], 64, "invalid_signature_locator")
        else:
            if (type(params) is not list or len(params) != 2 or type(params[0]) is not list
                    or len(params[0]) != 1 or params[1] != {"searchTransactionHistory": True}
                    or params[1].get("searchTransactionHistory") is not True):
                fail("invalid_rpc_parameters")
            b58decode(params[0][0], 64, "invalid_signature_locator")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        request = urllib.request.Request(self.url, data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST", headers={"Content-Type": "application/json", "Accept": "application/json",
                                    "Accept-Encoding": "identity", "Cache-Control": "no-store"})
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    fail("rpc_http_status")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    fail("rpc_compression_refused")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    fail("rpc_content_type")
                length = response.headers.get("Content-Length")
                if length is not None and (len(length) > 7 or not length.isascii()
                                           or not length.isdecimal() or int(length) > MAX_BYTES):
                    fail("rpc_response_too_large")
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        fail("rpc_timeout")
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    size += len(chunk)
                    if size > MAX_BYTES:
                        fail("rpc_response_too_large")
                    if not chunk:
                        break
                    chunks.append(chunk)
                if time.monotonic() >= deadline:
                    fail("rpc_timeout")
                if length is not None and size != int(length):
                    fail("rpc_truncated_response")
                result = checked_json(b"".join(chunks))
        except VerificationError:
            raise
        except (TimeoutError, OSError, urllib.error.URLError):
            fail("rpc_unavailable")
        if (type(result) is not dict or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not int or result["id"] != 1
                or "error" in result or "result" not in result):
            fail("invalid_rpc_envelope")
        return result["result"]


def read_and_verify(client, signature, request, *, now=None):
    """Read finalized evidence using only the three allowlisted RPC methods."""
    validate_request(request, now=now)
    b58decode(signature, 64, "invalid_signature_locator")
    genesis = client.read("getGenesisHash", [])
    if genesis != SOLANA_GENESIS:
        fail("wrong_solana_genesis")
    transaction = client.read("getTransaction", [signature, {
        "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0,
    }])
    signature_status = client.read("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    return verify_deposit(request, {"signature": signature, "genesis_hash": genesis,
        "transaction": transaction, "signature_status": signature_status}, now=now)
