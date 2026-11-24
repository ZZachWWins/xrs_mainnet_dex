"""Public-only unsigned Xeris purchase preparation and fail-closed evidence checks.

No networking, signing, key loading, submission, or journal mutation. Minting must
also be gated by the caller's verified Solana deposit and reserve obligations.
The caller owns durable route exclusivity, a fresh blockhash, single-send recovery,
and confirmation policy. This module does not prove irreversible finality.

Current RPC receipts have no swap-output field. The original isolated verifier
requires an otherwise-empty agreed window. verify_accounted_transition instead
replays supported concurrent instructions bound to full-wire Merkle roots and
three-node receipts. Unsupported activity requires reconciliation; neither path
guesses from a quote or sweeps a wallet balance. This is a bounded indexer.

Wire provenance: pool-setup/pilot/xeris/public_flow.py and its public Rust SDK
fixture checker; variant indices/fields from fresh-mainnet-node/src/token.rs.
"""
import base64
import hashlib
import json
import re
import struct

CHAIN = 'xeris-mainnet-v4'
TREASURY = '4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL'
LP = 'HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX'
POOL = 'xrs-xusdc-mainnet-v1'
USDC = 'xeris_usdc'
WRAPPED = 'xrs_native'
BASE_FEE_RAW = 1_000_000
U64_MAX = (1 << 64) - 1
NODES = frozenset(('mac_treasury', 'mac_auxiliary', 'droplet'))
PRODUCERS = frozenset((TREASURY, '86TZWQYSiRkccYEAWfU2zbTvwcDouRFFWJ4TLkuYmkB1',
                       '6Y4eJNvkhNoRgxYNEw93TtNvWYHKp3jvomzfVwsby6XH'))
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
BALANCE_FIELDS = ('native_raw', 'xusdc_raw', 'wxrs_raw', 'xusdc_supply_raw',
                  'wxrs_supply_raw', 'reserve_a_raw', 'reserve_b_raw')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def raw(value, *, positive=False):
    require(type(value) is int and (1 if positive else 0) <= value <= U64_MAX,
            'Expected an exact bounded raw integer')
    return value


def _decode58(text, length):
    require(type(text) is str and 1 <= len(text) <= 128, 'Invalid base58 text')
    value = 0
    for char in text:
        require(char in ALPHABET, 'Invalid base58 character')
        value = value * 58 + ALPHABET.index(char)
    decoded = bytes(len(text) - len(text.lstrip('1'))) + value.to_bytes((value.bit_length() + 7) // 8, 'big')
    require(len(decoded) == length, 'Invalid base58 byte length')
    require(decoded != bytes(length), 'Zero public identity is not permitted')
    return decoded


def address(value):
    _decode58(value, 32)
    return value


def _route_id(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value), 'Invalid route ID')
    return value


def _hash(value):
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) and value != '0' * 64,
            'Nonzero canonical block hash required')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _equal(actual, expected, message):
    # Unlike Python ==, this rejects bool/int confusion in receipt fields.
    require(_json(actual) == _json(expected), message)


def _u64(value):
    return struct.pack('<Q', raw(value))


def _string(value):
    encoded = value.encode('utf-8')
    return _u64(len(encoded)) + encoded


def _shortvec(value):
    require(type(value) is int and 0 <= value <= 65535, 'ShortU16 out of bounds')
    output = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        output.append(byte | (128 if value else 0))
        if not value:
            return bytes(output)


def quote_purchase(reserve_a_raw, reserve_b_raw, usdc_raw, slippage_bps=50):
    """Exact current-pool quote; never evidence of the eventual execution output."""
    a, b, amount = (raw(v, positive=True) for v in (reserve_a_raw, reserve_b_raw, usdc_raw))
    require(type(slippage_bps) is int and 0 <= slippage_bps <= 50, 'Slippage may not exceed 50 bps')
    raw(b + amount)
    fee = amount * 30 // 10_000
    output = a * (amount - fee) // (b + amount - fee)
    require(0 < output < a, 'Purchase would return zero or invalid output')
    minimum = (output * (10_000 - slippage_bps) + 9_999) // 10_000
    return {'input_usdc_raw': amount, 'expected_xrs_raw': output,
            'minimum_xrs_raw': minimum, 'lp_fee_usdc_raw': fee, 'fee_bps': 30,
            'slippage_bps': slippage_bps, 'operator_native_fee_allowance_raw': 3 * BASE_FEE_RAW,
            'mint_authority_native_fee_raw': BASE_FEE_RAW,
            'total_xeris_fees_raw': 4 * BASE_FEE_RAW}


