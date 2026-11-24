"""Unsigned user-owned redemption stages and exact public evidence verification.

Native route: user wraps native XRS, sells wXRS for xUSDC, then burns the EXACT
verified sale output. Direct route: user burns an explicit amount of xUSDC.
Tokens stay in the user's wallet until burned; there is no inbound custody
transfer or full-balance sweep. No private keys, signing, network or payout.

The burn instruction contains NO Solana destination, memo or redemption ID.
Consequently it cannot authorize an arbitrary submitted payout address. Caller
must create/authenticate the immutable redemption intent BEFORE accepting the
burn, reserve payout liquidity and fees, and consume each burn signature once.
Solana payout is a separate stage requiring finalized receipt verification and
journal recovery; this module never claims that a burn has paid the recipient.
"""
import base64
import hashlib
import json
import re
import struct

import xeris_purchase as p
from xeris_accounting import swap_output, verify_transition, verify_window


def quote_redemption(reserve_a_raw, reserve_b_raw, native_xrs_raw, slippage_bps=50):
    amount = p.raw(native_xrs_raw, positive=True)
    p.require(type(slippage_bps) is int and 0 <= slippage_bps <= 50, 'Slippage may not exceed 50 bps')
    output = swap_output(reserve_a_raw, reserve_b_raw, amount, False)
    minimum = (output * (10000 - slippage_bps) + 9999) // 10000
    return {'input_native_xrs_raw': amount, 'expected_xusdc_raw': output,
            'minimum_xusdc_raw': minimum, 'lp_fee_xrs_raw': amount * 30 // 10000,
            'fee_bps': 30, 'slippage_bps': slippage_bps,
            'user_native_fee_allowance_raw': 3 * p.BASE_FEE_RAW,
            'expected_gross_solana_usdc_raw': output,
            'payout_fee_raw': None, 'payout_ready': False}


def validate_fee_allowance(native_balance_raw, amount_raw, route_kind):
    balance, amount = p.raw(native_balance_raw), p.raw(amount_raw, positive=True)
    p.require(route_kind in ('native_xrs', 'xusdc'), 'Unknown redemption route')
    fee = (3 if route_kind == 'native_xrs' else 1) * p.BASE_FEE_RAW
    p.require(balance >= fee + (amount if route_kind == 'native_xrs' else 0), 'Insufficient native amount plus separate redemption fees')
    return {'native_fee_allowance_raw': fee}


def build_stage(stage, source_wallet, solana_recipient, blockhash, amount_raw,
                min_output_raw=None, *, route_id, route_kind='native_xrs'):
    p.require(stage in ('wrap', 'sell', 'burn'), 'Unknown redemption stage')
    p.require(route_kind in ('native_xrs', 'xusdc') and (route_kind == 'native_xrs' or stage == 'burn'), 'Invalid redemption route/stage')
    p.address(source_wallet); p.address(solana_recipient); p._route_id(route_id)
    p.require(type(blockhash) is bytes and len(blockhash) == 32 and blockhash != bytes(32), 'Nonzero 32-byte blockhash required')
    amount = p.raw(amount_raw, positive=True)
    p.require(stage == 'sell' or min_output_raw is None, 'Only sales accept a minimum output')
    if stage == 'wrap':
        operation, fields = 'WrapXrs', {'amount_raw': str(amount)}
        payload = struct.pack('<I', 13) + p._u64(amount)
        expected = {'from': source_wallet, 'to': source_wallet, 'amount': amount, 'tx_type': 'wrap_xrs', 'details': None}
    elif stage == 'sell':
        minimum = p.raw(min_output_raw, positive=True)
        args = {'amount': amount, 'min_output': minimum}
        args_json = json.dumps(args, separators=(',', ':'))
        operation, fields = 'ContractCall', {'contract_id': p.POOL, 'method': 'swap_a_to_b',
            'args_json': args_json, 'amount_raw': str(amount), 'min_output_raw': str(minimum)}
        payload = struct.pack('<I', 4) + p._string(p.POOL) + p._string('swap_a_to_b') + p._string(args_json)
        expected = {'from': source_wallet, 'to': p.POOL, 'amount': 0, 'tx_type': 'contract_call:swap_a_to_b',
                    'details': {'contract_id': p.POOL, 'method': 'swap_a_to_b', 'args': args}}
    else:
        operation, fields = 'TokenBurn', {'token_id': p.USDC, 'from': source_wallet, 'amount_raw': str(amount)}
        payload = struct.pack('<I', 2) + p._string(p.USDC) + p._string(source_wallet) + p._u64(amount)
        expected = {'from': source_wallet, 'to': '', 'amount': amount, 'tx_type': 'token_burn', 'details': {'token_id': p.USDC}}
    message = bytes([1, 0, 1, 2]) + p._decode58(source_wallet, 32) + bytes(32) + blockhash
    message += bytes([1, 1, 1, 0]) + p._shortvec(len(payload)) + payload
    template = message[:68] + bytes(32) + message[100:]
    return {'stage': stage, 'route_id': route_id, 'route_kind': route_kind, 'chain_id': p.CHAIN,
            'operator_wallet': source_wallet, 'source_wallet': source_wallet,
            'recipient': solana_recipient, 'solana_recipient': solana_recipient,
            'operation': operation, 'signer_public_address': source_wallet,
            'ordered_signer_public_keys': [source_wallet], 'program_id': '1' * 32,
            'fields': fields, 'fee_raw': str(p.BASE_FEE_RAW), 'expected_receipt_fields': expected,
            'instruction_hex': payload.hex(), 'instruction_bytes_base64': base64.b64encode(payload).decode(),
            'recent_blockhash_hex': blockhash.hex(), 'blockhash_offset': 68,
            'message_base64': base64.b64encode(message).decode(), 'message_sha256': hashlib.sha256(message).hexdigest(),
            'template_sha256': hashlib.sha256(template).hexdigest(),
            'unsigned_transaction_base64': base64.b64encode(bytes([1]) + bytes(64) + message).decode(),
            'status': 'UNSIGNED_ONLY', 'private_keys_accessed': False}


