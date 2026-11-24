"""Deterministic, unsigned-only preparation of the pinned Squads USDC payout.

Public inputs only. No file access, subprocesses, RPC transport, signatures or
submission. This prepares a candidate, not an authorization: callers must bind
it to their immutable settlement intent, run solana_payout.review_plan/preflight,
and durably journal the plan and reserved Squads index before any signing.
Never rebuild an unresolved attempt into a replacement payout.

Wire provenance: solana-payout/src/main.rs in this directory, using pinned
solana-message 2.4.0 and solana-pubkey 2.4.0. Account ordering follows that SDK's
CompiledKeys BTreeMap ordering (decoded public-key bytes within privilege groups).
The tests contain frozen public Rust-builder vectors and use the separate Python
wire decoder. Squads receives a compact inner message, not a Solana wire message.
"""

import base64
import hashlib

import solana_verify as sv


PROGRAM = "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf"
MULTISIG = "bHNiYFGPMqNvTMNxBdngKskVFGbzzgFvFRx1kmynLEU"
VAULT = "BqLiQacEpjPKon78RHLeTYUaaENgpvzDusCYqT3RpYfZ"
SOURCE = "BeM8R5hvdKGwiV5jKdug3kXoe6RaFaREjihKFNSjeBD6"
FUNDING = "BzyrcS63PR4kxt8WAbtCptsbN1czdSsyTt4AaaWJpKTA"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
MAX_PACKET_BYTES = 1232


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _pub(value):
    return sv.b58decode(value, 32, nonzero=False)


def _b64(value):
    return base64.b64encode(value).decode("ascii")


def _pda(seeds, program):
    _require(len(seeds) < 16 and all(type(s) is bytes and len(s) <= 32 for s in seeds),
             "Invalid PDA seeds")
    suffix = _pub(program) + b"ProgramDerivedAddress"
    for bump in range(255, 0, -1):
        candidate = hashlib.sha256(b"".join(seeds) + bytes([bump]) + suffix).digest()
        if not sv._is_curve_point(candidate):
            return sv.b58encode(candidate), bump
    raise ValueError("PDA derivation failed")


def _short(value):
    _require(type(value) is int and 0 <= value <= 65535, "Invalid short vector")
    result = bytearray()
    while True:
        part = value & 127
        value >>= 7
        result.append(part | (128 if value else 0))
        if not value:
            return bytes(result)


def _meta(key, signer=False, writable=False):
    return {"public_key": key, "is_signer": signer, "is_writable": writable}


def _ix(program, accounts, data):
    return {"program": program, "accounts": accounts, "data": data}


def _compile(instructions, payer):
    """Merge privileges then compile exactly like the pinned legacy SDK."""
    privileges = {}
    for instruction in instructions:
        privileges.setdefault(instruction["program"], [False, False])
        for account in instruction["accounts"]:
            flags = privileges.setdefault(account["public_key"], [False, False])
            flags[0] |= account["is_signer"]
            flags[1] |= account["is_writable"]
    privileges[payer] = [True, True]
    ordered = sorted((key for key in privileges if key != payer), key=_pub)
    groups = [[payer], [], [], []]
    for key in ordered:
        signer, writable = privileges[key]
        groups[(0 if writable else 1) if signer else (2 if writable else 3)].append(key)
    keys = [key for group in groups for key in group]
    _require(len(keys) <= 256, "Too many transaction accounts")
    header = bytes([len(groups[0]) + len(groups[1]), len(groups[1]), len(groups[3])])
    positions = {key: i for i, key in enumerate(keys)}
    compiled = [(positions[ix["program"]], bytes(positions[a["public_key"]] for a in ix["accounts"]), ix["data"])
                for ix in instructions]
    return header, keys, compiled