def validate_fee_allowance(operator_native_raw, mint_authority_native_raw):
    require(raw(operator_native_raw) >= 3 * BASE_FEE_RAW, 'Operator needs three native transaction fees before buying')
    require(raw(mint_authority_native_raw) >= BASE_FEE_RAW, 'Mint authority needs its native transaction fee')
    return {'operator_native_fee_allowance_raw': 3 * BASE_FEE_RAW,
            'mint_authority_native_fee_raw': BASE_FEE_RAW}


def build_stage(stage, operator_wallet, recipient, blockhash, amount_raw,
                min_output_raw=None, *, route_id):
    """Prepare one stage only; later stages require previously verified amounts.

    The journal must bind every stage to one deposit, operator, recipient and
    amount. It must not pre-sign all stages using an estimated swap output.
    """
    require(stage in ('mint', 'buy', 'unwrap', 'deliver'), 'Unknown purchase stage')
    address(operator_wallet); address(recipient); _route_id(route_id)
    require(operator_wallet not in PRODUCERS | {LP}, 'Route operator must be an isolated non-producer wallet')
    require(recipient != operator_wallet, 'Recipient must differ from route operator')
    require(type(blockhash) is bytes and len(blockhash) == 32 and blockhash != bytes(32), 'Nonzero 32-byte blockhash required')
    amount = raw(amount_raw, positive=True)
    require(stage == 'buy' or min_output_raw is None, 'Only swaps accept a minimum output')
    signer = TREASURY if stage == 'mint' else operator_wallet
    if stage == 'mint':
        operation = 'TokenMint'
        fields = {'token_id': USDC, 'to': operator_wallet, 'amount_raw': str(amount)}
        payload = struct.pack('<I', 0) + _string(USDC) + _string(operator_wallet) + _u64(amount)
        expected = {'from': TREASURY, 'to': operator_wallet, 'amount': amount,
                    'tx_type': 'token_mint', 'details': {'token_id': USDC}}
    elif stage == 'buy':
        minimum = raw(min_output_raw, positive=True)
        operation = 'ContractCall'
        args = {'amount': amount, 'min_output': minimum}
        args_json = json.dumps(args, separators=(',', ':'))
        fields = {'contract_id': POOL, 'method': 'swap_b_to_a', 'args_json': args_json,
                  'amount_raw': str(amount), 'min_output_raw': str(minimum)}
        payload = struct.pack('<I', 4) + _string(POOL) + _string('swap_b_to_a') + _string(args_json)
        expected = {'from': operator_wallet, 'to': POOL, 'amount': 0,
                    'tx_type': 'contract_call:swap_b_to_a',
                    'details': {'contract_id': POOL, 'method': 'swap_b_to_a', 'args': args}}
    elif stage == 'unwrap':
        operation = 'UnwrapXrs'
        fields = {'amount_raw': str(amount)}
        payload = struct.pack('<I', 14) + _u64(amount)
        expected = {'from': operator_wallet, 'to': operator_wallet, 'amount': amount,
                    'tx_type': 'unwrap_xrs', 'details': None}
    else:
        operation = 'NativeTransfer'
        fields = {'from': operator_wallet, 'to': recipient, 'amount_raw': str(amount)}
        payload = struct.pack('<I', 11) + _string(operator_wallet) + _string(recipient) + _u64(amount)
        expected = {'from': operator_wallet, 'to': recipient, 'amount': amount,
                    'tx_type': 'native_transfer', 'details': None}
    message = (bytes([1, 0, 1, 2]) + _decode58(signer, 32) + bytes(32) + blockhash
               + bytes([1, 1, 1, 0]) + _shortvec(len(payload)) + payload)
    template = message[:68] + bytes(32) + message[100:]
    return {'stage': stage, 'route_id': route_id, 'chain_id': CHAIN,
            'operator_wallet': operator_wallet, 'recipient': recipient, 'operation': operation,
            'signer_public_address': signer, 'ordered_signer_public_keys': [signer],
            'program_id': '1' * 32, 'fields': fields, 'fee_raw': str(BASE_FEE_RAW),
            'expected_receipt_fields': expected, 'instruction_hex': payload.hex(),
            'instruction_bytes_base64': base64.b64encode(payload).decode(),
            'recent_blockhash_hex': blockhash.hex(), 'blockhash_offset': 68,
            'message_base64': base64.b64encode(message).decode(),
            'message_sha256': hashlib.sha256(message).hexdigest(),
            'template_sha256': hashlib.sha256(template).hexdigest(),
            'unsigned_transaction_base64': base64.b64encode(bytes([1]) + bytes(64) + message).decode(),
            'status': 'UNSIGNED_ONLY', 'private_keys_accessed': False}