def validate_prepared(prepared):
    p.require(type(prepared) is dict, 'Prepared redemption must be an object')
    try:
        amount, minimum = prepared['fields']['amount_raw'], prepared['fields'].get('min_output_raw')
        p.require(type(amount) is str and re.fullmatch(r'[1-9][0-9]*', amount), 'Invalid prepared amount')
        p.require(minimum is None or type(minimum) is str and re.fullmatch(r'[1-9][0-9]*', minimum), 'Invalid prepared minimum')
        rebuilt = build_stage(prepared['stage'], prepared['source_wallet'], prepared['solana_recipient'],
            bytes.fromhex(p._hash(prepared['recent_blockhash_hex'])), int(amount),
            None if minimum is None else int(minimum), route_id=prepared['route_id'], route_kind=prepared['route_kind'])
        p._equal(prepared, rebuilt, 'Prepared redemption intent or wire was altered')
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('Malformed prepared redemption') from error
    return prepared


def verify_receipt(receipt, signature, prepared):
    validate_prepared(prepared); p._decode58(signature, 64)
    p.require(type(receipt) is dict, 'Execution receipt unavailable')
    p.raw(receipt.get('slot'), positive=True)
    tx, meta = receipt.get('transaction'), receipt.get('meta')
    p.require(type(tx) is dict and tx.get('signatures') == [signature], 'Receipt signature mismatch')
    p.require(type(meta) is dict and 'err' in meta and meta['err'] is None, 'Redemption instruction did not succeed')
    p._equal(meta.get('instructionOutcomes'), [{'index': 0, 'ok': True}], 'Exactly one committed instruction required')
    p.require(type(meta.get('fee')) is int and meta['fee'] == p.BASE_FEE_RAW, 'Unexpected execution fee')
    p._equal({key: tx.get(key) for key in prepared['expected_receipt_fields']}, prepared['expected_receipt_fields'], 'Receipt differs from reviewed redemption')
    return {'signature': signature, 'slot': receipt['slot'], 'stage': prepared['stage']}


