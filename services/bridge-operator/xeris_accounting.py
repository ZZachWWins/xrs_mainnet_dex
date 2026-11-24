"""Bounded public transaction accounting; no I/O, keys, signing or submission.

Consumes existing /blocks wire data, /v2/block summaries and getTransaction
receipts. Canonical three-node Merkle roots bind complete signed wire bytes.
Supported concurrency is intentionally narrow: single-instruction native/token
transfers, token mint/burn, wrap/unwrap, and the configured pool's two swaps.
Unrecognized/multi-instruction transactions require reconciliation. This is not
an independent consensus validator or a proof of irreversible finality.
"""
import base64
import hashlib
import json
import struct

import xeris_purchase as p

MAX_TRANSACTIONS = 4096
MAX_WIRE_BYTES = 256 * 1024


def encode58(data):
    number, result = int.from_bytes(data, 'big'), ''
    while number:
        number, digit = divmod(number, 58)
        result = p.ALPHABET[digit] + result
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + result


def _bytes(value, length=None):
    p.require(type(value) is list and len(value) <= MAX_WIRE_BYTES
              and all(type(v) is int and 0 <= v <= 255 for v in value), 'Invalid raw byte array')
    result = bytes(value)
    p.require(length is None or len(result) == length, 'Invalid raw byte length')
    return result


def _rpc_vec(value):
    # solana_short_vec's serde JSON is [[encoded length], element, ...].
    p.require(type(value) is list and 1 <= len(value) <= 65536, 'Invalid RPC short vector')
    p.require(_bytes(value[0]) == p._shortvec(len(value) - 1), 'Noncanonical RPC short vector')
    return value[1:]


def encode_rpc_transaction(tx):
    """Serialize the existing GET /blocks Transaction JSON to canonical wire.

    Accepts Solana SDK serde field names/short vectors, not jsonParsed messages.
    Collector stores returned base64 under the transaction's first signature.
    """
    p.require(type(tx) is dict and set(tx) == {'signatures', 'message'}, 'Invalid raw transaction')
    sigs = _rpc_vec(tx['signatures'])
    message = tx['message']
    # Live solana-sdk serde emits camelCase. Older public fixtures used
    # snake_case. Choose one complete dialect for this transaction and require
    # it consistently at message, header and instruction levels; never merge
    # aliases or silently prefer one conflicting spelling.
    p.require(type(message) is dict, 'Invalid raw message')
    if set(message) == {'header', 'accountKeys', 'recentBlockhash', 'instructions'}:
        keys_field, hash_field, program_field = 'accountKeys', 'recentBlockhash', 'programIdIndex'
        header_fields = ('numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts')
    elif set(message) == {'header', 'account_keys', 'recent_blockhash', 'instructions'}:
        keys_field, hash_field, program_field = 'account_keys', 'recent_blockhash', 'program_id_index'
        header_fields = ('num_required_signatures', 'num_readonly_signed_accounts', 'num_readonly_unsigned_accounts')
    else:
        raise ValueError('Invalid raw message field dialect')
    header = message['header']
    p.require(type(header) is dict and set(header) == set(header_fields), 'Invalid message header field dialect')
    keys, instructions = _rpc_vec(message[keys_field]), _rpc_vec(message['instructions'])
    wire = p._shortvec(len(sigs)) + b''.join(_bytes(sig, 64) for sig in sigs)
    wire += _bytes([header[key] for key in header_fields], 3)
    wire += p._shortvec(len(keys)) + b''.join(_bytes(key, 32) for key in keys) + _bytes(message[hash_field], 32)
    wire += p._shortvec(len(instructions))
    for ix in instructions:
        p.require(type(ix) is dict and set(ix) == {program_field, 'accounts', 'data'}, 'Invalid raw instruction field dialect')
        accounts, data = _rpc_vec(ix['accounts']), _rpc_vec(ix['data'])
        wire += _bytes([ix[program_field]], 1) + p._shortvec(len(accounts)) + _bytes(accounts)
        wire += p._shortvec(len(data)) + _bytes(data)
    decode_transaction(base64.b64encode(wire).decode(), allow_multi=True)
    return base64.b64encode(wire).decode()


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, count):
        p.require(type(count) is int and 0 <= count <= len(self.data) - self.offset, 'Truncated public wire data')
        value = self.data[self.offset:self.offset + count]
        self.offset += count
        return value

    def short(self):
        data = bytearray()
        for _ in range(3):
            byte = self.take(1)[0]; data.append(byte)
            if not byte & 128:
                value = sum((v & 127) << (7 * i) for i, v in enumerate(data))
                p.require(value <= 65535 and bytes(data) == p._shortvec(value), 'Noncanonical short vector')
                return value
        raise ValueError('Invalid short vector')

    def u64(self):
        return struct.unpack('<Q', self.take(8))[0]

    def string(self):
        count = self.u64()
        p.require(count <= 16384, 'Instruction string exceeds bound')
        try:
            return self.take(count).decode('utf-8')
        except UnicodeDecodeError as error:
            raise ValueError('Invalid instruction UTF-8') from error

    def end(self):
        p.require(self.offset == len(self.data), 'Trailing public wire bytes')