def validate_prepared(prepared):
    require(type(prepared) is dict, 'Prepared stage must be an object')
    try:
        amount_text = prepared['fields']['amount_raw']
        minimum_text = prepared['fields'].get('min_output_raw')
        require(type(amount_text) is str and re.fullmatch(r'[1-9][0-9]*', amount_text), 'Invalid prepared amount')
        require(minimum_text is None or type(minimum_text) is str and re.fullmatch(r'[1-9][0-9]*', minimum_text), 'Invalid prepared minimum')
        rebuilt = build_stage(prepared['stage'], prepared['operator_wallet'], prepared['recipient'],
                              bytes.fromhex(_hash(prepared['recent_blockhash_hex'])), int(amount_text),
                              None if minimum_text is None else int(minimum_text), route_id=prepared['route_id'])
        _equal(prepared, rebuilt, 'Prepared stage metadata or unsigned message was altered')
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('Malformed prepared stage') from error
    return prepared


def verify_receipt(receipt, signature, prepared):
    validate_prepared(prepared); _decode58(signature, 64)
    require(type(receipt) is dict, 'Execution receipt is unavailable')
    raw(receipt.get('slot'), positive=True)
    tx, meta = receipt.get('transaction'), receipt.get('meta')
    require(type(tx) is dict and tx.get('signatures') == [signature], 'Receipt signature mismatch')
    require(type(meta) is dict and 'err' in meta and meta['err'] is None, 'Execution did not succeed')
    _equal(meta.get('instructionOutcomes'), [{'index': 0, 'ok': True}], 'Exactly one committed instruction required')
    require(type(meta.get('fee')) is int and meta['fee'] == BASE_FEE_RAW, 'Unexpected execution fee')
    _equal({key: tx.get(key) for key in prepared['expected_receipt_fields']},
           prepared['expected_receipt_fields'], 'Receipt differs from reviewed stage')
    return {'signature': signature, 'slot': receipt['slot'], 'stage': prepared['stage']}


def _snapshot(snapshot, operator):
    require(type(snapshot) is dict and snapshot.get('chain_id') == CHAIN and snapshot.get('wallet') == operator,
            'Snapshot chain or wallet mismatch')
    raw(snapshot.get('slot'), positive=True); _hash(snapshot.get('block_hash'))
    # The collector must bracket all state reads with these same tip identities.
    _equal(snapshot.get('read_boundary'), {'start_slot': snapshot['slot'], 'end_slot': snapshot['slot'],
           'start_hash': snapshot['block_hash'], 'end_hash': snapshot['block_hash']}, 'Snapshot reads are not coherent')
    for name in BALANCE_FIELDS:
        raw(snapshot.get(name))
    require(snapshot['reserve_a_raw'] > 0 and snapshot['reserve_b_raw'] > 0, 'Empty pool')
    invariant = snapshot.get('pool_invariant', {})
    require(type(invariant) is dict, 'Pool configuration must be an object')
    require(invariant.get('contract_id') == POOL and invariant.get('contract_type') == 'Swap'
            and invariant.get('owner') == LP and invariant.get('is_active') is True, 'Pool identity mismatch')
    state = invariant.get('state')
    require(type(state) is dict and type(state.get('Swap')) is dict, 'Swap configuration missing')
    swap = state['Swap']
    _equal({key: swap.get(key) for key in ('token_a', 'token_b', 'fee_bps')},
           {'token_a': WRAPPED, 'token_b': USDC, 'fee_bps': 30}, 'Pool assets or fee mismatch')
    tokens = snapshot.get('token_metadata')
    require(type(tokens) is dict, 'Token metadata must be an object')
    for token, symbol, decimals in ((USDC, 'xUSDC', 6), (WRAPPED, 'wXRS', 9)):
        metadata = tokens.get(token)
        require(type(metadata) is dict, 'Token metadata missing')
        _equal({key: metadata.get(key) for key in ('token_id', 'symbol', 'decimals', 'mint_authority', 'mint_policy')},
               {'token_id': token, 'symbol': symbol, 'decimals': decimals, 'mint_authority': TREASURY, 'mint_policy': 'Standard'},
               'Token identity mismatch')
        maximum = raw(metadata.get('max_supply'), positive=True)
        require(snapshot['xusdc_supply_raw' if token == USDC else 'wxrs_supply_raw'] <= maximum,
                'Token supply exceeds its configured maximum')