def _apply_redemption(state, prepared):
    stage, amount = prepared['stage'], int(prepared['fields']['amount_raw'])
    state['native_raw'] -= p.BASE_FEE_RAW
    output = None
    if stage == 'wrap':
        p.require(state['native_raw'] >= amount + 2 * p.BASE_FEE_RAW, 'Reserve native amount and sell/burn fees')
        state['native_raw'] -= amount; state['wxrs_raw'] += amount; state['wxrs_supply_raw'] += amount
    elif stage == 'sell':
        p.require(state['native_raw'] >= p.BASE_FEE_RAW, 'Reserve burn fee')
        p.require(state['wxrs_raw'] >= amount, 'Insufficient wrapped amount for this redemption')
        output = swap_output(state['reserve_a_raw'], state['reserve_b_raw'], amount, False)
        p.require(output >= int(prepared['fields']['min_output_raw']), 'Sale output below signed minimum')
        state['wxrs_raw'] -= amount; state['xusdc_raw'] += output
        state['reserve_a_raw'] += amount; state['reserve_b_raw'] -= output
    else:
        p.require(state['xusdc_raw'] >= amount, 'Insufficient xUSDC for explicit burn')
        state['xusdc_raw'] -= amount; state['xusdc_supply_raw'] -= amount
    return output


def verify_accounted_transition(before, after, prepared, receipt, signature, evidence):
    verify_receipt(receipt, signature, prepared)
    output, concurrent = verify_transition(before, after, prepared, receipt, signature, evidence, _apply_redemption)
    return {'status': 'VERIFIED_ACCOUNTED_REDEMPTION_STAGE', 'route_id': prepared['route_id'],
            'stage': prepared['stage'], 'route_kind': prepared['route_kind'], 'signature': signature,
            'receipt_slot': receipt['slot'], 'observed_slot': after['slot'],
            'actual_output_xusdc_raw': output,
            'burned_xusdc_raw': int(prepared['fields']['amount_raw']) if prepared['stage'] == 'burn' else None,
            'solana_recipient': prepared['solana_recipient'], 'concurrent_transactions_accounted': concurrent,
            'payout_authorized': False, 'irreversible_finality_proven': False}


def verify_burn_intent(intent, prepared, receipt, signature):
    """Match a committed burn to a pre-existing authenticated journal intent.

    This pure equality check cannot prove authentication/pre-existence, canonical
    finality, reserve sufficiency or single-use. The caller MUST establish all
    those facts separately. Return is an obligation candidate, never payout auth.
    ``signed_message_sha256`` MUST originate from the user-approved message,
    persisted before submission. The journal must separately authenticate the
    source user’s selected payout address; a burn signature alone does NOT bind
    that address, even if this equality check passes.
    """
    verify_receipt(receipt, signature, prepared)
    p.require(prepared['stage'] == 'burn' and type(intent) is dict, 'Burn intent required')
    expected = {'route_id': prepared['route_id'], 'route_kind': prepared['route_kind'],
                'source_wallet': prepared['source_wallet'], 'solana_recipient': prepared['solana_recipient'],
                'burn_amount_raw': prepared['fields']['amount_raw'],
                'signed_message_sha256': prepared['message_sha256'], 'burn_signature': signature}
    p._equal({key: intent.get(key) for key in expected}, expected, 'Burn differs from immutable redemption intent')
    p.require(intent.get('authenticated_before_submission') is True, 'Pre-existing authenticated intent is required')
    return {'status': 'BURN_INTENT_MATCHED', **expected,
            'gross_usdc_obligation_raw': int(prepared['fields']['amount_raw']),
            'payout_authorized': False, 'irreversible_finality_proven': False}


def verify_burn_canonical(intent, prepared, receipt, signature, evidence, before_anchor, after_anchor):
    """Verify exact burn inclusion without requiring pool availability or a quiet wallet.

    Anchors contain chain_id, slot and block_hash from pinned canonical observers.
    The complete signed-wire Merkle window still must agree on all three nodes;
    only the target burn needs an execution receipt. Foreign transactions may be
    arbitrary/multi-instruction because their effects cannot alter the already
    committed exact burn amount. This proves neither intent authentication nor
    irreversible finality or permission to pay; the journal enforces those gates.
    """
    result = verify_burn_intent(intent, prepared, receipt, signature)
    for anchor in (before_anchor, after_anchor):
        p.require(type(anchor) is dict and anchor.get('chain_id') == p.CHAIN, 'Wrong canonical anchor chain')
        p.raw(anchor.get('slot'), positive=True); p._hash(anchor.get('block_hash'))
    verify_window(before_anchor, after_anchor, prepared, receipt, signature, evidence, target_only=True)
    return {**result, 'status': 'VERIFIED_CANONICAL_BURN', 'receipt_slot': receipt['slot'],
            'observed_slot': after_anchor['slot'], 'payout_authorized': False,
            'irreversible_finality_proven': False}
