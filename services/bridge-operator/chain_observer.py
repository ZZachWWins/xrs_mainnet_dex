"""Read-only, pinned three-node evidence archive for settlement.

This is bounded RPC observation, not an independent consensus implementation.
Signed wire bytes from a Mac node are accepted only when their Merkle root and
order match the block independently returned by all three configured nodes.
The archive never fills a lost raw-data window with guesses. No keys, signing,
transaction submission, node changes or funds are accessed by this module.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from public_rpc import PublicReadClient
import xeris_purchase as p
import xeris_accounting as accounting

CHECKPOINT_SLOT = 27201
CHECKPOINT_HASH = '0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3'
NODES = tuple(sorted(p.NODES))
CONFIRMATION_BLOCKS = 12
MAX_WINDOW = 150


class ObservationError(ValueError):
    pass


def need(condition, reason):
    if not condition:
        raise ObservationError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def uint(value, positive=False):
    need(type(value) is int and (1 if positive else 0) <= value < 2**64, 'invalid_integer')
    return value


def bytes32(value):
    need(type(value) is list and len(value) == 32 and all(type(x) is int and 0 <= x <= 255 for x in value), 'invalid_block_bytes')
    return bytes(value)


def data(response):
    need(type(response) is dict and response.get('success') is True and type(response.get('data')) is dict, 'invalid_explorer_response')
    return response['data']


def summary(block):
    need(type(block) is dict, 'invalid_block')
    slot = uint(block.get('slot'), True)
    for field in ('hash', 'merkle_root'):
        p._hash(block.get(field))
    need(block.get('proposer') in p.PRODUCERS, 'unexpected_producer')
    count = uint(block.get('transaction_count'))
    rows = block.get('transactions')
    need(type(rows) is list and len(rows) == count <= accounting.MAX_TRANSACTIONS, 'incomplete_transactions')
    txs = []
    for row in rows:
        need(type(row) is dict and row.get('block_slot') == slot and type(row.get('block_slot')) is int and row.get('status') == 'included', 'invalid_transaction_summary')
        p._decode58(row.get('signature'), 64)
        uint(row.get('amount'))
        txs.append({key: row.get(key) for key in ('signature', 'block_slot', 'status', 'from', 'to', 'amount', 'tx_type', 'details')})
    need(len({tx['signature'] for tx in txs}) == count, 'duplicate_block_transaction')
    return {'slot': slot, 'hash': block['hash'], 'merkle_root': block['merkle_root'],
            'proposer': block['proposer'], 'transaction_count': count, 'transactions': txs}


def work_hash(block):
    """Recompute the v4 header commitment using the unchanged node's codec.

    Provenance: fresh-mainnet-node/src/pow.rs pow_preimage_with_recovery,
    POW_PREIMAGE_V2_SLOT=1; scrypt N=4096,r=4,p=1 from slot0. This binds the
    raw parent's hash to the block hash independently observed on all nodes.
    It does not validate the producer certificate or difficulty/consensus.
    """
    slot=uint(block.get('slot'),True)
    proposer=bytes32(block.get('proposer'));parent=bytes32(block.get('previous_hash'))
    poh=bytes32(block.get('poh_hash'));root=bytes32(block.get('merkle_root'))
    timestamp=block.get('poh_timestamp')
    need(type(timestamp) is int and 0<timestamp<2**128,'invalid_work_timestamp')
    nonce=uint(block.get('nonce'))
    envelope=block.get('proposer_sig')
    need(type(envelope) is list and 0<len(envelope)<=262144 and
         all(type(x) is int and 0<=x<=255 for x in envelope),'invalid_recovery_envelope')
    envelope=bytes(envelope)
    sized=lambda value:len(value).to_bytes(4,'little')+value
    old=(b'XRS_POW_V2'+slot.to_bytes(8,'little')+proposer+sized(parent)+
         timestamp.to_bytes(16,'little')+sized(poh)+nonce.to_bytes(8,'big')+sized(root))
    preimage=b'XRS_POW_MAINNET_V4\0'+sized(p.CHAIN.encode())+sized(old)+sized(envelope)
    return hashlib.scrypt(preimage,salt=b'',n=4096,r=4,p=1,dklen=32)


def normalize_raw(block):
    need(type(block) is dict, 'invalid_raw_block')
    slot = uint(block.get('slot'), True)
    rows = block.get('transactions')
    need(type(rows) is list and len(rows) <= accounting.MAX_TRANSACTIONS, 'invalid_raw_transactions')
    wires, signatures, raw_map = [], [], {}
    for tx in rows:
        encoded = accounting.encode_rpc_transaction(tx)
        decoded = accounting.decode_transaction(encoded, allow_multi=True)
        signature = decoded['signature']
        need(signature not in raw_map, 'duplicate_raw_transaction')
        signatures.append(signature)
        raw_map[signature] = encoded
        wires.append(base64.b64decode(encoded))
    root = bytes32(block.get('merkle_root')).hex()
    need(accounting.merkle_root(wires) == root, 'raw_merkle_mismatch')
    need(work_hash(block)==bytes32(block.get('hash')),'raw_header_commitment_mismatch')
    return {'slot': slot, 'hash': bytes32(block.get('hash')).hex(),
            'previous_hash': bytes32(block.get('previous_hash')).hex(),
            'merkle_root': root, 'proposer': accounting.encode58(bytes32(block.get('proposer'))),
            'signatures': signatures, 'raw_transactions_by_signature': raw_map}


def receipt_core(value):
    """Keep execution evidence, excluding display floats such as amount_xrs."""
    need(type(value) is dict and type(value.get('meta')) is dict and
         type(value.get('transaction')) is dict, 'invalid_receipt')
    uint(value.get('slot'), True)
    meta, tx = value['meta'], value['transaction']
    uint(meta.get('fee')); uint(tx.get('amount'))
    signatures = tx.get('signatures')
    need(type(signatures) is list and 1 <= len(signatures) <= 64, 'invalid_receipt_signatures')
    for signature in signatures:
        p._decode58(signature,64)
    need(all(key in meta for key in ('err','fee','instructionOutcomes')), 'receipt_outcomes_missing')
    outcomes=meta['instructionOutcomes']
    need(type(outcomes) is list and 1<=len(outcomes)<=256 and all(
        type(row) is dict and set(row)=={'index','ok'} and type(row['index']) is int and
        row['index']==index and type(row['ok']) is bool for index,row in enumerate(outcomes)),
        'receipt_outcomes_incomplete')
    failed=next((row['index'] for row in outcomes if not row['ok']),None)
    expected_error=None if failed is None else {'InstructionError':[failed,'ExecutionFailed']}
    need(canonical(meta['err'])==canonical(expected_error),'receipt_outcome_unknown_or_inconsistent')
    return {'slot':value['slot'],
        'meta':{key:meta[key] for key in ('err','fee','instructionOutcomes')},
        'transaction':{key:tx.get(key) for key in ('signatures','from','to','amount','tx_type','details')}}


class BlockArchive:
    """Dedicated immutable SQLite archive; no implicit pruning or reorg overwrite."""
    def __init__(self, path):
        path = Path(path)
        need(not path.is_symlink(), 'archive_symlink_rejected')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need(not tables or tables == {'observer_metadata', 'observer_blocks'}, 'unrelated_archive')
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS observer_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observer_blocks(slot INTEGER PRIMARY KEY,hash TEXT NOT NULL,
            previous_hash TEXT NOT NULL,evidence TEXT NOT NULL,digest TEXT NOT NULL);
        ''')
        binding = canonical({'schema': 1, 'chain': p.CHAIN, 'nodes': NODES,
                             'checkpoint_slot': CHECKPOINT_SLOT, 'checkpoint_hash': CHECKPOINT_HASH})
        self.db.execute('INSERT OR IGNORE INTO observer_metadata VALUES(?,?)', ('binding', binding))
        need(self.db.execute("SELECT value FROM observer_metadata WHERE key='binding'").fetchone()[0] == binding, 'archive_binding_mismatch')
        path.chmod(0o600)

    def close(self):
        self.db.close()

    def bounds(self):
        row = self.db.execute('SELECT MIN(slot),MAX(slot) FROM observer_blocks').fetchone()
        return tuple(row)

    def get(self, slot):
        row = self.db.execute('SELECT * FROM observer_blocks WHERE slot=?', (uint(slot, True),)).fetchone()
        need(row is not None, 'archive_window_missing')
        need(hashlib.sha256(row['evidence'].encode()).hexdigest() == row['digest'], 'archive_digest_mismatch')
        return json.loads(row['evidence'])

    def append(self, record):
        block = record['canonical_blocks_by_node'][NODES[0]]
        slot, block_hash, previous_hash = block['slot'], block['hash'], block['previous_hash']
        body = canonical(record)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT evidence FROM observer_blocks WHERE slot=?', (slot,)).fetchone()
            if old:
                need(old[0] == body, 'archive_conflict')
            else:
                last = self.db.execute('SELECT slot,hash FROM observer_blocks ORDER BY slot DESC LIMIT 1').fetchone()
                if last:
                    need(slot == last['slot'] + 1 and previous_hash == last['hash'], 'archive_gap_or_parent_conflict')
                self.db.execute('INSERT INTO observer_blocks VALUES(?,?,?,?,?)',
                    (slot, block_hash, previous_hash, body, hashlib.sha256(body.encode()).hexdigest()))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def window(self, start, end):
        need(type(start) is int and type(end) is int and 0 < start <= end and end-start <= MAX_WINDOW, 'invalid_window')
        return [self.get(slot) for slot in range(start, end + 1)]


class ChainObserver:
    def __init__(self, archive, reader=None, clock=None):
        self.archive = archive
        self.reader = reader or PublicReadClient()
        self.clock = clock or time.time
        self.last_checked = 0

    def _all(self, function):
        with ThreadPoolExecutor(max_workers=3) as pool:
            return dict(zip(NODES, pool.map(function, NODES)))

    def checkpoint(self):
        results = self._all(lambda node: data(self.reader.explorer(f'/v2/block/slot/{CHECKPOINT_SLOT}', node)))
        for block in results.values():
            need(block.get('slot') == CHECKPOINT_SLOT and type(block.get('slot')) is int and block.get('hash') == CHECKPOINT_HASH, 'mainnet_checkpoint_mismatch')
        return True

    def _tip(self, node):
        stats = data(self.reader.explorer('/v2/stats', node))
        slot = uint(stats.get('current_slot'), True)
        block = data(self.reader.explorer(f'/v2/block/slot/{slot}', node))
        need(block.get('slot') == slot, 'tip_slot_mismatch')
        p._hash(block.get('hash'))
        stamp = uint(block.get('poh_timestamp'), True)
        need(-10_000 <= int(self.clock()*1000)-stamp <= 120_000, 'stale_node_tip')
        return {'slot': slot, 'hash': block['hash']}

    def _record(self, raw):
        blocks = self._all(lambda node: summary(data(self.reader.explorer(f"/v2/block/slot/{raw['slot']}", node))))
        expected = None
        for block in blocks.values():
            need(all(block[k] == raw[k] for k in ('slot', 'hash', 'merkle_root', 'proposer')), 'raw_canonical_mismatch')
            need([row['signature'] for row in block['transactions']] == raw['signatures'], 'raw_transaction_order_mismatch')
            block['previous_hash'] = raw['previous_hash']
            need(expected is None or canonical(block) == expected, 'canonical_nodes_disagree')
            expected = canonical(block)
        receipts = {node: {} for node in NODES}
        for signature in raw['signatures']:
            values = self._all(lambda node: receipt_core(self.reader.xeris('getTransaction', [signature], node)))
            need(all(type(x) is dict for x in values.values()), 'receipt_unavailable')
            need(len({canonical(x) for x in values.values()}) == 1, 'receipt_nodes_disagree')
            for node, receipt in values.items():
                need(type(receipt.get('slot')) is int and receipt['slot'] == raw['slot'] and receipt.get('transaction', {}).get('signatures', [None])[0] == signature, 'receipt_identity_mismatch')
                receipts[node][signature] = receipt
        return {'canonical_blocks_by_node': blocks, 'raw_transactions_by_signature': raw['raw_transactions_by_signature'], 'receipts_by_node': receipts}

    def collect(self):
        self.checkpoint()
        tips = self._all(self._tip)
        common = min(tip['slot'] for tip in tips.values())
        # The raw source carries no extra authority: roots/order are checked
        # against all three canonical summaries before the data is archived.
        source = self.reader.raw_blocks('mac_treasury')
        need(type(source) is list and 1 <= len(source) <= 50, 'invalid_raw_window')
        raws = [normalize_raw(block) for block in source]
        need(len({raw['slot'] for raw in raws}) == len(raws), 'duplicate_raw_slot')
        by_slot = {raw['slot']: raw for raw in raws}
        common = min(common, max(by_slot))
        first, last = self.archive.bounds()
        start = common if last is None else last
        need(common >= start and common-start < 50, 'archive_outside_raw_window')
        for slot in range(start, common + 1):
            need(slot in by_slot, 'raw_window_gap')
            self.archive.append(self._record(by_slot[slot]))
        self.last_checked = int(self.clock())
        return self.anchor()

    def anchor(self):
        first, last = self.archive.bounds()
        need(last is not None, 'archive_empty')
        block = self.archive.get(last)['canonical_blocks_by_node'][NODES[0]]
        return {'chain_id': p.CHAIN, 'slot': last, 'block_hash': block['hash']}

    def snapshot_wallet(self, wallet):
        p.address(wallet)
        # Bootstrap the archive before the state bracket; if the next tip
        # arrives during the read, the earlier snapshot anchor remains stored.
        self.collect()
        def read_node(node):
            start = self._tip(node)
            balance = self.reader.xeris('getBalance', [wallet], node)
            need(type(balance) is dict and balance.get('context', {}).get('slot') == start['slot'], 'native_balance_slot_mismatch')
            native = uint(balance.get('value'))
            token_balances, tokens = {}, {}
            for token in (p.USDC, p.WRAPPED):
                response = self.reader.explorer(f'/token/balance/{wallet}/{token}', node)
                need(type(response) is dict and response.get('address') == wallet and response.get('token_id') == token, 'token_balance_identity_mismatch')
                token_balances[token] = uint(response.get('balance'))
                tokens[token] = response.get('token_info')
            response = self.reader.explorer('/contract/'+p.POOL, node)
            need(type(response) is dict and response.get('success') is True and type(response.get('contract')) is dict, 'pool_unavailable')
            contract = response['contract']
            swap = contract.get('state', {}).get('Swap', {})
            end = self._tip(node)
            need(start == end, 'snapshot_tip_changed')
            result = {'chain_id': p.CHAIN, 'wallet': wallet, 'slot': start['slot'], 'block_hash': start['hash'],
                'read_boundary': {'start_slot': start['slot'], 'end_slot': end['slot'], 'start_hash': start['hash'], 'end_hash': end['hash']},
                'native_raw': native, 'xusdc_raw': token_balances[p.USDC], 'wxrs_raw': token_balances[p.WRAPPED],
                'xusdc_supply_raw': tokens[p.USDC].get('current_supply') if type(tokens[p.USDC]) is dict else None,
                'wxrs_supply_raw': tokens[p.WRAPPED].get('current_supply') if type(tokens[p.WRAPPED]) is dict else None,
                'reserve_a_raw': swap.get('reserve_a'), 'reserve_b_raw': swap.get('reserve_b'),
                'pool_invariant': {**{k: contract.get(k) for k in ('contract_id', 'contract_type', 'owner', 'is_active')},
                    'state':{'Swap':{k:v for k,v in swap.items() if k not in ('reserve_a','reserve_b','price_observations')}}},
                'token_metadata': {token:{k:v for k,v in meta.items() if k!='current_supply'} for token,meta in tokens.items()}}
            p._snapshot(result, wallet)
            return result
        values = self._all(read_node)
        need(len({canonical(value) for value in values.values()}) == 1, 'snapshot_nodes_disagree')
        result = values[NODES[0]]
        # Account state and archive must be anchored to the same canonical tip.
        self.collect()
        record = self.archive.get(result['slot'])
        need(record['canonical_blocks_by_node'][NODES[0]]['hash'] == result['block_hash'], 'snapshot_archive_mismatch')
        return result

    def evidence(self, before, after, prepared, signature, *, target_only=False):
        for anchor in (before, after):
            need(anchor.get('chain_id') == p.CHAIN, 'anchor_chain_mismatch')
        records = self.archive.window(before['slot'], after['slot'])
        blocks = {node: [record['canonical_blocks_by_node'][node] for record in records] for node in NODES}
        need(blocks[NODES[0]][0]['hash'] == before['block_hash'] and blocks[NODES[0]][-1]['hash'] == after['block_hash'], 'anchor_hash_mismatch')
        wires, receipts = {}, {node: {} for node in NODES}
        for record in records:
            for txid, wire in record['raw_transactions_by_signature'].items():
                need(txid not in wires, 'duplicate_window_transaction')
                wires[txid] = wire
            for node in NODES:
                receipts[node].update(record['receipts_by_node'][node])
        target = receipts[NODES[0]].get(signature)
        need(target is not None, 'target_not_observed')
        need(after['slot'] >= target['slot'] + CONFIRMATION_BLOCKS, 'confirmations_pending')
        # Exclusive-route and pending-send checks belong to the fenced journal.
        # Do not invent them in an RPC observer. Runtime adds those facts before
        # invoking the pure account/burn verifier.
        return {'canonical_blocks_by_node': blocks, 'raw_transactions_by_signature': wires,
                'receipts_by_node': receipts, 'target_receipt': target,
                'observed_confirmation_blocks': after['slot']-target['slot']}