def _verify_window(before, after, prepared, receipt, signature, evidence):
    require(type(evidence) is dict and evidence.get('route_id') == prepared['route_id']
            and evidence.get('operator_wallet') == prepared['operator_wallet']
            and evidence.get('exclusive_wallet') is True, 'Exclusive route-wallet evidence required')
    _equal(evidence.get('pending_transaction_ids'), [], 'Unresolved route-wallet transaction exists')
    windows = evidence.get('canonical_blocks_by_node')
    require(type(windows) is dict and set(windows) == NODES, 'All three canonical node observations required')
    start, end = before['slot'], after['slot']
    require(start < receipt['slot'] <= end and end - start <= 150, 'Invalid or oversized observation window')
    agreed = None
    for blocks in windows.values():
        require(type(blocks) is list and len(blocks) == end - start + 1, 'Incomplete canonical block window')
        compact = []
        found = 0
        for index, block in enumerate(blocks):
            require(type(block) is dict and type(block.get('slot')) is int and block['slot'] == start + index,
                    'Missing or reordered canonical block')
            _hash(block.get('hash')); _hash(block.get('merkle_root'))
            require(block.get('proposer') in PRODUCERS, 'Unexpected block proposer')
            txs = block.get('transactions')
            require(type(txs) is list and type(block.get('transaction_count')) is int
                    and block['transaction_count'] == len(txs), 'Incomplete block transaction list')
            if index:
                for tx in txs:
                    require(type(tx) is dict and tx.get('signature') == signature
                            and type(tx.get('block_slot')) is int and tx['block_slot'] == receipt['slot'] == block['slot']
                            and tx.get('status') == 'included', 'Concurrent or unaccounted transaction: manual reconciliation required')
                    _equal({key: tx.get(key) for key in prepared['expected_receipt_fields']},
                           prepared['expected_receipt_fields'], 'Block transaction differs from reviewed stage')
                    found += 1
            compact.append({key: block[key] for key in ('slot', 'hash', 'merkle_root', 'proposer', 'transaction_count', 'transactions')})
        require(found == 1, 'Reviewed transaction must occur exactly once')
        require(blocks[0]['hash'] == before['block_hash'] and blocks[-1]['hash'] == after['block_hash'],
                'Snapshot block hash does not match canonical observations')
        if agreed is not None:
            _equal(compact, agreed, 'Nodes disagree on canonical block evidence')
        agreed = compact


def verify_isolated_transition(before, after, prepared, receipt, signature, evidence):
    """Verify output only from exact, isolated state changes with canonical evidence.

    Calls raise on interference, even if the transaction itself succeeded. A
    raised verification error must keep the original signature unresolved for
    operator reconciliation; it must never trigger a replacement mint or swap.
    """
    verify_receipt(receipt, signature, prepared)
    operator = prepared['operator_wallet']
    _snapshot(before, operator); _snapshot(after, operator)
    _equal(after['pool_invariant'], before['pool_invariant'], 'Pool configuration changed')
    _equal(after['token_metadata'], before['token_metadata'], 'Token configuration changed')
    _verify_window(before, after, prepared, receipt, signature, evidence)
    expected = {name: before[name] for name in BALANCE_FIELDS}
    stage, amount = prepared['stage'], int(prepared['fields']['amount_raw'])
    actual_output = None
    if stage == 'mint':
        require(before['xusdc_raw'] == before['wxrs_raw'] == 0, 'Mint requires an empty isolated route wallet')
        require(before['native_raw'] >= 3 * BASE_FEE_RAW, 'Pre-fund all three operator fees before minting')
        expected['xusdc_raw'] += amount
        expected['xusdc_supply_raw'] += amount
    else:
        expected['native_raw'] -= BASE_FEE_RAW
        if stage == 'buy':
            require(before['xusdc_raw'] == amount and before['wxrs_raw'] == 0, 'Buy requires only this route deposit and no prior wrapped holdings')
            require(before['native_raw'] >= 3 * BASE_FEE_RAW, 'Insufficient route fee allowance')
            actual_output = quote_purchase(before['reserve_a_raw'], before['reserve_b_raw'], amount)['expected_xrs_raw']
            require(actual_output >= int(prepared['fields']['min_output_raw']), 'Output below signed minimum')
            expected['xusdc_raw'] -= amount
            expected['wxrs_raw'] += actual_output
            expected['reserve_b_raw'] += amount
            expected['reserve_a_raw'] -= actual_output
        elif stage == 'unwrap':
            require(before['xusdc_raw'] == 0 and before['wxrs_raw'] == amount, 'Unwrap must match this route’s entire verified purchased amount')
            require(before['native_raw'] >= 2 * BASE_FEE_RAW, 'Reserve unwrap and delivery fees')
            expected['wxrs_raw'] -= amount
            expected['wxrs_supply_raw'] -= amount
            expected['native_raw'] += amount
        else:
            require(before['xusdc_raw'] == before['wxrs_raw'] == 0, 'Delivery requires completion of preceding route stages')
            require(before['native_raw'] >= amount + BASE_FEE_RAW, 'Insufficient native output plus delivery fee')
            expected['native_raw'] -= amount
    for value in expected.values():
        raw(value)
    _equal({name: after[name] for name in BALANCE_FIELDS}, expected, 'Post-state does not match the exact isolated stage; reconcile original signature')
    return {'status': 'VERIFIED_ISOLATED_STAGE', 'route_id': prepared['route_id'], 'stage': stage,
            'signature': signature, 'receipt_slot': receipt['slot'], 'observed_slot': after['slot'],
            'actual_output_xrs_raw': actual_output,
            'delivered_native_xrs_raw': amount if stage == 'deliver' else None,
            'recipient': prepared['recipient'], 'irreversible_finality_proven': False}