def _message(header, keys, compiled, blockhash):
    result = header + _short(len(keys)) + b"".join(_pub(key) for key in keys)
    result += blockhash + _short(len(compiled))
    for program_index, accounts, data in compiled:
        result += bytes([program_index]) + _short(len(accounts)) + accounts + _short(len(data)) + data
    return result


def build_payout(destination_owner: str, amount_raw: str, transaction_index: int,
                 blockhash: str) -> dict:
    """Build one unsigned mainnet payout to the owner's canonical USDC ATA.

    amount_raw is a canonical positive u64 decimal string, never a float.
    transaction_index is a positive u64 int, never bool/string. The caller must
    acquire and reserve the next index from finalized public multisig state.
    blockhash must be a canonical nonzero base58-encoded 32-byte hash. Freshness,
    ATA existence, rent and balance checks belong to the injected preflight.
    The same idempotent ATA instruction supports both new and existing ATAs.
    """
    amount = sv._raw(amount_raw, "invalid_payout_amount", positive=True)
    index = sv._uint(transaction_index, "invalid_transaction_index", positive=True)
    owner_bytes = _pub(destination_owner)
    blockhash_bytes = sv.b58decode(blockhash, 32, "invalid_recent_blockhash")
    multisig_bytes = _pub(MULTISIG)
    index_bytes = index.to_bytes(8, "little")
    transaction, transaction_bump = _pda([b"multisig", multisig_bytes, b"transaction", index_bytes], PROGRAM)
    proposal, proposal_bump = _pda([b"multisig", multisig_bytes, b"transaction", index_bytes, b"proposal"], PROGRAM)
    vault, vault_bump = _pda([b"multisig", multisig_bytes, b"vault", b"\0"], PROGRAM)
    destination, ata_bump = _pda([owner_bytes, _pub(TOKEN), _pub(USDC)], ATA)
    source, _ = _pda([_pub(VAULT), _pub(TOKEN), _pub(USDC)], ATA)
    _require(vault == VAULT and source == SOURCE, "Pinned reserve derivation mismatch")
    reserved = {SYSTEM, PROGRAM, TOKEN, USDC, ATA, MULTISIG, VAULT, SOURCE, transaction, proposal}
    _require(destination_owner not in reserved and destination not in reserved | {FUNDING},
             "Reserved or overlapping payout recipient")

    transfer_data = b"\x0c" + amount.to_bytes(8, "little") + b"\x06"
    transfer = _ix(TOKEN, [_meta(SOURCE, writable=True), _meta(USDC),
                           _meta(destination, writable=True), _meta(VAULT, signer=True)], transfer_data)
    inner_header, inner_keys, inner_compiled = _compile([transfer], VAULT)
    num_signers, readonly_signers, readonly_unsigned = inner_header
    writable_signers = num_signers - readonly_signers
    writable_unsigned = len(inner_keys) - num_signers - readonly_unsigned
    _require(inner_header == bytes([1, 0, 2]) and len(inner_keys) == 5,
             "Unexpected inner account privileges")
    compact = bytes([num_signers, writable_signers, writable_unsigned, len(inner_keys)])
    compact += b"".join(_pub(key) for key in inner_keys) + bytes([len(inner_compiled)])
    for program_index, accounts, data in inner_compiled:
        compact += bytes([program_index, len(accounts)]) + accounts + len(data).to_bytes(2, "little") + data
    compact += b"\0"  # No address lookup tables.

    create_data = bytes([48, 250, 78, 168, 208, 226, 218, 211, 0, 0])
    create_data += len(compact).to_bytes(4, "little") + compact + b"\0"
    execute_accounts = [_meta(MULTISIG), _meta(proposal, writable=True),
                        _meta(transaction), _meta(FUNDING, signer=True)]
    for i, key in enumerate(inner_keys):
        writable = i < writable_signers or num_signers <= i < num_signers + writable_unsigned
        execute_accounts.append(_meta(key, writable=writable))
    instructions = [
        _ix(ATA, [_meta(FUNDING, True, True), _meta(destination, writable=True),
                  _meta(destination_owner), _meta(USDC), _meta(SYSTEM), _meta(TOKEN)], b"\1"),
        _ix(PROGRAM, [_meta(MULTISIG, writable=True), _meta(transaction, writable=True),
                      _meta(FUNDING, signer=True), _meta(FUNDING, True, True), _meta(SYSTEM)], create_data),
        _ix(PROGRAM, [_meta(MULTISIG), _meta(proposal, writable=True),
                      _meta(FUNDING, signer=True), _meta(FUNDING, True, True), _meta(SYSTEM)],
            bytes([220, 60, 73, 224, 30, 108, 79, 159]) + index_bytes + b"\0"),
        _ix(PROGRAM, [_meta(MULTISIG), _meta(FUNDING, True, True), _meta(proposal, writable=True)],
            bytes([144, 37, 164, 136, 188, 216, 42, 248, 0])),
        _ix(PROGRAM, execute_accounts, bytes([194, 8, 161, 87, 153, 164, 25, 171])),
    ]
    header, keys, compiled = _compile(instructions, FUNDING)
    _require(header[0] == 1 and header[1] == 0 and keys[0] == FUNDING,
             "Unexpected outer signer")
    message = _message(header, keys, compiled, blockhash_bytes)
    template = _message(header, keys, compiled, bytes(32))
    unsigned = b"\1" + bytes(64) + message
    _require(len(unsigned) <= MAX_PACKET_BYTES, "Payout exceeds Solana packet limit")
    # Stored VaultTransaction uses Borsh u32 vector lengths, not compact lengths.
    stored_size = 3 + 4 + 32 * len(inner_keys) + 4 + sum(1 + 4 + len(a) + 4 + len(d) for _, a, d in inner_compiled) + 4
    return {
        "status": "UNSIGNED_PAYOUT_TEMPLATE_ONLY", "network": "Solana mainnet-beta",
        "amount_raw": str(amount), "decimals": 6, "multisig": MULTISIG,
        "vault": VAULT, "vault_token_account": SOURCE, "usdc_mint": USDC,
        "destination_token_account": destination, "destination_authority": destination_owner,
        "destination_owner": destination_owner, "ata_program": ATA, "ata_space": 165,
        "creates_destination_ata_idempotently": True, "member": FUNDING,
        "transaction_index": index, "transaction_pda": transaction, "proposal_pda": proposal,
        "transaction_space": 8 + 32 + 32 + 8 + 1 + 1 + 1 + 4 + stored_size,
        "proposal_space": 8 + 32 + 8 + 1 + 8 + 1 + 3 * (4 + 32),
        "bumps": {"transaction": transaction_bump, "proposal": proposal_bump,
                  "vault": vault_bump, "destination_ata": ata_bump},
        "required_signer_public_keys": [FUNDING], "recent_blockhash": blockhash,
        "instructions": [{"program": ix["program"], "accounts": ix["accounts"],
                          "data_base64": _b64(ix["data"])} for ix in instructions],
        "inner_transfer": {"program": TOKEN, "source": SOURCE, "destination": destination,
                           "mint": USDC, "authority": VAULT, "amount_raw": str(amount),
                           "decimals": 6, "data_base64": _b64(transfer_data)},
        "compact_inner_message_base64": _b64(compact), "inner_account_keys": inner_keys,
        "message_base64": _b64(message), "message_template_base64": _b64(template),
        "unsigned_transaction_base64": _b64(unsigned), "transaction_bytes": len(unsigned),
        "private_keys_accessed": False, "real_signatures": 0, "transactions_submitted": 0,
        "requires_verified_settlement_before_real_signing": True,
        "requires_journal_recipient_and_amount_binding": True,
        "cost_limit_semantics": "Client must verify quoted and simulated rent+fee within approved limit. This is not an on-chain maximum-SOL-debit guard; the member is passed to the trusted Squads program.",
    }