def decode_transaction(encoded, allow_multi=False):
    p.require(type(encoded) is str and len(encoded) <= 4 * MAX_WIRE_BYTES // 3 + 8, 'Invalid transaction encoding')
    try:
        wire = base64.b64decode(encoded, validate=True)
    except Exception as error:
        raise ValueError('Invalid transaction base64') from error
    p.require(base64.b64encode(wire).decode() == encoded and len(wire) <= MAX_WIRE_BYTES, 'Noncanonical transaction base64')
    reader = Reader(wire)
    count = reader.short()
    p.require(1 <= count <= 64, 'Invalid signature count')
    signatures = [reader.take(64) for _ in range(count)]
    p.require(all(sig != bytes(64) for sig in signatures), 'Unsigned observation is not execution evidence')
    message_offset = reader.offset
    required, readonly_signed, readonly_unsigned = reader.take(3)
    p.require(required == count and readonly_signed < required, 'Invalid signer header')
    key_count = reader.short()
    p.require(count <= key_count <= 256 and readonly_unsigned <= key_count - count, 'Invalid account-key count')
    keys = [reader.take(32) for _ in range(key_count)]
    reader.take(32)  # recent blockhash is retained in committed wire
    ix_count = reader.short()
    p.require(1 <= ix_count <= 256 and (allow_multi or ix_count == 1), 'Unsupported multi-instruction transaction; reconcile')
    instructions = []
    for _ in range(ix_count):
        program_index = reader.take(1)[0]
        accounts = list(reader.take(reader.short()))
        data = reader.take(reader.short())
        p.require(program_index < key_count and all(index < key_count for index in accounts), 'Invalid instruction account index')
        instructions.append({'program_index': program_index, 'accounts': accounts, 'data': data})
    reader.end()
    return {'wire': wire, 'message': wire[message_offset:], 'signature': encode58(signatures[0]),
            'signatures': [encode58(sig) for sig in signatures], 'signer': encode58(keys[0]),
            'keys': keys, 'instructions': instructions}


def _json_args(text):
    def pairs(items):
        result = {}
        for key, value in items:
            p.require(key not in result, 'Duplicate contract argument')
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid contract arguments') from error
    p.require(type(result) is dict and set(result) == {'amount', 'min_output'}, 'Unsupported swap arguments')
    p.raw(result['amount'], positive=True); p.raw(result['min_output'], positive=True)
    return result


def decode_effect(transaction):
    p.require(len(transaction['instructions']) == 1 and len(transaction['signatures']) == 1, 'Unsupported multi-instruction or multi-signer transaction; reconcile')
    reader = Reader(transaction['instructions'][0]['data'])
    variant = struct.unpack('<I', reader.take(4))[0]
    signer = p.address(transaction['signer'])
    effect = {'signer': signer, 'from': signer, 'to': '', 'amount': 0, 'details': None}
    if variant == 0:
        token, to, amount = reader.string(), reader.string(), reader.u64()
        effect.update(tx_type='token_mint', to=p.address(to), amount=amount, details={'token_id': token})
    elif variant in (1, 2):
        token, sender = reader.string(), reader.string()
        to = reader.string() if variant == 1 else ''
        amount = reader.u64()
        p.require(sender == signer, 'Instruction sender differs from signer')
        effect.update(tx_type='token_transfer' if variant == 1 else 'token_burn',
                      to=p.address(to) if to else '', amount=amount, details={'token_id': token})
    elif variant == 4:
        contract, method, args = reader.string(), reader.string(), reader.string()
        p.require(contract == p.POOL and method in ('swap_a_to_b', 'swap_b_to_a'), 'Unsupported contract activity; reconcile')
        effect.update(tx_type='contract_call:' + method, to=p.POOL,
                      details={'contract_id': contract, 'method': method, 'args': _json_args(args)})
    elif variant == 11:
        sender, to, amount = reader.string(), reader.string(), reader.u64()
        p.require(sender == signer, 'Instruction sender differs from signer')
        effect.update(tx_type='native_transfer', to=p.address(to), amount=amount)
    elif variant in (13, 14):
        effect.update(tx_type='wrap_xrs' if variant == 13 else 'unwrap_xrs', to=signer, amount=reader.u64())
    else:
        raise ValueError('Unsupported instruction activity; reconcile')
    reader.end()
    if variant != 4:
        p.raw(effect['amount'], positive=True)
    return effect


def merkle_root(wires):
    if not wires:
        return hashlib.sha256(b'\x01XRS_MERKLE_EMPTY_V2').hexdigest()
    layer = [hashlib.sha256(b'\x00' + wire).digest() for wire in wires]
    pad = hashlib.sha256(b'\x01XRS_MERKLE_ODD_PAD_V2').digest()
    while len(layer) > 1:
        layer = [hashlib.sha256(b'\x01' + layer[i] + (layer[i + 1] if i + 1 < len(layer) else pad)).digest()
                 for i in range(0, len(layer), 2)]
    return layer[0].hex()


def _receipt_fields(effect):
    return {key: effect[key] for key in ('from', 'to', 'amount', 'tx_type', 'details')}


def verify_window(before, after, prepared, target_receipt, signature, evidence, *, target_only=False, expected_target_ok=True):
    """Return ordered supported effects, fully bound to agreed canonical evidence.

    evidence: existing route/exclusive/pending fields and canonical_blocks_by_node;
    each block also has previous_hash (hex). raw_transactions_by_signature maps
    first signatures to canonical signed-wire base64. receipts_by_node maps all
    three node names to {signature: getTransaction result}, for the open interval.
    This function checks data consistency, not node authenticity/finality; the
    collector must read pinned nodes and archive evidence before the 50-block raw
    API window expires. No metadata supplied by a browser is trusted evidence.
    target_only checks deterministic burn inclusion without interpreting other
    transactions; it MUST NOT be used to infer variable swap output.
    """
    p.require(type(expected_target_ok) is bool, 'Explicit boolean execution outcome required')
    p.require(type(evidence) is dict and evidence.get('route_id') == prepared['route_id']
              and evidence.get('operator_wallet') == prepared['operator_wallet'], 'Bound route evidence required')
    if not target_only:
        p.require(evidence.get('exclusive_wallet') is True, 'Exclusive route evidence required')
    p._equal(evidence.get('pending_transaction_ids'), [], 'Unresolved route-wallet transaction exists')
    windows, raw_txs, receipts = (evidence.get(k) for k in ('canonical_blocks_by_node', 'raw_transactions_by_signature', 'receipts_by_node'))
    p.require(type(windows) is dict and set(windows) == p.NODES, 'All three canonical node observations required')
    p.require(type(raw_txs) is dict and len(raw_txs) <= MAX_TRANSACTIONS, 'Bounded raw transaction evidence required')
    p.require(type(receipts) is dict and set(receipts) == p.NODES and all(type(v) is dict for v in receipts.values()), 'All three receipt observations required')
    start, end = before['slot'], after['slot']
    p.require(type(start) is int and type(end) is int and 0 < start < target_receipt['slot'] <= end and end - start <= 150, 'Invalid or oversized observation window')
    agreed = None
    for blocks in windows.values():
        p.require(type(blocks) is list and len(blocks) == end - start + 1, 'Incomplete canonical block window')
        compact = []
        for index, block in enumerate(blocks):
            p.require(type(block) is dict and type(block.get('slot')) is int and block['slot'] == start + index, 'Missing or reordered canonical block')
            for field in ('hash', 'merkle_root', 'previous_hash'): p._hash(block.get(field))
            p.require(block.get('proposer') in p.PRODUCERS, 'Unexpected block proposer')
            txs = block.get('transactions')
            p.require(type(txs) is list and type(block.get('transaction_count')) is int and block['transaction_count'] == len(txs), 'Incomplete block transaction list')
            if index: p.require(block['previous_hash'] == blocks[index - 1]['hash'], 'Canonical block parent mismatch')
            compact.append({key: block[key] for key in ('slot', 'hash', 'previous_hash', 'merkle_root', 'proposer', 'transaction_count', 'transactions')})
        p.require(blocks[0]['hash'] == before['block_hash'] and blocks[-1]['hash'] == after['block_hash'], 'Snapshot block hash mismatch')
        if agreed is not None: p._equal(compact, agreed, 'Nodes disagree on canonical block evidence')
        agreed = compact
    records, seen, target_count = [], set(), 0
    for index, block in enumerate(agreed):
        wires = []
        for summary in block['transactions']:
            p.require(type(summary) is dict, 'Invalid block transaction summary')
            sig = summary.get('signature'); p._decode58(sig, 64)
            p.require(sig not in seen, 'Repeated transaction signature in observation window')
            seen.add(sig)
            p.require(len(seen) <= MAX_TRANSACTIONS and sig in raw_txs, 'Missing or excessive raw transactions')
            tx = decode_transaction(raw_txs[sig], allow_multi=not index or (target_only and sig != signature))
            p.require(tx['signature'] == sig, 'Raw transaction signature mismatch')
            wires.append(tx['wire'])
            if not index: continue
            p.require(type(summary.get('block_slot')) is int and summary['block_slot'] == block['slot'] and summary.get('status') == 'included', 'Block transaction slot/status mismatch')
            if target_only and sig != signature:
                continue  # Burn inclusion is deterministic; unrelated effects need no replay.
            effect = decode_effect(tx)
            p._equal({key: summary.get(key) for key in _receipt_fields(effect)}, _receipt_fields(effect), 'Raw instruction differs from canonical summary')
            agreed_receipt = None
            for node in p.NODES:
                r = receipts[node].get(sig)
                p.require(type(r) is dict and type(r.get('slot')) is int and r['slot'] == block['slot'], 'Missing receipt or receipt slot mismatch')
                meta, decoded = r.get('meta'), r.get('transaction')
                p.require(type(meta) is dict and type(decoded) is dict and decoded.get('signatures') == tx['signatures'], 'Receipt signature mismatch')
                outcomes = meta.get('instructionOutcomes')
                p.require(type(outcomes) is list and len(outcomes) == 1 and type(outcomes[0]) is dict
                          and type(outcomes[0].get('index')) is int and outcomes[0]['index'] == 0
                          and type(outcomes[0].get('ok')) is bool, 'Incomplete single-instruction outcome')
                ok = outcomes[0]['ok']
                p._equal(meta.get('err'), None if ok else {'InstructionError': [0, 'ExecutionFailed']}, 'Inconsistent or unknown execution outcome')
                p.require(type(meta.get('fee')) is int and meta['fee'] == p.BASE_FEE_RAW, 'Unexpected receipt fee')
                p._equal({key: decoded.get(key) for key in _receipt_fields(effect)}, _receipt_fields(effect), 'Receipt differs from raw instruction')
                core = {'slot': r['slot'], 'meta': {'err': meta['err'], 'fee': meta['fee'], 'instructionOutcomes': outcomes},
                        'transaction': {'signatures': decoded['signatures'], **_receipt_fields(effect)}}
                if agreed_receipt is not None: p._equal(core, agreed_receipt, 'Nodes disagree on execution receipt')
                agreed_receipt = core
            if sig == signature:
                target_count += 1
                p.require(block['slot'] == target_receipt['slot'], 'Target receipt slot mismatch')
                p.require(tx['message'] == base64.b64decode(prepared['message_base64']), 'Target signed message differs from prepared intent')
                p.require(ok is expected_target_ok, 'Target execution outcome differs from required outcome')
            records.append({'signature': sig, 'slot': block['slot'], 'effect': effect, 'ok': ok})
        p.require(merkle_root(wires) == block['merkle_root'], 'Raw transactions do not match canonical Merkle root')
    p.require(target_count == 1, 'Target transaction must occur exactly once')
    return records


def verify_failed_original(before, after, prepared, receipt, signature, evidence):
    """Prove exact single-instruction failure; absence/timeout never qualifies.

    This proves a failed original, not permission to refund or replace it.
    The fenced settlement journal applies any compensation policy separately.
    """
    if prepared.get('stage') in ('mint','buy','unwrap','deliver'):
        p.validate_prepared(prepared)
    else:
        import xeris_redemption
        xeris_redemption.validate_prepared(prepared)
    p.require(type(receipt) is dict and type(receipt.get('slot')) is int and
              type(after.get('slot')) is int and after['slot'] >= receipt['slot']+12,
              'Twelve subsequent canonical blocks required')
    verify_window(before, after, prepared, receipt, signature, evidence,
                  target_only=True, expected_target_ok=False)
    return {'status':'VERIFIED_FAILED_ORIGINAL','signature':signature,
            'stage':prepared['stage'],'receipt_slot':receipt['slot'],
            'observed_slot':after['slot'],'refund_authorized':False,
            'replacement_authorized':False,'irreversible_finality_proven':False}


def swap_output(a, b, amount, buying):
    p.raw(a, positive=True); p.raw(b, positive=True); p.raw(amount, positive=True)
    fee = amount * 30 // 10000
    output = (a if buying else b) * (amount - fee) // ((b if buying else a) + amount - fee)
    p.raw((b if buying else a) + amount)
    p.require(0 < output < (a if buying else b), 'Invalid swap output')
    return output


def apply_concurrent(state, effect, operator, ok):
    # No route-wallet activity may be silently folded into the target output.
    p.require(effect['signer'] != operator and effect['to'] != operator, 'Concurrent route-wallet activity; reconcile')
    if not ok: return
    kind, amount, details = effect['tx_type'], effect['amount'], effect['details']
    if kind.startswith('contract_call:'):
        buying = details['method'] == 'swap_b_to_a'
        amount = details['args']['amount']
        output = swap_output(state['reserve_a_raw'], state['reserve_b_raw'], amount, buying)
        p.require(output >= details['args']['min_output'], 'Concurrent successful swap violates signed minimum')
        state['reserve_a_raw'] += -output if buying else amount
        state['reserve_b_raw'] += amount if buying else -output
    elif kind in ('token_mint', 'token_burn'):
        token = details['token_id']
        if token == p.WRAPPED: p.require(kind != 'token_mint', 'Ordinary wrapped mint is forbidden')
        if token in (p.USDC, p.WRAPPED):
            if kind == 'token_mint': p.require(effect['signer'] == p.TREASURY, 'Wrong xUSDC mint authority')
            state['xusdc_supply_raw' if token == p.USDC else 'wxrs_supply_raw'] += amount if kind == 'token_mint' else -amount
    elif kind in ('wrap_xrs', 'unwrap_xrs'):
        state['wxrs_supply_raw'] += amount if kind == 'wrap_xrs' else -amount
    elif kind not in ('native_transfer', 'token_transfer'):
        raise ValueError('Unsupported concurrent effect')
    for value in state.values(): p.raw(value)


def verify_transition(before, after, prepared, target_receipt, signature, evidence, apply_target):
    operator = prepared['operator_wallet']
    p._snapshot(before, operator); p._snapshot(after, operator)
    p._equal(after['pool_invariant'], before['pool_invariant'], 'Pool configuration or LP shares changed')
    p._equal(after['token_metadata'], before['token_metadata'], 'Token configuration changed')
    records = verify_window(before, after, prepared, target_receipt, signature, evidence)
    expected = {name: before[name] for name in p.BALANCE_FIELDS}
    output = None
    for record in records:
        if record['signature'] == signature:
            output = apply_target(expected, prepared)
        else:
            apply_concurrent(expected, record['effect'], operator, record['ok'])
        for value in expected.values(): p.raw(value)
        for token, field in ((p.USDC, 'xusdc_supply_raw'), (p.WRAPPED, 'wxrs_supply_raw')):
            p.require(expected[field] <= before['token_metadata'][token]['max_supply'], 'Replayed token supply exceeds maximum')
    p._equal({name: after[name] for name in p.BALANCE_FIELDS}, expected, 'Post-state differs from complete supported accounting; reconcile original signature')
    return output, len(records) - 1