def _apply_purchase_accounted(state, prepared):
    # Invoked at the exact transaction position after replaying earlier swaps.
    from xeris_accounting import swap_output
    stage, amount = prepared['stage'], int(prepared['fields']['amount_raw'])
    output = None
    if stage == 'mint':
        require(state['xusdc_raw'] == state['wxrs_raw'] == 0, 'Mint requires an empty isolated route wallet')
        require(state['native_raw'] >= 3 * BASE_FEE_RAW, 'Pre-fund all operator fees before minting')
        state['xusdc_raw'] += amount; state['xusdc_supply_raw'] += amount
    else:
        state['native_raw'] -= BASE_FEE_RAW
        if stage == 'buy':
            require(state['xusdc_raw'] == amount and state['wxrs_raw'] == 0, 'Buy requires only this deposit and no prior wrapped holdings')
            require(state['native_raw'] >= 2 * BASE_FEE_RAW, 'Insufficient route fee allowance')
            output = swap_output(state['reserve_a_raw'], state['reserve_b_raw'], amount, True)
            require(output >= int(prepared['fields']['min_output_raw']), 'Output below signed minimum')
            state['xusdc_raw'] -= amount; state['wxrs_raw'] += output
            state['reserve_b_raw'] += amount; state['reserve_a_raw'] -= output
        elif stage == 'unwrap':
            require(state['xusdc_raw'] == 0 and state['wxrs_raw'] == amount, 'Unwrap must equal verified purchase output')
            require(state['native_raw'] >= BASE_FEE_RAW, 'Reserve delivery fee')
            state['wxrs_raw'] -= amount; state['wxrs_supply_raw'] -= amount; state['native_raw'] += amount
        else:
            require(state['xusdc_raw'] == state['wxrs_raw'] == 0, 'Delivery requires completion of preceding stages')
            require(state['native_raw'] >= amount, 'Insufficient native output plus delivery fee')
            state['native_raw'] -= amount
    return output


def verify_accounted_transition(before, after, prepared, receipt, signature, evidence):
    """Bounded concurrent-chain verification using full wire/Merkle accounting.

    Unlike verify_isolated_transition, unrelated standard transfers and known
    pool swaps are replayed. Any unsupported activity still requires manual
    reconciliation of the original signature, never replacement submission.
    See xeris_accounting.verify_window for the pinned-collector evidence schema.
    """
    from xeris_accounting import verify_transition
    verify_receipt(receipt, signature, prepared)
    output, concurrent = verify_transition(before, after, prepared, receipt, signature,
                                          evidence, _apply_purchase_accounted)
    return {'status': 'VERIFIED_ACCOUNTED_STAGE', 'route_id': prepared['route_id'],
            'stage': prepared['stage'], 'signature': signature, 'receipt_slot': receipt['slot'],
            'observed_slot': after['slot'], 'actual_output_xrs_raw': output,
            'delivered_native_xrs_raw': int(prepared['fields']['amount_raw']) if prepared['stage'] == 'deliver' else None,
            'recipient': prepared['recipient'], 'concurrent_transactions_accounted': concurrent,
            'irreversible_finality_proven': False}
